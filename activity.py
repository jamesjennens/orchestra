#!/usr/bin/env python3
"""Offline comment activity feed over one saved issues.jsonl export.

`refresh` writes views/issues.jsonl. Filtering that single snapshot locally
answers "which comments are new" for every task without one database call per
issue. Ordering uses each comment's own created_at; a new comment does not
necessarily advance its issue's updated_at, so updated_at is not a watermark.
"""
import argparse
import json
import sys
from datetime import datetime,timezone
from pathlib import Path

FRESHNESS='Export file timestamp is the local snapshot write time, not an authoritative server mutation time; refresh to reduce lag.'

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

def build_entries(rows,since=None):
    """Comment entries at or after `since` (inclusive), ordered by comment time, issue ID then comment ID."""
    found=[]
    for row in rows:
        issue_id=str(row.get('id'))
        for comment in row.get('comments') or []:
            if not isinstance(comment,dict) or comment.get('id') is None:raise ValueError(f'{issue_id}: comment entry without an id')
            comment_id=str(comment['id']);label=f'{issue_id}-c{comment_id}'
            moment=parse_moment(comment.get('created_at'),f'{label} created_at')
            if since is not None and moment<since:continue
            found.append((moment,issue_id,comment_order(comment_id),{'entry_id':label,'issue_id':issue_id,'issue_title':row.get('title') or '','author':comment.get('author') or 'unknown','timestamp':utc_text(moment),'body':comment.get('text') or ''}))
    found.sort(key=lambda item:item[:3])
    return [item[3] for item in found]

def feed(rows,since=None,source=None):
    """Feed payload plus export freshness metadata; the snapshot time is not a mutation time."""
    entries=build_entries(rows,since)
    modified=None
    if source is not None:
        try:modified=utc_text(datetime.fromtimestamp(Path(source).stat().st_mtime,timezone.utc))
        except OSError:modified=None
    return {'export':{'path':str(source) if source is not None else None,'modified':modified,'note':FRESHNESS},
            'since':utc_text(since) if since is not None else None,'since_inclusive':True,
            'count':len(entries),'entries':entries}

def format_text(result):
    origin=result['export']['path'] or '(in-memory records)'
    lines=[f'Export: {origin} (file modified {result["export"]["modified"] or "unknown"})',result['export']['note'],
           f'Since: {result["since"] or "beginning of export"} (inclusive)',f'Entries: {result["count"]}']
    for entry in result['entries']:
        lines+=['',f'[{entry["timestamp"]}] {entry["entry_id"]} by {entry["author"]} - {entry["issue_id"]}: {entry["issue_title"]}']
        lines+=[f'    {line}' for line in entry['body'].splitlines() or ['']]
    return '\n'.join(lines)+'\n'

def main(argv=None):
    parser=argparse.ArgumentParser(description='List comment activity from one saved issues.jsonl export.')
    parser.add_argument('--export',required=True,help='saved views/issues.jsonl snapshot from the kit refresh')
    parser.add_argument('--since',help='inclusive timezone-aware ISO-8601 lower bound')
    parser.add_argument('--json',action='store_true',help='emit a JSON feed instead of text')
    args=parser.parse_args(argv)
    try:
        since=parse_moment(args.since,'--since') if args.since is not None else None
        result=feed(load_export(args.export),since,args.export)
    except (ValueError,OSError) as e:
        sys.stderr.write(f'activity: {e}\n');return 2
    if args.json:sys.stdout.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    else:sys.stdout.write(format_text(result))
    return 0

if __name__=='__main__':sys.exit(main())
