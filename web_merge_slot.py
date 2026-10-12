"""Web merge-slot effects. The caller holds project and live-authority locks.

Native slot state, the existing context and guarded receipts remain authoritative.
The ordinary host coordination operations keep their payloads and answers.
"""
from pathlib import Path

import coordination
import record_json


class SlotError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


FIELDS = {'check': set(), 'acquire': {'task', 'target'}, 'release': set(),
          'force-release': {'expected_holder', 'reason'}, 'create': set()}
#: Fields an operation may carry beyond FIELDS. A force-release names the task the page saw
#: held (kittrial-5bb.268 item 4): a holder who released and took the slot again for another
#: task is a different acquisition, and a stale page must not release it. Optional so an
#: older named client (holder only) keeps working with the holder-only comparison.
OPTIONAL = {'force-release': {'expected_task'}}


def validate(payload):
    if not isinstance(payload, dict) or payload.get('operation') not in FIELDS:
        raise SlotError(422, 'Supply a merge-slot operation')
    operation = payload['operation']
    extra = set(payload) - FIELDS[operation] - {'operation'}
    if not FIELDS[operation] | {'operation'} <= set(payload) or not extra <= OPTIONAL.get(operation, set()):
        raise SlotError(422, 'Invalid fields for merge-slot ' + operation)
    if operation == 'acquire':
        if not isinstance(payload['task'], str) or len(payload['task']) > 96:
            raise SlotError(422, 'Supply a short native task id')
        try:
            coordination.identifier(payload['task'])
        except ValueError:
            raise SlotError(422, 'Supply a short native task id') from None
        text(payload['target'], 'target', 160)
    if operation == 'force-release':
        text(payload['expected_holder'], 'expected_holder', 96)
        text(payload['reason'], 'reason', 1000)
        if 'expected_task' in payload:
            if not isinstance(payload['expected_task'], str) or len(payload['expected_task']) > 96:
                raise SlotError(422, 'expected_task must be a short native task id')
            try:
                coordination.identifier(payload['expected_task'])
            except ValueError:
                raise SlotError(422, 'expected_task must be a short native task id') from None
    return operation


def text(value, field, bound):
    if not isinstance(value, str) or not value.strip() or len(value) > bound or '\0' in value:
        raise SlotError(422, field + ' must be nonblank text of at most ' + str(bound) + ' characters')


def snapshot(run, project, *, repair=False):
    """The slot as the page sees it: bd's check, the slot row and the context file.

    The endpoint calls this with the project's `.coordination.lock` held for a read too
    (about 1.1 s on real bd: two bd calls). That is deliberate (kittrial-5bb.268 item 6): the
    holder comes from bd's check and the task from `.merge-context.json`, which an acquire
    writes BEFORE bd takes the slot (coordination.apply_native). Read without the lock, a page
    could pair one acquisition's holder with another's task, and item 4's force-release
    comparison would then accept or refuse the wrong acquisition. The lock lives in
    endpoint.py, which this change leaves alone.
    """
    try:
        state = record_json.loads(run(['merge-slot', 'check', '--json']))
    except (ValueError, OSError):
        raise SlotError(503, 'The native merge slot cannot be read; ask an installation operator to inspect it.') from None
    if not isinstance(state, dict):
        raise SlotError(503, 'The native merge slot returned an unreadable state.')
    missing = coordination.merge_slot_missing(state)
    damaged = not missing and coordination.merge_slot_damaged(state)
    if not missing and (not isinstance(state.get('available'), bool)
                        or (state.get('holder') is not None and
                            (not isinstance(state['holder'], str) or len(state['holder']) > 96))):
        raise SlotError(503, 'The native merge-slot state is invalid; ask an installation operator to inspect it.')
    if not missing:
        slot_id = Path(project).name + coordination.MERGE_SLOT_SUFFIX
        try:
            rows = record_json.loads(run(['show', slot_id, '--json']))
            row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
        except (ValueError, OSError):
            raise SlotError(503, 'The native merge-slot row cannot be read; ask an installation operator to inspect it.') from None
        if not isinstance(row, dict) or row.get('id') != slot_id:
            raise SlotError(503, 'The native merge-slot row is unreadable; ask an installation operator to inspect it.')
        # bd's check can still name a holder when someone closed the row, removed
        # its label or assigned it. These disagreeing native shapes need repair.
        wanted = 'in_progress' if state.get('holder') else 'open'
        damaged = damaged or row.get('status') != wanted or bool(row.get('assignee')) \
                  or coordination.MERGE_SLOT_LABEL not in (row.get('labels') or [])
    if (missing or damaged) and not repair:
        raise SlotError(503, 'The merge slot is ' + ('missing' if missing else 'damaged')
                        + '. A signed-in owner or superuser must use the explicit create/repair action.')
    result = {'available': bool(state.get('available')), 'holder': state.get('holder'),
              'context': None, 'missing': missing, 'damaged': damaged}
    if missing:
        return result
    path = Path(project) / '.merge-context.json'
    try:
        context = coordination.load_json(path) if path.exists() else None
    except (ValueError, OSError):
        raise SlotError(503, 'Merge-slot context cannot be read; an installation operator must inspect it before retrying.') from None
    if context is not None and (not isinstance(context, dict) or set(context) != {'holder', 'task', 'target'}
                                or any(not isinstance(context[k], str) for k in context)):
        raise SlotError(503, 'Merge-slot context has an invalid shape; an installation operator must inspect it before retrying.')
    if context and context['holder'] == result['holder']:
        # An older host accepts an unbounded target. Show it with explicit omission,
        # without altering the saved context or weakening same-context acquisition.
        result['context'] = dict(holder=context['holder'], task=context['task'], target=context['target'][:160])
        if len(context['target']) > 160:
            result['context']['target_omitted_chars'] = len(context['target']) - 160
    return result


def apply(payload, actor, run, project, operation_id, *, operators=None,
          coordinator_writes=False, independent_approval=False, owners=None):
    operation = validate(payload)
    before = snapshot(run, project, repair=operation == 'create')
    if operation == 'check':
        return before
    if operation == 'force-release':
        # The project lock remains held across the comparison and bd's own atomic
        # holder comparison. A stale page cannot release the next holder's slot.
        if before['available'] or before['holder'] != payload['expected_holder']:
            raise SlotError(409, 'The merge-slot holder changed; read it again before forcing a release.')
        if 'expected_task' in payload and (before['context'] or {}).get('task') != payload['expected_task']:
            raise SlotError(409, 'The merge slot is now held for another task; read it again before forcing a release.')
        native = record_json.loads(run(['merge-slot', 'release', '--holder', payload['expected_holder'], '--json']))
    else:
        command = {'operation': 'merge-' + operation}
        if operation == 'acquire':
            command.update(task=payload['task'], target=payload['target'])
            rows = record_json.loads(run(['show', payload['task'], '--json']))
            row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
            from reserved_comments import is_record_anchor
            if not isinstance(row, dict) or coordination.is_merge_slot(row) or is_record_anchor(row):
                raise SlotError(422, 'The merge-slot task must be one ordinary native task')
        from review_workflow import CoordinatorRuleRefusal
        try:
            native = coordination.apply_native(command, actor, run, project, operators=operators,
                                               coordinator_writes=coordinator_writes,
                                               independent_approval=independent_approval,
                                               owners=owners, verified_web_authority=True)
        except CoordinatorRuleRefusal:
            # Keep the rule type for the endpoint marker and full HTTP sentence.
            raise
        except ValueError as error:
            raise SlotError(409, str(error)) from None
        if operation == 'acquire' and native.get('acquired') is not True:
            raise SlotError(409, 'The merge slot is held by ' + str(native.get('holder')))
    after = snapshot(run, project)
    return {'slot': after, 'operation': operation, 'operation_id': operation_id, 'actor': actor,
            'task_id': payload.get('task') or (before['context'] or {}).get('task'),
            'holder_before': before['holder'], 'holder_after': after['holder'],
            'reason': payload.get('reason'), 'result': native}
