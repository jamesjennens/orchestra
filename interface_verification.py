"""Exact revision-bound compatibility records; reports never establish trust.

The host hook computes trusted results from configured local source objects. Like
capability verification, trust requires a protected record, its native author,
and the actor's CURRENT configured authority. No approval or lifecycle is changed.
"""
import hashlib
import re
from pathlib import Path

import interface_compatibility as classifier
from interface_schema_source import prove, Unproved
from export_requirements import parse_json
from requirements import canonical_bytes, content_hash
from coordination import identifier
from reserved_comments import INTERFACE_VERIFICATION_PREFIX as PREFIX, refuse_http_actor

BODY_MAX = 16384
REQUEST = {'operation_id','key','old_revision','old_record_sha256','new_revision','new_record_sha256','direction','decision_id'}
FIELDS = REQUEST | {'schema_version','project','native_id','actor','trusted','claimed','request_sha256',
                    'repository_binding','verifier_binding','result','old_source','new_source','sha256'}


def configured_repository(config, owner_project):
    roots=config.get('interface_schema_repositories',{}) if isinstance(config,dict) else {}
    if not isinstance(roots,dict) or len(roots)>1000:raise ValueError('Invalid configured interface schema repositories')
    for project,root in roots.items():
        identifier(project)
        if not isinstance(root,str) or len(root)>1000 or '\x00' in root or not Path(root).is_absolute():raise ValueError('Interface schema repositories must be configured absolute local paths')
    return Path(roots[owner_project]) if owner_project in roots else None


def configured_bindings(config):
    bindings=config.get('interface_verifier_bindings',{}) if isinstance(config,dict) else {}
    if not isinstance(bindings,dict) or len(bindings)>1000:raise ValueError('Invalid configured interface verifier bindings')
    from http_authority import http_actor_id
    from recovery import identity
    for actor,issuer in bindings.items():
        if not isinstance(actor,str) or http_actor_id(actor)!=actor:raise ValueError('Interface verifier bindings name exact HTTP actor ids')
        identity(issuer,'Invalid native interface verifier binding')
        try:refuse_http_actor(issuer,False)
        except ValueError:raise ValueError('Interface verifier bindings require native issuers') from None
    return dict(bindings)


def repository_binding(repository):
    if repository is None:return None
    try:return hashlib.sha256(str(Path(repository).resolve()).encode()).hexdigest()
    except (OSError,RuntimeError):raise ValueError('Configured schema repository cannot be resolved') from None


def request(payload):
    import interface_records as interfaces
    if not isinstance(payload,dict) or set(payload)!=REQUEST: raise ValueError('Wrong interface check fields')
    identifier(payload['operation_id']);interfaces.valid_key(payload['key'])
    for name in ('old_revision','new_revision'):
        if type(payload[name]) is not int or payload[name]<1: raise ValueError('Positive interface revisions required')
    if payload['old_revision']>=payload['new_revision']: raise ValueError('Interface revisions must increase')
    for name in ('old_record_sha256','new_record_sha256'):
        if not isinstance(payload[name],str) or len(payload[name])!=64 or any(x not in '0123456789abcdef' for x in payload[name]): raise ValueError('Exact interface hashes required')
    if payload['direction'] not in ('request','response'): raise ValueError('Explicit request/response direction required')
    if payload['decision_id'] is not None:identifier(payload['decision_id'])


def parse(body):
    if not isinstance(body,str) or not body.startswith(PREFIX) or len(body)>BODY_MAX:return None
    try:
        if len(body.encode())>BODY_MAX:return None
        value=parse_json(body[len(PREFIX):])
        if not isinstance(value,dict) or set(value)!=FIELDS or type(value['schema_version']) is not int or value['schema_version']!=1:return None
        request({k:value[k] for k in REQUEST});identifier(value['actor']);identifier(value['native_id']);identifier(value['project'])
        if type(value['trusted']) is not bool or value['claimed'] not in (None,'additive','breaking','unknown'):return None
        if value['trusted'] and value['claimed'] is not None:return None
        if value['trusted']:
            from recovery import identity
            identity(value['verifier_binding'],'Invalid native interface verifier binding')
            refuse_http_actor(value['verifier_binding'],False)
        elif value['verifier_binding'] is not None:return None
        if value['request_sha256']!=content_hash(dict({k:value[k] for k in REQUEST},trusted=value['trusted'],claimed=value['claimed'])):return None
        if value['repository_binding'] is not None and not re.fullmatch('[0-9a-f]{64}',value['repository_binding']):return None
        result=value['result']
        fields={'algorithm','classification','compatibility','media_type','direction','reasons','old_sha256','new_sha256'}
        if not isinstance(result,dict) or set(result)!=fields:return None
        if result['classification'] not in ('additive','breaking','unknown'):return None
        if any(not isinstance(result[k],str) or not re.fullmatch('[0-9a-f]{64}',result[k]) for k in ('old_sha256','new_sha256')):return None
        if not isinstance(result['media_type'],str) or not re.fullmatch(r'[a-zA-Z0-9.+-]{1,100}/[a-zA-Z0-9.+-]{1,100}',result['media_type']):return None
        if value['trusted'] and result['classification']!='unknown' and (value['old_source'] is None or value['new_source'] is None):return None
        for name in ('old_source','new_source'):
            proof=value[name]
            if proof is not None and (not isinstance(proof,dict) or not source_matches(proof,dict(type='repo-path',anchor=None,commit=proof.get('source_commit'),path=proof.get('path'),sha256=proof.get('sha256')))):return None
        if result['compatibility'] != ('compatibility-unverified' if result['classification']=='unknown' else result['classification']):return None
        if result['direction']!=value['direction'] or not isinstance(result['algorithm'],str) or len(result['algorithm'])>80:return None
        if not isinstance(result['reasons'],list) or len(result['reasons'])>8 or any(not isinstance(s,str) or len(s)>160 for s in result['reasons']):return None
        if value['sha256']!=content_hash(value) or PREFIX+canonical_bytes(value).decode()!=body:return None
        return value
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError):return None


def read_record(view,body,comment):
    value=parse(body)
    if value is None:
        view['warnings'].append({'code':'malformed-interface-verification','detail':'Malformed compatibility evidence ignored'})
    else:
        view.setdefault('interface_verifications',[]).append({'record':value,'author':comment.get('author'),'comment_id':comment.get('id')})


def revisions(row,operators):
    """Canonical current reader plus exact live F3 binding for historical old pins."""
    import interface_records as interfaces
    kind=interfaces.KIND;live=kind.live_row(row,operators)[0]
    view=kind.entry_view(row,operators)
    if view['state'] in ('malformed','unsupported','conflicted','incomplete'):raise ValueError('Interface anchor is not readable')
    ledger=kind.existing_revisions(live);approved=set()
    from keyed_entries import evidence_is_live
    for comment in live.get('comments') or []:
        evidence=kind.parse_acceptance(comment.get('text') if isinstance(comment,dict) else None)
        if evidence is None or evidence['id']!=row.get('id') or not evidence_is_live(evidence,comment.get('author'),operators):continue
        record=ledger.get(evidence['revision'])
        if record and evidence['key']==record['key'] and evidence['record_sha256']==record['sha256'] and evidence['acceptance_state']==record['acceptance_state'] and record['acceptance_state']!='draft':approved.add(record['revision'])
    return view,ledger,approved


def pair(row,payload,operators):
    view,ledger,approved=revisions(row,operators)
    old,new=ledger.get(payload['old_revision']),ledger.get(payload['new_revision'])
    if old is None or new is None or old['sha256']!=payload['old_record_sha256'] or new['sha256']!=payload['new_record_sha256']:raise ValueError('Stale interface revision/hash pins')
    previous=max((n for n in approved if n<new['revision']),default=None)
    if previous!=old['revision']:raise ValueError('Old pin must be the preceding live approved revision')
    targets=[r for r in (view.get('record'),view.get('proposed')) if r]
    if new not in targets or old['key']!=payload['key'] or new['key']!=payload['key'] or old['owner_project']!=new['owner_project']:raise ValueError('Interface check must bind a current revision of one interface owner')
    return old,new


def authority(actor,project,operators,verifiers,state,rows,journal,decision_id,owners=None,bindings=None,*,http_only=False):
    from http_authority import http_actor_id,still_coordinates
    head=http_actor_id(actor)
    if http_only and head is None:return None
    issuer=(bindings or {}).get(head) if head is not None else actor
    if issuer not in set(operators)|set(verifiers):return None
    try:refuse_http_actor(issuer,False)
    except ValueError:return None
    if head is None:return issuer
    if not isinstance(state,dict):return None
    if head.startswith('agent_'):
        agent=(state.get('agents') or {}).get(head) or {}
        descriptor={'kind':'agent','agent_id':head,'user_id':agent.get('owner')}
        if not decision_id or not still_coordinates(state,descriptor,project):return False
        import delegations
        row=next((r for r in rows if r.get('id')==decision_id),None)
        try:delegations.check_acceptance(row,project=project,state=state,journal=journal,agent=head,kind='interfaces',operators=operators,owners=owners)
        except (ValueError,TypeError,KeyError,OSError):return False
    elif not still_coordinates(state,{'kind':'person','user_id':head},project):return False
    return issuer


def party_allows(actor,proposers,state,project,independent=False):
    from http_authority import http_actor_id,actor_parties
    head=http_actor_id(actor)
    if head is None:return not independent  # Native names alone do not prove distinct people.
    if not isinstance(state,dict) or not proposers:return False
    normalized=dict(state);normalized.setdefault('credentials',{});normalized.setdefault('users',{});normalized.setdefault('agents',{})
    by_agent=head.startswith('agent_')
    for proposer in proposers:
        if not isinstance(proposer,str) or not proposer:return False
        if by_agent and proposer.split('/',1)[0]==head:return False
        if independent:
            caller=set(actor_parties(normalized,actor,project));other=set(actor_parties(normalized,proposer,project))
            # Unknown native attribution cannot establish independence.
            if not caller or not other or any(p not in normalized['users'] for p in caller|other) or caller&other:return False
    return True


def source_matches(proof,pointer):
    if not isinstance(proof,dict) or set(proof)!={'proof','source_commit','path','sha256','objects','bytes'}:return False
    if pointer.get('type')!='repo-path' or pointer.get('anchor') is not None:return False
    commit=proof.get('source_commit');chain=proof.get('objects')
    if not isinstance(proof.get('path'),str) or not proof['path'] or len(proof['path'])>500:return False
    if not isinstance(proof.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',proof['sha256']):return False
    if not isinstance(commit,str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',commit):return False
    if not isinstance(chain,list) or not 3<=len(chain)<=34 or chain[0]!=commit:return False
    if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{%d}'%len(commit),v) for v in chain):return False
    return proof.get('proof')=='git-object-hash-chain-v1' and commit==pointer.get('commit') and proof.get('path')==pointer.get('path') and proof.get('sha256')==pointer.get('sha256') and type(proof.get('bytes')) is int and 0<=proof['bytes']<=classifier.BYTE_MAX


def derive(entry,row,project,*,operators=(),verifiers=(),repository=None,state=None,rows=(),journal=None,owners=None,target=None,bindings=None,independent=False):
    """No schema fetch/read. Trust exact immutable proof pins and current authority."""
    target=target or entry.get('record') or entry.get('proposed')
    block={'state':'compatibility-unverified','classification':'unknown','reports':0,'trusted_results':0,'revision':target.get('revision') if target else None,'record_sha256':target.get('sha256') if target else None,'key':target.get('key') if target else None,'checked_directions':[]}
    if target is None:return block
    outcomes=[]
    for item in entry.get('interface_verifications',[]):
        value=item['record']
        if value['new_revision']!=target['revision'] or value['new_record_sha256']!=target['sha256']:continue
        block['reports']+=1
        try:
            old,new=pair(row,value,operators)
            if not value['trusted'] or value['actor']!=item['author'] or value['project']!=project or value['native_id']!=row.get('id'):continue
            if value['repository_binding']!=repository_binding(repository) or repository is None:continue
            issuer=authority(value['actor'],project,operators,verifiers,state,rows,journal,value['decision_id'],owners,bindings)
            if not issuer or issuer!=value['verifier_binding']:continue
            import interface_records as interfaces
            ledger=interfaces.KIND.existing_revisions(interfaces.KIND.live_row(row,operators)[0])
            proposers=interfaces.KIND.draft_authors_for(row,new['revision'],ledger)
            if not party_allows(value['actor'],proposers,state,project,independent):continue
            result=value['result']
            if result['algorithm']!=classifier.VERSION or result['old_sha256']!=old['schema']['sha256'] or result['new_sha256']!=new['schema']['sha256']:continue
            if old['schema']['media_type']!=new['schema']['media_type'] or result['media_type']!=new['schema']['media_type']:continue
            if not source_matches(value['old_source'],old['schema']) or not source_matches(value['new_source'],new['schema']):continue
            outcomes.append((value['direction'],result['classification'],result['media_type']))
        except (ValueError,TypeError,KeyError,OSError):continue
    block['trusted_results']=len(outcomes)
    block['checked_directions']=sorted({direction for direction,_,_ in outcomes})
    if any(status=='breaking' for _,status,_ in outcomes):block.update(state='breaking-without-version',classification='breaking')
    elif outcomes and all(status=='additive' for _,status,_ in outcomes):
        # No unqualified JSON Schema claim from a check of just one variance.
        # OpenAPI comparisons already prove BOTH request and response contracts.
        if set(block['checked_directions'])=={'request','response'} or all(media in ('application/vnd.oai.openapi+json','application/vnd.oai.openapi') for _,_,media in outcomes):
            block.update(state='additive',classification='additive')
    return block


def record(payload,actor,run,project,*,operators=(),verifiers=(),repository=None,state=None,owners=None,trusted=False,claimed=None,bindings=None,independent=False,check_only=False,http_only=False):
    """Caller holds project then authority/config locks; no caller-supplied result trusted.

    `trusted` is a host route argument, never a payload field. Source proof uses
    fixed offline Git reads. An uncertain comment append reconciles on exact retry.
    A new operation ID explicitly retries a previously recorded unknown check.
    """
    request(payload)
    if type(trusted) is not bool:raise ValueError('Host trust must be explicit')
    if claimed not in (None,'additive','breaking','unknown'):raise ValueError('Invalid reported classification')
    import interface_records as interfaces
    rows=interfaces.KIND.read_key_rows(run,payload['key']);interfaces.KIND.require_unique_key(rows,payload['key'])
    entry=interfaces.KIND.find_entry(rows,payload['key'],operators)
    row=next(r for r in rows if r.get('id')==entry['native_id']);old,new=pair(row,payload,operators)
    project_name=Path(project).name
    issuer=None
    if trusted:
        if payload['decision_id']:
            import delegations
            decision=delegations.read_issue(run,payload['decision_id'])
            if decision is not None:rows.append(decision)
        issuer=authority(actor,project_name,operators,verifiers,state,rows,project,payload['decision_id'],owners,bindings,http_only=http_only)
        if not issuer:raise ValueError('Current configured interface verifier authority required')
        ledger=interfaces.KIND.existing_revisions(interfaces.KIND.live_row(row,operators)[0])
        if not party_allows(actor,interfaces.KIND.draft_authors_for(row,new['revision'],ledger),state,project_name,independent):raise ValueError('Current verifier/proposer party rule refuses this check')
        if claimed is not None:raise ValueError('A trusted check computes its own result')
    request_sha=content_hash(dict(payload,trusted=trusted,claimed=claimed))
    for item in entry.get('interface_verifications',[]):
        existing=item['record']
        if existing['operation_id']==payload['operation_id'] and existing['actor']==actor and existing['trusted']==trusted:
            if (existing['project']!=project_name or existing['native_id']!=row['id'] or item['author']!=actor or existing['request_sha256']!=request_sha) or trusted and (existing['repository_binding']!=repository_binding(repository) or existing['verifier_binding']!=issuer):raise ValueError('Interface check operation conflicts with stored evidence')
            if existing['result']['algorithm']!=classifier.VERSION:
                raise ValueError('Stored interface check evidence is stale; use a new operation')
            if trusted and existing['result']['classification']!='unknown':
                result=existing['result']
                if (result['old_sha256']!=old['schema']['sha256'] or result['new_sha256']!=new['schema']['sha256']
                        or result['media_type']!=new['schema']['media_type'] or old['schema']['media_type']!=new['schema']['media_type']
                        or not source_matches(existing['old_source'],old['schema']) or not source_matches(existing['new_source'],new['schema'])):
                    raise ValueError('Stored interface check evidence is stale; use a new operation')
            return {'record':existing,'comment_id':item['comment_id'],'reconciled':True}
        # Read-only replay guards must never repair a missing or voided record.
    if check_only:raise ValueError('Stored interface check is missing or voided; use a new operation')
    result={'algorithm':classifier.VERSION,'classification':'unknown','compatibility':'compatibility-unverified','media_type':new['schema']['media_type'],
            'direction':payload['direction'],'reasons':['contributor-report' if not trusted else 'source-proof-unavailable'],
            'old_sha256':old['schema']['sha256'],'new_sha256':new['schema']['sha256']}
    old_source=new_source=None
    if trusted and repository is not None and old['schema']['media_type']==new['schema']['media_type']:
        try:
            old_bytes,old_source=prove(old['schema'],repository);new_bytes,new_source=prove(new['schema'],repository)
            result=classifier.classify(old_bytes,new_bytes,media_type=new['schema']['media_type'],direction=payload['direction'])
        except (Unproved,OSError,ValueError):old_source=new_source=None
    value=dict(payload,schema_version=1,project=project_name,native_id=row['id'],actor=actor,trusted=bool(trusted),claimed=claimed,
               request_sha256=request_sha,repository_binding=repository_binding(repository),verifier_binding=issuer,result=result,old_source=old_source,new_source=new_source)
    value['sha256']=content_hash(value);body=PREFIX+canonical_bytes(value).decode()
    if parse(body)!=value:raise ValueError('Interface compatibility evidence exceeds its supported record schema')
    raw=run(['comments','add',row['id'],body,'--json'])
    from keyed_records import _comment_id
    return {'record':value,'comment_id':_comment_id(raw),'reconciled':False}
