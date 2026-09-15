"""Generate bounded current views, full task pages and linked daily journals."""
import json
import os
import re
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from lifecycle import project_facts, DIMENSIONS

def write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    tmp.write_text(text,encoding='utf-8');os.replace(tmp,path)

def render(rows,dest):
    dest=Path(dest)
    stamp=datetime.now(timezone.utc).isoformat(timespec='seconds')
    banner=f'Exported {stamp}. Query Beads for current state. Do not hand-edit generated files.\n\n'
    valid=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}')
    ids={r['id'] for r in rows}
    if len(ids)!=len(rows) or any(not valid.fullmatch(x) for x in ids):raise ValueError('Invalid/duplicate issue ID')
    def parent(r):return next((d['depends_on_id'] for d in r.get('dependencies',[]) if d.get('type')=='parent-child'),None)
    entries={};daily=defaultdict(list);backlinks=defaultdict(list)
    for r in rows:
        for c in r.get('comments') or []:
            cid=str(c['id'])
            if not valid.fullmatch(cid):raise ValueError('Invalid comment ID')
            eid=f'{r["id"]}-c{cid}'
            date=str(c.get('created_at',''))[:10]
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):date='undated'
            entries[eid]=(r,c,date)
    for eid,(r,c,date) in entries.items():
        for relation,target in re.findall(r'(?im)^(Supersedes|Contradicts|Supports|Comments-on):\s*([A-Za-z0-9_.-]+)\s*$',c.get('text','')):
            if target in entries:backlinks[target].append((relation.lower(),eid,date))
    current=['# Current project work\n\n',banner,'[Daily journal](journal/INDEX.md) | [All records](INDEX.md)\n\n']
    facts=[r for r in project_facts(rows) if r['has_lifecycle']]
    if facts:
        def cell(value):return str(value or 'unknown').replace('|','\\|').replace('\n',' ')
        current.extend(['## Lifecycle evidence\n\n','Facts apply only to the exact scope shown. Unknown includes missing, inconsistent or superseded evidence.\n\n',
                        '| Task | '+' | '.join(DIMENSIONS)+' | Source commit | Integration commit | Release | Environment |\n',
                        '| --- | '+' | '.join(['---']*10)+' |\n'])
        for fact in sorted(facts,key=lambda r:r['id']):
            scope=fact['scope'] or {}
            values=[]
            for dim in DIMENSIONS:
                entry=fact['facts'][dim];value=entry['value']
                values.append(f'[{value}](jobs/{entry["event_id"]}.md)' if entry['event_id'] else value)
            current.append('| ['+fact['id']+'](jobs/'+fact['id']+'.md) | '+' | '.join(values+[cell(scope.get(k)) for k in ('source_commit','integration_commit','release_id','environment')])+' |\n')
        current.append('\n')
    for r in sorted(rows,key=lambda x:x['id']):
        rid=r['id'];par=parent(r)
        body=f'# {rid}: {r["title"]}\n\n'+banner+f'**Status:** {r["status"]} | **Assignee:** {r.get("assignee") or "unassigned"}\n\n'
        if par:body+=f'Parent: [{par}]({par}.md)\n\n'
        for field in ('description','design','acceptance_criteria','notes'):
            if r.get(field):body+=f'## {field.replace("_"," ").title()}\n\n{r[field]}\n\n'
        children=[x for x in rows if parent(x)==rid]
        if children:body+='## Tasks\n\n'+''.join(f'- [{x["id"]}: {x["title"]}]({x["id"]}.md) — {x["status"]}\n' for x in children)+'\n'
        for c in r.get('comments') or []:
            eid=f'{rid}-c{c["id"]}';date=entries[eid][2]
            annotation='\n'.join(f'- {relation}: [{other}](../journal/{day}.md#{other})' for relation,other,day in backlinks[eid]) or 'No linked later annotations.'
            body+=f'## Entry {eid}\n\n[Daily entry](../journal/{date}.md#{eid})\n\n{c.get("text", "")}\n\nLater annotations:\n\n{annotation}\n\n'
            daily[date].append(f'<a id="{eid}"></a>\n\n## {eid}\n\n{c.get("created_at", "")} — {c.get("author", "unknown")} — [{rid}](../jobs/{rid}.md)\n\n{c.get("text", "")}\n\nLater annotations:\n\n{annotation}\n')
        write(dest/'jobs'/f'{rid}.md',body)
        if r.get('issue_type')=='epic' and r['status']!='closed':
            current.append(f'## [{rid}: {r["title"]}](jobs/{rid}.md)\n\n')
            if r.get('notes'): current.append(r['notes'][:800]+'\n\n')
            active=[x for x in children if x['status']!='closed']
            current.extend(f'- [{x["id"]}: {x["title"]}](jobs/{x["id"]}.md) — {x["status"]}\n' for x in active[:8])
            if len(active)>8:current.append(f'- {len(active)-8} more tasks on the job page.\n')
            current.append('\n')
    standalone=[r for r in rows if not parent(r) and r.get('issue_type')!='epic' and r['status']!='closed']
    if standalone:current.append('## Standalone tasks and decisions\n\n'+''.join(f'- [{r["id"]}: {r["title"]}](jobs/{r["id"]}.md)\n' for r in standalone[:20]))
    for day,parts in daily.items():write(dest/'journal'/f'{day}.md',f'# {day} (UTC)\n\n'+banner+'\n---\n\n'.join(parts))
    write(dest/'journal/INDEX.md','# Daily journal\n\n'+banner+''.join(f'- [{d}]({d}.md)\n' for d in sorted(daily)))
    write(dest/'INDEX.md','# All records\n\n'+banner+''.join(f'- [{r["id"]}: {r["title"]}](jobs/{r["id"]}.md) — {r["status"]}\n' for r in sorted(rows,key=lambda x:x['id'])))
    write(dest/'issues.jsonl',''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    write(dest/'CURRENT.md',''.join(current))
    write(dest/'COORDINATION.md',''.join(current))
    return {'issues':len(rows),'comments':len(entries),'generated_at':stamp}
