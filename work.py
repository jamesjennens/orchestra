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

def queue(rows,actor,args,project=None):
    parser=Parser(add_help=False)
    group=parser.add_mutually_exclusive_group();group.add_argument('--mine',action='store_true');group.add_argument('--owner')
    parser.add_argument('--state',choices=['none','awaiting-review','changes-requested','awaiting-integration','integrated','legacy-review-ready','error'])
    parser.add_argument('--limit',type=int,default=20);parser.add_argument('--offset',type=int,default=0)
    a=parser.parse_args(args)
    if not 1<=a.limit<=100 or a.offset<0:raise ValueError('Invalid work queue page')
    owner=actor if a.mine else a.owner
    fact_errors={}
    try:
        facts={r['id']:r for r in project_facts(rows)}
    except (ValueError,KeyError,TypeError) as exc:
        # A malformed record must remain visible in the bounded queue rather
        # than making a whole project appear empty.
        facts={}
        fact_errors['__export__']=str(exc)[:300]
    items=[]
    for row in rows:
        if project is not None and row.get('project') not in (None,project):continue
        if row.get('issue_type') in ('event','gate','merge-slot'):continue
        if owner is not None and row.get('assignee')!=owner:continue
        try:review=workflow(row);state=review['review_state'];error=fact_errors.get('__export__')
        except (ValueError,KeyError,TypeError) as e:review={};state='error';error=str(e)[:300]
        task_id=row.get('id')
        if not isinstance(task_id,str) or not task_id:
            task_id='[malformed-record]'
            state='error';error=error or 'Record has no task ID'
        fact=facts.get(task_id,{}).get('facts',{})
        current_contribution=review.get('contribution') or {}
        current_scope=facts.get(row['id'],{}).get('scope') or {}
        if state=='awaiting-integration' and current_contribution.get('commit','').lower()==current_scope.get('source_commit','').lower() and fact.get('integrated',{}).get('value')=='passed':state='integrated'
        if row.get('status')=='closed' and state not in ('changes-requested','awaiting-review','awaiting-integration','legacy-review-ready','error'):continue
        if a.state and a.state!=state:continue
        contribution=review.get('contribution') or {}
        scope=facts.get(row['id'],{}).get('scope') or {}
        handoff_requests=[]
        request_dir = getattr(queue, '_request_dir', None)
        if request_dir and request_dir.is_dir():
            for request_file in request_dir.glob('*.json'):
                try:
                    request=json.loads(request_file.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    continue
                if request.get('task')==task_id and request.get('status')=='pending':
                    handoff_requests.append({'request_id':request.get('request_id'),'from_actor':request.get('from_actor'),
                                             'to_actor':request.get('to_actor'),'requester':request.get('requester'),
                                             'reason':request.get('reason')})
        items.append({'task':task_id,'title':str(row.get('title',''))[:200],'owner':row.get('assignee'),'status':row.get('status'),'review_state':state,
                      'contribution_id':contribution.get('comment_id'),'commit':contribution.get('commit'),'pending_review_items':len(review.get('pending_requests',[])),
                      'pending_handoff_requests':handoff_requests,
                      'lifecycle':{k:v['value'] for k,v in fact.items()},'lifecycle_scope':scope,
                      'lifecycle_matches_contribution':None if not contribution else scope.get('source_commit','').lower()==contribution['commit'].lower(),'error':error})
    priority={'changes-requested':0,'error':1,'awaiting-review':2,'legacy-review-ready':2,'awaiting-integration':3}
    items.sort(key=lambda r:(priority.get(r['review_state'],4),r['task']))
    return {'owner':owner,'total':len(items),'items':items[a.offset:a.offset+a.limit],'next_offset':a.offset+a.limit if a.offset+a.limit<len(items) else None,
            'coverage':'Fresh current view; structured review takes precedence over legacy review-ready labels. Lifecycle facts remain independent.'}

def execute(path,actor,action,args,attachments,run):
    if action=='work':
        queue._request_dir=path/'.handoff-requests'
        rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
        return queue(rows,actor,args,path.name)
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
        if payload.get('operation')=='request':
            from handoff import request as handoff_request
            return handoff_request(path,actor,payload,run)
        if payload.get('operation')=='disposition':
            from handoff import dispose as handoff_disposition
            return handoff_disposition(path,actor,payload,run)
        if payload.get('operation') in ('accept','decline','withdraw','supersede'):
            from handoff import dispose as handoff_disposition
            normalized=dict(payload,operation='disposition',disposition=payload['operation'])
            return handoff_disposition(path,actor,normalized,run)
        return handoff(path,actor,payload,run)
    from review_workflow import execute as review
    rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
    result=review(rows,task,actor,payload,run)
    if payload.get('operation')=='request-changes':
        # Retry repairs a label update interrupted after the durable review comment.
        run(['update',task,'--remove-label','review-ready','--json'])
    return result
