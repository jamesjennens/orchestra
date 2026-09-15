#!/usr/bin/env python3
"""Offline activity feed over one saved issues.jsonl export.

`refresh` writes views/issues.jsonl. Filtering that single snapshot locally
answers "which comments are new" for every task without one database call per
issue. Ordering uses each entry's own created_at; a new comment does not
necessarily advance its issue's updated_at, so updated_at is not a watermark.

Native lifecycle records are exported as issues with issue_type 'event'. They
are carried beside comments with their own stable native id, created_at,
created_by, parent-child dependency and the description text verbatim: the feed
does not interpret lifecycle evidence, so a structured lifecycle-v1 description
stays text. Coverage is the exported comments and state events of one snapshot,
not a complete audit log.

The optional durable cursor (--scope with --cursor/--cursor-out) maps stable
entry identities to content hashes. A resumed read delivers identities that are
new or whose content changed (revisions) and suppresses unchanged ones, so late
entries and timestamp ties are not lost, while identities absent from a partial
export keep their place. Cursor size grows linearly with the number of seen
identities.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime,timezone
from pathlib import Path

FRESHNESS='Export file timestamp is the local snapshot write time, not an authoritative server mutation time; refresh to reduce lag.'
COVERAGE_NOTE='Coverage is the exported comments and native state-change events of this snapshot only, not a complete audit of every mutation.'
CURSOR_NOTE='Cursor size grows linearly with the number of seen entry identities; identities absent from a partial export are retained.'
CURSOR_VERSION=1
COMMENT_KIND='comment'
EVENT_KIND='event'
COVERAGE_SOURCES=['comments','native-state-events']
IDENTITY_FIELDS=('entry_id','kind','issue_id','timestamp','author','body','event_title')

def parse_moment(value,label='timestamp'):
    """Timezone-aware ISO-8601 text -> aware UTC datetime; naive or malformed text fails clearly."""
    if not isinstance(value,str) or not value.strip():raise ValueError(f'{label} must be a non-empty ISO-8601 timestamp string')
    text=value.strip()
    if text.endswith(('Z','z')):text=text[:-1]+'+00:00'
    try:moment=datetime.fromisoformat(text)
    except ValueError:raise ValueError(f'{label} is not valid ISO-8601: {value!r}') from None
    if moment.tzinfo is None or moment.utcoffset() is None:raise ValueError(f'{label} must include a timezone offset or Z: {value!r}')
    return moment.astimezone(timezone.utc)

def utc_text(moment):return moment.astimezone(timezone.utc).isoformat().replace('+00:00','Z')

def comment_order(comment_id):
    """Numeric comment IDs sort numerically; other IDs sort as text."""
    text=str(comment_id)
    return (0,int(text),'') if text.isdigit() else (1,0,text)

def load_export(source):
    """Read one JSONL issue record per non-empty line from a saved export."""
    path=Path(source)
    try:text=path.read_text(encoding='utf-8-sig')
    except OSError as e:raise ValueError(f'cannot read export {path}: {e}') from None
    rows=[]
    for number,line in enumerate(text.splitlines(),1):
        if not line.strip():continue
        try:row=json.loads(line)
        except json.JSONDecodeError as e:raise ValueError(f'{path}: line {number} is not valid JSON ({e.msg})') from None
        if not isinstance(row,dict):raise ValueError(f'{path}: line {number} is not a JSON issue object')
        if not isinstance(row.get('id'),str) or not row['id']:raise ValueError(f'{path}: line {number} has no string issue id')
        if row.get('comments') is not None and not isinstance(row['comments'],list):raise ValueError(f'{path}: line {number} has a non-list comments field')
        rows.append(row)
    return rows

def is_event(row):return str(row.get('issue_type') or '')==EVENT_KIND

def parent_of(row):
    """Parent task ID from the native parent-child dependency, if any."""
    for dependency in row.get('dependencies') or []:
        if not isinstance(dependency,dict) or dependency.get('type')!='parent-child':continue
        parent=dependency.get('depends_on_id')
        if isinstance(parent,str) and parent:return parent
    return None

def event_entry(row,native_id,titles):
    """One native state-change event: stable native ID, verbatim description, no evidence interpretation."""
    moment=parse_moment(row.get('created_at'),f'{native_id} created_at')
    description=row.get('description')
    if description is not None and not isinstance(description,str):raise ValueError(f'{native_id}: event description is not text')
    parent=parent_of(row)
    return {'entry_id':native_id,'kind':EVENT_KIND,'issue_id':parent or native_id,'issue_title':titles.get(parent) or '' if parent else '',
            'author':row.get('created_by') or 'unknown','timestamp':utc_text(moment),'body':description or '','event_title':row.get('title') or ''}

def entry_identity(entry):return {field:entry.get(field) for field in IDENTITY_FIELDS}

def content_hash(entry):
    """Stable content digest of one entry: same identity plus same content means already delivered."""
    payload=json.dumps(entry_identity(entry),ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

def build_entries(rows,since=None,seen=None):
    """Entries at or after `since` (inclusive), ordered by entry time, issue ID then entry ID.

    Comments and native state events are ordered together. With `seen` (stable entry identity ->
    content hash) unchanged identities are suppressed and changed content is returned as a revision.
    """
    titles={}
    for row in rows:
        if isinstance(row.get('id'),str):titles.setdefault(str(row['id']),row.get('title') or '')
    found=[]
    for row in rows:
        issue_id=str(row.get('id'))
        if is_event(row):
            entry=event_entry(row,issue_id,titles)
            found.append((parse_moment(entry['timestamp'],f'{issue_id} created_at'),entry['issue_id'],comment_order(issue_id),entry))
            # Events can themselves receive later corrections or annotations.
            # Keep those comments under their actual event issue identity.
        for comment in row.get('comments') or []:
            if not isinstance(comment,dict) or comment.get('id') is None:raise ValueError(f'{issue_id}: comment entry without an id')
            comment_id=str(comment['id']);label=f'{issue_id}-c{comment_id}'
            moment=parse_moment(comment.get('created_at'),f'{label} created_at')
            found.append((moment,issue_id,comment_order(comment_id),{'entry_id':label,'kind':COMMENT_KIND,'issue_id':issue_id,'issue_title':row.get('title') or '','author':comment.get('author') or 'unknown','timestamp':utc_text(moment),'body':comment.get('text') or ''}))
    found.sort(key=lambda item:item[:3])
    entries=[item[3] for item in found if since is None or item[0]>=since]
    if seen is None:return entries
    delivered=[]
    for entry in entries:
        previous=seen.get(entry['entry_id'])
        if previous==content_hash(entry):continue
        entry['revision']=previous is not None
        delivered.append(entry)
    return delivered

def coverage_of(rows):
    """Explicit coverage: which sources this snapshot feed reads, never a complete audit claim."""
    comments=sum(len(row.get('comments') or []) for row in rows)
    events=sum(1 for row in rows if is_event(row))
    return {'sources':list(COVERAGE_SOURCES),'complete':False,'note':COVERAGE_NOTE,'comment_entries':comments,'event_entries':events}

def feed(rows,since=None,source=None,seen=None,scope=None):
    """Feed payload plus coverage and export freshness metadata; a snapshot time is not a mutation time."""
    candidates=build_entries(rows,since)
    entries=candidates if seen is None else build_entries(rows,since,seen)
    modified=None
    if source is not None:
        try:modified=utc_text(datetime.fromtimestamp(Path(source).stat().st_mtime,timezone.utc))
        except OSError:modified=None
    result={'export':{'path':str(source) if source is not None else None,'modified':modified,'note':FRESHNESS},
            'coverage':coverage_of(rows),
            'since':utc_text(since) if since is not None else None,'since_inclusive':True,
            'count':len(entries),'entries':entries}
    if seen is not None:
        revisions=sum(1 for entry in entries if entry.get('revision'))
        fresh=len(entries)-revisions
        result['cursor']={'version':CURSOR_VERSION,'scope':scope,'seen_before':len(seen),'seen':len(seen)+fresh,'new':fresh,'revisions':revisions,
                          'suppressed':len(candidates)-len(entries),'note':CURSOR_NOTE}
    return result

def load_cursor(source,scope):
    """Read a durable cursor and require the same explicit project/source scope before resuming."""
    path=Path(source)
    try:text=path.read_text(encoding='utf-8-sig')
    except OSError as e:raise ValueError(f'cannot read cursor {path}: {e}') from None
    try:payload=json.loads(text)
    except json.JSONDecodeError as e:raise ValueError(f'cursor {path} is not valid JSON ({e.msg})') from None
    if not isinstance(payload,dict):raise ValueError(f'cursor {path} is not a JSON object')
    if payload.get('version')!=CURSOR_VERSION:raise ValueError(f'cursor {path} has unsupported version {payload.get("version")!r}')
    stored=payload.get('scope')
    if not isinstance(stored,str) or not stored:raise ValueError(f'cursor {path} has no scope')
    if stored!=scope:raise ValueError(f'cursor scope {stored!r} does not match --scope {scope!r}; refusing to mix projects or exports')
    seen=payload.get('seen')
    if not isinstance(seen,dict):raise ValueError(f'cursor {path} has no seen identity map')
    for identity,digest in seen.items():
        if not isinstance(identity,str) or not identity or not isinstance(digest,str) or not digest:raise ValueError(f'cursor {path} has a malformed entry identity {identity!r}')
    return dict(seen)

def next_cursor(seen,delivered):
    """Union of prior identities and the entries just delivered; identities absent from this snapshot stay."""
    updated=dict(seen)
    for entry in delivered:updated[entry['entry_id']]=content_hash(entry)
    return updated

def write_cursor(target,scope,seen):
    """Write the cursor atomically: same-directory temporary file, then os.replace."""
    path=Path(target)
    try:path.parent.mkdir(parents=True,exist_ok=True)
    except OSError as e:raise ValueError(f'cannot create cursor directory {path.parent}: {e}') from None
    payload={'version':CURSOR_VERSION,'scope':scope,'note':CURSOR_NOTE,'seen':{identity:seen[identity] for identity in sorted(seen)}}
    try:handle=tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=str(path.parent),prefix=path.name+'.',suffix='.tmp',delete=False)
    except OSError as e:raise ValueError(f'cannot create a temporary cursor beside {path}: {e}') from None
    try:
        with handle:
            json.dump(payload,handle,ensure_ascii=False,indent=2)
            handle.write('\n')
        os.replace(handle.name,str(path))
    except OSError as e:
        try:os.unlink(handle.name)
        except OSError:pass
        raise ValueError(f'cannot write cursor {path}: {e}') from None

def format_text(result):
    origin=result['export']['path'] or '(in-memory records)'
    lines=[f'Export: {origin} (file modified {result["export"]["modified"] or "unknown"})',result['export']['note'],
           f'Coverage: {result["coverage"]["note"]}',
           f'Since: {result["since"] or "beginning of export"} (inclusive)',f'Entries: {result["count"]}']
    if result.get('cursor') is not None:
        cursor=result['cursor']
        lines+=[f'Cursor: scope {cursor["scope"]}, seen {cursor["seen"]} (previous {cursor["seen_before"]}), new {cursor["new"]}, revisions {cursor["revisions"]}, suppressed {cursor["suppressed"]}',cursor['note']]
    for entry in result['entries']:
        marker=' [native event]' if entry['kind']==EVENT_KIND else ''
        note=' (revision)' if entry.get('revision') else ''
        lines+=['',f'[{entry["timestamp"]}] {entry["entry_id"]}{marker}{note} by {entry["author"]} - {entry["issue_id"]}: {entry["issue_title"] or entry.get("event_title") or ""}']
        lines+=[f'    {line}' for line in entry['body'].splitlines() or ['']]
    return '\n'.join(lines)+'\n'

def main(argv=None):
    parser=argparse.ArgumentParser(description='List comment and native state-event activity from one saved issues.jsonl export.')
    parser.add_argument('--export',required=True,help='saved views/issues.jsonl snapshot from the kit refresh')
    parser.add_argument('--since',help='inclusive timezone-aware ISO-8601 lower bound; not combined with --cursor')
    parser.add_argument('--json',action='store_true',help='emit a JSON feed instead of text')
    parser.add_argument('--scope',help='explicit project/source identity the cursor belongs to (required with --cursor/--cursor-out)')
    parser.add_argument('--cursor',help='durable cursor JSON to resume from; delivers new, late and edited entries only')
    parser.add_argument('--cursor-out',help='write the updated cursor atomically after a successful feed')
    args=parser.parse_args(argv)
    try:
        if args.scope is not None and not (args.cursor or args.cursor_out):raise ValueError('--scope only applies with --cursor or --cursor-out')
        if (args.cursor or args.cursor_out) and not args.scope:raise ValueError('--scope is required with --cursor/--cursor-out so a cursor cannot silently cross projects or exports')
        if args.since is not None and (args.cursor is not None or args.cursor_out is not None):raise ValueError('--since and --cursor/--cursor-out are not combined: the cursor is the novelty test, and a timestamp bound would drop late entries')
        since=parse_moment(args.since,'--since') if args.since is not None else None
        rows=load_export(args.export)
        seen=None
        if args.cursor is not None:seen=load_cursor(args.cursor,args.scope)
        elif args.cursor_out is not None:seen={}
        result=feed(rows,since,args.export,seen,args.scope)
    except (ValueError,OSError) as e:
        sys.stderr.write(f'activity: {e}\n');return 2
    if args.json:sys.stdout.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    else:sys.stdout.write(format_text(result))
    if args.cursor_out is not None:
        try:
            sys.stdout.flush()
            write_cursor(args.cursor_out,args.scope,next_cursor(seen,result['entries']))
        except (ValueError,OSError) as e:
            sys.stderr.write(f'activity: {e}\n');return 2
    return 0

if __name__=='__main__':sys.exit(main())
