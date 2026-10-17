"""Strict web adapter over the unchanged native lifecycle payload.

The endpoint holds the project and configured authority locks. Its authority
callback verifies live coordinate permission; request data cannot supply it.
"""
import re

import lifecycle
import web_lifecycle_marks as marks
from requirements import content_hash


class LifecycleError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class LifecycleUncertain(RuntimeError):
    """A native event exists but its required host mark is not confirmed."""


FIELDS = {'read': {'task'},
          'scope': {'task', 'scope', 'expected_scope_token', 'evidence', 'provenance'},
          'fact': {'task', 'dimension', 'value', 'scope', 'evidence', 'provenance'}}
OPTIONAL = {'scope': {'note'}, 'fact': {'note', 'trigger'}}


def validate(body, actor=None, operation_id=None):
    if not isinstance(body, dict) or not isinstance(body.get('operation'), str):
        raise LifecycleError(422, 'Supply a lifecycle operation')
    operation = body['operation']
    required = FIELDS.get(operation)
    if (required is None or not required | {'operation'} <= set(body)
            or set(body) - required - {'operation'} - OPTIONAL.get(operation, set())):
        raise LifecycleError(422, 'Invalid fields for lifecycle ' + operation)
    if (not isinstance(body['task'], str) or len(body['task']) > 96
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', body['task'])):
        raise LifecycleError(422, 'Supply a short native task id')
    if operation == 'read':
        return operation, None
    if not isinstance(operation_id, str) or not lifecycle.LABEL.fullmatch(operation_id):
        raise LifecycleError(422, 'A stable operation identity is required for a lifecycle write')
    if operation == 'scope':
        token = body['expected_scope_token']
        if token is not None and (not isinstance(token, str) or not re.fullmatch('[a-f0-9]{64}', token)):
            raise LifecycleError(422, 'expected_scope_token must be the current token or null')
    elif body['dimension'] not in lifecycle.DIMENSIONS:
        raise LifecycleError(422, 'The web route writes only the six lifecycle facts')
    try:
        native = dict(schema_version=1, operation_id=operation_id, task=body['task'],
                      actor=actor, dimension='lifecycle-scope' if operation == 'scope' else body['dimension'],
                      value=content_hash(body['scope']) if operation == 'scope' else body['value'],
                      scope=body['scope'], evidence=body['evidence'], provenance=body['provenance'])
        for key in OPTIONAL.get(operation, ()):
            if key in body:
                native[key] = body[key]
        lifecycle.validate_payload(native)
        # Native compatibility permits historical pointer lists. Bound new HTTP
        # inputs here without changing how those old events validate or read.
        if len(native['evidence']) > 50:
            raise ValueError('At most 50 evidence pointers may be supplied')
        for pointer in native['evidence']:
            lifecycle.one_line(pointer, 'evidence pointer', 2000)
    except (ValueError, TypeError, KeyError) as error:
        raise LifecycleError(422, str(error)) from None
    return operation, native


def task_state(rows, task):
    issue = next((r for r in rows if r.get('id') == task and r.get('issue_type') != 'event'), None)
    if issue is None:
        raise LifecycleError(404, 'Lifecycle task does not exist')
    if issue.get('malformed'):
        raise LifecycleError(503, 'Lifecycle task cannot be read; an operator repairs its native row')
    from coordination import is_merge_slot
    from reserved_comments import is_record_anchor
    if is_merge_slot(issue) or is_record_anchor(issue):
        raise LifecycleError(422, 'The lifecycle route requires an ordinary native task')
    state = next((s for s in lifecycle.project_facts(rows) if s['id'] == task), None)
    if state is None:
        raise LifecycleError(503, 'Lifecycle state cannot be read')
    return issue, state


def review_state(issue, operators=None, project=None):
    import review_workflow as review
    ordered, voids, invalid, refused, positions, reverts, bad_reverts = review.history(issue, operators, project)
    state = review.projection(ordered, voids, invalid, refused, positions, reverts, bad_reverts,
                              closed=issue.get('status') == 'closed')
    return ordered, state


def approved_source(issue, scope, operators=None, owners=None, project=None, parties=None):
    """Raw standing approval and both native and authenticated party checks."""
    import review_workflow as review
    try:
        ownership = review.owner_map(owners)
    except (ValueError, OSError):
        raise LifecycleError(503, 'Ownership cannot be read; an operator repairs the session registry') from None
    ordered, state = review_state(issue, operators, project)
    contribution = state.get('contribution') or {}
    allowed, reason = review.independent_approval_state(issue, operators, ownership, project)
    if not allowed:
        raise LifecycleError(409, 'A current independent approval is required: ' + str(reason))
    commit = contribution.get('commit')
    if (not isinstance(commit, str) or not lifecycle.COMMIT.fullmatch(commit)
            or scope is None or scope.get('source_commit') != commit):
        raise LifecycleError(409, 'The full source commit must match the current approved contribution')
    entry = review.approving_entry(ordered, contribution['comment_id'])
    approver = entry[1].get('author') if entry else None
    if not callable(parties):
        raise LifecycleError(503, 'Configured web party ownership is unavailable')
    approving_party = parties(approver)
    if (not approving_party or approving_party & parties(contribution.get('author'))
            or (issue.get('assignee') and approving_party & parties(issue['assignee']))):
        raise LifecycleError(409, 'The approval must be from another party than the author and current assignee')
    return dict(id=contribution['comment_id'], commit=commit, author=contribution.get('author'),
                approver=approver, standing=True)


def held_slot(run, project, actor, task):
    from web_merge_slot import snapshot, SlotError
    try:
        slot = snapshot(run, project)
    except SlotError as error:
        raise LifecycleError(error.status, str(error)) from None
    if slot.get('holder') != actor or (slot.get('context') or {}).get('task') != task:
        raise LifecycleError(409, 'Hold the merge slot for this task before recording reviewed or integrated facts')
    return slot


def mark_admission(project, enabled, event_id=None):
    journal = marks.read_journal(project)
    if journal['invalid']:
        raise LifecycleError(503, 'Web lifecycle mark family needs operator repair')
    if event_id is not None and event_id in journal['entries']:
        return journal
    if enabled is not True:
        raise LifecycleError(503, 'Web lifecycle mark writer is OFF; an installation operator enables '
                             'web_lifecycle_mark_writes after restore readiness')
    # A complete maximum-sized entry must fit before any native effect.
    if (len(journal['entries']) >= marks.MAX_RECORDS
            or journal['bytes'] + marks.MAX_RECORD_BYTES > marks.MAX_FAMILY_BYTES):
        raise LifecycleError(503, 'Web lifecycle mark family is full; an operator reconciles its capacity')
    return journal


def read(task, actor, run, project, *, coordinate=False, mark_writes=False,
         operators=None, owners=None, parties=None):
    rows = lifecycle.read_event_rows(run)
    issue, state = task_state(rows, task)
    scope = state['scope']
    # kittrial-5bb.297, fail closed and still readable: a review chain that exists but
    # cannot be parsed may carry a hold this read cannot see, so the page ANSWERS with
    # that reason (in every affordance and hold refusal below) instead of erroring -
    # an operator reading the page is how the chain gets repaired.
    try:
        _, review = review_state(issue, operators, project)
    except ValueError as error:
        review = None
        unreadable = ('the review chain of this task cannot be read (%s), so a hold it may '
                      'carry cannot be seen; an operator repairs the chain (voids reconcile '
                      'it), and until then no fact and no integration is allowed'
                      % str(error)[:120])
    facts = {dimension: dict(fact) for dimension, fact in state['facts'].items()}
    events = {row.get('id'): row for row in rows if isinstance(row, dict) and row.get('issue_type') == 'event'}
    for dimension, fact in facts.items():
        payload = (lifecycle.native_event(events.get(fact.get('event_id'))) or {}).get('payload')
        if (payload is not None and payload['scope'] == scope and payload['dimension'] == dimension
                and payload['value'] == fact['value']):
            # The current native evidence is the source of optional read details;
            # an older scope, ambiguous event or legacy row supplies none.
            fact.update({field: payload[field] for field in ('note', 'trigger') if field in payload})
    approval = None
    reason = None
    try:
        approval = approved_source(issue, scope, operators, owners, project, parties)
        held_slot(run, project, actor, task)
    except LifecycleError as error:
        reason = str(error)
    except ValueError as error:
        # kittrial-5bb.297: the standing-approval read shares the review chain; an
        # unreadable chain is the same fail-closed reason, not a failed page.
        reason = unreadable if review is None else str(error)
    mark_reason = None
    try:
        mark_admission(project, mark_writes)
    except LifecycleError as error:
        mark_reason = str(error)
    hold_refusals = {}
    for dimension in ('reviewed', 'integrated'):
        if review is None:
            hold_refusals[dimension] = unreadable
            continue
        try:
            # The same voids and journal the canonical readers use
            # (kittrial-5bb.272 review item 1): a voided release-hold reads held here too.
            lifecycle.require_unheld_fact(issue, dimension, task, operators=operators, journal=project)
        except ValueError as error:
            hold_refusals[dimension] = str(error)
    from coordination import require_unheld_merge_task
    try:
        require_unheld_merge_task(issue, task, operators, project)
    except ValueError as error:
        hold_refusals['merge-acquire'] = str(error)
    affordances = {}
    for dimension in lifecycle.DIMENSIONS:
        why = None if coordinate else 'Current coordinate permission is required'
        if why is None and scope is None:
            why = 'Set a lifecycle scope first'
        if why is None and dimension in ('reviewed', 'integrated'):
            if review is None:
                why = unreadable
            else:
                try:
                    lifecycle.require_unheld_fact(issue, dimension, task, operators=operators, journal=project)
                except ValueError as error:
                    why = str(error)
            if why is None:
                why = reason
        affordances[dimension] = dict(allowed=why is None, reason=why)
        if dimension == 'integrated':
            affordances[dimension]['passed'] = dict(allowed=why is None and mark_reason is None,
                                                    reason=why or mark_reason)
    return dict(task=task, scope=scope, scope_token=content_hash(scope) if scope is not None else None,
                facts=facts, contribution=(review or {}).get('contribution'),
                standing_approval=approval, approval_reason=reason, hold_refusals=hold_refusals,
                scope_write=dict(allowed=coordinate, reason=None if coordinate else 'Current coordinate permission is required'),
                affordances=affordances, mark_writer_enabled=mark_writes is True)


def apply(body, actor, run, project, project_name, operation_id, *, authority,
          parties, mark_writes=False, operators=None, owners=None):
    operation, payload = validate(body, actor, operation_id)
    if operation == 'read':
        raise LifecycleError(422, 'Use the lifecycle read path')
    if not callable(authority) or authority() is not True:
        raise LifecycleError(403, 'Verified live coordinate authority is required in this project')
    rows = lifecycle.read_event_rows(run)
    issue, state = task_state(rows, body['task'])
    index = lifecycle._operation_index(rows)
    prior = index.get(operation_id)
    if prior is not None and prior[0] != payload:
        raise LifecycleError(409, 'Operation identity already names different lifecycle content')
    # Exact native retry precedes CAS: the successful scope itself moved the
    # token. The guarded HTTP identity separately binds the original CAS input.
    if prior is None:
        token = content_hash(state['scope']) if state['scope'] is not None else None
        if operation == 'scope' and body['expected_scope_token'] != token:
            raise LifecycleError(409, 'Lifecycle scope changed; read it again before writing')
        if operation == 'fact' and state['scope'] != payload['scope']:
            raise LifecycleError(409, 'Lifecycle scope changed; use the current scope')
    strict = payload['dimension'] in ('reviewed', 'integrated')
    if strict:
        try:
            # The same voids and journal the canonical readers use
            # (kittrial-5bb.272 review item 1): a voided release-hold reads held here too.
            lifecycle.require_unheld_fact(issue, payload['dimension'], body['task'],
                                          operators=operators, journal=project)
        except ValueError as error:
            raise LifecycleError(409, str(error)) from None
        approved_source(issue, payload['scope'], operators, owners, project, parties)
        held_slot(run, project, actor, body['task'])
    marked = payload['dimension'] == 'integrated' and payload['value'] == 'passed'
    if marked:
        try:
            mark_admission(project, mark_writes, prior[1] if prior else None)
        except LifecycleError as error:
            if prior:
                raise LifecycleUncertain('Native integration exists but its host mark is unconfirmed: ' + str(error)) from None
            raise
    # Re-read permission immediately before the native effect. The endpoint
    # retains both locks across the approval, scope, holder and permission checks.
    if authority() is not True:
        raise LifecycleError(403, 'Coordinate authority changed before the lifecycle write')
    # Dispatch through the shared native writer so added native policy gates
    # remain on this route too. Web authority/approval checks above are mandatory
    # regardless of the optional host author/independence switches.
    try:
        result = lifecycle.apply_native(payload, actor, run, operators=operators, journal=project)
    except ValueError as error:
        if getattr(run, 'attempted_write', None) is False:
            # Preserve the shared native refusal sentence in full. A refusal
            # after a possible write must remain uncertain in the guarded path.
            raise LifecycleError(409, str(error)) from None
        raise LifecycleUncertain('Native lifecycle effect is unconfirmed: ' + str(error)) from error
    if marked:
        try:
            fresh = lifecycle.read_event_rows(run)
            event = next((r for r in fresh if r.get('id') == result['event_id']), None)
            descriptor = dict(route=marks.route_for(project_name, body['task']),
                              capability='coordinate', project=project_name)
            journal = marks.read_journal(project)
            existing = journal['entries'].get(result['event_id']) if not journal['invalid'] else None
            if existing is not None and marks.matches(existing, project_name, event):
                mark = existing
            else:
                mark = marks.issue(project, project_name, event, descriptor, enabled=mark_writes)
            result = dict(result, mark_sha256=mark['sha256'])
        except Exception as error:
            raise LifecycleUncertain('Native integration exists but host mark publication is unconfirmed: ' + str(error)) from error
    return dict(result, task=body['task'], dimension=payload['dimension'], value=payload['value'],
                operation_id=operation_id, actor=actor, scope_token=content_hash(payload['scope']))
