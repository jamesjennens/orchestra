"""Read-only D2 diagnostics. Evidence is data, never lifecycle/write authority."""
import re

RETRY_REASON='Lifecycle update in progress; retry the same operation if interrupted.'


def intermediate_pairs(rows,events):
    """Match only complete, uniquely ordered native retry pairs; retain uncertainty.

    Child numbers establish adjacency across all native child rows, not just one
    dimension. A missing child cannot be assumed to be a lifecycle operation.
    """
    import lifecycle
    from review_workflow import _recorded_at
    by_task={};native={};ambiguous=set();seen_ids=set()
    for row in rows:
        if not isinstance(row,dict):continue
        parents=[d.get('depends_on_id') for d in row.get('dependencies') or []
                 if isinstance(d,dict) and d.get('type')=='parent-child']
        if len(parents)!=1:continue
        task=parents[0];identity=row.get('id');order=None
        if isinstance(identity,str) and isinstance(task,str) and identity.startswith(task+'.'):
            suffix=identity[len(task)+1:]
            if suffix.isdigit():order=int(suffix)
        if order is None or row.get('malformed'):
            ambiguous.add(task);continue
        key=(task,order)
        if key in native or identity in seen_ids:ambiguous.add(task)
        native[key]=row;seen_ids.add(identity)
        by_task.setdefault(task,[]).append((order,row))
    classified={e['id']:e for e in events}
    pairs=[];unknown=[]
    for row in rows:
        if not isinstance(row,dict):continue
        event=lifecycle.native_event(row)
        if not event or event['id'] not in classified or event['payload'] is not None:continue
        description=row.get('description')
        if not isinstance(description,str) or not description.endswith('\n\nReason: '+RETRY_REASON):continue
        task=event['task'];order=event['order'];following_row=native.get((task,order+1)) if order is not None else None
        following=lifecycle.native_event(following_row) if following_row else None
        reason=None
        if task in ambiguous or order is None:reason='native-order-unclassifiable'
        elif following is None or following['id'] not in classified or following['payload'] is None:
            reason='following-canonical-event-unavailable'
        elif following['dimension']!=event['dimension'] or following_row.get('created_by')!=row.get('created_by'):
            reason='pair-dimension-or-author-mismatch'
        elif event['value']!=('unknown' if following['value']=='pending' else 'pending'):
            reason='intermediate-value-mismatch'
        else:
            first_at=_recorded_at(event['created_at']);last_at=_recorded_at(following['created_at'])
            if first_at is None or last_at is None or first_at>last_at:
                reason='pair-time-unclassifiable'
            prior=[lifecycle.native_event(r) for n,r in by_task.get(task,[]) if n<order]
            prior=[p for p in prior if p and p['dimension']==event['dimension']]
            previous=max(prior,key=lambda p:p['order']) if prior else None
            stated=re.match(r'(?:Set|Changed) [A-Za-z0-9-]+ from ([^\n]+) to ',description)
            if (previous and previous['value']!=following['value']) or (stated and stated[1]!=following['value']):
                reason='previous-value-disagrees'
        item=dict(task=task,event_id=event['id'],trust='data')
        if reason:
            unknown.append(dict(item,reason=reason));continue
        pairs.append(dict(item,following_event_id=following['id'],operation_id=following['payload']['operation_id'],
                          dimension=event['dimension'],native_order=[order,following['order']],
                          basis='adjacent-native-children-and-canonical-following-payload'))
    return pairs,unknown


HISTORY_VERSION_CAP=128
HISTORY_CALL_CAP=24
HISTORY_TOTAL_VERSION_CAP=1024
HISTORY_BYTES_CAP=1024*1024
HISTORY_TOTAL_BYTES_CAP=4*1024*1024
HISTORY_SECONDS=10.0
HISTORY_CALL_SECONDS=3.0


class AssignmentHistory:
    """Host adapter evidence, separate from all caller/native JSON row fields."""
    def __init__(self,entries=None,coverage=None):
        self.entries=entries or {}
        self.coverage=coverage or dict(available=False,reason='native-history-not-supplied')


def load_assignment_history(rows,read,clock=None):
    """Bounded history enrichment. Only the host audit adapter supplies ``read``.

    read(id, version_limit, byte_limit, timeout) must bound its native process.
    Limits include one extra version used to detect a per-issue truncated page.
    No ordinary lifecycle reader calls this function.
    """
    import time,record_json,lifecycle
    from actor_names import ROW_ID
    clock=clock or time.monotonic;started=clock()
    tasks={r['id']:r for r in rows if isinstance(r,dict) and isinstance(r.get('id'),str)
           and r.get('issue_type') not in ('event','gate')}
    events=lifecycle.classified_events(rows)
    identities=[]
    for task in sorted({e['task'] for e in events}):
        if task not in tasks:continue
        identities.append(task)
        identities.extend(e['id'] for e in sorted((e for e in events if e['task']==task),
                           key=lambda e:(e['order'] if e['order'] is not None else -1,e['id'])))
    identities=list(dict.fromkeys(identities));entries={};calls=versions=bytes_read=received_bytes=0;stop=None
    for identity in identities:
        remaining=HISTORY_SECONDS-(clock()-started)
        if calls>=HISTORY_CALL_CAP:stop='whole-audit-history-call-cap';break
        if versions>=HISTORY_TOTAL_VERSION_CAP:stop='whole-audit-history-version-cap';break
        if bytes_read>=HISTORY_TOTAL_BYTES_CAP:stop='whole-audit-history-byte-cap';break
        if remaining<=0:stop='whole-audit-history-time-budget';break
        if ROW_ID.fullmatch(identity) is None:
            entries[identity]=dict(reason='invalid-native-history-id');continue
        version_limit=min(HISTORY_VERSION_CAP+1,HISTORY_TOTAL_VERSION_CAP-versions)
        byte_limit=min(HISTORY_BYTES_CAP,HISTORY_TOTAL_BYTES_CAP-bytes_read)
        calls+=1
        try:
            raw=read(identity,version_limit,byte_limit,min(HISTORY_CALL_SECONDS,remaining))
            if not isinstance(raw,str):raise ValueError('invalid history')
            size=len(raw.encode('utf-8'));bytes_read+=size;received_bytes+=size
            if size>byte_limit:
                entries[identity]=dict(reason='native-history-byte-cap');continue
            if clock()-started>HISTORY_SECONDS:
                entries[identity]=dict(reason='whole-audit-history-time-budget');stop='whole-audit-history-time-budget';break
            found=record_json.loads(raw)
            if not isinstance(found,list):raise ValueError('invalid history')
            versions+=len(found)
            if len(found)>=version_limit:
                reason='native-history-version-cap' if version_limit==HISTORY_VERSION_CAP+1 else 'whole-audit-history-version-cap'
                entries[identity]=dict(reason=reason);continue
            entries[identity]=dict(versions=found,reason=None)
        except HistoryReadError as error:
            bytes_read+=error.bytes_read
            entries[identity]=dict(reason=error.reason)
        except (ValueError,TypeError,OSError,UnicodeError,RecursionError):
            entries[identity]=dict(reason='native-history-unavailable-or-damaged')
    coverage=dict(available=True,requested_ids=len(identities),read_calls=calls,
        returned_versions=versions,charged_bytes=bytes_read,received_bytes=received_bytes,unread_ids=len(set(identities)-set(entries)),
        stopped_reason=stop,elapsed_seconds=round(clock()-started,6),
        limits=dict(per_issue_versions=HISTORY_VERSION_CAP,per_issue_detection_versions=1,
            whole_audit_calls=HISTORY_CALL_CAP,whole_audit_versions=HISTORY_TOTAL_VERSION_CAP,
            per_issue_bytes=HISTORY_BYTES_CAP,whole_audit_bytes=HISTORY_TOTAL_BYTES_CAP,
            whole_audit_history_seconds=HISTORY_SECONDS,per_call_seconds=HISTORY_CALL_SECONDS),
        meaning='Native versions prove fields at their commit only. The committer is the service identity, not who assigned. No interval or historical principal authority is inferred.')
    return AssignmentHistory(entries,coverage)


class HistoryReadError(Exception):
    def __init__(self,reason,bytes_read=0):
        self.reason=reason;self.bytes_read=bytes_read;super().__init__(reason)


def _history_versions(history,identity,current,event=False):
    from review_workflow import _recorded_at
    if not isinstance(history,AssignmentHistory):return None,'native-assignment-history-unavailable'
    entry=history.entries.get(identity)
    if not entry:return None,history.coverage.get('stopped_reason') or 'native-history-not-read'
    if not isinstance(entry,dict):return None,'native-history-damaged'
    if entry.get('reason'):return None,entry['reason']
    versions=entry.get('versions')
    if not isinstance(versions,list) or not versions:return None,'native-history-absent'
    seen=set();times=set();last=None;validated=[]
    for version in versions:
        if not isinstance(version,dict):return None,'native-history-damaged'
        commit=version.get('CommitHash');stamp=_recorded_at(version.get('CommitDate'));issue=version.get('Issue')
        if (not isinstance(commit,str) or re.fullmatch('[a-z0-9]{32}',commit) is None
                or stamp is None or not isinstance(issue,dict) or issue.get('id')!=identity):
            return None,'native-history-damaged'
        if not isinstance(version.get('CommitDate'),str) or re.search(r'(?:Z|[+-]00:00)$',version['CommitDate']) is None:
            return None,'native-history-time-unclassifiable'
        if commit in seen or stamp in times:return None,'native-history-order-tie'
        if last is not None and stamp>=last:return None,'native-history-order-unclassifiable'
        seen.add(commit);times.add(stamp);last=stamp;validated.append(version)
    keys=('id','issue_type','title','description','created_by','created_at','updated_at') if event else ('id','issue_type','assignee','created_at','updated_at')
    for key in keys:
        if versions[0]['Issue'].get(key)!=current.get(key):return None,'native-history-current-head-mismatch'
    if _recorded_at(current.get('created_at')) is None or _recorded_at(current.get('updated_at')) is None:
        return None,'native-history-current-head-unclassifiable'
    return validated,None


def historical_assignment(issue,event,project=None,history=None,native_event=None):
    """Join exact native creation commits. Never interpolate between version points."""
    import task_assignment
    from review_workflow import _recorded_at
    unknown=dict(state='unknown',assignee=None,assigned_by=None,notice_ids=[],native_versions=[],
                 reason='native-assignment-history-unavailable',basis='none',trust='data',
                 historical_principal_ownership_verified=False)
    parent,reason=_history_versions(history,event['task'],issue)
    if reason:return dict(unknown,reason=reason)
    if not isinstance(native_event,dict):return dict(unknown,reason='native-event-row-unavailable')
    child,reason=_history_versions(history,event['id'],native_event,event=True)
    if reason:return dict(unknown,reason=reason)
    creation=child[-1];commit=creation['CommitHash']
    # An import/backdated raw row is not proved to have been recorded at its first
    # visible commit. Native timestamps have one-second rounding; larger gaps stay unknown.
    at=_recorded_at(native_event.get('created_at'));committed=_recorded_at(creation['CommitDate'])
    if at is None or abs((committed-at).total_seconds())>1:
        return dict(unknown,reason='native-event-creation-gap')
    for key in ('id','issue_type','title','description','created_by','created_at'):
        if creation['Issue'].get(key)!=native_event.get(key):
            return dict(unknown,reason='native-event-history-was-edited')
    matching=[v for v in parent if v['CommitHash']==commit]
    if len(matching)!=1:return dict(unknown,reason='native-assignment-commit-gap')
    point=matching[0]
    if point['CommitDate']!=creation['CommitDate']:
        return dict(unknown,reason='native-history-commit-mismatch')
    assignee=point['Issue'].get('assignee')
    if assignee is not None and (not isinstance(assignee,str) or task_assignment.ACTOR.fullmatch(assignee) is None):
        return dict(unknown,reason='native-assignee-unclassifiable')
    result=dict(unknown,state='known',assignee=assignee,reason=None,basis='same-native-event-creation-commit',
                native_versions=[commit],at_commit=point['CommitDate'],who_assigned_reason='not-proved-by-native-committer')
    # Notices are only attribution evidence. A validated connected notice plus an
    # exact native holder transition is needed; a shape-valid comment alone never
    # establishes the historical holder above.
    if not project:return result
    parsed=[(task_assignment.parse_comment(c,project=project,task=issue.get('id')),c)
            for c in issue.get('comments') or [] if isinstance(c,dict)]
    parsed=[(p,c) for p,c in parsed if p is not None]
    operations=[p['operation_id'] for p,c in parsed]
    notice_times=[_recorded_at(c.get('created_at')) for p,c in parsed]
    if (len(set(operations))!=len(operations) or None in notice_times or len(set(notice_times))!=len(notice_times)):
        return dict(result,who_assigned_reason='assignment-notice-order-unclassifiable')
    found=task_assignment.notices(issue,project,None,limit=len(issue.get('comments') or []))['items']
    index=parent.index(point);transition=None
    for new,old in zip(parent[index:],parent[index+1:]):
        if new['Issue'].get('assignee')!=old['Issue'].get('assignee'):
            transition=(new,old);break
    if transition is None:return result
    new,old=transition;change_at=_recorded_at(new['CommitDate'])
    candidates=[]
    by_id={str(c.get('id')):c for c in issue.get('comments') or [] if isinstance(c,dict)}
    for notice in found:
        payload=notice['notice'];comment=by_id.get(notice['comment_id'],{})
        start=_recorded_at(payload['at']);end=_recorded_at(comment.get('created_at'))
        if (payload['to']==assignee and payload['from']==old['Issue'].get('assignee')
                and start is not None and end is not None and start<change_at<end<at):
            candidates.append(notice)
    if len(candidates)==1:
        notice=candidates[0];result.update(assigned_by=notice['notice']['assigned_by'],
            notice_ids=[notice['comment_id']],who_assigned_reason=None,
            attribution_basis='validated-anchored-notice-and-native-holder-transition')
    return result


OPERATOR_AUDIT_BYTES=256*1024


class OperatorHistory:
    """Host-only authority trail context; never accepted from native row JSON."""
    def __init__(self,entries=(),baseline=None,listed=(),reason=None,observed_at=None):
        from datetime import datetime,timezone
        self.entries=entries;self.baseline=baseline;self.listed=listed;self.reason=reason
        self.observed_at=observed_at or datetime.now(timezone.utc)


def read_operator_history(root,operators):
    """Host release-query adapter only: reuse the existing validated audit reader."""
    import admin,stat
    try:
        path=root/admin.AUTHORITY_CHANGES_AUDIT
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode):return OperatorHistory(reason='authority-audit-not-regular')
        if info.st_size>OPERATOR_AUDIT_BYTES:return OperatorHistory(reason='authority-audit-byte-cap')
        # The shared reader groups permission and parse failures together. The
        # stat already obtained identifies mode-000 without another file read.
        if not (info.st_mode & (stat.S_IRUSR|stat.S_IRGRP|stat.S_IROTH)):
            return OperatorHistory(reason='authority-audit-unreadable')
        entries,baseline,damage=admin.read_authority_changes(root,max_bytes=OPERATOR_AUDIT_BYTES)
        if damage:return OperatorHistory(reason='authority-audit-damaged')
        if len(entries)>admin.AUTHORITY_CHANGES_MAX:return OperatorHistory(reason='authority-audit-entry-cap')
        return OperatorHistory(entries,baseline,list(operators or ()))
    except FileNotFoundError:return OperatorHistory(reason='authority-audit-missing')
    except (OSError,ValueError,TypeError,UnicodeError,RecursionError):
        return OperatorHistory(reason='authority-audit-unreadable')


def operator_eligibility(history,issuer,at,operators):
    """Membership within the retained, replayable authority trail only."""
    import admin,recovery
    from review_workflow import _recorded_at
    unknown=lambda reason:dict(state='unknown',reason=reason,basis='none')
    if not isinstance(history,OperatorHistory):return unknown('authority-history-not-supplied')
    if history.reason:return unknown(history.reason)
    try:
        if set(history.listed)!=recovery.configured_operators(operators):
            return unknown('authority-current-list-mismatch')
        baseline=history.baseline
        if not baseline or baseline['lists']['operators'] is None:
            return unknown('authority-baseline-unavailable')
        replay=admin.authority_change_replay(history.entries,{'operators':history.listed},'operators',baseline)
        if not replay or not replay['agrees'] or replay['listed_more_than_once']:
            return unknown('authority-trail-current-list-mismatch')
        stamp=_recorded_at(at);start=_recorded_at(baseline['at'])
        if stamp is None or start is None or stamp<=start or stamp>history.observed_at:
            return unknown('authority-time-outside-retained-trail')
        membership=set(baseline['lists']['operators']);previous=start
        for entry in history.entries:
            moment=_recorded_at(entry['at'])
            if moment is None or moment<previous or moment>history.observed_at:
                return unknown('authority-trail-time-unclassifiable')
            previous=moment
            if moment==stamp:return unknown('authority-trail-time-tie')
            if moment<stamp and entry['list']=='operators':
                if entry['change']=='add':membership.add(entry['actor'])
                else:membership.discard(entry['actor'])
        return dict(state='eligible' if issuer in membership else 'ineligible',reason=None,
                    basis='retained-authority-trail',baseline_at=baseline['at'])
    except (ValueError,TypeError,KeyError,RecursionError):return unknown('authority-history-damaged')


def review_void_diagnostics(rows,operators=None,journal=None,subset=None,operator_history=None):
    """Explain decisive currently ineligible review VOIDs without changing a verdict.

    The diagnostic compares an otherwise canonical counterfactual, one issuer at
    a time. Only the actual current-list projection governs every release gate.
    No historical operator membership is inferred from a native record. A host
    trail can prove prior eligibility; absent proof is explicitly unknown.
    """
    import lifecycle,recovery,review_workflow as reviews
    try:authority=recovery.configured_operators(operators)
    except ValueError:return []
    diagnostics=[]
    for issue in rows:
        if not isinstance(issue,dict) or issue.get('issue_type') in ('event','gate'):continue
        task=issue.get('id')
        if subset is not None and task not in subset:continue
        # Most release reads have no candidate at all. Do not repeat canonical
        # projections just to discover an empty optional diagnostic stream.
        if not any(isinstance(c,dict) and isinstance(c.get('text'),str)
                   and c['text'].startswith(recovery.PREFIX) and isinstance(c.get('author'),str)
                   and c['author'] not in authority
                   for c in issue.get('comments') or []):continue
        current_reason=lifecycle.unreadable_hold_chain(issue,operators,journal)
        try:
            actual_voids,invalid,refused=reviews.applied_review_voids(issue,operators)
            reverts,invalid_reverts=reviews.revert_records(issue,operators,journal,voids=list(actual_voids.values()))
            positions={str(c.get('id')):i for i,c in enumerate(issue.get('comments') or [])}
            actual=None if current_reason else reviews.project(issue,operators,journal,reverts,invalid_reverts)
        except (ValueError,KeyError,TypeError):continue
        current=None if actual is None else bool(reviews.integration_hold_sentence(actual))
        candidates={}
        for comment in issue.get('comments') or []:
            text=comment.get('text') if isinstance(comment,dict) else None
            if not isinstance(text,str) or not text.startswith(recovery.PREFIX):continue
            try:
                import record_json
                payload=record_json.loads(text[len(recovery.PREFIX):])
                recovery.validate(payload,task)
                if (payload['operator'] in authority or payload['operator']!=comment.get('author')
                        or payload['target_kind'] not in recovery.REVIEW_KIND_PREFIXES):continue
                eligibility=operator_eligibility(operator_history,payload['operator'],comment.get('created_at'),operators)
                if eligibility['state']=='ineligible':continue
                # Reuse canonical validation (native binding, bytes, order, chain).
                expanded=set(authority)|{payload['operator']}
                applied,_,_=reviews.applied_review_voids(issue,expanded)
                if payload['target'] not in applied or str(applied[payload['target']][1].get('id'))!=str(comment.get('id')):continue
                # Change ONLY this VOID's applicability, never the operator list
                # used by attestations, reverts, other VOIDs or the real verdict.
                alternative_voids=dict(actual_voids)
                alternative_voids[payload['target']]=(payload,comment)
                ordered=reviews.records(issue,set(alternative_voids))
                filtered=dict(issue,comments=[c for c in issue.get('comments') or [] if str(c.get('id')) not in alternative_voids])
                if lifecycle.unreadable_hold_chain(filtered,operators,journal):continue
                alternative=reviews.projection(ordered,list(alternative_voids.values()),
                    invalid,refused,positions,reverts,invalid_reverts,closed=issue.get('status')=='closed')
                counter=bool(reviews.integration_hold_sentence(alternative))
                if current==counter:continue
                target=next(c for c in issue.get('comments') or [] if str(c.get('id'))==payload['target'])
                original=record_json.loads(target['text'][len(reviews.PREFIX):])
                operation=original.get('operation')
                hold_id=payload['target'] if operation=='hold' else original.get('hold') if operation=='release-hold' else None
                candidates[str(comment['id'])]=dict(task=task,void_id=str(comment['id']),
                    target_id=payload['target'],target_operation=operation,hold_id=hold_id,
                    release_id=payload['target'] if operation=='release-hold' else None,
                    issuer=payload['operator'],current_result='unreadable' if current is None else 'held' if current else 'unheld',
                    current_reason=current_reason or (reviews.integration_hold_sentence(actual) if actual is not None else None),
                    counterfactual_reason=reviews.integration_hold_sentence(alternative),
                    counterfactual_result='held' if counter else 'unheld',
                    reason=('currently-ineligible-review-void' if eligibility['state']=='eligible'
                            else 'review-void-eligibility-unknown'),
                    historical_operator_eligibility=eligibility['state'],
                    eligibility_basis=eligibility['basis'],eligibility_reason=eligibility['reason'],trust='data')
            except (ValueError,TypeError,KeyError,StopIteration,RecursionError):continue
        diagnostics.extend(candidates[key] for key in sorted(candidates))
    return diagnostics


def review_void_report(rows,operators=None,journal=None,subset=None,operator_history=None):
    """A bounded additive report; never an input to target selection or a gate."""
    diagnostics=review_void_diagnostics(rows,operators,journal,subset,operator_history)
    if not diagnostics:return {}
    return dict(review_void_diagnostics=diagnostics[:50],review_void_diagnostics_total=len(diagnostics),
                review_void_diagnostics_omitted=max(0,len(diagnostics)-50))
