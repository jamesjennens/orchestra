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

def queue(rows,actor,args,request_dir=None):
    parser=Parser(add_help=False)
    group=parser.add_mutually_exclusive_group();group.add_argument('--mine',action='store_true');group.add_argument('--owner')
    parser.add_argument('--state',choices=['none','awaiting-review','changes-requested','awaiting-integration','integrated','legacy-review-ready','error'])
    parser.add_argument('--limit',type=int,default=20);parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--handoff-limit',type=int,default=20);parser.add_argument('--handoff-offset',type=int,default=0)
    a=parser.parse_args(args)
    if not 1<=a.limit<=100 or a.offset<0 or not 1<=a.handoff_limit<=100 or a.handoff_offset<0:raise ValueError('Invalid work queue page')
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
        handoff_requests=[];journal_errors=[]
        if request_dir and request_dir.is_dir():
            request_files=sorted(request_dir.glob('*.json'))
            if len(request_files)>1000:raise ValueError('Handoff request journal exceeds bounded queue coverage')
            for request_file in request_files:
                try:
                    request=json.loads(request_file.read_text(encoding='utf-8'))
                    from handoff import validate_request_record
                    validate_request_record(request)
                except (OSError,json.JSONDecodeError,ValueError) as exc:
                    journal_errors.append({'path':request_file.name,'error':str(exc)[:300]})
                    continue
                if request.get('task')==row['id'] and request.get('status')=='pending':
                    handoff_requests.append({'request_id':request['request_id'],'from_actor':request['from_actor'],
                                             'to_actor':request['to_actor'],'requester':request['requester'],
                                             'reason':request['reason']})
        handoff_total=len(handoff_requests)
        handoff_requests=handoff_requests[a.handoff_offset:a.handoff_offset+a.handoff_limit]
        if journal_errors:
            state='error';error='Malformed handoff journal: '+json.dumps(journal_errors[:10],sort_keys=True)
        items.append({'task':row['id'],'title':str(row.get('title',''))[:200],'owner':row.get('assignee'),'status':row.get('status'),'review_state':state,
                      'contribution_id':contribution.get('comment_id'),'commit':contribution.get('commit'),'pending_review_items':len(review.get('pending_requests',[])),
                      'pending_handoff_requests':handoff_requests,
                      'pending_handoff_total':handoff_total,
                      'pending_handoff_next_offset':a.handoff_offset+a.handoff_limit if a.handoff_offset+a.handoff_limit<handoff_total else None,
                      'lifecycle':{k:v['value'] for k,v in fact.items()},'lifecycle_scope':scope,
                      'lifecycle_matches_contribution':None if not contribution else scope.get('source_commit','').lower()==contribution['commit'].lower(),'error':error})
    priority={'changes-requested':0,'error':1,'awaiting-review':2,'legacy-review-ready':2,'awaiting-integration':3}
    items.sort(key=lambda r:(priority.get(r['review_state'],4),r['task']))
    return {'owner':owner,'total':len(items),'items':items[a.offset:a.offset+a.limit],'next_offset':a.offset+a.limit if a.offset+a.limit<len(items) else None,
            'coverage':'Fresh current view; structured review takes precedence over legacy review-ready labels. Lifecycle facts remain independent; malformed handoff journals are surfaced as errors.'}

def execute(path,actor,action,args,attachments,run):
    if action=='work':
        rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
        return queue(rows,actor,args,path/'.handoff-requests')
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
            return handoff_request(path,actor,payload)
        if payload.get('operation')=='disposition':
            from handoff import disposition as handoff_disposition
            return handoff_disposition(path,actor,payload,run)
        return handoff(path,actor,payload,run)
    from review_workflow import execute as review
    rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
    result=review(rows,task,actor,payload,run)
    if payload.get('operation')=='request-changes':
        # Retry repairs a label update interrupted after the durable review comment.
        run(['update',task,'--remove-label','review-ready','--json'])
    return result
