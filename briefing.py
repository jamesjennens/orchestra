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
from claim_limit import actor_holds
from lifecycle import project_facts
from requirements import _unique_object, canonical_bytes, content_hash


def _read_history_snapshot(path):
    from admin import read_regular_text
    value = record_json.loads(read_regular_text(path), object_pairs_hook=_unique_object)
    canonical_bytes(value)
    return value


PREFIX='Kind: task-checkpoint-v1\n'
KINDS={'blocker','question','decision','correction','dependency'}
GROUPING_ADVICE=('To fit the limit, group the items: one open item per theme with an id prefix, resolved ones folded into one entry, '
 'and a longer text in a repository document that an item source links')
CHECKPOINT_ITEMS_MAX=100
CHECKPOINT_TEXT_LIMIT=400
#: The top-level text fields of a checkpoint and their limits; the validator and the
#: `checkpoint` help both read this list (kittrial-5bb.97).
CHECKPOINT_FIELD_LIMITS=(('source_commit',128),('branch',200),('intent',600),('acceptance',1000),('summary',1000),('next_action',600))
CHECKPOINT_SOURCE_LIMIT=240
CHECKPOINT_MAX_BYTES=80000
BRIEF_ITEM_OFFSET_MIN=0
# A checkpoint holds at most CHECKPOINT_ITEMS_MAX open items; one read may ask for all of them
# (the web service's checkpoint template needs every one, kittrial-5bb.113).
BRIEF_ITEM_LIMIT_MIN,BRIEF_ITEM_LIMIT_MAX=1,CHECKPOINT_ITEMS_MAX
HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX=1,20
HISTORY_BUDGET_MIN,HISTORY_BUDGET_MAX=256,8000
#: Entry kinds a history read can select server-side (kittrial-5bb.8).
HISTORY_KINDS=('comment','event')
#: Directions a history page can be read in; the released kit always wrote oldest-first.
HISTORY_ORDERS=('oldest','newest')
#: A released history cursor has exactly the base keys. order/entry_kind/scp are the
#: additive filters (kittrial-5bb.8); they are omitted while they are at their defaults
#: so a plain page still emits the token the released kit wrote, and every old cursor
#: still reads as oldest-first.
HISTORY_CURSOR_BASE={'v','kind','project','task','snapshot','since','index','offset'}
HISTORY_CURSOR_EXTRA={'order','entry_kind','scp'}
HISTORY_CURSOR_KEYS=HISTORY_CURSOR_BASE|HISTORY_CURSOR_EXTRA
FIELD_NAME_LIMIT=60
FIELD_NAME_COUNT=8
#: The minimal worked payload for disposing of ONE outstanding direction while carrying
#: the open items (kittrial-5bb.287). The name is referenced by the `checkpoint` help and
#: by docs/BRIEFINGS.md, so one name is spelled in one place.
DIRECTION_EXAMPLE_FILE='example-direction-resolution.json'
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
# `brief TASK --full` is bounded too (kittrial-5bb.8 review, F4): the fuller-context
# block carries at most this many encoded bytes in total, and at most this many for any
# single field. What the cap removes is reported per field and `show TASK` keeps the
# complete text, so a hostile description cannot make one answer unbounded.
FULL_CONTEXT_BUDGET=20000
FULL_FIELD_BUDGET=8000
# Text that would rewrite or reorder what a reader sees: C0 controls other than tab and
# newline, DEL, the C1 block, the Unicode line/paragraph separators (a bare separator
# ends the line a reader is looking at) and the bidi controls (a right-to-left override
# can reorder the answer on screen). Each is rendered as the same \uXXXX escape the JSON
# form already uses, so the fuller-context read cannot emit a raw ESC/BEL/CR or hide text.
CONTROL_TEXT_RE=re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]')

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
    if found[0].get('malformed'):
        raise ValueError('Task %s is malformed: %s'%(task,found[0].get('error') or 'JSON nested too deeply'))
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
    # kittrial-5bb.293 item 2 (rev3): field_sizes is NOT a key in the snapshot data at
    # all -- the legacy whole-data cursor shape hashes every key, so excluding it in
    # activity_cursor is not enough. It is computed beside the snapshot (the brief
    # caller does it); state_sha256 already covers the three fields.
    return {'project':project,'task':task,'state_sha256':content_hash(state),
            'entries':entries,'entry_digests':digests}

def entry_digests(data):
    """Digests for snapshots predating the embedded entry_digests field."""
    found=data.get('entry_digests')
    return found if isinstance(found,dict) else {e['entry_id']:content_hash(e) for e in data['entries']}

def activity_cursor(data):
    # entry_digests is derived entirely from entries, not extra activity. Keep
    # the released hash shape so staged writes remain fresh on older readers.
    # kittrial-5bb.293 item 2 (rev2): field_sizes is NOT part of the hashed snapshot
    # and NOT in the cursor token -- a cursor written by 0133e8e reads fresh on this
    # kit and the other way round. The lengths ride beside the cursor (the brief's
    # field_changes carries them); state_sha256 already covers the three fields.
    compatible={k:v for k,v in data.items() if k not in ('entry_digests','field_sizes')}
    return token({'v':1,'kind':'activity','project':data['project'],'task':data['task'],'sha256':content_hash(compatible)})

#: A project name as ``admin.validate_name`` accepts it.
PROJECT_NAME=re.compile(r'[a-z][a-z0-9]{1,23}')
#: Shown instead of a recorded project name that is not one.
UNRECOGNISED_PROJECT='(unrecognised project name)'

def checkpoint_taken_in(cursor,project):
    """The project a checkpoint's cursor was taken in, when it is not ``project``; else None.

    The cursor is text a checkpoint writer supplied, so the name is shown only when it is
    a project name, never a newline, instruction text or terminal escape: anything else
    reads as ``UNRECOGNISED_PROJECT`` (kittrial-5bb.142). An unreadable cursor is ignored;
    checkpoint validation already refuses one, so this only keeps a read from failing.
    """
    try:
        recorded=untoken(cursor)
    except ValueError:
        return None
    named=recorded.get('project') if isinstance(recorded,dict) else None
    if not isinstance(named,str) or named==project:return None
    return named if PROJECT_NAME.fullmatch(named) else UNRECOGNISED_PROJECT

def cursor_matches(cursor,data,compatible=None):
    # Read receipts already written by the first provenance kit in their shape.
    if cursor==(compatible if compatible is not None else activity_cursor(data)):return True
    previous=token({'v':1,'kind':'activity','project':data['project'],'task':data['task'],'sha256':content_hash(data)})
    if cursor==previous:return True
    # A project restored under another name (restore-new) keeps its tasks and checkpoints,
    # but the project name is part of the cursor and of the hashed snapshot, so every
    # checkpoint read as stale although nothing changed (kittrial-5bb.131). The cursor
    # names the project it was taken in: the same task with the same content under that
    # name is the same activity. Only reading changes; cursors are written as before, so
    # every kit still reads what every other kit writes, and an older kit keeps reporting
    # the renamed project's checkpoints as stale until a fresh checkpoint is written.
    try:
        recorded=untoken(cursor)
    except ValueError:
        return False
    project=recorded.get('project') if isinstance(recorded,dict) else None
    if not isinstance(project,str) or project==data['project']:return False
    # The task and the content hash are inside the token, so only the same task with the
    # same content can match.
    return cursor_matches(cursor,dict(data,project=project))

def text(value,label,limit,empty=False):
    # Names the field, the length it had and the limit (kittrial-5bb.97).
    check_text(value,label,limit,empty=empty,nul=False)

def shown_name(name):
    """A caller-written field name as it may appear in a refusal (kittrial-5bb.113 review).

    An ordinary name is shown as it is. Anything else is shown as a quoted JSON string in
    ASCII, so a newline, an escape sequence, a bidi or zero-width character or a NUL is
    spelled out and never sent raw, and its square brackets are escaped too, so a name
    cannot be written to look like the next numbered problem of the refusal.
    """
    if re.fullmatch(r'[A-Za-z0-9_.-]+',name):return name
    quoted=json.dumps(name,ensure_ascii=True)
    return quoted.replace('[','\\u005b').replace(']','\\u005d').replace('\x7f','\\u007f')

def field_names(values):
    """Bounded, sorted caller-supplied field names for an error message.

    Names are capped individually and in count so a caller cannot turn a
    validation error into an echo of an arbitrarily large payload.
    """
    names=sorted(values)
    shown=[shown_name(name[:FIELD_NAME_LIMIT])+('...' if len(name)>FIELD_NAME_LIMIT else '') for name in names[:FIELD_NAME_COUNT]]
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
            problems.append('%s: expected a list of at most %d items' % (field,CHECKPOINT_ITEMS_MAX))
            if isinstance(items,list):problems.append(GROUPING_ADVICE)
            continue
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

def id_list(ids,limit=FIELD_NAME_COUNT):
    """The ids of one refusal, bounded the way a field-name list is.

    A refusal must name what it compared, or a caller cannot act on it
    (kittrial-5bb.287); it must also stay bounded, so a pathological payload
    cannot make one sentence unbounded. Every id in the delta is named when they
    fit, and the rest are counted.
    """
    shown=sorted(ids)
    if len(shown)<=limit:return ', '.join(shown)
    return ', '.join(shown[:limit])+' (+%d more)'%(len(shown)-limit)

def transition(previous,current,called_from='request',current_id=None):
    """Follow one linked pair: every unresolved item is carried or resolved.

    The rule itself is unchanged: the items open in the parent and absent now
    must be exactly the ids the current record resolves. What changed
    (kittrial-5bb.287) is the refusal: it names the ids it compared, and it says
    which of its two callers is refusing. `save_checkpoint` refuses THIS REQUEST,
    which the caller can fix by editing the payload; `checkpoint_state` refuses a
    STORED PAIR while it walks the chain, which no caller can fix by editing a
    payload - the task needs reconciliation by an operator before it reads or
    writes again. Without the ids, three different caller mistakes (an item
    dropped without a resolution, an item resolved and still carried, an id that
    was never open) produced one identical sentence and six payload shapes could
    not find the way out.

    ``current_id`` is the comment id of the record being compared, and is named
    only in the stored-pair refusal: a chain is read from records, so that
    refusal can point the operator at the pair to reconcile.
    """
    old={x['id'] for x in previous['open_items']} if previous else set()
    now={x['id'] for x in current['open_items']}
    resolved={x['id'] for x in current['resolved']}
    if old-now!=resolved:
        # Three different caller mistakes reach this one rule, and they used to share
        # one sentence that named nothing (kittrial-5bb.287):
        #   1. an id open in the parent that this record neither carries nor resolves;
        #   2. an id this record RESOLVES and still carries in open_items, so the pair
        #      (dropped, resolved) does not match;
        #   3. an id this record resolves that is not open in the parent at all - the
        #      parent already resolves it, or it was never an open item. A direction is
        #      dispositioned with `directions` and never with `resolved`, so this case
        #      must say so rather than send the caller hunting for an item problem that
        #      may not exist.
        # Named ids are bounded like a field-name list so one sentence stays bounded.
        dropped=id_list(old-now)
        carried=id_list((old|now)&resolved)
        not_open=id_list(resolved-old-now)
        parts=[]
        if old-now:
            parts.append('open in the previous checkpoint and neither carried in open_items nor resolved: %s; '
                         'resolve or supersede every id named here, or carry each forward unchanged' % (dropped,))
        if (old|now)&resolved:
            parts.append('%s in resolved and also in open_items; an item is resolved by omitting its id from '
                         'open_items, or carried unchanged by leaving it out of resolved'
                         % (('%s are'%carried) if len((old|now)&resolved)>1 else ('%s is'%carried)))
        if resolved-old-now:
            many=len(resolved-old-now)>1
            parts.append('%s in resolved and not open in the previous or the current record: the previous '
                         'checkpoint already resolves %s, or %s never an open item. A direction is '
                         'dispositioned with directions, never with resolved; a genuinely unresolved item is '
                         're-opened under a new id'
                         % (('%s are'%not_open) if many else ('%s is'%not_open),
                            'them' if many else 'it','they were' if many else 'it was'))
        if not parts:
            # Cannot happen while old-now != resolved with both sides read from the
            # same two records; kept so the refusal is never empty.
            parts.append('the items open in the previous checkpoint and the ids this record resolves differ')
        if called_from=='chain':
            parent=current.get('previous')
            raise ValueError('Checkpoint history is unreadable: stored pair %s -> %s does not carry every '
                             'unresolved item forward: %s. This is a stored pair, not this request: reconcile '
                             'the history before reading, writing or briefing this task (an operator preserves '
                             'the original evidence in a correction record and publishes a re-anchoring '
                             'checkpoint)'
                             % (parent if parent is not None else '(none)',
                                current_id if current_id is not None else '(unknown)', '; '.join(parts)))
        raise ValueError('Carry every unresolved item forward or explicitly resolve/supersede it with reason '
                         'and evidence: '+'. '.join(parts)
                         +'. Compared here: the previous checkpoint\'s open_items, this record\'s open_items '
                         'and this record\'s resolved. No client update is required: the refusal names the ids '
                         'in this request that do not match, and the same ids fix it')
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
        transition(current[0] if current else None,p,'chain',cid)
        current=(p,c);parent=cid;visited.add(cid)
        history.append((cid,p))
        if normalize:provenance[cid]=normalize_provenance(p)
    if len(visited)!=len(records):raise ValueError('Unlinked checkpoint history; reconcile missing/conflicting revisions')
    return {'current':current,'invalid':invalid,'history':history,'provenance':provenance}

def checkpoints(issue):
    state=checkpoint_state(issue)
    return state['current'],state['invalid']

def lane_state_record(comment,cid,author):
    """True when a native comment is the ``Kind: lane-state-v1`` record written WITH checkpoint ``cid``.

    The declared wait state travels beside its checkpoint, written by that checkpoint's own
    author in the same act (kittrial-5bb.263; docs/LANE_BOARD_DESIGN.md section 1). It is
    therefore part of the checkpoint's own receipt, NOT activity the checkpoint failed to
    incorporate, and must not make that checkpoint read stale. A record that names another
    checkpoint, or was written by another author, is not this checkpoint's and stays
    ordinary activity. An older kit does not know the record at all, so it reads it as
    activity and reports the checkpoint stale; that rollback reading is deliberate and
    documented (docs/LANE_BOARD_DESIGN.md section 5, the capability record).
    """
    if not isinstance(comment,dict) or author is None or comment.get('author')!=author:
        return False
    from lane_state import parse
    payload=parse(comment.get('text'))
    return payload is not None and str(payload.get('checkpoint'))==str(cid)

def lane_state_entry_ids(data,cid,author):
    """Snapshot entry ids of the lane-state records that belong to checkpoint ``cid``."""
    found=set()
    for entry in data.get('entries') or []:
        if entry.get('kind')!='comment':continue
        if lane_state_record({'author':entry.get('author'),'text':entry.get('body')},cid,author):
            found.add(entry['entry_id'])
    return found

def excluding_checkpoint(data,cid,author=None):
    """Reuse the snapshot and its entry hashes when excluding its newest receipt.

    The reserved ``Kind: lane-state-v1`` record written with this checkpoint, by this
    checkpoint's own author, is part of the same receipt and is excluded with it, so
    declaring a wait state can never make the checkpoint it belongs to read stale
    (kittrial-5bb.263 review ``lane-state-must-not-stale-its-own-checkpoint``). A record
    that names an older checkpoint, or was written by another author, is kept as activity.
    ``author`` is the checkpoint comment's author; when it is unknown nothing is excluded.
    """
    eid=data['task']+'-c'+str(cid)
    dropped={eid}|lane_state_entry_ids(data,cid,author)
    return dict(data,entries=[e for e in data['entries'] if e['entry_id'] not in dropped],
                entry_digests={k:v for k,v in entry_digests(data).items() if k not in dropped})

def clip(value,limit):
    value=str(value or '')
    return {'text':value[:limit],'omitted_chars':max(0,len(value)-limit)}

def plain_text(value):
    """Terminal-safe text: C0/C1 controls (DEL included) other than tab and newline, the
    Unicode line/paragraph separators and the bidi controls become the same \\uXXXX escape
    the JSON form already emits, so no answer carries a raw ESC/BEL, a bare CR, a
    separator that ends the reader's line, or a bidi override that reorders it."""
    return CONTROL_TEXT_RE.sub(lambda match:'\\u%04x'%ord(match.group()),str(value or ''))


def clip_context_label(value,limit=120):
    """Escape first, then clip without cutting a generated Unicode escape."""
    text=plain_text(value)
    end=min(len(text),limit)
    for match in re.finditer(r'\\u[0-9a-fA-F]{4}',text):
        if match.start()>=end:
            break
        if match.start()<end<match.end():
            end=match.start()
            break
    if end<len(text):
        partial=re.search(r'\\u[0-9a-fA-F]{0,3}$',text[:end])
        if partial:
            end=partial.start()
    return {'text':text[:end],'omitted_chars':len(text)-end}


def inline_context_text(value):
    """One terminal line, including labels whose separators are already escaped."""
    text=re.sub(r'(?:[\t\r\n\u2028\u2029]|\\u202[89])+',' ',str(value or ''))
    return plain_text(text)


CONTEXT_POINTER_RE = re.compile(r'(task|comment|ref|cap|item|file|room|commit|doc|archive|req|design):([A-Za-z0-9._/#:@-]{1,200})\Z')
# The traceability kinds (TRACEABILITY_DESIGN.md §2.4) also fix their value's shape;
# a value outside it is prose, like any token outside the grammar (§2.1). `@` is
# legal only as the one revision separator of a `req` value, so the ten older kinds
# read exactly as before.
REQ_POINTER_VALUE_RE = re.compile(r'([A-Za-z0-9._/#:-]+)(?:@([1-9][0-9]{0,8}))?\Z')
DESIGN_POINTER_VALUE_RE = re.compile(r'([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*)(?:#([A-Za-z0-9._-]{1,120}))?\Z')
CONTEXT_POINTER_ITEM_MAX = 10
CONTEXT_POINTER_BRIEF_MAX = 50


def parse_context_pointers(text):
    """Read tokens, never rewrite or validate the checkpoint's prose."""
    if not isinstance(text,str):
        return []
    result=[]
    for token in text.split():
        match=CONTEXT_POINTER_RE.fullmatch(token)
        if not match:
            continue
        kind,value=match[1],match[2]
        if kind=='req':
            if not REQ_POINTER_VALUE_RE.fullmatch(value):
                continue
        elif kind=='design':
            if not DESIGN_POINTER_VALUE_RE.fullmatch(value):
                continue
        elif '@' in value:
            continue
        result.append({'pointer':token,'kind':kind,'value':value})
    return result


def needs_requirement_governance(*groups):
    """Whether any item's text holds a `req:` token (the only kind that reads governance)."""
    return any(pointer['kind']=='req' for items,field in groups for item in items or []
               if isinstance(item,dict) for pointer in parse_context_pointers(item.get(field)))


class ContextPointers:
    """Read-only resolution over a caller-authorized, project-scoped snapshot.

    No runner, path or journal is accepted: resolution cannot perform I/O. Production
    supplies project-scoped, already-authorized rows. ``can_read`` is an optional
    additional restriction for direct embedders/tests; it never widens that scope.
    Unloaded kinds are uniformly unchecked; absent/hidden loaded kinds unavailable.
    ``governance`` is the project's requirements-governance snapshot, already read by
    the caller (``requirement_governance.snapshot``), as data: owner requirement
    evidence is checked against it, and without it that evidence is unverifiable.
    """
    def __init__(self,rows,project,operators=None,can_read=None,governance=None):
        self.project=project
        self.operators=operators
        self.governance=governance
        self.remaining=CONTEXT_POINTER_BRIEF_MAX
        self._source_rows=rows
        self._can_read=can_read
        self.rows=None
        self.tasks={}
        self.comments={}
        self.catalogs={}
        self.reviews={}
        self.requirements=None
        self._ledgers={}
    def _load(self):
        # Pure prose and unchecked kinds never build a project-wide index.
        if self.rows is not None:
            return
        self.rows=[row for row in self._source_rows if isinstance(row,dict) and
                   row.get('project_id',self.project)==self.project and
                   not row.get('malformed') and (self._can_read is None or self._can_read(row))]
        from reserved_comments import is_record_anchor,is_record_comment
        from coordination import is_merge_slot
        for row in self.rows:
            if is_record_anchor(row) or is_merge_slot(row):
                continue
            self.tasks.setdefault(str(row.get('id')),[]).append(row)
            for comment in row.get('comments') or []:
                if isinstance(comment,dict) and comment.get('id') is not None and not is_record_comment(comment.get('text',comment.get('body'))):
                    self.comments.setdefault(str(comment['id']),[]).append((row,comment))

    def loaded(self):
        """The caller-readable, project-scoped rows every resolution reads (kittrial-5bb.425: the trace read
        counts only these, so hidden equals absent in its totals too)."""
        self._load()
        return self.rows

    def requirement_keys(self):
        """Every requirement key a readable row claims (kittrial-5bb.425, the trace read's requirements view)."""
        self._load()
        return sorted(self._requirement_index())

    def ledger(self,key):
        """A requirement's validated ledger (see `_ledger`), for a reader that needs its revisions too."""
        self._load()
        return self._ledger(key)

    def requirement(self,key):
        """`req:KEY` resolved for any key, including one the token grammar cannot spell (kittrial-5bb.425)."""
        self._load()
        entry={'pointer':'req:'+key,'kind':'req','state':'unavailable','trust':'data'}
        label,_=self._requirement(entry,key,None)
        if entry['state'] in ('live','superseded'):
            entry['label']=clip_context_label(label)
        return entry

    def _catalog(self,kind):
        if kind not in self.catalogs:
            import reference_records,capability_records
            module=reference_records if kind=='ref' else capability_records
            entries,_=module.catalog(self.rows,self.operators)
            indexed={}
            for item in entries:
                indexed.setdefault(item.get('key'),[]).append(item)
            self.catalogs[kind]=indexed
        return self.catalogs[kind]

    def _requirement_index(self):
        """Requirement rows by every key a revision comment on them claims.

        The claim is read leniently here only to find the row; resolution then reads
        the row's strict ledger, so a malformed ledger is unavailable, never unknown.
        """
        if self.requirements is None:
            from requirement_records import REVISION_PREFIX
            from reserved_comments import parse_requirement_record
            self.requirements={}
            for row in self.rows:
                if 'requirement' not in (row.get('labels') or []):
                    continue
                keys=set()
                for comment in row.get('comments') or []:
                    text=comment.get('text') if isinstance(comment,dict) else None
                    if isinstance(text,str) and text.startswith(REVISION_PREFIX):
                        record=parse_requirement_record(text)
                        if record is not None and isinstance(record.get('key'),str):
                            keys.add(record['key'])
                for key in keys:
                    self.requirements.setdefault(key,[]).append(row)
        return self.requirements

    def _governed(self,evidence):
        if self.governance is None:
            raise ValueError('Owner requirement evidence cannot be checked without governance')
        from requirement_governance import validate_evidence_files
        validate_evidence_files(self.governance,self.project,evidence)

    def _ledger(self,key):
        """One requirement's validated ledger, read once per resolver (kittrial-5bb.425 review b2: every token
        of a key re-validated the whole ledger, so planted tokens cost a full validation each).

        Returns 'unknown' (no single row claims the key), None (the ledger cannot be read), or
        (row, revisions, accepted revision numbers, terminal owner state or None)."""
        if key in self._ledgers:
            return self._ledgers[key]
        rows=self._requirement_index().get(key,[])
        if len(rows)!=1:
            self._ledgers[key]='unknown'
            return 'unknown'
        row=rows[0]
        import requirement_records as records
        import requirement_owner_records as owner_records
        try:
            revisions=records.existing_revisions(row)
            if records.existing_kind(row)!='requirement' or not revisions:
                raise ValueError('not a requirement')
            if {record.get('key') for record in revisions.values()}!={key}:
                raise ValueError('conflicting keys')
            for evidence in owner_records.existing_acceptances(row).values():
                self._governed(evidence)
            accepted=[n for n in sorted(revisions) if records.resolved_acceptance(row,revisions[n],self.operators)]
            if any(revisions[n]['acceptance_state']=='accepted' and n not in accepted for n in revisions):
                raise ValueError('acceptance cannot be verified')
            terminal,_=owner_records.state_ledger(row)
            if terminal is not None:
                self._governed(terminal)
        except (ValueError,TypeError,KeyError):
            # The same failures the requirements read shows as unreadable: never promoted.
            self._ledgers[key]=None
            return None
        self._ledgers[key]=(row,revisions,accepted,terminal)
        return self._ledgers[key]

    def _requirement(self,entry,key,number):
        """§2.4 `req`: flags unknown-key, unknown-revision, draft, superseded, withdrawn."""
        ledger=self._ledger(key)
        if ledger=='unknown':
            entry['flags']=['unknown-key']
            return None,None
        if ledger is None:
            entry['flags']=[]
            return None,None
        row,revisions,accepted,terminal=ledger
        if number is None:
            number=accepted[-1] if accepted else max(revisions)
        elif number not in revisions:
            entry['flags']=['unknown-revision']
            return None,None
        record=revisions[number]
        flags=[]
        if number not in accepted:
            flags.append('draft')
        if any(n>number for n in accepted):
            flags.append('superseded')
        if terminal is not None and terminal['state'] not in flags:
            flags.append(terminal['state'])
        entry['state']='superseded' if {'superseded','withdrawn'} & set(flags) else 'live'
        entry['flags']=flags
        entry['reference']={'id':row['id'],'revision':number,'sha256':record['sha256']}
        return record.get('title'),'requirements get '+str(row['id'])

    def _design(self,entry,scope,section):
        """§2.4 `design`: unknown-scope, not-selected, unknown-section.

        The section is checked against the server-derived index of the revision shown (kittrial-5bb.418), read
        from the loaded rows like every record: a heading with that anchor is `live`, none is the flag
        `unknown-section`. A design entry with no single index (one written before documents were stored, or
        two differing copies) leaves the section `unchecked`."""
        entry['flags']=['unknown-scope']
        label=follow=None
        shown=None
        matches=self._catalog('ref').get('design.'+scope,[])
        if len(matches)==1 and matches[0].get('state') not in ('malformed','unsupported','conflicted','incomplete'):
            view=matches[0]
            record=view.get('record')
            proposed=view.get('proposed')
            if record is not None:
                entry['state']='superseded' if record.get('acceptance_state')=='superseded' else 'live'
                entry['flags']=[]
            elif proposed is not None:
                record=proposed
                entry['state']='live'
                entry['flags']=['not-selected']
            if record is not None:
                label=record.get('name') or record.get('title') or 'design.'+scope
                follow='ref get design.'+scope
                shown=(view,record)
        if section is not None:
            entry['section']={'anchor':section,'state':'unchecked'}
            if shown is not None:
                import design_documents
                view,record=shown
                authority=record.get('authority') or {}
                if design_documents.is_document_authority(authority):
                    row=next((item for item in self.rows if str(item.get('id'))==str(view.get('native_id'))),None)
                    index=design_documents.selected_index(row,'design.'+scope,authority)
                    if index is not None:
                        if any(heading['anchor']==section for heading in index['headings']):
                            entry['section']['state']='live'
                        else:
                            entry['section']['state']='unknown'
                            entry['flags'].append('unknown-section')
        return label,follow

    def resolve(self,pointer):
        kind,value=pointer['kind'],pointer['value']
        entry={'pointer':pointer['pointer'],'kind':kind,'state':'unchecked','trust':'data'}
        if kind in ('req','design'):
            self._load()
            entry['state']='unavailable'
            if kind=='req':
                match=REQ_POINTER_VALUE_RE.fullmatch(value)
                label,follow=self._requirement(entry,match[1],int(match[2]) if match[2] else None)
            else:
                match=DESIGN_POINTER_VALUE_RE.fullmatch(value)
                label,follow=self._design(entry,match[1],match[2])
            if entry['state'] in ('live','superseded'):
                entry['label']=clip_context_label(label)
                entry['follow']=follow
            return entry
        if kind not in ('task','comment','ref','cap'):
            return entry
        self._load()
        entry['state']='unavailable'
        label=follow=None
        if kind=='task':
            matches=self.tasks.get(value,[])
            if len(matches)==1:
                row=matches[0]
                entry['state']='superseded' if row.get('status')=='closed' and row.get('close_reason')=='superseded' else 'live'
                label=row.get('title')
                follow='brief '+value
        elif kind=='comment':
            matches=self.comments.get(value,[])
            if len(matches)==1:
                row,comment=matches[0]
                text=comment.get('text',comment.get('body'))
                if isinstance(text,str):
                    entry['state']='live'
                    label='%s / %s: %s'%(row['id'],comment.get('author') or 'unknown',text)
                    follow='history '+str(row['id'])+' --recent 10'
                    # Reuse the same workflow projection as brief; no comment read.
                    if row['id'] not in self.reviews:
                        from work import workflow
                        self.reviews[row['id']]=workflow(row,operators=self.operators)
                    prior=self.reviews[row['id']].get('prior_contributions') or []
                    if any(str(item.get('comment_id'))==value and item.get('relation')=='supersedes' for item in prior):
                        entry['state']='superseded'
        else:
            matches=self._catalog(kind).get(value,[])
            if len(matches)==1:
                view=matches[0]
                record=view.get('record') or view.get('proposed')
                if record is not None and view.get('state') not in ('malformed','unsupported','conflicted','incomplete'):
                    entry['state']='superseded' if record.get('acceptance_state')=='superseded' else 'live'
                    label=record.get('name') or record.get('title') or value
                    follow=('ref get ' if kind=='ref' else 'capability get ')+value
        if entry['state'] in ('live','superseded'):
            entry['label']=clip_context_label(label)
            entry['follow']=follow
        return entry

    def item_pointers(self,items,field):
        """Resolution by item id; never add fields to a carryable checkpoint item."""
        shown={}
        for item in items:
            pointers=parse_context_pointers(item.get(field))
            count=min(len(pointers),CONTEXT_POINTER_ITEM_MAX,self.remaining)
            self.remaining-=count
            resolved=[self.resolve(pointer) for pointer in pointers[:count]]
            resolved += [{'pointer':p['pointer'],'kind':p['kind'],'state':'unresolved','trust':'data'}
                         for p in pointers[count:]]
            shown[item['id']]={'pointers':resolved,'pointers_omitted':len(pointers)-count}
        return shown

def encoded_size(text):
    """Encoded JSON string bytes: the unit history budgets an answer in (control
    characters and Unicode must not make a bounded answer silently much larger)."""
    return len(json.dumps(text,ensure_ascii=False).encode())

def fitting_prefix(text,budget):
    """Longest prefix of `text` whose encoded JSON string fits `budget` bytes."""
    if budget<=0:return ''
    lo,hi=0,len(text)
    while lo<hi:
        middle=(lo+hi+1)//2
        if encoded_size(text[:middle])<=budget:lo=middle
        else:hi=middle-1
    return text[:lo]

NEWER_MAX=5
# A stable newer-activity marker is the newest other-party entry's ID plus this many
# hex characters of that entry's current content digest. The digest is what makes an
# EDIT to that same entry move the marker; the ID alone would not (kittrial-5bb.351).
MARKER_DIGEST=16
#: The task fields whose edits move the marker (kittrial-5bb.354). Never status, assignee,
#: priority, notes or labels: a claim must not wake the agent that made it.
MARKER_FIELDS=('title','description','acceptance_criteria')

def fields_digest(row):
    """Digest of the CURRENT title, description and acceptance of one task row."""
    return content_hash({name:str(row.get(name) or '') for name in MARKER_FIELDS})

def activity_marker(newest_other,digest):
    """The stable newer-activity marker (kittrial-5bb.351, fields folded in by kittrial-5bb.354).

    ``<entry_id>:<16 hex>`` names the newest other-party entry the checkpoint did not
    incorporate; ``fields:<16 hex>`` stands when there is none. The hex covers that
    entry's current content AND the current title, description and acceptance, so a new
    other-party entry, an edit to the newest one and an edit of one of those three fields
    each move it, while a claim, a status change or the owner's own comment never does.
    Never a timestamp.
    """
    combined=content_hash({'entry':content_hash(newest_other) if newest_other is not None else None,'fields':digest})
    return (newest_other['entry_id'] if newest_other is not None else 'fields')+':'+combined[:MARKER_DIGEST]

def same_activity_actor(author, owner):
    """Parent and sub-label activity belong to the same actor (kittrial-5bb.371).

    Apply the claim namespace rule in either direction: a parent can write on its
    sub-label claim, and a sub-label can write on its parent's claim. A shared text
    prefix without the slash boundary never establishes ownership. Missing actors
    remain other-party activity.
    """
    return actor_holds(author, owner) or actor_holds(owner, author)

def wait_activity_marker(data,row):
    """The stable marker a worker's wait fingerprints (kittrial-5bb.369).

    ``newer_activity_marker`` is relative to the NEWEST checkpoint, so the caller's own
    checkpoint both resets it and (through ``checkpoint_at``) moves the released wait
    fingerprint: the agent's plan checkpoint wakes its own wait once with nothing new.
    This marker is instead the newest entry by anybody OTHER than the task's assignee
    across the task's whole history, folded with the current title/description/
    acceptance digest by the same :func:`activity_marker`. The agent's own checkpoint,
    lane-state record and comments are its own entries, so they never move it; a new or
    edited other-party entry, another actor's checkpoint and a field edit do. It is
    computed from the read's own full snapshot, so brief and the queue rows cannot
    disagree.
    """
    owner=row.get('assignee')
    entries=[entry for entry in data.get('entries') or []
             if isinstance(entry,dict) and not same_activity_actor(entry.get('author'),owner)]
    newest=max(entries,key=lambda entry:(parse_moment(entry['timestamp']),entry['entry_id'])) if entries else None
    return activity_marker(newest,fields_digest(row))

def comment_author_allowed(author, operators=None, owners=None, authority=None, project=None):
    """Current coordinator/owner membership or configured host operator, never history."""
    from review_workflow import listed_operator_actor
    if listed_operator_actor(author, operators, owners):
        return True
    state = authority() if callable(authority) else authority
    if not isinstance(state, dict) or not isinstance(author, str):
        return False
    users, agents = state.get('users') or {}, state.get('agents') or {}
    members = (state.get('memberships') or {}).get(project) or {}
    user = users.get(author)
    if isinstance(user, dict):
        return not user.get('disabled') and members.get(author) in ('owner', 'coordinator')
    agent_id = author.split('/', 1)[0]
    agent = agents.get(agent_id)
    if not isinstance(agent, dict) or not agent.get('enabled') or project not in (agent.get('projects') or []):
        return False
    account = agent.get('owner')
    user = users.get(account)
    if not isinstance(user, dict) or user.get('disabled'):
        return False
    from http_authority import coordinator_grant_capabilities, ROLE_CAPABILITIES, CAP_COORDINATE
    held = ROLE_CAPABILITIES.get(members.get(account), frozenset())
    return CAP_COORDINATE in coordinator_grant_capabilities(state, {'agent_id': agent_id}, project, held)


def pending_other_comments(entries, owner, selected=None, acknowledged=None, authority=None):
    """One comment-attention decision over an already ordered activity snapshot.

    The caller supplies the shared newer_selection result and current-authority
    predicate. Reserved records and unknown bodies are never plain comments.
    A validated response consumes through its entry in that same snapshot.
    """
    from reserved_comments import reserved_match
    remaining = entries
    if acknowledged is not None:
        position = next((i for i, entry in enumerate(entries)
                         if entry['entry_id'] == acknowledged), None)
        if position is not None:
            remaining = entries[position + 1:]
    selected_ids = None if selected is None else {entry['entry_id'] for entry in selected}
    return any(entry.get('kind') == 'comment'
               and isinstance(entry.get('body'), str) and bool(entry['body'].strip())
               and reserved_match(entry.get('body')) is None
               and not same_activity_actor(entry.get('author'), owner)
               and authority is not None and authority(entry.get('author'))
               and (selected_ids is None or entry['entry_id'] in selected_ids)
               for entry in remaining)


def checkpoint_snapshot(rows, row, state):
    """One full activity snapshot for both queue selection and displayed fields."""
    has_checkpoint = state['current'] is not None and not state['invalid']
    project = untoken(state['current'][0]['activity_cursor']).get('project', 'work-queue') if has_checkpoint else 'work-queue'
    return snapshot(rows, project, row['id'])


def checkpoint_selection(full, state, index, comment, cache=None):
    """Reuse selection only for the same request's snapshot and parsed history.

    An assignee's earlier checkpoint and the current checkpoint have different
    indexes. Identity checks prevent reuse across snapshots or new parses.
    """
    cid, payload = state['history'][index]
    key = (str(cid), index)
    found = cache.get(key) if cache is not None else None
    if (found is not None and found[0] is full and found[1] is state['history']
            and found[2] == (comment.get('author'), comment.get('created_at'))):
        return found[3]
    data = excluding_checkpoint(full, cid, comment.get('author'))
    prov = retained_provenance(state, upto=index)
    result = (data, prov, newer_selection(data, prov, comment.get('created_at')))
    if cache is not None:
        cache[key] = (full, state['history'], (comment.get('author'), comment.get('created_at')), result)
    return result


def assignee_comment_pending(row, state=None, response=None, full=None, authority=None, selection_cache=None):
    """Unconsumed comments for an open assignee, reusing checkpoint provenance.

    Review state does not decide whether a person has written to the assignee.
    It remains independent evidence; an invalid checkpoint cannot assert freshness.
    """
    owner = row.get('assignee')
    if row.get('status') == 'closed' or not owner:
        return False
    try:
        state = state if state is not None else checkpoint_state(row)
        if state['invalid']:
            return False
        if not state['provenance']:
            state = dict(state, provenance={cid:normalize_provenance(payload)
                                           for cid,payload in state['history']})
        comments = {str(c['id']): c for c in row.get('comments') or []}
        own = [(i, cid, payload) for i, (cid, payload) in enumerate(state['history'])
               if same_activity_actor(comments[str(cid)].get('author'), owner)]
        full = full if full is not None else checkpoint_snapshot([row], row, state)
        selected = None
        if own:
            index, cid, payload = own[-1]
            comment = comments[str(cid)]
            selected = checkpoint_selection(full, state, index, comment, selection_cache)[2][1]
        acknowledged = None
        if isinstance(response, dict) and same_activity_actor(response.get('author'), owner):
            acknowledged = row['id'] + '-c' + str(response['comment_id'])
        return pending_other_comments(full['entries'], owner, selected, acknowledged, authority=authority)
    except (ValueError, TypeError, KeyError, StopIteration):
        return False


def newer_comment_by_other(row,checkpoint_comment):
    """The released ``newer_activity`` boolean, in ONE place (kittrial-5bb.354).

    True when a comment by anyone other than the assignee was written after the
    checkpoint comment, the lane-state record written with that checkpoint excepted
    (kittrial-5bb.263). Unchanged from the rule ``work`` rows carried since
    kittrial-5bb.114, so older clients read what they always read; the counts and the
    marker are the precise signals. Raises StopIteration when the checkpoint comment is
    not on the row.
    """
    comments=row.get('comments') or []
    position=next(index for index,comment in enumerate(comments)
                  if str(comment.get('id'))==str(checkpoint_comment.get('id')))
    checkpoint_id=str(checkpoint_comment.get('id'));checkpoint_author=checkpoint_comment.get('author')
    return any(not same_activity_actor(comment.get('author'),row.get('assignee'))
               and not lane_state_record(comment,checkpoint_id,checkpoint_author)
               for comment in comments[position+1:])
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
# kittrial-5bb.284. A disposition can only be written through a checkpoint's `directions`
# field, and save_checkpoint refuses that field while the installation switch
# checkpoint_provenance_writes is off (the default everywhere). Counting an ordinary
# comment as an outstanding direction then leaves every coordinator note outstanding for
# ever and impossible for any worker to resolve, so the readers do not count them while
# the check cannot be acted on, and they say why. A closed task lists none either way.
DIRECTIONS_OFF_NOTE=('Direction dispositions need the installation switch checkpoint_provenance_writes, '
                     'which is off here; ordinary comments are not counted as outstanding directions while it is off.')
DIRECTIONS_CLOSED_NOTE='CLOSED TASK: it takes no new direction disposition, so none is listed.'

def directions_counted(provenance_writes,status):
    """(counted, reason) for other actors' ordinary comments as outstanding directions.

    ``reason`` is None while the directions check applies, else 'switch-off' (the
    installation cannot record a disposition) or 'closed' (a closed task lists none).
    The library readers default to the pre-existing behaviour (counted); every server
    caller passes the installation's checkpoint_provenance_writes value.
    """
    if status=='closed':return False,'closed'
    if not provenance_writes:return False,'switch-off'
    return True,None

def directions_check_state(reason):
    """The ``check`` label a direction reader carries when the check cannot apply.

    ``off`` while the installation switch is off (the pre-existing label), and
    ``closed`` for a closed task: a reader must not say the switch is off when it
    is on and the task is simply closed (kittrial-5bb.284 review S3, folded into
    kittrial-5bb.287). ``reason`` is ``directions_counted``'s second value.
    """
    return 'off' if reason=='switch-off' else reason
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

def unresolved_directions(entries,direction_idx,owner,counted=True):
    """Directions (other-actor comments) that are not yet resolved/superseded.

    A direction is outstanding when it is absent from the checkpoint's
    directions, only acknowledged, or its content changed since the recorded
    disposition (edit invalidates the earlier acknowledgement). Digest/cursor
    incorporation alone never resolves a direction; acknowledgement is distinct
    from resolution and completion. ``counted`` is False while the installation
    cannot record a disposition (kittrial-5bb.284), so nothing is reported as an
    outstanding direction that no reader could ever resolve.
    """
    if not counted:return []
    import followon
    outstanding=[]
    for e in entries:
        if e['kind']!='comment' or e['author']==owner:continue
        if str(e.get('body','')).startswith(('Kind: task-checkpoint-v1','Kind: contribution-review-v1')):continue
        # A follow-on WITHDRAWAL record is the coordinator's own "superseded"
        # disposition of the direction, not a direction of its own (kittrial-5bb.343).
        if followon.withdrawal_text(str(e.get('body',''))):continue
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

def retained_provenance(state,upto=None):
    """Retained evidence merged across the chain, optionally up to one linked record.

    ``upto`` is the index of the linked record whose answer is wanted (the
    newest-my-own-checkpoint read); the default is the whole chain. A chain whose
    newest record is legacy returns None: nothing newer can claim incorporation using
    evidence belonging to an earlier checkpoint.
    """
    history=state['history'] if upto is None else state['history'][:upto+1]
    if not history:return None
    exact,trunc,recorded=chain_evidence(history,state['provenance'])
    if not recorded:return None
    latest=state['provenance'][history[-1][0]]
    return dict(latest,digests=exact,older=trunc,verifiable=len(exact)+len(trunc),
                complete=not trunc and len(exact)>=latest['covered'])

def field_changes_since(recorded_fields, current_fields, only_if_unexplained=False):
    """What changed in title/description/acceptance since a checkpoint (kittrial-5bb.293).

    ``recorded_fields`` is the ``fields`` map a checkpoint's activity cursor carried
    (name -> length at checkpoint time; ``None`` on an older cursor). ``current_fields``
    is the same map from the live snapshot. A field whose length differs is reported
    with its old and new length; an older cursor reports the field names only.
    """
    if not isinstance(current_fields, dict):
        return []
    if not isinstance(recorded_fields, dict):
        # An older cursor carries no lengths: the caller reports a change ONLY when
        # nothing else explains the stale flag, and then without old_length (rev2).
        if not only_if_unexplained:
            return []
        # Never all three fields with old_length None: one summary entry instead
        # (kittrial-5bb.293 item 2 rev2).
        return [{'field': 'task_fields', 'old_length': None, 'new_length': None,
                 'note': 'The task fields changed after this checkpoint; their lengths were not recorded.'}]
    changed = []
    for name in ('title', 'description', 'acceptance_criteria'):
        old_len = recorded_fields.get(name)
        new_len = current_fields.get(name)
        if old_len is None and new_len is None:
            continue
        if old_len != new_len:
            changed.append({'field': name, 'old_length': old_len, 'new_length': new_len})
    return changed


def newer_selection(data,prov,checkpoint_timestamp):
    """The ONE selection of what a checkpoint did not incorporate (kittrial-5bb.354).

    Returns (coverage, entries, fresh, changed_or_late, unverified_count). The counts,
    the marker, the boolean and the 'changes since your checkpoint' section all read
    this, so no route can count differently. With recorded provenance the selection is
    exact (new, edited and late entries); a legacy checkpoint has none, so the released
    timestamp rule applies and coverage is 'timestamp' (kittrial-5bb.367): a weaker but
    real coverage, because a backdated arrival or an edit of an older entry can be
    missed. 'unknown' remains for a legacy checkpoint with no timestamp at all.
    """
    coverage='unknown' if prov is None else ('snapshot' if prov['complete'] else 'windowed')
    if prov is None and checkpoint_timestamp:
        coverage='timestamp'
        data=dict(data,entries=[e for e in data['entries']
                               if parse_moment(e['timestamp'])>=parse_moment(checkpoint_timestamp)])
    entries,fresh,changed_late,unverified=unincorporated(data,prov)
    return coverage,entries,fresh,changed_late,unverified

def newer_activity_summary(data,prov,checkpoint_timestamp,owner,direction_idx=None,direction_data=None,counted=True,directions_reason='switch-off',field_changes=None,marker_fields=None,selection=None):
    """Bounded, per-entry view of activity the current checkpoint did not incorporate.

    `prov` is the checkpoint's normalized provenance, so edited entries (same ID,
    new content) and late arrivals backdated before the checkpoint are identified
    exactly within the recorded window; entries older than an evicted window are
    reported as an explicit `unverified_count` rather than being called fresh, so
    bounded coverage is never presented as complete. Directions from other actors
    are tracked to an explicit acknowledged/resolved/superseded disposition; every
    collection is bounded with an explicit omitted count. Reading changes nothing.
    """
    coverage,entries,fresh,changed_late,unverified=(newer_selection(data,prov,checkpoint_timestamp)
                                                 if selection is None else selection)
    own=[e for e in entries if same_activity_actor(e['author'],owner)]
    others=[e for e in entries if not same_activity_actor(e['author'],owner)]
    authors=sorted({e['author'] for e in others})
    # A stable marker of the newest OTHER-party entry, so a bridge wakes on the
    # second coordinator comment too, not only when the `newer_activity` boolean
    # first flips false->true (kittrial-5bb.351). The content digest is part of
    # it: an edit to that entry (same ID, new body) must move the marker. Never a
    # timestamp. Null when there is nothing newer, or when coverage is unknown:
    # the marker is a precise claim, so a reader falls back to the boolean then.
    # kittrial-5bb.354: with ``marker_fields`` (the current fields digest) the marker is
    # the shared activity_marker, so it also moves on a title/description/acceptance edit.
    # kittrial-5bb.367: a legacy checkpoint read by the timestamp rule ('timestamp'
    # coverage) gets the same marker, from the newest other-party entry at/after it;
    # the coverage label tells the client that a backdated arrival or an older edit
    # can be missed.
    marker=None
    if coverage!='unknown' and (others or marker_fields is not None):
        newest=max(others,key=lambda e:(parse_moment(e['timestamp']),e['entry_id'])) if others else None
        marker=(activity_marker(newest,marker_fields) if marker_fields is not None
                else newest['entry_id']+':'+content_hash(newest)[:MARKER_DIGEST])
    refs=[{'entry_id':e['entry_id'],'kind':e['kind'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
           'changed':any(c is e for c in changed_late)} for e in entries[:NEWER_MAX]]
    history='history '+data['task']
    note=('Activity exists that the saved checkpoint did not incorporate; its next_action below may be stale. '
          'Entry refs are the oldest unincorporated ones with per-entry authors; counts cover the full snapshot, including edited and late backdated entries the excerpt omits. '
          'Reading clears nothing: acknowledging a direction, reconciling it into a new checkpoint and completing it stay separate explicit acts.')
    if coverage=='unknown':
        note+=' Coverage is UNKNOWN: this checkpoint predates per-entry digests, so reconcile with history before trusting the counts.'
    elif coverage=='timestamp':
        note+=(' Coverage is TIMESTAMP: this checkpoint predates per-entry digests, so entries are selected by time; '
               'a backdated arrival or an edit of an older entry can be missed. Reconcile with history when in doubt.')
    elif coverage=='windowed':
        note+=(' Coverage is WINDOWED: '+str(unverified)+' older entries have no per-entry evidence recorded by this checkpoint, so their current content is unverified and they are excluded from the fresh/changed counts. '
               'Run "checkpoint '+data['task']+' --verify" for the classification supported by evidence retained across the task\'s checkpoints (entries no checkpoint ever recorded stay unknown).')
    result={'own_count':len(own),'other_count':len(others),
            'newer_activity_marker':marker,
            'fresh_count':len(fresh),'changed_or_late_count':len(changed_late),
            'unverified_count':unverified,
            'coverage':coverage,
            'other_authors':{'items':[clip(a,96) for a in authors[:NEWER_MAX]],'omitted':max(0,len(authors)-NEWER_MAX)},
            'entries':refs,'omitted':max(0,len(entries)-NEWER_MAX),
            'history':history,
            'history_new':'history '+data['task']+' --since '+utc_text(parse_moment(checkpoint_timestamp,'checkpoint timestamp')) if checkpoint_timestamp else history,
            'verify':'checkpoint '+data['task']+' --verify',
            'field_changes':field_changes or [],
            'note':note}
    if direction_idx is not None:
        outstanding=unresolved_directions(data['entries'] if direction_data is None else direction_data,direction_idx,owner,counted)
        result['unresolved_directions']={'total':len(outstanding),
            'items':[{'entry_id':e['entry_id'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
                      'state':(direction_idx.get(e['entry_id']) or {}).get('state','unacknowledged')} for e in outstanding[:NEWER_MAX]],
            'omitted':max(0,len(outstanding)-NEWER_MAX)}
        if not counted:result['unresolved_directions']['check']=directions_check_state(directions_reason)
    return result

def compact_integration(value):
    """One integration block without the two additive native-identity keys.

    ``review_state.integration`` gained ``record_id``/``order`` for the recorded
    hand-back's write gate and reader (kittrial-5bb.343). The compact brief is a
    bounded projection (its long-chain size is held below 6000 bytes), so it drops
    exactly those two keys; every key the brief already carried stays.
    """
    if not isinstance(value,dict):
        return value
    return {key:item for key,item in value.items() if key not in ('record_id','order')}


def brief(rows,project,task,offset=0,limit=5,operators=None,journal=None,verifiers=None,actor=None,full=False,
          independent_approval=False,owners=None,provenance_writes=True,identity_names=None,
          field_history=None):
    if type(offset) is not int or offset<BRIEF_ITEM_OFFSET_MIN or type(limit) is not int or not BRIEF_ITEM_LIMIT_MIN<=limit<=BRIEF_ITEM_LIMIT_MAX:
        raise ValueError('Invalid unresolved-item page: --items-offset must be >= %d and --items-limit must be %d..%d' % (BRIEF_ITEM_OFFSET_MIN,BRIEF_ITEM_LIMIT_MIN,BRIEF_ITEM_LIMIT_MAX))
    issue=task_row(rows,task);state=checkpoint_state(issue);current=state['current'];invalid=state['invalid']
    # DISPLAY ONLY (kittrial-5bb.333): the stored assignee is the actor id and `owner` stays
    # exactly that value. `owner_name`/`assigned_to` are labels built from an actor id the
    # caller already knows; they never turn a stored name into an actor.
    owner_raw=issue.get('assignee')
    owner_name=(identity_names or {}).get(owner_raw) if isinstance(owner_raw,str) and owner_raw else None
    assigned_to='%s (%s)' % (owner_name,owner_raw) if owner_name and owner_name!=owner_raw else (owner_raw or 'unassigned')
    data=snapshot(rows,project,task);p,c=current if current else (None,None)
    facts=next(r for r in project_facts(rows) if r['id']==task)
    items=p['open_items'] if p else []
    if offset>len(items):raise ValueError('Unresolved-item offset exceeds total')
    deps=[d for d in (issue.get('dependencies') or []) if d.get('type')!='parent-child']
    from work import workflow
    from review_state import RECONCILIATION_WARNING_PREFIX, is_integration_warning, scopes_for
    from guidance import brief_block
    review=workflow(issue,scopes_for(rows,task),operators=operators,journal=journal,
                    actor=actor,independent_approval=independent_approval,owners=owners,
                    provenance_writes=provenance_writes)
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
    # The compact brief keeps its released size bound (kittrial-5bb.6 item 2a and the
    # long-chain test). The additive integration record identity/order (kittrial-
    # 5bb.343) is carried by `review TASK` and `work` rows, where the follow-on write
    # gate and the reader need it; the compact brief's bounded prior slice drops those
    # two keys so a long chain does not grow past 6000 bytes.
    for entry in bounded_priors:
        entry['integration']=compact_integration(entry.get('integration'))
    review_next={'changes-requested':'Address the outstanding review requests for the current contribution; read review '+task+'.',
                 'awaiting-review':'Reviewer: retrieve and verify the current contribution, then record a review request or approval.',
                 'awaiting-integration':'Authorized integrator: integrate the approved contribution and record scoped integration evidence.',
                 'held':'The contribution is held; read review '+task+' for the record.',
                 'integrated':'Integration is recorded for this contribution; follow the project release/deployment workflow and scoped lifecycle evidence.',
                 'withdrawn':'The current contribution was withdrawn by its author or a coordinator; read review '+task+' or deliver a new revision.',
                 'superseded':'The current contribution was marked superseded; read review '+task+' or deliver a new revision.'}
    if issue.get('status')=='closed' and review['review_state']=='awaiting-integration':
        review_next['awaiting-integration']='This task is closed; approval and integration evidence remain readable in review '+task+'. Closure is not approval or integration.'
    if review.get('review_state')=='held':
        # Say WHICH reason the contribution is held (kittrial-5bb.10 review item 5): an
        # open hold and a failed attestation are different records with different
        # remedies, and the shared projection already knows which one applies.
        from review_workflow import integration_hold_sentence
        held_sentence=integration_hold_sentence(review)
        if held_sentence:
            review_next['held']=held_sentence[:1].upper()+held_sentence[1:]+' Read review '+task+'.'
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
    counted,directions_off_reason=directions_counted(provenance_writes,issue.get('status'))
    if p is not None:
        dispositions=chain_dispositions(state['history'])
        outstanding=unresolved_directions(direction_data,dispositions,context[0],counted)
        dir_out={'total':len(outstanding),
                 'items':[{'entry_id':e['entry_id'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
                           'state':(dispositions.get(e['entry_id']) or {}).get('state','unacknowledged')} for e in outstanding[:NEWER_MAX]],
                 'omitted':max(0,len(outstanding)-NEWER_MAX)}
        if not counted:
            dir_out['check']=directions_check_state(directions_off_reason);dir_out['reason']=directions_off_reason
        if dir_out['total']:
            ids=', '.join(i['entry_id'] for i in dir_out['items'][:NEWER_MAX])
            ids+=(f' (+{dir_out["omitted"]} more)' if dir_out['omitted'] else '')
            next_action='OUTSTANDING DIRECTIONS ('+str(dir_out['total'])+'): '+ids+' — resolve or supersede with evidence. '+next_action
    if p is not None:
        excluded=excluding_checkpoint(data,c['id'],c.get('author'))
        excluded_cursor=activity_cursor(excluded)
        stale=not cursor_matches(p['activity_cursor'],excluded,compatible=excluded_cursor)
        if stale:
            # kittrial-5bb.293 item 2: a title/description/acceptance change is newer
            # activity too. The checkpoint's cursor carries the field lengths it saw;
            # the live snapshot carries the current ones.
            try:
                recorded=untoken(p['activity_cursor'])
            except ValueError:
                recorded=None
            recorded_fields=(recorded or {}).get('fields') if isinstance(recorded,dict) else None
            current_sizes={k:len(str(issue.get(k) or '')) for k in ('title','description','acceptance_criteria')}
            changes=field_changes_since(recorded_fields, current_sizes)
            newer=newer_activity_summary(excluded,retained_provenance(state),c.get('created_at'),context[0],dispositions,direction_data,counted,directions_off_reason,field_changes=changes,marker_fields=fields_digest(issue))
            if not recorded_fields and not changes and not newer.get('entries'):
                # An older cursor carries no lengths and no entry explains the stale
                # flag: the change is in the task fields (state_sha256). Report that
                # without old_length, never all three fields with old_length None
                # (kittrial-5bb.293 item 2 rev2).
                changes=field_changes_since(None, current_sizes, only_if_unexplained=True)
                newer['field_changes']=changes
            if review['review_state'] not in review_next:
                dirs=newer.get('unresolved_directions')
                dir_note='' if not dirs else '; '+str(dirs['total'])+' outstanding direction(s) not yet resolved/superseded'
                next_action=('STALE CHECKPOINT: activity the checkpoint did not incorporate exists ('+str(newer['other_count'])
                             +' by other actors, '+str(newer['own_count'])+' own'+dir_note
                             +('' if newer['coverage'] in ('snapshot','windowed') else
                               (', coverage TIMESTAMP — a backdated arrival or an edit of an older entry can be missed'
                                if newer['coverage']=='timestamp' else
                                ', coverage UNKNOWN — reconcile with history before trusting counts'))
                             +'). Read newer activity first: '+newer['history']
                             +'. The recorded checkpoint next action was: '+p['next_action'])
    # The one newer-activity definition (kittrial-5bb.354): brief carries the SAME fields
    # the work rows and the agent routes carry, from the same function, beside the
    # released `checkpoint.newer_activity` (which means "the checkpoint is stale", own
    # entries and task-state changes included).
    activity=None
    if p is not None:
        try:
            activity=activity_fields(issue,c,retained_provenance(state),fields_digest(issue),newer if stale else None,
                                     dir_out['total'],data=data)
        except StopIteration:
            activity=None
    # Which of title, description and acceptance changed: only the tracker's version
    # history knows (field_history.py), read ONLY when the checkpoint is stale and only
    # here - never on a wait or next poll. ``field_history`` is the caller's bounded,
    # read-only, cached reader; without one the section says the fields are not known.
    changed_fields=[];fields_note=None
    if p is not None and stale:
        import field_history as field_versions
        if field_history is None:
            fields_note=field_versions.UNKNOWN_NOTE%'this route does not read the tracker history'
        else:
            try:
                history_text=field_history(task)
            except field_versions.TooLarge:
                history_text=False
            except Exception:
                history_text=None
            if history_text is False:
                changed_fields,fields_note=[],field_versions.TOO_LARGE_NOTE
            else:
                changed_fields,fields_note=field_versions.changes(history_text,issue,c.get('created_at'))
    section=changes_since_checkpoint(
        excluded if p is not None else data, retained_provenance(state) if p is not None else None,
        {'comment_id':c['id'],'author':c.get('author'),'timestamp':c.get('created_at')} if p is not None else None,
        context[0],fields=changed_fields,current=p is not None and not stale,fields_note=fields_note)
    # The project the checkpoint was taken in, when it is not this one (kittrial-5bb.136):
    # after restore-new under another name an unchanged task reads current, which is true
    # of the copy, but the restore is still worth reconciling before trusting the
    # checkpoint's next action. Before kittrial-5bb.131 the STALE reading was that prompt.
    if review.get('next_actor') == 'anyone':
        claim_step = 'This task is open and unassigned. Claim %s before starting work.' % task
        next_action = claim_step + (' ' + next_action if p is not None else '')
    taken_in=None if p is None else checkpoint_taken_in(p['activity_cursor'],project)
    if taken_in and not stale and review['review_state'] not in review_next:
        # A prefix, like OUTSTANDING DIRECTIONS, so neither hides the other.
        next_action=('CHECKPOINT FROM PROJECT '+taken_in+': written before this project was restored or '
                     'copied under its current name; its task content is unchanged. Confirm the history '
                     'before relying on it. '+next_action)
    # The compact brief carries the next-action ROLE once, inside the shared review
    # projection block (kittrial-5bb.6 review item 2: it used to be carried twice, at the
    # top level and in `review`). 6000 bytes is the size the long-chain test in
    # tests/test_follow_on_contributions.py holds, NOT a bound the code enforces: a
    # one-contribution brief with disagreeing integration facts measures 6088 and 6368
    # bytes (kittrial-5bb.6 review item 2a). So the reconciliation signal is carried in
    # the one way that does not repeat itself: `work_state` is `reconciliation-needed`
    # exactly when `reconciliation_needed` is true, and `reconciliation_reasons` names the
    # mismatching facts whenever there are any. The boolean itself is carried by `work`
    # rows and by the `review` read, which have no such bound.
    # kittrial-5bb.284 review B1: deliberately NO `next_action` prefix here. The
    # `directions_check` block below already states why nothing is outstanding, so a
    # prefix adds nothing but noise: it would rewrite the next action of every
    # checkpointed brief on every installation (the switch is off everywhere) even
    # when there is no comment to acknowledge, and because it was added last it would
    # land in FRONT of STALE CHECKPOINT:, OUTSTANDING DIRECTIONS and CHECKPOINT FROM
    # PROJECT, which would make docs/WORKER_GUIDE.md's "prefixed STALE CHECKPOINT:"
    # sentence untrue.
    brief_review=dict(review,pending_requests=pending[:5],pending_total=len(pending),more='review '+task if len(pending)>5 else None,
                      prior_contributions=bounded_priors,prior_contributions_total=len(priors),
                      prior_contributions_more='review '+task if len(priors)>PRIOR_BRIEF_LIMIT else None,
                      reverts=reverts[-REVERT_BRIEF_LIMIT:],reverts_total=len(reverts),
                      reverts_more='review '+task if len(reverts)>REVERT_BRIEF_LIMIT else None,
                      integration=compact_integration(review.get('integration')))
    brief_review.pop('reconciliation_needed',None)
    # Internal attention acknowledgement metadata belongs in the full projection.
    brief_review.pop('last_response',None)
    # The coordinator's recorded hand-back (kittrial-5bb.343): the same additive
    # block a work row carries. An APPLYING one is the assignee's move, so `next`
    # quotes the recorded text once and the shared review block names the role.
    followon_block=review.get('followon')
    if followon_block is not None:
        result_followon=followon_block
    else:
        result_followon=None
    if isinstance(followon_block,dict) and followon_block.get('applies'):
        import followon as followon_records
        next_action=followon_records.next_text(followon_block,checkpoint_available=provenance_writes)
        brief_review['next_actor']='assignee'
    # Avoid adding off-state bytes to compact long-chain briefs. The setting
    # is carried only when ON; absence means false, including older endpoints.
    if not brief_review.get('approval_requires_other_party'):
        brief_review.pop('approval_requires_other_party',None)
    from task_assignment import notices as assignment_notices
    notice_block=assignment_notices(issue,project,actor)
    notice_records=assignment_notices(issue,project,None,limit=20)
    result={**({'assignment_records':notice_records} if notice_records['total'] else {}),**({'assignment_notices':notice_block} if notice_block['total'] else {}),**attention,'reference_drafts_matching':reference_drafts,'task':task,'title':clip(issue.get('title'),200),'owner':clip(owner_raw or 'unassigned',96),**({'owner_name':clip(owner_name,96),'assigned_to':clip(assigned_to,200)} if owner_name and owner_name!=owner_raw else {}),'status':issue.get('status'),
            'activity_cursor':activity_cursor(data),'checkpoint':None if p is None else {'comment_id':str(c['id']),'author':clip(c.get('author'),96),'timestamp':c.get('created_at'),'source_commit':p['source_commit'],'branch':p['branch'],'incorporated_activity_cursor':p['activity_cursor'],
                'newer_activity':stale,**({'taken_in_project':taken_in} if taken_in else {})},
            'newer':newer,
            # kittrial-5bb.354: only with a checkpoint, so a task without one keeps the
            # compact size the long-chain brief holds; the text form and the web task
            # read still say there is no checkpoint (SECTION_NO_CHECKPOINT).
            **({'activity':activity,'changes_since_checkpoint':section} if p is not None else {}),
            'directions':dir_out,
            'intent':clip(p['intent'] if p else issue.get('description'),600),'acceptance':clip(p['acceptance'] if p else issue.get('acceptance_criteria'),1000),
            'current_position':p['summary'] if p else 'No checkpoint yet; current position and unresolved items have not been summarized.',
            'next_action':next_action,
            'unresolved':{'coverage':'explicit checkpoint items only; unsummarized prose is not classified','total':len(items) if p else None,'items':items[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(items) else None},
            'review':brief_review,
            **({'followon':result_followon} if result_followon is not None else {}),
            'lifecycle_matches_contribution':matches_contribution,
            'deployed_delivery':deployed_delivery,
            'deployed_delivery_is_current_contribution':deployed_current,
            'deployed_live':deployed_live,
            'dependencies':{'total':len(deps),'items':[{k:clip(d.get(k),160) for k in ('depends_on_id','type')} for d in deps[:8]],'omitted':max(0,len(deps)-8)},
            'lifecycle':{dim:dict(value=f['value'],event_id=f['event_id'],
                                 **({'reason':f['reason']} if f.get('reason') else {}))
                         for dim,f in facts['facts'].items()},
            'lifecycle_scope':{k:clip(v,160) for k,v in (facts['scope'] or {}).items()},
            # The additive `Reconciliation needed:` line is appended AFTER the top-level
            # warning lines main already had, so the FIRST warning a reader sees is still
            # the one main showed (kittrial-5bb.6 review item 4c). The other integration
            # warnings keep main's place and main's order.
            'warnings':((['Malformed checkpoint comments ignored: '+', '.join(invalid[:5])] if invalid else [])
                       +[w for w in integration_warnings if not w.startswith(RECONCILIATION_WARNING_PREFIX)]
                       +(['Direction baseline: comments strictly before the newest legacy checkpoint have UNKNOWN coverage and are not outstanding directions; timestamp ties remain possible directions.'] if context[1] is not None else [])
                       +['Newer activity also includes edits, deletions or changed task fields. Prose resolutions never silently clear explicit items.']
                       +[w for w in integration_warnings if w.startswith(RECONCILIATION_WARNING_PREFIX)]),
            'evidence':{'issue':'show '+task,'history':'history '+task,'checkpoint_entry':task+'-c'+str(c['id']) if c else None}}
    if p is not None:
        governance=None
        # Read the governance snapshot only for a `req:` token: every other brief reads
        # nothing new (TRACEABILITY_DESIGN §2.4; context index §2.1.2 "no new I/O").
        if journal is not None and needs_requirement_governance((result['unresolved']['items'],'source'),
                                                                (p['resolved'][:limit],'evidence')):
            import requirement_governance
            try:
                governance=requirement_governance.snapshot(journal,project)
            except (ValueError,OSError):
                governance=None
        pointers=ContextPointers(rows,project,operators,governance=governance)
        result['unresolved']['pointer_items']=pointers.item_pointers(result['unresolved']['items'],'source')
        result['resolved']={'items':p['resolved'][:limit],
                            'pointer_items':pointers.item_pointers(p['resolved'][:limit],'evidence'),
                            'total':len(p['resolved']),'omitted':max(0,len(p['resolved'])-limit)}
    if p is not None and not counted:
        # The brief states plainly WHY no direction is outstanding (kittrial-5bb.284):
        # a reader must not infer an unacknowledged instruction from a count that the
        # installation cannot clear. `state` names the actual reason, so a closed task
        # is never reported as the switch being off (review B1).
        result['directions_check']={'state':'off' if directions_off_reason=='switch-off' else directions_off_reason,
            'reason':directions_off_reason,
            'note':DIRECTIONS_OFF_NOTE if directions_off_reason=='switch-off' else DIRECTIONS_CLOSED_NOTE}
    if journal is not None:
        # The standing guidance channel (kittrial-5bb.99): every endpoint brief
        # carries the current version; a direct library call with no project path
        # cannot read the guidance and omits the block.
        result['guidance']=brief_block(journal,actor)
    # kittrial-5bb.293 item 1: when a compact field is cut, say how to read the whole
    # over the route the caller uses. The endpoint names `show TASK`; the HTTP brief
    # rewrites this to the GET route and carries the link. The block is present ONLY
    # when a field was actually cut: an uncut brief then carries exactly the bytes it
    # carried before this pointer existed, which is what keeps the long prior chain
    # under the 6000-byte bound the follow-on test holds (kittrial-5bb.293 r6 item 2).
    if (result.get('title') or {}).get('omitted_chars') or \
            (result.get('intent') or {}).get('omitted_chars') or \
            (result.get('acceptance') or {}).get('omitted_chars'):
        result['read_whole'] = {'route': 'show %s' % task,
                                'note': 'The complete text: show %s.' % task}
    if full:
        # The explicit fuller-context read (kittrial-5bb.8): the compact brief stays
        # compact, and a caller that needs the whole text asks for it and is told how
        # many characters the compact read clips from each field. The block is BOUNDED
        # like a history page: at most FULL_CONTEXT_BUDGET encoded bytes in total and
        # FULL_FIELD_BUDGET for any one field, with what the cap removed reported per
        # field, so a hostile description cannot make one answer unbounded (review, F4).
        # Control characters are escaped exactly as the JSON form already escapes them,
        # and `show TASK` keeps the complete text.
        full_fields={'title':str(issue.get('title') or ''),
                     'owner':str(issue.get('assignee') or 'unassigned'),
                     'intent':str(p['intent'] if p else (issue.get('description') or '')),
                     'acceptance':str(p['acceptance'] if p else (issue.get('acceptance_criteria') or '')),
                     'current_position':str(result['current_position']),'next_action':str(next_action)}
        counts={'title':result['title']['omitted_chars'],'owner':result['owner']['omitted_chars'],
                'intent':result['intent']['omitted_chars'],'acceptance':result['acceptance']['omitted_chars'],
                # These two are never clipped by the compact read; they are carried whole.
                'current_position':0,'next_action':0}
        bounded={};bounded_omitted={};truncated=[];remaining=FULL_CONTEXT_BUDGET
        for name,value in full_fields.items():
            text=plain_text(value)
            kept=fitting_prefix(text,max(0,min(remaining,FULL_FIELD_BUDGET)))
            bounded[name]=kept
            bounded_omitted[name]=len(text)-len(kept)
            if bounded_omitted[name]:truncated.append(name)
            remaining-=encoded_size(kept)
        result['full_context']={'note':'Task context, bounded to %d encoded bytes in total; omission_counts are the '
                                       'characters the compact brief clips from each field, and full_omitted_chars are the characters '
                                       'this cap removed. `show TASK` carries the complete text.'
                                       % FULL_CONTEXT_BUDGET,
                                'omission_counts':counts,
                                'full_omitted_chars':bounded_omitted,
                                'truncated':truncated,
                                'complete':not truncated,
                                'context_budget':FULL_CONTEXT_BUDGET,
                                'field_budget':FULL_FIELD_BUDGET,
                                **bounded}
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

PROVENANCE_OFF=('checkpoint_provenance_writes is off (the installation default). Readers accept the new shape; '
                'omit provenance/directions to write an older-kit-compatible checkpoint. '
                'An operator enables the installation switch only after the rollback target can read these fields.')
SERVER_DERIVED='carried and direction_owner are server-derived; omit them from the request'

def save_checkpoint(rows,project,task,p,actor,run,provenance_writes=False):
    # The declared wait state travels BESIDE the checkpoint in the same payload file
    # (kittrial-5bb.263; docs/LANE_BOARD_DESIGN.md section 1). `lane_state` is popped out
    # here, before the checkpoint is validated, so the checkpoint object keeps its closed
    # field set: nothing existing grows a key, an unknown field is still refused, and an
    # older kit reads and re-checkpoints the task exactly as it did before. The checkpoint
    # write below is the ONLY writer of a lane-state record.
    lane=None
    if isinstance(p,dict) and 'lane_state' in p:
        p=dict(p);lane=p.pop('lane_state')
    problems=checkpoint_problems(p,task)
    if lane is not None:
        from lane_state import problems as lane_state_problems
        problems=problems+lane_state_problems(lane,task)
    if problems:
        # A record that is refused anyway is told everything else that would refuse it, the
        # installation switch included (review 01a10c0b). A record with nothing else wrong
        # meets these two below, each alone and in the order they always had.
        if isinstance(p,dict):
            if 'carried' in p or 'direction_owner' in p:problems.append(SERVER_DERIVED)
            if not provenance_writes and any(k in p for k in ('incorporated_digests','provenance','directions')):
                problems.append(PROVENANCE_OFF)
        raise ValueError(problems_message(problems))
    issue=task_row(rows,task)
    from coordination import is_merge_slot,merge_slot_sentence
    if is_merge_slot(issue):raise ValueError(merge_slot_sentence(task)+'; it takes no checkpoint')
    if 'carried' in p or 'direction_owner' in p:raise ValueError(SERVER_DERIVED)
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
    # Identity is the checkpoint body alone: the committed checkpoint receipt cannot carry
    # lane_state (its field set is closed and an older kit must read it unchanged). A
    # retry therefore reconciles the checkpoint exactly as before, and the declared wait
    # state is completed or refused beside it by `lane_state.ensure` below.
    identity=request_identity(body)
    comments={str(c['id']):c for c in issue.get('comments') or []}
    for cid,existing in state['history']:
        c=comments[cid]
        if c.get('author')!=actor:continue
        if request_identity(existing)==identity:
            recorded=state['provenance'][cid] or {'covered':0}
            answer={'comment_id':str(c['id']),'reconciled':True,'covered':recorded['covered'],'bytes':len(str(c['text']))}
            if lane is not None:
                # A retry that reconciles the checkpoint must still end with the record
                # present: a crash between the two writes is completed here, and a second
                # identical write publishes nothing.
                from lane_state import ensure as ensure_lane_state
                answer['lane_state']=ensure_lane_state(run,issue.get('comments'),actor,task,lane,str(c['id']))
            return answer
    # Direction dispositions must reference the digests actually incorporated for a
    # NEW write (a stored receipt was already validated when it was written).
    if not provenance_writes and any(k in p for k in ('incorporated_digests','provenance','directions')):
        raise ValueError(PROVENANCE_OFF)
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
    transition(current[0] if current else None,payload,'request')
    result=json.loads(run(['comments','add',task,PREFIX+canonical_bytes(payload).decode(),'--json']))
    answer={'comment_id':str(result['id']),'reconciled':False,'covered':payload.get('provenance',{}).get('covered',0),'bytes':size}
    if lane is not None:
        # The lane-state record names the checkpoint just published and is written in the
        # same act, through the same internal run path.
        from lane_state import ensure as ensure_lane_state
        answer['lane_state']=ensure_lane_state(run,issue.get('comments'),actor,task,lane,answer['comment_id'])
    return answer

def verify_checkpoint(rows,project,task):
    """Verify retained evidence using one linked, parsed checkpoint state."""
    state=checkpoint_state(task_row(rows,task));current=state['current']
    if state['invalid']:raise ValueError('Malformed checkpoint entries require correction before verification')
    snap=snapshot(rows,project,task)
    if current is None:
        return {'task':task,'checkpoint':None,'coverage':'none',
                'fresh':len(snap['entries']),'changed':0,'unchanged':0,'unverified':0,
                'note':'No checkpoint exists; every entry is unincorporated.'}
    cid=str(current[1]['id']);snap=excluding_checkpoint(snap,cid,current[1].get('author'));total=len(snap['entries'])
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

#: The 'changes since your checkpoint' section (kittrial-5bb.354): at most this many
#: entries, newest first, each body clipped to SECTION_BODY_CHARS, and all bodies
#: together within SECTION_BUDGET encoded bytes; what does not fit is counted.
SECTION_ENTRIES_MAX=10
SECTION_BODY_CHARS=300
SECTION_BUDGET=3000
SECTION_NO_CHECKPOINT=('This task has no checkpoint yet, so there is nothing to compare with: '
                       'everything on it is new to you. Read its history.')

def entry_state(entry,fresh,checkpoint_timestamp):
    """new, edited or late: how one unincorporated entry relates to the checkpoint."""
    if not any(e is entry for e in fresh):return 'edited'
    if checkpoint_timestamp and parse_moment(entry['timestamp'])<parse_moment(checkpoint_timestamp):return 'late'
    return 'new'

def changes_since_checkpoint(data,prov,checkpoint,owner,fields=None,current=False,fields_note=None):
    """The bounded 'changes since your checkpoint' section (kittrial-5bb.354).

    ``checkpoint`` is {comment_id, author, timestamp} of the newest valid checkpoint, or
    None. The entries are the SAME selection the counts and the marker read
    (:func:`newer_selection`), newest first, each with its author, time, entry id,
    whether it is the owner's own and whether it is new, edited or a late arrival; the
    body is untrusted text, so it is made terminal-safe and clipped. ``fields`` are the
    changed task fields, already in their reported shape, and ``fields_note`` says why
    they could not be determined when that is so (None when the reading is exact).
    ``current`` says the
    checkpoint's cursor matches the task as it is now: nothing is listed and coverage
    reads 'current', as on the queue rows. Reading clears nothing.
    """
    history='history '+data['task']+' --since-checkpoint'
    if checkpoint is None:
        return {'checkpoint':None,'coverage':None,'total':None,'own_count':None,'other_count':None,
                'unverified_count':None,'entries':[],'omitted':0,'fields':[],'fields_note':None,
                'history':'history '+data['task'],'note':SECTION_NO_CHECKPOINT}
    stamp=checkpoint.get('timestamp')
    if current:coverage,entries,fresh,unverified='current',[],[],0
    else:coverage,entries,fresh,_changed,unverified=newer_selection(data,prov,stamp)
    newest=sorted(entries,key=lambda e:(parse_moment(e['timestamp']),e['entry_id']),reverse=True)
    shown=[];spent=0
    for entry in newest[:SECTION_ENTRIES_MAX]:
        body=clip(plain_text(entry.get('body')),SECTION_BODY_CHARS)
        cost=encoded_size(body['text'])
        if shown and spent+cost>SECTION_BUDGET:break
        spent+=cost
        shown.append({'entry_id':entry['entry_id'],'kind':entry['kind'],'author':clip(entry['author'],96),
                      'timestamp':entry['timestamp'],'own':same_activity_actor(entry['author'],owner),
                      'state':entry_state(entry,fresh,stamp),'body':body})
    own=sum(1 for e in entries if same_activity_actor(e['author'],owner))
    note=('Newest first; read these before acting on the checkpoint. Bodies are clipped; read the whole '
          'entries with '+history+'. Reading clears nothing: a new checkpoint incorporates them.')
    if coverage=='timestamp':
        note+=(' Coverage is TIMESTAMP (the checkpoint predates per-entry digests): entries are selected by time, '
               'so a backdated arrival or an edit to an older entry is not seen.')
    elif coverage=='unknown':
        note+=' Coverage is UNKNOWN (the checkpoint predates per-entry digests): entries are selected by time, so an edit to an older entry is not seen.'
    elif coverage=='windowed':
        note+=' Coverage is WINDOWED: '+str(unverified)+' older entries cannot be re-verified and are not listed.'
    return {'checkpoint':{'comment_id':str(checkpoint.get('comment_id')),'author':clip(checkpoint.get('author'),96),
                          'timestamp':stamp},
            'coverage':coverage,'total':len(entries),'own_count':own,'other_count':len(entries)-own,
            'unverified_count':unverified,'entries':shown,'omitted':len(entries)-len(shown),
            'fields':list(fields or []),'fields_note':None if current else fields_note,
            'history':history,'note':note}

def checkpoint_queue_fields(rows,row,state=None,provenance_writes=True,full=None,selection_cache=None):
    """Bounded queue fields; share the brief's linked state and full-row snapshot.

    This is the ONE computation of the newer-activity fields (kittrial-5bb.354,
    docs/BRIEFINGS.md "Newer activity: one definition"): the work rows carry its answer,
    and so, unchanged, do brief's ``activity`` block, the agent routes and the web task
    read. ``newer_activity`` keeps its released rule (:func:`newer_comment_by_other`);
    the counts and the marker are the precise signals, and the marker also moves on an
    edit of the title, description or acceptance (:func:`activity_marker`).
    ``wait_newer_activity_marker`` (kittrial-5bb.369) is the checkpoint-independent
    marker the worker's wait fingerprints. On a read that fails, every field is null and
    the caller keeps its own ``newer_activity``.
    """
    empty={'newer_activity_marker':None,'newer_activity_fields':None,
           'wait_newer_activity_marker':None,
           'newer_activity_by_others':None,'newer_activity_own':None,
           'newer_activity_coverage':None,'unresolved_directions':None}
    try:
        if state is None:state=checkpoint_state(row)
        elif not state['provenance']:
            state=dict(state,provenance={cid:normalize_provenance(p) for cid,p in state['history']})
        # The wait marker does not depend on a checkpoint, and it must not move when the
        # caller writes its FIRST one either (kittrial-5bb.369), so it is computed from the
        # task's full snapshot for every row. The project name only seeds the snapshot's
        # task key; the marker reads entries and task fields, never the project.
        has_checkpoint=state['current'] is not None and not state['invalid']
        full=full if full is not None else checkpoint_snapshot(rows,row,state)
        wait_marker=wait_activity_marker(full,row)
        if not has_checkpoint:
            return dict(empty,wait_newer_activity_marker=wait_marker)
        p,c=state['current']
        data=excluding_checkpoint(full,c['id'],c.get('author'))
        dispositions=chain_dispositions(state['history'])
        context=direction_context(row,state);direction_data=direction_entries(data,context)
        counted,_directions_off_reason=directions_counted(provenance_writes,row.get('status'))
        prov=retained_provenance(state);digest=fields_digest(row)
        newer=None
        if not cursor_matches(p['activity_cursor'],data):
            selection = checkpoint_selection(full, state, len(state['history']) - 1, c, selection_cache)[2]
            newer=newer_activity_summary(data,prov,c.get('created_at'),
                                         context[0],dispositions,direction_data,counted,_directions_off_reason,
                                         marker_fields=digest,selection=selection)
        fields=activity_fields(row,c,prov,digest,newer,
                               len(unresolved_directions(direction_data,dispositions,context[0],counted)),
                               data=full)
        fields['wait_newer_activity_marker']=wait_marker
        return fields
    except (ValueError,TypeError,KeyError,StopIteration):return empty

def activity_fields(row,checkpoint_comment,prov,digest,newer,unresolved,data=None):
    """The eight newer-activity fields from one read's own results (kittrial-5bb.354).

    ``newer`` is that read's :func:`newer_activity_summary` (computed with
    ``marker_fields=digest``), or None when the checkpoint's cursor is current. brief and
    the queue rows both call this, each with what it already computed, so the fields
    cannot differ and brief reads nothing twice. Raises StopIteration when the checkpoint
    comment is not on the row. ``newer_activity_fields`` is the current title/description/
    acceptance digest alone (kittrial-5bb.367 F2b), so a reader can separate a field edit
    from the marker's entry-plus-fields hex; it is null in the empty (no checkpoint) row.
    ``data`` is the task's FULL snapshot, from which ``wait_newer_activity_marker`` is
    computed (kittrial-5bb.369); it is null when the caller has no snapshot.
    """
    if newer is None:
        # Nothing newer: the marker still stands, so a later claim, which makes the cursor
        # stale but adds no entry and no field edit, reads the same marker. kittrial-5bb.367:
        # a legacy checkpoint with a timestamp gets the fields marker too (the timestamp
        # rule can select later entries); only a checkpoint with no timestamp at all has
        # none, because there is no selection to compare with.
        own=others=0;coverage='current'
        legacy_without_stamp=prov is None and not (checkpoint_comment or {}).get('created_at')
        marker=None if legacy_without_stamp else activity_marker(None,digest)
    else:
        own=newer['own_count'];others=newer['other_count'];coverage=newer['coverage']
        marker=newer['newer_activity_marker']
    return {'newer_activity':newer_comment_by_other(row,checkpoint_comment),
            'newer_activity_marker':marker,
            # kittrial-5bb.367 item F2b: the CURRENT title/description/acceptance digest on
            # its own, so a bridge can tell a field edit from the agent's own checkpoint
            # without unpicking the marker's combined hex.
            'newer_activity_fields':digest[:MARKER_DIGEST],
            # kittrial-5bb.369: the checkpoint-independent marker a worker's wait
            # fingerprints. Its own plan checkpoint does not move it; any other-party
            # entry (a comment, an edit, another actor's checkpoint) or field edit does.
            'wait_newer_activity_marker':wait_activity_marker(data,row) if isinstance(data,dict) else None,
            'newer_activity_by_others':others,'newer_activity_own':own,
            'newer_activity_coverage':coverage,
            'unresolved_directions':unresolved}

def snapshot_checkpoints(data):
    """Valid checkpoints of one snapshot in linked order, exactly as ``checkpoint`` reads them.

    Checkpoint comments are ordinary snapshot entries, so a checkpoint-relative history
    read or a cursor continuation can resolve the newest checkpoint without another
    export read and without mixing in live activity (kittrial-5bb.8).
    """
    prefix=data['task']+'-c'
    comments=[]
    for entry in data['entries']:
        if entry['kind']!='comment' or not entry['entry_id'].startswith(prefix):continue
        comments.append({'id':entry['entry_id'][len(prefix):],'text':entry['body'],
                         'created_at':entry['timestamp'],'author':entry['author']})
    return checkpoint_state({'id':data['task'],'comments':comments})

def snapshot_comment_index(data):
    """Checkpoint comment records keyed by comment id, read from the snapshot's own entries."""
    prefix=data['task']+'-c'
    return {entry['entry_id'][len(prefix):]:{'id':entry['entry_id'][len(prefix):],'created_at':entry['timestamp'],
                                             'author':entry['author']}
            for entry in data['entries'] if entry['kind']=='comment' and entry['entry_id'].startswith(prefix)}

def checkpoint_view(data,state,index,comments=None):
    """One linked checkpoint of a snapshot, with the CHAIN's retained evidence.

    ``provenance`` is ``retained_provenance`` of the chain up to and including this
    record, not the record's own map: once an earlier checkpoint exists the newest
    record stores only a delta, so a record-local map cannot say what an EARLIER record
    incorporated (kittrial-5bb.8 review, F1). ``record_provenance`` keeps that record's
    own normalized map for callers that need it.
    """
    comments=snapshot_comment_index(data) if comments is None else comments
    cid,payload=state['history'][index]
    comment=comments.get(cid) or {'id':cid,'created_at':None,'author':None}
    return {'comment_id':cid,'entry_id':data['task']+'-c'+cid,'payload':payload,'comment':comment,
            'record_provenance':normalize_provenance(payload),
            'provenance':retained_provenance(state,index),
            'chain_length':index+1}

def latest_checkpoint(data,actor=None):
    """The newest valid checkpoint of a snapshot, or None; a corrupt chain still refuses.

    The default baseline is the newest valid checkpoint BY ANYONE: the kit lets any
    project actor write a real checkpoint on a task it does not hold, so the newest
    record need not be the reader's own (kittrial-5bb.8 review, F3). ``actor`` restricts
    the baseline to that actor's newest valid checkpoint — the optional "my own newest"
    read — and returns None when that actor never wrote one.
    """
    state=snapshot_checkpoints(data)
    if not state['history']:return None
    index=len(state['history'])-1
    if actor is not None:
        comments=snapshot_comment_index(data)
        mine=[position for position,(cid,_payload) in enumerate(state['history'])
              if (comments.get(cid) or {}).get('author')==actor]
        if not mine:return None
        index=mine[-1]
    return checkpoint_view(data,state,index)

def checkpoint_by_id(data,cid):
    """The linked checkpoint with this comment id, or None (a cursor's stored boundary)."""
    state=snapshot_checkpoints(data)
    for index,(chain_id,_payload) in enumerate(state['history']):
        if chain_id==str(cid):return checkpoint_view(data,state,index)
    return None

def since_checkpoint_entries(data,checkpoint):
    """Entries the baseline checkpoint did not incorporate, in immutable snapshot order.

    The selection uses the checkpoint CHAIN's retained evidence, never the newest
    record's own map alone. Recorded provenance then makes it exact in both directions:
    an entry the chain proves was incorporated is dropped even when it sorts after the
    baseline, and a late backdated arrival or an entry edited since the checkpoint —
    including one an EARLIER checkpoint recorded — is selected even when it sorts before
    the baseline. An entry the retained evidence cannot re-verify at all is kept when it
    orders after the baseline (an extra entry is safer than a hidden one) and is counted
    by ``checkpoint_gaps``, because nothing can show whether its content changed. A
    legacy checkpoint records no provenance, so the released brief's timestamp rule
    applies: every entry at or after the checkpoint's timestamp is selected, and the
    answer says plainly what that cannot see (kittrial-5bb.8 review, F1).
    """
    entries=data['entries']
    if checkpoint is None:return list(entries)
    if checkpoint['provenance'] is None:
        stamp=parse_moment(checkpoint['comment'].get('created_at'),'checkpoint timestamp')
        return [entry for entry in entries
                if entry['entry_id']!=checkpoint['entry_id']
                and parse_moment(entry['timestamp'])>=stamp]
    positions={entry['entry_id']:index for index,entry in enumerate(entries)}
    boundary=positions.get(checkpoint['entry_id'])
    prov=checkpoint['provenance'];unincorporated_ids=set()
    selected,_fresh,_changed,_unverified=unincorporated(data,prov)
    unincorporated_ids={entry['entry_id'] for entry in selected}
    result=[]
    for index,entry in enumerate(entries):
        # The baseline checkpoint's own entry is never "after the checkpoint", and a
        # checkpoint never records its own comment in its provenance.
        if entry['entry_id']==checkpoint['entry_id']:continue
        after=boundary is None or index>boundary
        if entry['entry_id'] in unincorporated_ids:result.append(entry)
        elif after and classify_entry(entry,prov)!='incorporated':result.append(entry)
    return result

def checkpoint_gaps(data,checkpoint):
    """(coverage, unverified_count, note) for a checkpoint-relative read.

    ``coverage`` is `snapshot` (retained evidence re-verifies every entry the checkpoint
    incorporated), `windowed` (entries beyond the retained bounds have no per-entry
    evidence), `timestamp` (a legacy record, read by the released timestamp rule, so a
    backdated arrival or an edit of an older entry can be missed) or `unknown` (a legacy
    record with no timestamp at all). `unverified_count` is how many current
    entries the read could be hiding: entries the chain recorded but cannot re-verify
    that sort before the baseline. ``note`` says in plain words what this read cannot
    show, so a reader never has to infer a hidden instruction from a count
    (kittrial-5bb.8 review, F1/F3).
    """
    if checkpoint is None:
        return None,0,None
    who=('newest valid checkpoint, by anyone: '+str(checkpoint['comment_id'])+' by '
         +str(checkpoint['comment'].get('author'))+' at '+str(checkpoint['comment'].get('created_at')))
    prov=checkpoint['provenance']
    if prov is None:
        coverage='timestamp' if checkpoint['comment'].get('created_at') else 'unknown'
        return (coverage,0,who+'. It records no per-entry provenance (a legacy record), so the released timestamp rule is '
                'used: every entry at or after the checkpoint is shown. An entry edited since the checkpoint, or an arrival '
                'backdated to before it, CANNOT be seen in this read; read the whole history (history '+data['task']+') to be sure.')
    if prov['covered']-prov['verifiable']<=0:
        return ('snapshot',0,who+'. Retained evidence from the checkpoint chain re-verifies every entry it incorporated, '
                'so this read hides nothing it can prove.')
    count=sum(1 for entry in data['entries']
              if entry['entry_id']!=checkpoint['entry_id'] and classify_entry(entry,prov)=='unverified')
    return ('windowed',count,who+'. Retained evidence re-verifies '+str(prov['verifiable'])+' of the '+str(prov['covered'])
            +' entries this checkpoint recorded; '+str(count)+' current entries have no per-entry evidence and are only shown '
            'when they order after the checkpoint, so an edit to one of those older entries CANNOT be seen here. Run '
            'checkpoint '+data['task']+' --verify or read the whole history before trusting this read alone.')

def history_page(data,project,task,limit=5,since=None,cursor=None,body_budget=4000,
                 recent=None,kind=None,since_checkpoint=False,actor=None,since_my_checkpoint=False):
    """One page of an immutable snapshot, oldest-first or newest-first.

    ``recent`` (the CLI's ``--recent``/``--last``) pages the same snapshot newest-first
    and is the page size for that direction; each body is still delivered in body
    order, so a fragmented body reconstructs without gaps or duplicates and the
    continuation cursor walks toward older entries. ``kind`` filters entries by
    comment/event. ``since_checkpoint`` selects the activity the newest valid checkpoint
    did not incorporate; ``since_my_checkpoint`` uses this actor's own newest valid
    checkpoint instead of the newest one by anyone. Cursors bind the direction and both
    filters; a cursor written by the released kit has no direction key and keeps its
    oldest-first meaning, and unknown cursor keys are still refused.
    """
    # `--recent` is validated before `--limit`: parse_args mirrors the recent value into
    # the page size, so a caller that asked for too many newest entries must hear about
    # --recent (the option it gave), not about the internal limit (kittrial-5bb.8 review, F7).
    if recent is not None and (type(recent) is not int or not HISTORY_LIMIT_MIN<=recent<=HISTORY_LIMIT_MAX):
        raise ValueError('History --recent/--last must be %d..%d' % (HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX))
    if type(limit) is not int or not HISTORY_LIMIT_MIN<=limit<=HISTORY_LIMIT_MAX:raise ValueError('History limit must be %d..%d' % (HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX))
    if recent is not None:limit=recent
    if kind is not None and kind not in HISTORY_KINDS:raise ValueError('History --kind must be one of '+', '.join(HISTORY_KINDS))
    if type(body_budget) is not int or not HISTORY_BUDGET_MIN<=body_budget<=HISTORY_BUDGET_MAX:raise ValueError('History body budget must be %d..%d' % (HISTORY_BUDGET_MIN,HISTORY_BUDGET_MAX))
    if data.get('project')!=project or data.get('task')!=task:raise ValueError('History scope mismatch')
    if since_my_checkpoint and not actor:raise ValueError('History --since-my-checkpoint requires a caller actor')
    digest=content_hash(data);index=0;start=0;order='oldest'
    since=utc_text(parse_moment(since,'--since')) if since is not None else None
    c=None
    if cursor:
        c=untoken(cursor)
        if not isinstance(c,dict) or not HISTORY_CURSOR_BASE<=set(c)<=HISTORY_CURSOR_KEYS or type(c['v']) is not int or c['v']!=1 or c['kind']!='history':raise ValueError('Invalid history cursor')
        if (c['project'],c['task'],c['snapshot'])!=(project,task,digest):raise ValueError('Cursor snapshot/scope mismatch')
        if since is not None and since!=c['since']:raise ValueError('Cursor --since mismatch')
        if c.get('order','oldest') not in HISTORY_ORDERS:raise ValueError('Invalid history cursor')
        if c.get('entry_kind') is not None and c['entry_kind'] not in HISTORY_KINDS:raise ValueError('Invalid history cursor')
        if recent is not None and c.get('order','oldest')!='newest':raise ValueError('Cursor direction mismatch: this cursor continues an oldest-first page')
        if kind is not None and kind!=c.get('entry_kind'):raise ValueError('Cursor --kind mismatch')
        since=c['since'];index=c['index'];start=c['offset']
        if type(index) is not int or type(start) is not int or index<0 or start<0:raise ValueError('Invalid cursor position')
        order='newest' if (recent is not None or c.get('order')=='newest') else 'oldest'
    elif recent is not None:order='newest'
    entry_kind=kind if kind is not None else (c.get('entry_kind') if c is not None else None)
    # Resolve the checkpoint only when one is asked for, so an ordinary history read
    # never starts refusing because a checkpoint chain is corrupt.
    use_checkpoint=bool(since_checkpoint or since_my_checkpoint or (c is not None and c.get('scp') is not None))
    checkpoint=None
    if use_checkpoint:
        if c is not None and c.get('scp') is not None:
            # A cursor stores the boundary it was written with, so continuing a
            # --since-my-checkpoint page needs no repeated selector.
            checkpoint=checkpoint_by_id(data,c['scp'])
        else:
            checkpoint=latest_checkpoint(data,actor=actor if since_my_checkpoint else None)
    checkpoint_id=checkpoint['comment_id'] if checkpoint else None
    if c is not None and c.get('scp')!=checkpoint_id:raise ValueError('Cursor --since-checkpoint mismatch')
    entries=since_checkpoint_entries(data,checkpoint) if use_checkpoint else list(data['entries'])
    if since:entries=[e for e in entries if parse_moment(e['timestamp'])>=parse_moment(since)]
    if entry_kind is not None:entries=[e for e in entries if e['kind']==entry_kind]
    if order=='newest' and c is None:index=len(entries)-1
    # An empty selection is an answer, not a cursor fault: the newest-first first page
    # of nothing (the ordinary poller case) reports the same total_entries 0 the same
    # read without --recent reports. Only a real cursor position is refused.
    if order=='oldest':
        if index>len(entries) or (index==len(entries) and start) or (index<len(entries) and start>len(entries[index]['body'])):raise ValueError('Invalid cursor position')
    elif index>=0 and (index>=len(entries) or start>len(entries[index]['body'])):raise ValueError('Invalid cursor position')
    page=[];remaining=body_budget
    while 0<=index<len(entries) and len(page)<limit and remaining>0:
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
        index+=-1 if order=='newest' else 1;start=0
    # The released token shape is unchanged for a plain oldest-first page; the extra
    # keys appear only when a direction or filter is actually in force.
    extra={}
    if order=='newest':extra['order']='newest'
    if entry_kind is not None:extra['entry_kind']=entry_kind
    if checkpoint_id is not None:extra['scp']=checkpoint_id
    exhausted=index<0 if order=='newest' else index>=len(entries)
    next_value=None if exhausted else token({'v':1,'kind':'history','project':project,'task':task,'snapshot':digest,
                                             'since':since,'index':index,'offset':start,**extra})
    coverage,unverified,note=checkpoint_gaps(data,checkpoint) if use_checkpoint else (None,0,None)
    return {'task':task,'snapshot':digest,'since':since,'since_inclusive':True,
            'order':'newest-first' if order=='newest' else 'oldest-first',
            'kind':entry_kind,'since_checkpoint':checkpoint_id,
            # Whose checkpoint is the baseline, when, and what it cannot show: a reader
            # must be able to tell from this answer alone what it was not shown
            # (kittrial-5bb.8 review, F1/F3).
            'checkpoint_author':clip(checkpoint['comment'].get('author'),96) if checkpoint else None,
            'checkpoint_timestamp':checkpoint['comment'].get('created_at') if checkpoint else None,
            'checkpoint_coverage':coverage,'unverified_entries':unverified,'checkpoint_note':note,
            'snapshot_entries':len(data['entries']),'total_entries':len(entries),
            'omitted_entries':len(data['entries'])-len(entries),
            'entries':page,'next_cursor':next_value,'activity_cursor':activity_cursor(data),
            'coverage':'Snapshot of exported comments and native task events, not every database mutation. Continue this snapshot or restart for new activity.'}

class Parser(argparse.ArgumentParser):
    hints={}
    def error(self,message):
        hint=next((text for flag,text in self.hints.items() if flag in message),None)
        raise ValueError(message+('; hint: '+hint if hint else ''))

def parse_args(action,args):
    parser=Parser(add_help=False);parser.hints=BRIEF_MISTAKEN_FLAGS if action=='brief' else {}
    parser.add_argument('task');parser.add_argument('--json',action='store_true')
    if action=='brief':
        parser.add_argument('--items-offset',type=int,default=0);parser.add_argument('--items-limit',type=int,default=5)
        parser.add_argument('--full',action='store_true')
    elif action=='history':
        parser.add_argument('--limit',type=int);parser.add_argument('--since');parser.add_argument('--cursor');parser.add_argument('--body-budget',type=int,default=4000)
        parser.add_argument('--recent',type=int);parser.add_argument('--last',type=int)
        parser.add_argument('--kind',choices=list(HISTORY_KINDS));parser.add_argument('--since-checkpoint',action='store_true')
        parser.add_argument('--since-my-checkpoint',action='store_true')
    else:raise ValueError('Unknown briefing action')
    parsed=parser.parse_args(args);identity(parsed.task)
    if action=='history':
        # --recent and --last are two spellings of the same newest-first read
        # (kittrial-5bb.8); either one is the page size for that direction.
        if parsed.recent is not None and parsed.last is not None and parsed.recent!=parsed.last:raise ValueError('History: --recent and --last are the same option; give one value')
        parsed.recent=parsed.recent if parsed.recent is not None else parsed.last
        if parsed.recent is not None and parsed.limit is not None:raise ValueError('History: use --limit or --recent/--last, not both')
        if parsed.limit is None:parsed.limit=parsed.recent if parsed.recent is not None else 5
        if parsed.since_my_checkpoint:
            parsed.since_checkpoint=True
    return parsed

def format_changes(section,activity):
    """Text lines of the 'changes since your checkpoint' section (kittrial-5bb.354)."""
    if not section:return []
    if section['checkpoint'] is None:return ['Changes since your checkpoint: '+section['note']]
    lines=[]
    if activity:
        lines.append('Newer activity: newer_activity=%s marker=%s fields=%s by_others=%s own=%s coverage=%s unresolved_directions=%s'%(
            activity.get('newer_activity'),activity.get('newer_activity_marker'),activity.get('newer_activity_fields'),
            activity.get('newer_activity_by_others'),
            activity.get('newer_activity_own'),activity.get('newer_activity_coverage'),activity.get('unresolved_directions')))
    if not section['total'] and not section['fields']:
        return lines+['Changes since your checkpoint: none (coverage %s).'%section['coverage']
                      +(' '+section['fields_note'] if section.get('fields_note') else '')]
    lines.append('Changes since your checkpoint (READ THESE FIRST): %d entries, %d by others, %d own; coverage %s.'%(
        section['total'],section['other_count'],section['own_count'],section['coverage']))
    for entry in section['entries']:
        body=re.sub(r'[\r\n]+',' / ',entry['body']['text'])
        more=' [+%d characters]'%entry['body']['omitted_chars'] if entry['body']['omitted_chars'] else ''
        lines.append('  %s %s [%s, %s%s] by %s: %s%s'%(entry['timestamp'],entry['entry_id'],entry['kind'],entry['state'],
                                                     ', own' if entry['own'] else '',entry['author']['text'],body,more))
    if section['omitted']:lines.append('  ... %d more; read them with: %s'%(section['omitted'],section['history']))
    for field in section['fields']:
        lines.append('  field %s changed%s: now %s%s'%(field['field'],
                     '' if field.get('author') else ' (who made the change is not recorded)',
                     json.dumps(field['current']['text'],ensure_ascii=False),
                     ' [+%d characters]'%field['current']['omitted_chars'] if field['current']['omitted_chars'] else '')
                     +('; previous %s'%json.dumps(field['previous']['text'],ensure_ascii=False) if field.get('previous')
                       else '; previous value not recorded'))
    if section.get('fields_note'):lines.append('  '+section['fields_note'])
    lines.append('  '+section['note'])
    return lines

def bound_context_pointer_display(groups):
    """Keep a composed web brief within the same budget across existing page reads."""
    remaining=CONTEXT_POINTER_BRIEF_MAX
    result=[]
    for group in groups:
        shown={}
        for identity,item in group.items():
            pointers=[]
            for pointer in item.get('pointers') or []:
                if pointer.get('state')=='unresolved':
                    pointers.append(dict(pointer))
                elif remaining:
                    pointers.append(dict(pointer));remaining-=1
                else:
                    pointers.append({k:pointer[k] for k in ('pointer','kind','trust')} | {'state':'unresolved'})
            shown[identity]={'pointers':pointers,'pointers_omitted':sum(p['state']=='unresolved' for p in pointers)}
        result.append(shown)
    return result


def format_context_pointers(item):
    lines=[]
    for pointer in item.get('pointers') or []:
        label=inline_context_text((pointer.get('label') or {}).get('text'))
        # Flags and the section anchor are kit vocabulary and the token's own value.
        extra=(' flags='+(','.join(pointer['flags']) or 'none') if 'flags' in pointer else '')
        section=pointer.get('section')
        if isinstance(section,dict):
            extra+=' section='+str(section.get('anchor'))+' '+str(section.get('state'))
        lines.append('  '+pointer['pointer']+' ['+pointer['state']+'; trust=data]'+extra+(' '+label if label else ''))
    if item.get('pointers_omitted'):
        lines.append('  Pointers unresolved by bounds: '+str(item['pointers_omitted']))
    return lines


def format_brief(result):
    def excerpt(value):return value['text']+(f' [excerpt; {value["omitted_chars"]} characters omitted — use show/history]' if value['omitted_chars'] else '')
    lines=[f'{result["task"]}: {excerpt(result["title"])}',f'Owner: {excerpt(result["owner"])} | Status: {result["status"]}',
           'Review/contribution: '+json.dumps(result['review'],ensure_ascii=False),
           'Intent: '+excerpt(result['intent']),'Acceptance: '+excerpt(result['acceptance']),
           'Current position: '+result['current_position'],'Next: '+result['next_action']]
    lines.extend(item['message'] for item in (result.get('assignment_notices') or {}).get('items',[]))
    guidance=result.get('guidance')
    if guidance and guidance.get('present') and guidance.get('unbound'):
        # Unbound text has no version anyone may follow (kittrial-5bb.105); the warning line below says why.
        lines.append('Guidance: present but withheld until the operator repairs it | attention: True')
    elif guidance and guidance.get('present'):
        # A same-text repair keeps the original setter; show the repair beside it (kittrial-5bb.121).
        repaired=(' (repaired by %s at %s)'%(guidance['repaired_by'],guidance.get('repaired_at') or 'unknown')
                  if guidance.get('repaired_by') else '')
        lines.append('Guidance: version %s set by %s at %s%s | acknowledged: %s | attention: %s'%(
            guidance['version'],guidance.get('set_by') or 'unknown',guidance.get('set_at') or 'unknown',repaired + ' via ' + (guidance.get('set_via') or 'unknown legacy route'),
            guidance.get('acknowledged'),guidance.get('attention')))
    if guidance and guidance.get('warning'):
        lines.append('Guidance warning: '+guidance['warning'])
    if isinstance(guidance,dict) and isinstance(guidance.get('item'),dict):
        # kittrial-5bb.325: the unacknowledged version is a MOVE, not only a flag, and the
        # line names the version and the command. The text is still read through guidance get.
        lines.append('Guidance action: '+guidance['item']['text'])
    cp=result['checkpoint']
    if cp:lines += [f'Checkpoint: {cp["comment_id"]} by {excerpt(cp["author"])} at {cp["timestamp"]}',f'Branch: {cp["branch"] or "unknown"} | Source commit: {cp["source_commit"] or "unknown"}',
                    'Newer/changed activity: '+str(cp['newer_activity']), 'Incorporated activity cursor: '+cp['incorporated_activity_cursor']]
    else:lines += ['No checkpoint yet; unresolved items are UNKNOWN, not zero.']
    if cp and cp.get('taken_in_project'):lines.append('Checkpoint taken in project: '+cp['taken_in_project'])
    newer=result['newer']
    if newer:
        authors=', '.join(a['text'] for a in newer['other_authors']['items']) or 'none'
        if newer['other_authors']['omitted']:authors+=f' (+{newer["other_authors"]["omitted"]} more)'
        if newer['coverage']=='snapshot':coverage=''
        elif newer['coverage']=='timestamp':coverage=' Coverage TIMESTAMP (checkpoint predates per-entry digests; a backdated arrival or an older edit can be missed).'
        elif newer['coverage']=='unknown':coverage=' Coverage UNKNOWN (checkpoint predates per-entry digests).'
        else:coverage=f' Coverage WINDOWED ({newer["unverified_count"]} older entries unverifiable; run: {newer["verify"]}).'
        lines += [f'Newer activity not in checkpoint: {newer["other_count"]} by other actors ({authors}), {newer["own_count"]} own; {newer["fresh_count"]} fresh, {newer["changed_or_late_count"]} changed/late.'+coverage]
        lines += ['  '+e['entry_id']+' ['+e['kind']+(' changed' if e['changed'] else '')+'] '+e['timestamp']+' by '+e['author']['text'] for e in newer['entries']]
        if newer['omitted']:lines += [f'  ... {newer["omitted"]} more; fresh activity: '+newer['history_new']+'; full: '+newer['history']]
        lines += ['Read newer activity: '+newer['history_new'], newer['note']]
    lines += (format_changes(result.get('changes_since_checkpoint'),result.get('activity')) if result['checkpoint']
              else ['Changes since your checkpoint: '+SECTION_NO_CHECKPOINT])
    dirs=result['directions']
    if dirs:
        ids=', '.join(i['entry_id'] for i in dirs['items'])
        if dirs['omitted']:ids+=f' (+{dirs["omitted"]} more)'
        states={}
        for i in dirs['items']:states[i['state']]=states.get(i['state'],0)+1
        lines += [f'Outstanding directions: {dirs["total"]} [{", ".join(k+"="+str(v) for k,v in sorted(states.items()))}] '+ids,
                  '  Resolve or supersede each with note+evidence (digest-checked); acknowledgement alone does not clear a direction.']
    unresolved=result['unresolved'];lines += [f'Unresolved items: {unresolved["total"] if unresolved["total"] is not None else "unknown"}']
    for item in unresolved['items']:
        lines += [f'- {item["id"]} [{item["kind"]}]: {inline_context_text(item["text"])} (source: {inline_context_text(item["source"])})']
        lines += format_context_pointers((unresolved.get('pointer_items') or {}).get(item['id'],{}))
    for item in (result.get('resolved') or {}).get('items',[]):
        lines += ['Resolved '+str(item['id'])+': '+inline_context_text(item['reason'])+' (evidence: '+inline_context_text(item['evidence'])+')']
        lines += format_context_pointers((result['resolved'].get('pointer_items') or {}).get(item['id'],{}))
    if (result.get('resolved') or {}).get('omitted'):
        lines += ['Resolved items omitted: '+str(result['resolved']['omitted'])]
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
    full=result.get('full_context')
    if full:
        lines += ['Full context (bounded; omission_counts are characters the compact read clipped, full_omitted_chars what this cap removed):',
                  'Title: '+full['title'],'Owner: '+full['owner'],'Intent: '+full['intent'],
                  'Acceptance: '+full['acceptance'],'Current position: '+full['current_position'],
                  'Next: '+full['next_action'],
                  'Omission counts: '+json.dumps(full['omission_counts'],ensure_ascii=False),
                  'Bounded full context: omitted '+json.dumps(full['full_omitted_chars'],ensure_ascii=False)
                  +('; complete' if full['complete'] else '; truncated fields: '+', '.join(full['truncated']))
                  +' — '+full['note']]
    return '\n'.join(lines)+'\n'

def help_limits(action):
    """Documented limits for the machine-readable help of one briefing command."""
    if action=='brief':
        return {'items-offset':'>= %d'%BRIEF_ITEM_OFFSET_MIN,
                'items-limit':'%d..%d'%(BRIEF_ITEM_LIMIT_MIN,BRIEF_ITEM_LIMIT_MAX),
                'full_context':'<= %d encoded bytes in total, <= %d per field (--full)'%(FULL_CONTEXT_BUDGET,FULL_FIELD_BUDGET)}
    if action=='history':
        return {'limit':'%d..%d'%(HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX),
                'recent/last':'%d..%d (same range; newest-first page size; the same option under two spellings)'%(HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX),
                'kind':'one of '+', '.join(HISTORY_KINDS),
                'body-budget':'%d..%d encoded bytes'%(HISTORY_BUDGET_MIN,HISTORY_BUDGET_MAX)}
    limits={key:'<= %d characters'%limit for key,limit in CHECKPOINT_FIELD_LIMITS}
    limits.update({'open_items':'<= %d'%CHECKPOINT_ITEMS_MAX,'resolved':'<= %d'%CHECKPOINT_ITEMS_MAX,
            'directions/carried effective entries':'<= %d'%DIRECTIONS_MAX,
            'provenance exact digests':'<= %d'%DIGEST_WINDOW,
            'provenance older digests':'<= %d'%OLDER_MAX,
            'item text/reason':'<= %d characters'%CHECKPOINT_TEXT_LIMIT,
            'item source/evidence':'<= %d characters'%CHECKPOINT_SOURCE_LIMIT,
            'payload':'<= %d KB canonical bytes'%(CHECKPOINT_MAX_BYTES//1000),
            'lane_state.waiting_on':'<= 96 characters (a role or an actor id)',
            'lane_state.reason':'<= 300 characters (needs-investigation only)',
            'error field names':'<= %d names, each <= %d characters'%(FIELD_NAME_COUNT,FIELD_NAME_LIMIT)})
    return limits

def help_notes(action):
    if action=='brief':
        return ['Unresolved items come from the latest valid checkpoint; a missing checkpoint means unknown, not zero.',
                'The compact brief stays compact. `brief TASK --full` adds a BOUNDED full_context block '
                '(title, owner, intent, acceptance, current_position, next_action) plus omission_counts, the '
                'characters the compact read clips from each field, and full_omitted_chars, the characters '
                'this block\'s own cap removed: at most %d encoded bytes in total and %d for any one field, '
                'with control characters escaped as the JSON form escapes them. `show TASK` keeps the '
                'complete text.'%(FULL_CONTEXT_BUDGET,FULL_FIELD_BUDGET),
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
        return ['Pages are snapshot-bound; pass next_cursor back to continue the same snapshot.',
                'Example (ordinary recent review retrieval): history TASK --recent 10 — the ten newest '
                'entries, newest first (--recent/--last accept 1..%d, so the newest 20 is the largest page '
                'of that direction); continue older pages with the returned next_cursor.'%HISTORY_LIMIT_MAX,
                '--recent N and --last N are the same option. Use it instead of --limit: it pages the '
                'same immutable snapshot newest-first, and a fragmented body is still delivered in body '
                'order so it reconstructs exactly with no gaps or duplicates. A selection with nothing in '
                'it answers total_entries 0, exactly like the same read without --recent.',
                'Example (only what arrived since the latest checkpoint): history TASK --since-checkpoint. '
                'The baseline is the NEWEST VALID CHECKPOINT, BY ANYONE (the kit lets another project actor '
                'write a real checkpoint on a task it does not hold), and the answer names that checkpoint\'s '
                'comment id, author and timestamp as well as checkpoint_coverage and unverified_entries. '
                'history TASK --since-my-checkpoint uses this actor\'s own newest valid checkpoint instead.',
                'Coverage is part of the answer: snapshot means retained evidence from the checkpoint chain '
                're-verifies every entry the checkpoint incorporated; windowed means N entries recorded by '
                'the checkpoint have no per-entry evidence and are only returned when they order after it, '
                'so an edit to one of them cannot be seen (unverified_entries counts them); timestamp means the '
                'baseline is a legacy record with no provenance, where the released timestamp rule applies — '
                'every entry at or after the checkpoint is shown — so an entry edited since it, or an arrival '
                'backdated to before it, CANNOT be seen; unknown means a legacy record with no timestamp at all. '
                'checkpoint_note states this in words. Read history '
                'TASK or run checkpoint TASK --verify before trusting a windowed, timestamp or unknown read alone.',
                'Example (comments only): history TASK --kind comment --recent 20. --kind accepts comment '
                'or event; --since stays inclusive and timezone-aware and composes with both filters.',
                'The JSON result names the effective order, kind and checkpoint boundary and reports '
                'snapshot_entries, total_entries and omitted_entries, so a bounded read states its omissions.',
                'A cursor binds the direction, kind and checkpoint boundary as well as the snapshot and --since: '
                'continue with the returned token alone, or repeat the same values. A released oldest-first '
                'cursor keeps its meaning and unknown cursor keys are still refused.']
    return ['A JSON file attachment is required; payload.task must equal TASK.',
            'checkpoint TASK --latest returns only the newest valid checkpoint record: its comment_id, '
            'author and timestamp, source commit and branch, activity cursor, intent, acceptance, summary, '
            'next action, open items and resolutions, and coverage (provenance or legacy). Its recorded '
            'provenance is a COUNT (covered, digests, older, verifiable, complete, chain) so a long task '
            'stays bounded; add --provenance to return the retained digest map itself. It reads nothing '
            'else of the history; an invalid or absent checkpoint is reported rather than guessed.',
            'The record has exactly these fields: schema_version (1), task, previous (the current checkpoint\'s '
            'comment_id, or null for the first), activity_cursor (from brief or history), source_commit and branch '
            '(text; empty when there is none), intent, acceptance, summary, next_action, open_items (each: id, kind, '
            'text, source; kind is one of blocker, correction, decision, dependency, question) and resolved (each: '
            'id, reason, evidence).',
            'An optional sibling key lane_state (kittrial-5bb.263) writes one Kind: lane-state-v1 record beside '
            'this checkpoint in the same act. Its fields are schema_version (1), task, state (one of working, '
            'waiting-on-coordinator, waiting-on-review, waiting-on-user, idle, needs-investigation), waiting_on (a role coordinator/'
            'reviewer/user or an actor id; required with a waiting state and refused without one) and expect_by '
            '(optional ISO-8601 with an offset; a past value is accepted). Do not send checkpoint: the writer '
            'sets it to the comment id this checkpoint just published, which is what makes the record name the '
            'task\'s newest checkpoint at write time. It is NOT a checkpoint field: the checkpoint object '
            'above is unchanged and its field set stays closed, and lane_state is popped out before the checkpoint '
            'is validated. Nothing is inferred: no person, and no actor turned into a role. State '
            'needs-investigation (kittrial-5bb.398) raises an out-of-band fault flag on the lane for the coordinator: '
            'it requires reason (text, <= 300 characters, one printable line, shown escaped and clipped) and allows '
            'about_task (a task id); it takes no waiting_on or expect_by, and every other state refuses reason and '
            'about_task. Only the coordinator can clear it; a later state record never does.',
            'A refusal names every problem with the record at once: the sentence itself for one problem, and for '
            'more a numbered list of the same sentences ("Invalid checkpoint (N problems): [1] ... [2] ..."), at '
            'most %d of them. A stale previous and a changed activity cursor are separate refusals.'
            % CHECKPOINT_PROBLEMS_MAX,
            'The carry refusal names the ids it compared (kittrial-5bb.287): the ids open in the previous '
            'checkpoint that this record neither carries nor resolves, the ids in `resolved` that are also in '
            '`open_items`, and the ids in `resolved` that are not open items at all. An item is resolved by '
            'OMITTING its id from `open_items` and adding it to `resolved`; naming it in `resolved` while still '
            'carrying it does not resolve it. A direction is NEVER resolved by `resolved`: a direction is another '
            'actor\'s ordinary comment, dispositioned with `directions` (installations with the provenance write '
            'switch off cannot record one, so none is outstanding there). No client update is required: the '
            'refusal names the ids in the request that do not match, and those ids are what fix it.',
            'Minimal example payload that disposes of ONE outstanding direction while carrying the open items '
            '(templates/%s): "open_items" repeats every unresolved id and text byte for byte, "resolved" is empty, '
            'and "directions" holds the direction\'s id, its full digest from `checkpoint TASK --directions`, and '
            'the disposition. Write it with `checkpoint TASK --file %s`.'
            % (DIRECTION_EXAMPLE_FILE, DIRECTION_EXAMPLE_FILE),
            '--json is accepted in any position; the saved checkpoint is always returned as JSON on stdout.',
            '--provenance, --verify and --directions are read-only alternatives to --file; they change no checkpoint or direction disposition.',
            'checkpoint_provenance_writes is off by default: writers emit the older-kit shape until an operator enables new writes after a reader-first rollout.',
            'The server derives provenance deltas and carried acknowledgements; newest evidence follows linked checkpoint order.',
            'Only the current assignee may record direction dispositions; --directions returns full current digests in pages of 1..100.',
            'Every unresolved item must be carried forward unchanged or explicitly resolved with reason and evidence.']

def exported_rows(run):
    """Bound rows and classify native events before briefing lifecycle facts."""
    from lifecycle import read_event_rows
    return read_event_rows(run)

def checkpoint_writes_enabled(root):
    """Reader-first rollout: only an installation operator enables new writes."""
    from admin import config
    marker=root/'deployment.private.json'
    # One rule everywhere (kittrial-5bb.173 item 2): a symlinked deployment.private.json is
    # FOLLOWED, as every other route and host command already follows it (os.open on the
    # path). A checkpoint no longer refuses a link that every other reader accepts.
    if not marker.exists():return False
    settings=config(root)
    if not isinstance(settings,dict):raise ValueError('Deployment configuration must be a JSON object')
    value=settings.get('checkpoint_provenance_writes',False)
    if type(value) is not bool:
        import sys
        print('Warning: checkpoint_provenance_writes must be a boolean; treating the malformed value as OFF.',file=sys.stderr)
        return False
    return value

def direction_page(rows,project,task,offset=0,limit=50,provenance_writes=True):
    if offset<0 or not 1<=limit<=100:raise ValueError('Direction page requires --offset >= 0 and --limit 1..100')
    issue=task_row(rows,task);state=checkpoint_state(issue)
    if state['invalid']:raise ValueError('Malformed checkpoint entries require correction before reading directions')
    data=snapshot(rows,project,task);context=direction_context(issue,state)
    counted,directions_off_reason=directions_counted(provenance_writes,issue.get('status'))
    entries=unresolved_directions(direction_entries(data,context),chain_dispositions(state['history']),context[0],counted)
    if offset>len(entries):raise ValueError('Direction offset exceeds total')
    page={'task':task,'activity_cursor':activity_cursor(data),'total':len(entries),
            'items':[{'id':e['entry_id'],'digest':entry_digests(data)[e['entry_id']],
                      'author':clip(e['author'],96),'timestamp':e['timestamp']} for e in entries[offset:offset+limit]],
            'next_offset':offset+limit if offset+limit<len(entries) else None,
            'coverage':'Current full digests for outstanding directions, including entries outside stored provenance windows. '
                       'Pages are fresh reads; compare activity_cursor across pages and restart if it changes.'}
    if not counted:page['check']=directions_check_state(directions_off_reason);page['reason']=directions_off_reason
    return page

def latest_checkpoint_read(rows,project,task,include_provenance=False):
    """The newest valid checkpoint record alone (kittrial-5bb.8): `checkpoint TASK --latest`.

    A bounded alternative to reading a long task's whole comment history when a caller
    needs only the current position, its unresolved items and what evidence it carries.
    The recorded provenance is returned as COUNTS by default: on a long task the digest
    map is tens of kilobytes, and a caller that wants it asks with `--provenance`
    (review, F5).
    """
    issue=task_row(rows,task)
    if issue.get('issue_type')=='event':raise ValueError('Use history/show for an event; checkpoint requires a task or job')
    state=checkpoint_state(issue)
    if state['current'] is None:
        return {'task':task,'checkpoint':None,'invalid_checkpoint_comments':state['invalid'][:5],
                'note':'No valid structured checkpoint on this task; unresolved items are unknown, not zero.'}
    p,c=state['current'];prov=normalize_provenance(p)
    counts=None if prov is None else {'covered':prov['covered'],'digests':len(prov['digests']),'older':len(prov['older']),
                                      'verifiable':prov['verifiable'],'complete':prov['complete'],'chain':prov['chain']}
    return {'task':task,
            'checkpoint':{'comment_id':str(c['id']),'author':c.get('author'),'timestamp':c.get('created_at'),
                          'source_commit':p['source_commit'],'branch':p['branch'],'activity_cursor':p['activity_cursor'],
                          'intent':p['intent'],'acceptance':p['acceptance'],'summary':p['summary'],
                          'next_action':p['next_action'],'open_items':p['open_items'],'resolved':p['resolved'],
                          'open_items_total':len(p['open_items']),'resolved_total':len(p['resolved'])},
            'coverage':'provenance' if prov is not None else 'legacy',
            'provenance':counts,
            'provenance_digests':(prov if include_provenance else None),
            'provenance_note':'Counts only; add --provenance to return the retained digest map itself.' if counts is not None else None,
            'invalid_checkpoint_comments':state['invalid'][:5],
            'evidence':{'history':'history '+task,'checkpoint_entry':task+'-c'+str(c['id'])}}

def execute(root,path,project,actor,action,args,attachments,run,operators=None,verifiers=None,
            independent_approval=False,owners=None,identity_names=None):
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
            return json.dumps(direction_page(exported_rows(run),project,page.task,page.offset,page.limit,
                                             checkpoint_writes_enabled(root)),ensure_ascii=False,indent=2)+'\n'
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
        if '--latest' in args[1:]:
            # A bounded latest-checkpoint read (kittrial-5bb.8): one record and a
            # provenance COUNT, with the digest map only on --provenance.
            rest=[token for token in args[1:] if token!='--latest']
            if rest not in ([],['--provenance']):raise ValueError('Use checkpoint TASK --file checkpoint.json [--json]')
            return json.dumps(latest_checkpoint_read(exported_rows(run),project,args[0],include_provenance=bool(rest)),
                              ensure_ascii=False,indent=2)+'\n'
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
        saved=_read_history_snapshot(file);data=saved['data']
        if content_hash(data)!=digest:raise ValueError('History cache integrity mismatch')
    else:
        rows=exported_rows(run)
        if action=='brief':
            def field_history(task_id):
                # One bounded read-only `bd history` per stale brief, through the read
                # cache under the tracker's generation (kittrial-5bb.354), stopped at
                # HISTORY_MAX_BYTES while it is read (kittrial-5bb.362); one smaller
                # read follows a refused one, and a second refusal raises TooLarge.
                import field_history as field_versions
                from read_cache import cached_task_history
                def bounded(limit):
                    argv=field_versions.command(task_id,limit)
                    reader=getattr(run,'bounded',None)
                    if reader is not None:
                        return reader(argv,field_versions.HISTORY_MAX_BYTES)
                    text=run(argv)          # a runner without a bounded read (a test double)
                    if len(text.encode('utf-8'))>field_versions.HISTORY_MAX_BYTES:
                        raise field_versions.TooLarge('history answer over the cap')
                    return text
                def fetch():
                    try:
                        return bounded(field_versions.HISTORY_LIMIT)
                    except field_versions.TooLarge:
                        return bounded(field_versions.RETRY_LIMIT)
                return cached_task_history(root,path,task_id,fetch,name=project)
            result=brief(rows,project,a.task,a.items_offset,a.items_limit,operators=operators,journal=path,
                         verifiers=verifiers,actor=actor,full=a.full,
                         independent_approval=independent_approval,owners=owners,
                         provenance_writes=checkpoint_writes_enabled(root),
                         identity_names=identity_names,field_history=field_history)
            return json.dumps(result,ensure_ascii=False,indent=2)+'\n' if a.json else format_brief(result)
        data=snapshot(rows,project,a.task);digest=content_hash(data);cache.mkdir(exist_ok=True)
        file=cache/(digest+'.json')
        if file.is_symlink():raise ValueError('Invalid snapshot path')
        if not file.exists():
            from coordination import atomic
            atomic(file,{'captured_at':datetime.now(timezone.utc).isoformat(),'data':data})
        saved=_read_history_snapshot(file)
    result=history_page(data,project,a.task,a.limit,a.since,a.cursor,a.body_budget,a.recent,a.kind,a.since_checkpoint,
                        actor=actor,since_my_checkpoint=a.since_my_checkpoint)
    result['captured_at']=saved['captured_at']
    # JSON for both modes keeps fragments and opaque continuation cursors exact.
    return json.dumps(result,ensure_ascii=False,indent=2)+'\n'
