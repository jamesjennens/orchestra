"""Review/contribution transport and bounded current work queues."""
import argparse
import json
from lifecycle import project_facts

CONTRACT_VERSION = 'cli-contract-v1'
WORK_STATES = ['none','awaiting-review','changes-requested','awaiting-integration','integrated','legacy-review-ready','error']
WORK_LIMIT_MIN, WORK_LIMIT_MAX = 1, 100
WORK_OFFSET_MIN = 0

# Common mistakes get a targeted hint instead of argparse's bare "unrecognized
# arguments". Keys are matched as substrings of the parser error message.
MISTAKEN_FLAGS = {
    '--review-state': 'use --state for the review-state filter',
    '--state-name': 'use --state',
    '--assignee': 'use --owner ACTOR or --mine',
    '--task': 'work lists the queue; use "review TASK" for one task',
    '--review': 'work lists the queue; use "review TASK" for one task',
}

# Help is recognised wherever it appears as a standalone token, but not when the
# token before it is an option that takes a value: `work --owner -h` is still the
# option error it has always been, never a help request.
HELP_TOKENS = ('-h', '--help')
VALUE_OPTIONS = {'--owner', '--state', '--limit', '--offset', '--handoff-limit',
                 '--handoff-offset', '--file', '-f', '--items-offset', '--items-limit',
                 '--since', '--cursor', '--body-budget'}

def help_requested(args):
    """True when args ask for help; side-effect free for every command."""
    for index, token in enumerate(args):
        if token in HELP_TOKENS and not (index and args[index - 1] in VALUE_OPTIONS):
            return True
    return False

def help_payload(action='work'):
    """Machine-readable help returned through the normal JSON envelope on exit 0."""
    usage = {
        'work': 'work [--mine | --owner ACTOR] [--state STATE] [--limit N] [--offset N] '
                '[--handoff-limit N] [--handoff-offset N] [--json]',
        'review': 'review TASK [--file payload.json]',
        'handoff': 'handoff TASK --file payload.json',
        'brief': 'brief TASK [--items-offset N] [--items-limit N] [--json]',
        'history': 'history TASK [--limit N] [--since TIME] [--cursor TOKEN] '
                   '[--body-budget BYTES]',
        'checkpoint': 'checkpoint TASK --file checkpoint.json [--json]',
    }
    payload = {'schema_version': 1, 'contract': CONTRACT_VERSION, 'command': action,
               'usage': usage.get(action, action),
               'options': help_options(action),
               'exit_codes': {'0': 'result or help JSON on stdout',
                              '2': 'validation/transport error on stderr; stdout is not written'}}
    if action == 'work':
        payload['limits'] = {
            'limit': '%d..%d' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX),
            'offset': '>= %d' % WORK_OFFSET_MIN,
            'handoff-limit': '%d..%d' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX),
            'handoff-offset': '>= %d' % WORK_OFFSET_MIN,
        }
        payload['output'] = {
            'top_level': ['owner', 'total', 'items', 'next_offset', 'coverage'],
            'item_identity': 'items[].task is the native task ID; items[].contribution_id is the '
                             'contribution comment ID, not a Git commit or latest_comment_id',
            'item_fields': ['task', 'title', 'owner', 'status', 'review_state', 'contribution_id',
                            'commit', 'pending_review_items', 'pending_handoff_requests',
                            'pending_handoff_total', 'pending_handoff_next_offset', 'lifecycle',
                            'lifecycle_scope', 'lifecycle_matches_contribution', 'error'],
        }
    elif action == 'review':
        payload['operations'] = ['read (review TASK)', 'contribute', 'request-changes',
                                 'respond', 'approve']
        payload['notes'] = [
            'Contribution payloads use contribution = the contribution record comment_id, '
            'never a Git SHA and never latest_comment_id.',
            'A JSON file attachment is required for every operation except read.',
        ]
    elif action == 'handoff':
        payload['operations'] = ['transfer (from_actor/to_actor)', 'request', 'disposition']
        payload['notes'] = ['A JSON file attachment is required; payload.task must equal TASK.']
    elif action in ('brief', 'history', 'checkpoint'):
        import briefing
        payload['limits'] = briefing.help_limits(action)
        payload['notes'] = briefing.help_notes(action)
    return payload

def help_options(action):
    common = [
        {'flag': '--json', 'description': 'accepted for consistency; structured output is always JSON'},
        {'flag': '-h, --help', 'description': 'return this help as JSON on stdout with exit code 0'},
    ]
    if action == 'work':
        return [
            {'flag': '--mine', 'description': 'show only tasks owned by the requesting actor'},
            {'flag': '--owner ACTOR', 'description': 'show only tasks owned by ACTOR (mutually exclusive with --mine)'},
            {'flag': '--state STATE', 'description': 'filter by review state: ' + ', '.join(WORK_STATES)},
            {'flag': '--limit N', 'description': 'page size %d..%d (default 20)' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX)},
            {'flag': '--offset N', 'description': 'page offset >= %d (default 0)' % WORK_OFFSET_MIN},
            {'flag': '--handoff-limit N', 'description': 'pending handoff requests per task %d..%d (default 20)' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX)},
            {'flag': '--handoff-offset N', 'description': 'pending handoff offset >= %d (default 0)' % WORK_OFFSET_MIN},
            *common,
        ]
    if action == 'review':
        return [
            {'flag': 'TASK', 'description': 'task to read, or the task a payload applies to'},
            {'flag': '--file payload.json', 'description': 'transport a contribution/review payload as text'},
            *common,
        ]
    if action == 'handoff':
        return [
            {'flag': 'TASK', 'description': 'task to hand off or disposition'},
            {'flag': '--file payload.json', 'description': 'transport a handoff payload as text'},
            *common,
        ]
    if action == 'brief':
        return [
            {'flag': 'TASK', 'description': 'task to brief'},
            {'flag': '--items-offset N', 'description': 'unresolved-item page offset >= 0 (default 0)'},
            {'flag': '--items-limit N', 'description': 'unresolved items per page 1..10 (default 5)'},
            *common,
        ]
    if action == 'history':
        return [
            {'flag': 'TASK', 'description': 'task whose snapshot-bound history is paged'},
            {'flag': '--limit N', 'description': 'entries per page 1..20 (default 5)'},
            {'flag': '--since TIME', 'description': 'only entries at or after this timestamp'},
            {'flag': '--cursor TOKEN', 'description': 'continue the exact snapshot page'},
            {'flag': '--body-budget N', 'description': 'encoded body bytes per page 256..8000 (default 4000)'},
            *common,
        ]
    if action == 'checkpoint':
        return [
            {'flag': 'TASK', 'description': 'task the checkpoints belong to'},
            {'flag': '--file checkpoint.json', 'description': 'transport the checkpoint payload as text'},
            {'flag': '--json', 'description': 'accepted in any position; the saved checkpoint is always returned as JSON'},
            {'flag': '-h, --help', 'description': 'return this help as JSON on stdout with exit code 0'},
        ]
    return common

class Parser(argparse.ArgumentParser):
    def error(self,message):
        hint=next((text for flag,text in MISTAKEN_FLAGS.items() if flag in message),None)
        raise ValueError(message+('; hint: '+hint if hint else ''))

def workflow(issue):
    from review_workflow import project
    result=project(issue)
    if result['review_state']=='none' and 'review-ready' in (issue.get('labels') or []):
        result=dict(result,review_state='legacy-review-ready')
    return result

def queue(rows,actor,args,request_dir=None):
    if help_requested(args):return help_payload('work')
    parser=Parser(add_help=False)
    group=parser.add_mutually_exclusive_group();group.add_argument('--mine',action='store_true');group.add_argument('--owner')
    parser.add_argument('--state',choices=WORK_STATES)
    parser.add_argument('--limit',type=int,default=20);parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--handoff-limit',type=int,default=20);parser.add_argument('--handoff-offset',type=int,default=0)
    parser.add_argument('--json',action='store_true')  # output is always JSON; accepted for consistency
    a=parser.parse_args(args)
    if not WORK_LIMIT_MIN<=a.limit<=WORK_LIMIT_MAX or a.offset<WORK_OFFSET_MIN or not WORK_LIMIT_MIN<=a.handoff_limit<=WORK_LIMIT_MAX or a.handoff_offset<WORK_OFFSET_MIN:
        raise ValueError('Invalid work page: --limit and --handoff-limit must be %d..%d; '
                         '--offset and --handoff-offset must be >= %d' % (WORK_LIMIT_MIN,WORK_LIMIT_MAX,WORK_OFFSET_MIN))
    owner=actor if a.mine else a.owner
    journal_requests=[];journal_errors=[]
    if request_dir and request_dir.is_dir():
        request_files=sorted(request_dir.glob('*.json'))
        if len(request_files)>1000:raise ValueError('Handoff request journal exceeds bounded queue coverage')
        for request_file in request_files:
            try:
                request=json.loads(request_file.read_text(encoding='utf-8'))
                from handoff import validate_request_record
                validate_request_record(request)
                journal_requests.append(request)
            except (OSError,json.JSONDecodeError,ValueError) as exc:
                journal_errors.append({'path':request_file.name,'error':str(exc)[:300]})
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
        handoff_requests=[{'request_id':request['request_id'],'from_actor':request['from_actor'],
                           'to_actor':request['to_actor'],'requester':request['requester'],
                           'reason':request['reason']}
                          for request in journal_requests
                          if request.get('task')==row['id'] and request.get('status')=='pending']
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
    result={'owner':owner,'total':len(items),'items':items[a.offset:a.offset+a.limit],'next_offset':a.offset+a.limit if a.offset+a.limit<len(items) else None,
            'coverage':'Fresh current view; structured review takes precedence over legacy review-ready labels. Lifecycle facts remain independent; malformed handoff journals are surfaced as errors.'}
    if journal_errors:
        result['journal_errors']=journal_errors
        result['coverage']=result['coverage']+' Journal validation completed before task filters; errors apply to this entire page.'
    return result

def execute(path,actor,action,args,attachments,run):
    # Help is recognised anywhere it is a standalone token and never touches the
    # native export, the coordination lock or an attachment.
    if help_requested(args):
        return help_payload(action)
    if action in ('review','handoff'):
        # Structured output is already JSON; accept the flag consistently with brief/show/work.
        args=[token for token in args if token!='--json']
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
