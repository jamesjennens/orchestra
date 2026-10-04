"""Review/contribution transport and bounded current work queues."""
import argparse
import json
import record_json
from lifecycle import integration_evidence, project_facts

CONTRACT_VERSION = 'cli-contract-v1'
WORK_STATES = ['none','awaiting-review','changes-requested','awaiting-integration','integrated','legacy-review-ready','withdrawn','superseded','error']
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
                 '--since', '--cursor', '--body-budget', '--ref-limit', '--ref-offset',
                 '--proposal-limit', '--proposal-offset', '--capability-limit', '--capability-offset'}

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
                '[--handoff-limit N] [--handoff-offset N] [--ref-limit N] [--ref-offset N] [--json]',
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
            'ref-limit': '%d..%d' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX),
            'ref-offset': '>= %d' % WORK_OFFSET_MIN,
            'proposal-limit': '%d..%d' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX),
            'proposal-offset': '>= %d' % WORK_OFFSET_MIN,
            'capability-limit': '%d..%d' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX),
            'capability-offset': '>= %d' % WORK_OFFSET_MIN,
        }
        payload['output'] = {
            'top_level': ['owner', 'total', 'items', 'next_offset', 'coverage', 'attention', 'guidance'],
            'guidance': 'guidance: the project\'s current standing guidance version (kittrial-5bb.99), '
                        'with present, version (null for unbound text), set_at, set_by (null when the audit record does not bind to '
                        'the text), unbound (true when the text is withheld), previous_version, the calling '
                        'actor\'s acknowledged state and attention (true when guidance is set, or cannot be '
                        'read, and this actor has not acknowledged the current version); next_action names '
                        '`guidance get`, or, for unbound text, says the guidance is being repaired by the '
                        'operator and that the withheld text must not be followed. '
                        'A project with no guidance reads as present: false, never an error.',
            'attention': 'attention.reference_matches: accepted reference entries that match your own '
                         'in-progress tasks by title, keys only, at most 3 per task and 10 tasks, with a count '
                         'of matching drafts; attention.reference_review: project-wide reference catalog counts (expired, '
                         'due_soon, unset = draft-only entries, acceptance_inert, malformed, total), always; '
                         'items only for an approver (an actor on the deployment operator allowlist), '
                         'paged by --ref-limit/--ref-offset; task filters never hide it. '
                         'attention.proposal_queue: the contributed requirement proposal queue, in the agent '
                         'attention shape (state, summary, counts, actions, truncated, computed_at) plus items '
                         'and next_offset; counts always, items only for an actor on the deployment operator '
                         'allowlist, paged by --proposal-limit/--proposal-offset; proposal text appears only as '
                         'a bounded excerpt with trust. '
                         'attention.capability_index:the capability index in the agent attention shape '
                         '(state, summary, counts, actions, truncated, computed_at) plus items and next_offset; '
                         'counts (drifted, reported_only, unverified_stale, alias_pending, draft_pending, '
                         'malformed, total) always, items only for an actor on the deployment operator '
                         'allowlist, paged by --capability-limit/--capability-offset',
            'item_identity': 'items[].task is the native task ID; items[].contribution_id is the '
                             'contribution comment ID, not a Git commit or latest_comment_id',
            'item_fields': ['task', 'title', 'owner', 'status', 'review_state', 'contribution_id',
                            'commit', 'pending_review_items', 'pending_handoff_requests',
                            'pending_handoff_total', 'pending_handoff_next_offset', 'lifecycle',
                            'lifecycle_scope', 'lifecycle_matches_contribution', 'error',
                            'deployed_delivery', 'deployed_delivery_is_current_contribution',
                            'workflow_state', 'integration', 'integration_disagreements',
                            'integration_warnings', 'review_request', 'review_requests'],
        }
    elif action == 'review':
        payload['operations'] = ['read (review TASK)', 'contribute', 'request-changes',
                                 'respond', 'approve', 'withdraw', 'request-review',
                                 'resolve-item', 'decline-review']
        payload['notes'] = [
            'Contribution payloads use contribution = the contribution record comment_id, '
            'never a Git SHA and never latest_comment_id.',
            'A JSON file attachment is required for every operation except read.',
            'A request-changes item may carry severity blocking or note (absent means blocking); '
            'only a blocking item holds up approval. Each pending/note item also carries the '
            'request-changes summary that asked for it.',
            'withdraw (author or coordinator, with reason) or request-review (naming a reviewer) '
            'is additive; older kits refuse the unknown operation rather than misreading it.',
            'Writing the new shapes (withdraw, request-review, resolve-item, decline-review, an '
            'item severity, a request-changes summary) is refused unless this installation has '
            'review_workflow_writes on; readers here understand them either way. An operator '
            'turns it on with `admin.py review-writes on --actor OPERATOR`.',
            'A text field over its limit is refused with the field, its length and the limit; for a '
            'review item or a resolution the error also names which one (its index and id). The limits '
            'are listed in `limits`.',
        ]
        payload['limits'] = review_workflow_limits()
    elif action == 'handoff':
        payload['operations'] = ['transfer (from_actor/to_actor)', 'request', 'disposition']
        payload['notes'] = ['A JSON file attachment is required; payload.task must equal TASK.']
        import handoff
        payload['limits'] = handoff.help_limits()
    elif action in ('brief', 'history', 'checkpoint'):
        import briefing
        payload['limits'] = briefing.help_limits(action)
        payload['notes'] = briefing.help_notes(action)
    return payload

def review_workflow_limits():
    import review_workflow
    return review_workflow.help_limits()


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
            {'flag': '--ref-limit N', 'description': 'reference attention items per page %d..%d (default 20)' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX)},
            {'flag': '--ref-offset N', 'description': 'reference attention offset >= %d (default 0)' % WORK_OFFSET_MIN},
            {'flag': '--proposal-limit N', 'description': 'proposal queue items per page %d..%d (default 20)' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX)},
            {'flag': '--proposal-offset N', 'description': 'proposal queue offset >= %d (default 0)' % WORK_OFFSET_MIN},
            {'flag': '--capability-limit N', 'description': 'capability attention items per page %d..%d (default 20)' % (WORK_LIMIT_MIN, WORK_LIMIT_MAX)},
            {'flag': '--capability-offset N', 'description': 'capability attention offset >= %d (default 0)' % WORK_OFFSET_MIN},
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

def workflow(issue,scopes=None,operators=None,reverts=None,journal=None,invalid_reverts=None):
    from review_state import project as reviewed
    return reviewed(issue,scopes,operators,reverts,journal,invalid_reverts)

def queue(rows,actor,args,request_dir=None, operators=None, reverts=None, scopes=None, journal=None,
          reference_attention=False, verifiers=None):
    """One page of the work queue.

    ``reverts`` and ``scopes`` are PER TASK: each is a mapping of task id to that
    task's validated host-issued revert records / lifecycle integration scopes.
    ``None`` (the normal case) means the page resolves each task itself, once:
    ``scopes`` comes from the lifecycle evidence and ``reverts`` from
    ``review_state.reverts_by_task``, which reads the host
    ``.integration-reverts/`` journal under ``journal`` a single time for the whole
    page (kittrial-5bb.52 P3: the arguments used to be applied to every row and
    the per-task revert scan was O(rows) and repeated per row).

    ``reference_attention`` adds the project-level ``attention.reference_review``
    block (.41 7.1) for the `work` command; the HTTP queue and render do not ask for
    it (My work is a later slice).
    """
    if help_requested(args):return help_payload('work')
    parser=Parser(add_help=False)
    group=parser.add_mutually_exclusive_group();group.add_argument('--mine',action='store_true');group.add_argument('--owner')
    parser.add_argument('--state',choices=WORK_STATES)
    parser.add_argument('--limit',type=int,default=20);parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--handoff-limit',type=int,default=20);parser.add_argument('--handoff-offset',type=int,default=0)
    parser.add_argument('--ref-limit',type=int,default=20);parser.add_argument('--ref-offset',type=int,default=0)
    parser.add_argument('--proposal-limit',type=int,default=20);parser.add_argument('--proposal-offset',type=int,default=0)
    parser.add_argument('--capability-limit',type=int,default=20);parser.add_argument('--capability-offset',type=int,default=0)
    parser.add_argument('--json',action='store_true')  # output is always JSON; accepted for consistency
    a=parser.parse_args(args)
    if not WORK_LIMIT_MIN<=a.limit<=WORK_LIMIT_MAX or a.offset<WORK_OFFSET_MIN or not WORK_LIMIT_MIN<=a.handoff_limit<=WORK_LIMIT_MAX or a.handoff_offset<WORK_OFFSET_MIN:
        raise ValueError('Invalid work page: --limit and --handoff-limit must be %d..%d; '
                         '--offset and --handoff-offset must be >= %d' % (WORK_LIMIT_MIN,WORK_LIMIT_MAX,WORK_OFFSET_MIN))
    if not WORK_LIMIT_MIN<=a.proposal_limit<=WORK_LIMIT_MAX or a.proposal_offset<WORK_OFFSET_MIN:
        raise ValueError('Invalid work page: --proposal-limit must be %d..%d and --proposal-offset >= %d'
                         % (WORK_LIMIT_MIN,WORK_LIMIT_MAX,WORK_OFFSET_MIN))
    if not WORK_LIMIT_MIN<=a.capability_limit<=WORK_LIMIT_MAX or a.capability_offset<WORK_OFFSET_MIN:
        raise ValueError('Invalid work page: --capability-limit must be %d..%d and --capability-offset >= %d'
                         % (WORK_LIMIT_MIN,WORK_LIMIT_MAX,WORK_OFFSET_MIN))
    if not WORK_LIMIT_MIN<=a.ref_limit<=WORK_LIMIT_MAX or a.ref_offset<WORK_OFFSET_MIN:
        raise ValueError('Invalid work page: --ref-limit must be %d..%d and --ref-offset >= %d'
                         % (WORK_LIMIT_MIN,WORK_LIMIT_MAX,WORK_OFFSET_MIN))
    if reverts is not None and not isinstance(reverts,dict):
        raise ValueError('reverts must be a mapping of task id to that task\'s validated revert records')
    if scopes is not None and not isinstance(scopes,dict):
        raise ValueError('scopes must be a mapping of task id to that task\'s integration scopes')
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
    facts={r['id']:r for r in project_facts(rows)};evidence={r['id']:r['scopes'] for r in integration_evidence(rows)};items=[]
    from review_state import is_integration_warning, reverts_by_task
    if reverts is None:
        revert_map,revert_problems=reverts_by_task(rows,operators,journal)
    else:
        revert_map,revert_problems=reverts,{}
    from reserved_comments import is_record_anchor
    from review_workflow import author_key
    for row in rows:
        if row.get('issue_type') in ('event','gate','merge-slot'):continue
        # Record anchors (kittrial-5bb.64) are never work, whatever their status.
        if is_record_anchor(row):continue
        task_reverts=revert_map.get(row['id'],[])
        task_scopes=evidence.get(row['id']) if scopes is None else scopes.get(row['id'])
        try:review=workflow(row,task_scopes,operators=operators,reverts=task_reverts,journal=journal,
                            invalid_reverts=revert_problems.get(row['id']));state=review['review_state'];error=None
        except ValueError as e:review={};state='error';error=str(e)[:300]
        if owner is not None and row.get('assignee')!=owner:
            # A first-class review request (kittrial-5bb.94 item 4) puts the task in
            # the NAMED reviewer's queue even though they are not its assignee.
            named=[request for request in (review.get('pending_review_requests') or [])
                   if author_key(request.get('reviewer'))==author_key(owner)]
            if not named:continue
        # The caller's own named review requests, so a row that is in the queue only
        # because they were named says so (kittrial-5bb.94 item 4). Computed for the
        # CALLING actor, not the --owner filter.
        review_requests=[{'request':request.get('request'),'reviewer':request.get('reviewer'),
                          'contribution':request.get('contribution'),'summary':request.get('summary'),
                          'author':request.get('author'),'timestamp':request.get('timestamp')}
                         for request in (review.get('pending_review_requests') or [])
                         if actor is not None and author_key(request.get('reviewer'))==author_key(actor)]
        review_request=bool(review_requests) and row.get('assignee')!=actor
        # The integration overlay's warnings (kittrial-5bb.52): the owner decision
        # keeps any-pass-wins, and a host-issued revert is always surfaced, so both
        # the disagreement and the revert warning travel with the row.
        disagreements=review.get('integration_disagreements') or []
        integration_warnings=[w for w in review.get('warnings') or [] if is_integration_warning(w)]
        fact=facts.get(row['id'],{}).get('facts',{})
        # Closing a task clears its awaiting-review queue entry (kittrial-5bb.94
        # item 1): the record stays in history and `review TASK` still reports it,
        # but a closed task is no longer offered to a reviewer. Outstanding
        # requested changes, an approved-but-unintegrated revision and a malformed
        # history stay visible: closure is not acceptance, and a broken chain must
        # still be surfaced.
        if row.get('status')=='closed' and state not in ('changes-requested','awaiting-integration','error'):continue
        if a.state and a.state!=state:continue
        contribution=review.get('contribution') or {}
        scope=facts.get(row['id'],{}).get('scope') or {}
        # Name the delivery a passed deployed fact belongs to, and whether it is
        # the task's current contribution (a release can ship a superseded
        # revision). Additive fields (kittrial-5bb.95).
        deployed_scope=scope if (fact.get('deployed') or {}).get('value')=='passed' else None
        deployed_delivery=None if deployed_scope is None else {key:deployed_scope.get(key) for key in ('release_id','environment','source_commit','integration_commit')}
        deployed_current=None if (deployed_scope is None or not contribution.get('commit')) else deployed_scope.get('source_commit','').lower()==contribution['commit'].lower()
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
                      # ORIGINAL meaning: the NEWEST scope shown in `lifecycle`/`lifecycle_scope`
                      # names the current contribution. The ANY-scope answer is additive as
                      # `integration.matches_contribution`.
                      'lifecycle_matches_contribution':None if not contribution else scope.get('source_commit','').lower()==contribution['commit'].lower(),
                      'deployed_delivery':deployed_delivery,
                      'deployed_delivery_is_current_contribution':deployed_current,
                      'integration':review.get('integration'),'workflow_state':review.get('workflow_state'),'error':error,
                      # Additive (kittrial-5bb.94 item 4): the caller's open review
                      # requests, and whether this row is in their queue because they
                      # were NAMED rather than because they own the task.
                      'review_request':review_request,'review_requests':review_requests,
                      # Additive (kittrial-5bb.52): the integration disagreement entries
                      # naming both facts and both scopes, and their rendered warnings.
                      'integration_disagreements':disagreements,'integration_warnings':integration_warnings})
    priority={'changes-requested':0,'error':1,'awaiting-review':2,'legacy-review-ready':2,'awaiting-integration':3}
    items.sort(key=lambda r:(priority.get(r['review_state'],4),r['task']))
    result={'owner':owner,'total':len(items),'items':items[a.offset:a.offset+a.limit],'next_offset':a.offset+a.limit if a.offset+a.limit<len(items) else None,
            'coverage':'Fresh current view; structured review takes precedence over legacy review-ready labels. Lifecycle facts remain independent; malformed handoff journals are surfaced as errors.'}
    if journal is not None:
        # The standing guidance channel (kittrial-5bb.99): every work queue page
        # carries the current guidance version, so a worker that only runs `work`
        # still sees that the coordinator's guidance changed. A project with no
        # guidance reads as present: false, never an error.
        from guidance import brief_block
        result['guidance']=brief_block(journal,actor)
    warnings=[]
    for item in items:
        for warning in item['integration_warnings']:
            if warning not in warnings:warnings.append(warning)
    if warnings:warnings.sort();result['warnings']=warnings
    if journal_errors:
        result['journal_errors']=journal_errors
        result['coverage']=result['coverage']+' Journal validation completed before task filters; errors apply to this entire page.'
    if reference_attention:
        # Project-wide and computed regardless of the task filters; one malformed
        # entry is counted, never fails the queue (.41 7.3).
        from reference_records import work_attention
        result['attention']={'reference_review':work_attention(rows,actor,operators,limit=a.ref_limit,
                                                               offset=a.ref_offset)}
        # Accepted entries that match the caller's own in-progress tasks, keys only, from
        # the same export (kittrial-5bb.98). One bad entry never fails the queue.
        from reference_records import work_matches
        from reserved_comments import is_record_anchor as anchor
        result['attention']['reference_matches']=work_matches(
            rows,[row for row in rows if isinstance(row,dict) and row.get('assignee')==actor
                  and row.get('issue_type') not in ('event','gate','merge-slot') and not anchor(row)],operators)
        # The contributed requirement proposal queue (.58 5.1, kittrial-5bb.68): also
        # project-wide, from the same export, and one bad proposal never fails the queue.
        from proposal_records import work_attention as proposal_attention
        result['attention']['proposal_queue']=proposal_attention(
            rows,actor,operators,journal.name if journal is not None else '',project=journal,
            limit=a.proposal_limit,offset=a.proposal_offset)
        # The capability index (.60 section 8, kittrial-5bb.76): also project-wide and
        # from the same export, trust derivation included; one bad entry never fails it.
        from capability_records import work_attention as capability_attention
        result['attention']['capability_index']=capability_attention(
            rows,actor,operators,journal.name if journal is not None else '',verifiers=verifiers,project=journal,
            limit=a.capability_limit,offset=a.capability_offset)
    return result

def execute(path,actor,action,args,attachments,run,operators=None,verifiers=None,review_writes=None):
    # Help is recognised anywhere as a standalone token and never touches the
    # native export, the coordination lock or an attachment.
    if help_requested(args):
        return help_payload(action)
    if action in ('review','handoff'):
        # Structured output is already JSON; accept the flag consistently with brief/show/work.
        args=[token for token in args if token!='--json']
    if action=='work':
        rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
        return queue(rows,actor,args,path/'.handoff-requests', operators=operators, journal=path,
                     reference_attention=True, verifiers=verifiers)
    if len(args) not in (1,2):raise ValueError('Use review TASK [--file payload.json] or handoff TASK --file payload.json')
    task=args[0]
    if action=='review' and len(args)==1:
        from briefing import task_row
        rows=[json.loads(line) for line in run(['export','--all']).splitlines() if line.strip()]
        issue=task_row(rows,task)
        from review_state import scopes_for
        return workflow(issue,scopes_for(rows,task),operators=operators,journal=path)
    if len(args)!=2 or not args[1].startswith('@attachment:'):raise ValueError('A JSON file attachment is required')
    item=attachments.get(args[1].partition(':')[2],{})
    if not isinstance(item,dict) or item.get('flag') not in ('--file','-f') or not isinstance(item.get('text'),str):raise ValueError('Invalid attachment')
    payload=record_json.loads(item['text'])
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
    result=review(rows,task,actor,payload,run, operators=operators, journal=path,
                  review_writes=review_writes)
    if payload.get('operation')=='request-changes':
        # Retry repairs a label update interrupted after the durable review comment.
        run(['update',task,'--remove-label','review-ready','--json'])
    return result
