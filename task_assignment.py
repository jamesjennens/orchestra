"""Closed assignment requests and native structured task notices.

Notices are ordinary native comments written by the dedicated guarded assignment
operation. They do not grant authority, complete a handoff or acknowledge delivery.
This module has no store. Its apply function writes through the caller's guarded
NativeRunner after current target eligibility, assignee, review and slot checks.
"""
import datetime
from pathlib import Path
import re
import record_json
from actor_names import ROW_ID
from requirements import canonical_bytes, content_hash

PREFIX = 'Kind: task-assignment-v1\n'
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}\Z')
PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
REQUEST_FIELDS = frozenset({'schema_version', 'operation_id', 'task',
                            'expected_assignee', 'assignee', 'reason',
                            'allow_pending_review', 'allow_held_slot'})
NOTICE_FIELDS = frozenset({'schema_version', 'operation_id', 'project', 'task',
                           'assigned_by', 'from', 'to', 'at', 'reason',
                           'allow_pending_review', 'allow_held_slot', 'sha256'})
REASON_MAX = 1000


def _actor(value, field, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or ACTOR.fullmatch(value) is None:
        raise ValueError(field + ' must name one exact existing actor')


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > REASON_MAX:
        raise ValueError('Assignment needs a nonempty reason of at most 1000 characters')
    from review_workflow import plain_text
    plain_text(value, 'assignment reason')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Assignment reason must be valid UTF-8 text') from exc


def validate_request(payload, task=None):
    if not isinstance(payload, dict) or set(payload) != REQUEST_FIELDS:
        raise ValueError('Assignment requires its exact closed field set')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('Assignment schema_version must be 1')
    if not isinstance(payload['task'], str) or ROW_ID.fullmatch(payload['task']) is None:
        raise ValueError('Assignment needs an exact task id')
    if task is not None and payload['task'] != task:
        raise ValueError('Assignment task differs from the addressed task')
    _actor(payload['operation_id'], 'operation_id')
    _actor(payload['expected_assignee'], 'expected_assignee', nullable=True)
    _actor(payload['assignee'], 'assignee', nullable=True)
    _reason(payload['reason'])
    for key in ('allow_pending_review', 'allow_held_slot'):
        if type(payload[key]) is not bool:
            raise ValueError(key + ' must be an explicit boolean')


def _validate_notice(notice):
    if not isinstance(notice, dict) or set(notice) != NOTICE_FIELDS:
        raise ValueError('Assignment notice requires its exact field set')
    request = {key: notice[key] for key in REQUEST_FIELDS
               if key not in ('assignee', 'expected_assignee')}
    request.update(assignee=notice['to'], expected_assignee=notice['from'])
    validate_request(request)
    _actor(notice['assigned_by'], 'assigned_by')
    if not isinstance(notice['project'], str) or PROJECT.fullmatch(notice['project']) is None:
        raise ValueError('Assignment notice needs the actual project')
    if notice['from'] == notice['to']:
        raise ValueError('Assignment notice must record a holder change')
    if not isinstance(notice['at'], str):
        raise ValueError('Assignment notice needs server UTC time')
    try:
        at = datetime.datetime.fromisoformat(notice['at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Assignment notice needs server UTC time') from exc
    if at.tzinfo is None or at.utcoffset() != datetime.timedelta(0):
        raise ValueError('Assignment notice needs server UTC time')
    if notice['sha256'] != content_hash(notice):
        raise ValueError('Assignment notice hash differs')


def make_notice(payload, *, project, assigned_by, at):
    """Build from a validated request and trusted effect metadata, never body attribution."""
    validate_request(payload)
    notice = {key: payload[key] for key in REQUEST_FIELDS
              if key not in ('assignee', 'expected_assignee')}
    notice.update(project=project, assigned_by=assigned_by, at=at,
                  **{'from': payload['expected_assignee'], 'to': payload['assignee']})
    notice['sha256'] = content_hash(notice)
    _validate_notice(notice)
    return notice


def body(notice):
    _validate_notice(notice)
    return PREFIX + canonical_bytes(notice).decode('utf-8')


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('Duplicate assignment notice field')
        out[key] = value
    return out


def parse_comment(comment, *, project, task):
    """Only an exact record bound to its native comment author/project/task is a notice."""
    if not isinstance(comment, dict):
        return None
    text = comment.get('text')
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    try:
        notice = record_json.loads(text[len(PREFIX):], object_pairs_hook=_unique)
        _validate_notice(notice)
        if body(notice) != text or notice['project'] != project or notice['task'] != task:
            return None
        if comment.get('author') != notice['assigned_by']:
            return None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
    return notice


def holder_message(notice, actor):
    """A polling notice for either holder; no push or acknowledgement claim."""
    _validate_notice(notice)
    if actor == notice['from'] and actor is not None:
        return 'This task was taken from you by %s: %s' % (notice['assigned_by'], notice['reason'])
    if actor == notice['to'] and actor is not None:
        return 'Assigned to you by %s: %s' % (notice['assigned_by'], notice['reason'])
    return None


class AssignmentError(ValueError):
    """A bounded refusal before the dedicated assignment effect's first write."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def check_change(payload, row, review, slot, target_is_eligible):
    """Judge fresh canonical snapshots under the project/live-authority locks.

    The callback is a trusted current-project eligibility read, not a request field
    or a cached browser roster. This function never changes the row, workflow or
    slot. The caller separately authenticates CAP_COORDINATE before calling it.
    """
    validate_request(payload)
    if (not isinstance(row, dict) or row.get('malformed') or
            row.get('id') != payload['task'] or 'assignee' not in row or
            row.get('status') not in ('open', 'in_progress', 'closed')):
        raise AssignmentError(503, 'The addressed native task cannot be read completely; nothing was changed.')
    from reserved_comments import carries_record_label, is_record_anchor, MERGE_SLOT_LABEL
    labels = row.get('labels')
    if not isinstance(labels, (list, tuple)):
        raise AssignmentError(503, 'Task labels cannot be read completely; nothing was changed.')
    if carries_record_label(row) and not isinstance(row.get('comments'), list):
        raise AssignmentError(503, 'Record-anchor comments cannot be read completely; nothing was changed.')
    if (is_record_anchor(row) or MERGE_SLOT_LABEL in labels or
            row.get('issue_type') in ('decision', 'gate', 'job', 'event', 'merge-slot') or 'decision' in labels):
        raise AssignmentError(422, 'Assignment cannot change a record anchor or internal row.')
    current = row['assignee']
    # Native bd represents an unassigned row as null or an empty string. The
    # request uses exactly null; arbitrary whitespace is never normalized.
    current = None if current == '' else current
    try:
        _actor(current, 'native assignee', nullable=True)
    except ValueError as exc:
        raise AssignmentError(503, 'The native assignee is unreadable; nothing was changed.') from exc
    if current != payload['expected_assignee']:
        raise AssignmentError(409, 'The task assignee changed; refresh the task before assigning it.')
    if payload['assignee'] is not None:
        if not callable(target_is_eligible):
            raise AssignmentError(503, 'Target eligibility cannot be checked; nothing was changed.')
        verdict = target_is_eligible(payload['assignee'])
        if type(verdict) is not bool:
            raise AssignmentError(503, 'Target eligibility is unknown; nothing was changed.')
        if not verdict:
            raise AssignmentError(409, 'The target is not currently eligible in this project; refresh the roster.')
    if (not isinstance(review, dict) or review.get('review_state') not in
            ('none', 'awaiting-review', 'changes-requested', 'awaiting-integration',
             'held', 'integrated', 'reconciliation-needed', 'legacy-review-ready',
             'withdrawn', 'superseded') or 'contribution' not in review):
        raise AssignmentError(503, 'The current review cannot be read; nothing was changed.')
    if (not isinstance(slot, dict) or type(slot.get('available')) is not bool or
            slot.get('missing') or slot.get('damaged') or 'holder' not in slot or
            'context' not in slot):
        raise AssignmentError(503, 'The current merge slot cannot be read; nothing was changed.')
    holder = slot['holder']
    try:
        _actor(holder, 'merge-slot holder', nullable=True)
    except ValueError as exc:
        raise AssignmentError(503, 'The merge-slot holder is unreadable; nothing was changed.') from exc
    if slot['available'] != (holder is None):
        raise AssignmentError(503, 'The merge-slot state is inconsistent; nothing was changed.')
    context = slot['context']
    if context is not None and (not isinstance(context, dict) or
            context.get('holder') != holder or not isinstance(context.get('task'), str)):
        raise AssignmentError(503, 'The merge-slot context is unreadable; nothing was changed.')
    changed = current != payload['assignee']
    review_displaced = bool(changed and current and row['status'] != 'closed' and
                            review['contribution'] and review['review_state'] in
                            ('awaiting-review', 'changes-requested'))
    if review_displaced and not payload['allow_pending_review']:
        raise AssignmentError(409, 'This holder has a contribution awaiting review or changes requested. '
                              'Set allow_pending_review explicitly to change its holder; review items are retained.')
    if changed and current and holder == current and context is None:
        raise AssignmentError(503, 'The holder has the merge slot but its task context is unknown; nothing was changed.')
    slot_displaced = bool(changed and current and holder == current and context and
                          context['task'] == payload['task'])
    if slot_displaced and not payload['allow_held_slot']:
        raise AssignmentError(409, 'This holder holds the merge slot for this task. '
                              'Set allow_held_slot explicitly to assign it; the slot and its holder are retained.')
    return dict(changed=changed, previous_assignee=current, assignee=payload['assignee'],
                displaced_pending_review=review_displaced, displaced_held_slot=slot_displaced,
                review_items_retained=True, slot_retained=True)



def eligible_roster(state, project, host_registry, *, now=None, worker_actor_allowed=None):
    """Existing usable project identities, exposing actor/name/kind only.

    Inputs are trusted live service state and this project's validated session
    registry, never a browser-supplied roster. A worker namespace is included only
    after the canonical namespace/standing guard confirms it currently usable.
    """
    from http_authority import AuthorityDenied, CAP_TASKS, ROLE_CAPABILITIES, decide
    if not isinstance(state, dict) or any(not isinstance(state.get(k), dict)
            for k in ('projects', 'users', 'memberships', 'credentials', 'agents')):
        raise AssignmentError(503, 'The project identity roster cannot be read.')
    if project not in state['projects']:
        raise AssignmentError(404, 'Project not found')
    if not isinstance(host_registry, dict) or not isinstance(host_registry.get('records'), dict):
        raise AssignmentError(503, 'The project session roster cannot be read.')
    membership = state['memberships'].get(project, {})
    if not isinstance(membership, dict):
        raise AssignmentError(503, 'The project membership roster cannot be read.')
    found = {}

    def put(actor, name, kind):
        try:
            _actor(actor, 'roster actor')
        except ValueError:
            raise AssignmentError(503, 'The project actor roster cannot be read.') from None
        found.setdefault(actor, dict(actor=actor, name=name[:120] if isinstance(name, str) else actor,
                                    kind=kind))

    for user_id, user in state['users'].items():
        if (not isinstance(user, dict) or user.get('id') != user_id or user.get('disabled')
                or not ACTOR.fullmatch(user_id)):
            continue
        if user.get('superuser') or CAP_TASKS in ROLE_CAPABILITIES.get(membership.get(user_id), ()):
            put(user_id, user.get('display_name'), 'account')
    for credential_id, credential in state['credentials'].items():
        if not isinstance(credential, dict):
            raise AssignmentError(503, 'The credential roster cannot be read.')
        try:
            decide(state, dict(via='credential', user_id=credential.get('user_id'),
                               credential_id=credential_id, project=project,
                               capability=CAP_TASKS), now=now)
        except AuthorityDenied:
            continue
        except (TypeError, ValueError):
            raise AssignmentError(503, 'The credential roster cannot be read.') from None
        agent_id = credential.get('agent_id')
        actor = credential.get('actor') or credential.get('user_id')
        if agent_id:
            agent = state['agents'].get(agent_id)
            if not isinstance(agent, dict) or actor != agent_id:
                raise AssignmentError(503, 'The agent roster cannot be read.')
            put(actor, agent.get('name'), 'agent')
        elif actor == credential.get('user_id'):
            # Stable account actor; the account list already judges its current role.
            continue
        else:
            if not callable(worker_actor_allowed):
                raise AssignmentError(503, 'Worker target standing cannot be checked.')
            allowed = worker_actor_allowed(actor, credential_id, credential)
            if type(allowed) is not bool:
                raise AssignmentError(503, 'Worker target standing is unknown.')
            if allowed:
                put(actor, credential.get('name'), 'worker')
    for record in host_registry['records'].values():
        if not isinstance(record, dict) or not isinstance(record.get('actor'), str):
            raise AssignmentError(503, 'The project session roster cannot be read.')
        put(record['actor'], record.get('name'), 'session')
    return [found[key] for key in sorted(found)]



def standing_allowed(denied):
    """Preserve a canonical read fault instead of calling an unknown target ineligible."""
    if denied is None:
        return True
    if not isinstance(denied, dict) or denied.get('fault'):
        raise AssignmentError(503, 'Worker target standing cannot be read; nothing was changed.')
    if denied.get('authority_status') in (401, 403, 404):
        return False
    raise AssignmentError(503, 'Worker target standing cannot be read; nothing was changed.')


def apply(payload, actor, run, project, *, eligible, operators=None, at):
    """One assignee-only native update and one notice in the existing guard.

    Caller holds project and live-authority locks. Any exception after update is
    deliberately left to NativeRunner/run_guarded's uncertain-outcome handling.
    Never compensate by restoring an old holder or repeat a possibly committed write.
    """
    from review_state import project as reviewed, scopes_for
    from web_merge_slot import snapshot, SlotError
    validate_request(payload)
    try:
        rows = record_json.loads_rows(run(['export', '--all']))
        if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get('malformed') for row in rows):
            raise ValueError('Unreadable native snapshot')
        matches = [row for row in rows if row.get('id') == payload['task']]
        if not matches:
            raise AssignmentError(404, 'Task not found; nothing was changed.')
        if len(matches) != 1:
            raise ValueError('Ambiguous task snapshot')
        row = dict(matches[0])
        # bd 1.2.2 omits empty assignee/labels/comments on export. Missing comments
        # are empty only when the native count proves zero; never discard an
        # omitted nonempty history or silently replace malformed explicit fields.
        row.setdefault('assignee', None)
        row.setdefault('labels', [])
        if 'comments' not in row and type(row.get('comment_count')) is int and row['comment_count'] == 0:
            row['comments'] = []
        if not isinstance(row.get('comments'), list):
            raise ValueError('Incomplete task comments')
        review = reviewed(row, scopes_for(rows, payload['task']), operators=operators, journal=project)
        slot = snapshot(run, project)
    except AssignmentError:
        raise
    except (ValueError, OSError, SlotError):
        raise AssignmentError(503, 'Task, review or merge-slot context cannot be read; nothing was changed.') from None
    change = check_change(payload, row, review, slot, eligible)
    result = dict(schema_version=1, operation_id=payload['operation_id'], task=payload['task'],
                  assigned_by=actor, **change)
    if not change['changed']:
        return dict(result, notice=None, comment_id=None)
    notice = make_notice(payload, project=Path(project).name, assigned_by=actor, at=at)
    run(['update', payload['task'], '--assignee', payload['assignee'] or '', '--json'])
    comment = record_json.loads(run(['comments', 'add', payload['task'], body(notice), '--json']))
    if not isinstance(comment, dict) or not comment.get('id'):
        raise ValueError('Assignment notice result cannot be reconciled')
    return dict(result, notice=notice, comment_id=str(comment['id']))


def notices(issue, project, actor, limit=5):
    """Bounded durable notices for this holder, newest native comment first.

    The native history remains complete; counts expose omitted older notices.
    A notice does not make the recipient an assignee or confer any next move.
    """
    if not isinstance(project,str) or not project:
        return {'items':[], 'total':0, 'omitted':0}
    found=[]
    for comment in reversed(issue.get('comments') or []):
        notice=parse_comment(comment,project=project,task=issue.get('id'))
        if notice is None:continue
        message=holder_message(notice,actor) if actor else None
        if message or actor is None:
            found.append(dict(comment_id=str(comment.get('id')),task=issue['id'],message=message,notice=notice))
    return {'items':found[:limit], 'total':len(found), 'omitted':max(0,len(found)-limit)}
