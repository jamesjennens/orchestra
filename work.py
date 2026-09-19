"""Review/contribution transport and bounded current work queues."""
import argparse
import json
from lifecycle import project_facts

class Parser(argparse.ArgumentParser):
    def error(self,message):raise ValueError(message)

def workflow(issue):
    from review_workflow import project
    result=project(issue)
    if result['review_state']=='none' and 'review-ready' in (issue.get('labels') or []):
        result=dict(result,review_state='legacy-review-ready')
    return result

def checkpoint_newer_counts(row):
    """Newer-activity counts for the work queue: own entries vs other actors' entries
    that the task's current checkpoint did not incorporate. None without a checkpoint
    or with a malformed/conflicting checkpoint history (brief remains the authority).
    """
    try:
        from briefing import checkpoints,newer_activity_summary,snapshot,untoken
        from requirements import content_hash
        (p,c),_invalid=checkpoints(row)
        if p is None:return None
        cursor=untoken(p['activity_cursor'])
        if not isinstance(cursor,dict) or cursor.get('kind')!='activity' or cursor.get('task')!=row['id']:return None
        excluded=snapshot([row],cursor['project'],row['id'],str(c['id']))
        if content_hash(excluded)==cursor['sha256']:return {'own':0,'others':0}
        newer=newer_activity_summary(excluded,str(c['id']),c.get('created_at'),row.get('assignee'))
        return {'own':newer['own_count'],'others':newer['other_count']}
    except (ValueError,TypeError,KeyError):return None

def queue(rows,actor,args):
    parser=Parser(add_help=False)
    group=parser.add_mutually_exclusive_group();group.add_argument('--mine',action='store_true');group.add_argument('--owner')
    parser.add_argument('--state',choices=['none','awaiting-review','changes-requested','awaiting-integration','integrated','legacy-review-ready','error'])
    parser.add_argument('--limit',type=int,default=20);parser.add_argument('--offset',type=int,default=0)
    a=parser.parse_args(args)
    if not 1<=a.limit<=100 or a.offset<0:raise ValueError('Invalid work queue page')
    owner=actor if a.mine else a.owner
    facts={r['id']:r for r in project_facts(rows)};items=[]
    for row in rows:
        if row.get('issue_type') in ('event','gate','merge-slot'):continue
        if owner is not None and row.get('assignee')!=owner:continue
        try:review=workflow(row);state=review['review_state'];error=None
        except ValueError as e:review={};state='error';error=str(e)[:300]
        fact=facts.get(row['id'],{}).get('facts',{})
        current_contribution=review.get('contribution') or {}
        current_scope=facts.get(row['id'],{}).get('scope') or {}
        if state=='awaiting-integration' and current_contribution.get('commit','').lower()==current_scope.get('source_commit','').lower() and fact.get('integrated',{}).get('value')=='passed':state='integrated'
        if row.get('status')=='closed' and state not in ('changes-requested','awaiting-review','awaiting-integration','legacy-review-ready','error'):continue
        if a.state and a.state!=state:continue
        contribution=review.get('contribution') or {}
        scope=facts.get(row['id'],{}).get('scope') or {}
        newer_counts=checkpoint_newer_counts(row)
        items.append({'task':row['id'],'title':str(row.get('title',''))[:200],'owner':row.get('assignee'),'status':row.get('status'),'review_state':state,
                      'contribution_id':contribution.get('comment_id'),'commit':contribution.get('commit'),'pending_review_items':len(review.get('pending_requests',[])),
                      'newer_activity_by_others':None if newer_counts is None else newer_counts['others'],'newer_activity_own':None if newer_counts is None else newer_counts['own'],
                      'lifecycle':{k:v['value'] for k,v in fact.items()},'lifecycle_scope':scope,
                      'lifecycle_matches_contribution':None if not contribution else scope.get('source_commit','').lower()==contribution['commit'].lower(),'error':error})
    priority={'changes-requested':0,'error':1,'awaiting-review':2,'legacy-review-ready':2,'awaiting-integration':3}
    items.sort(key=lambda r:(priority.get(r['review_state'],4),r['task']))
    return {'owner':owner,'total':len(items),'items':items[a.offset:a.offset+a.limit],'next_offset':a.offset+a.limit if a.offset+a.limit<len(items) else None,
            'coverage':'Fresh current view; structured review takes precedence over legacy review-ready labels. Lifecycle facts remain independent.'}

def execute(path,actor,action,args,attachments,run):
    if action=='work':
        rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
        return queue(rows,actor,args)
    if len(args) not in (1,2):raise ValueError('Use review TASK [--file payload.json] or handoff TASK --file payload.json')
    task=args[0]
    if action=='review' and len(args)==1:
        from briefing import task_row
        rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
        return workflow(task_row(rows,task))
    if len(args)!=2 or not args[1].startswith('@attachment:'):raise ValueError('A JSON file attachment is required')
    item=attachments.get(args[1].partition(':')[2],{})
    if not isinstance(item,dict) or item.get('flag') not in ('--file','-f') or not isinstance(item.get('text'),str):raise ValueError('Invalid attachment')
    payload=json.loads(item['text'])
    if not isinstance(payload,dict) or payload.get('task')!=task:raise ValueError('Payload task mismatch')
    if action=='handoff':
        from handoff import execute as handoff
        return handoff(path,actor,payload,run)
    from review_workflow import execute as review
    rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
    result=review(rows,task,actor,payload,run)
    if payload.get('operation')=='request-changes':
        # Retry repairs a label update interrupted after the durable review comment.
        run(['update',task,'--remove-label','review-ready','--json'])
    return result
