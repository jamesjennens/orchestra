"""Review/contribution transport and bounded current work queues."""
import argparse
import json
import record_json
from claim_limit import actor_holds
from lifecycle import integration_evidence, project_facts
from review_state import RECONCILIATION_STATE, next_actor, active_work

CONTRACT_VERSION = 'cli-contract-v1'
WORK_STATES = ['none','awaiting-review','changes-requested','awaiting-integration','held','integrated','reconciliation-needed','legacy-review-ready','withdrawn','superseded','error']
WORK_LIMIT_MIN, WORK_LIMIT_MAX = 1, 100
WORK_OFFSET_MIN = 0

#: Issue types that are project scaffolding or records, never ordinary claimable
#: work (kittrial-5bb.6 deliverable b): an epic is a container, a decision and a
#: gate are records a person must judge, and a job is a tracker-level label rather
#: than a unit of work. `event`, `merge-slot` and the record anchors below are
#: already excluded from the queue for other reasons and stay excluded here.
CLAIMABLE_EXCLUDED_TYPES = ('epic','decision','gate','job','event','merge-slot')
# gt:slot marks a project's merge slot (coordination.MERGE_SLOT_LABEL); the type of
# that row is `task`, so the label is what keeps it out of claimable work and out of
# `ready` (kittrial-5bb.290).
MERGE_SLOT_LABEL = 'gt:slot'

#: A task carrying this LABEL is a decision even when its type is `task`
#: (reference_records.py:480 reads the label as the decision marker). It is never
#: ordinary claimable work, so the label joins the type exclusion (review item 4).
DECISION_LABEL = 'decision'

#: A task carrying one of these LABELS is a requirement RECORD (kittrial-5bb.277):
#: requirement_records.py creates it with `--type task` and the controlled type label
#: `requirement` or `brd-section` (requirement_http.py reads the BRD sections), so
#: neither the type nor the record-anchor check kept it out of the worker queue and a
#: new agent claimed many of them. A requirement record is a readable, visible row
#: that a person edits and an operator accepts -- never a unit of work -- so it is
#: never ordinary claimable work, whatever its status, assignee or contribution.
#: It is deliberately NOT added to reserved_comments.RECORD_ANCHOR_FAMILIES: it must
#: stay a visible row (kittrial-5bb.276 needs a claimed one visible and releasable).
REQUIREMENT_RECORD_LABELS = ('requirement', 'brd-section')


def requirement_record(labels):
    """True when any label marks a requirement record (kittrial-5bb.277).

    Tolerant of None and of non-string entries: a malformed label list must never
    make a requirement row look like claimable work, and must never fail the queue.
    """
    if not isinstance(labels, (list, tuple)):
        return False
    return any(isinstance(label, str) and label in REQUIREMENT_RECORD_LABELS
               for label in labels)


def drop_internal_from_ready(rows):
    """The rows `ready` may offer as work: merge slots and record anchors gone.

    kittrial-5bb.290: a lane applying the usual claim rule to `ready --json` found
    the project's merge slot (P0, label gt:slot) as the only ready row -- every
    contributor write to it is refused by design, and claiming it would jam the
    project's one slot. `work --claimable` already excludes merge slots and record
    anchors; `ready` must agree. A row is dropped when it IS a merge slot (the
    exact id / gt:slot label) or a record anchor (label + v1 record comment).
    A row that carries a record type label but was read WITHOUT comments cannot
    prove it is not an anchor, so it is marked `internal`/`claimable: false`
    rather than offered -- a client can filter on the field without knowing ids.
    Requirement records (`requirement`/`brd-section`) stay visible (kittrial-5bb.277):
    they are not record anchors and not claimable work, but they are real rows.
    """
    from reserved_comments import carries_record_label, hide_records, is_record_anchor
    from coordination import is_merge_slot
    if not isinstance(rows, list):
        return rows
    shown = []
    for row in rows:
        if not isinstance(row, dict):
            shown.append(row)
            continue
        if is_merge_slot(row):
            continue
        if is_record_anchor(row):
            continue
        if carries_record_label(row) and not isinstance(row.get('comments'), list):
            # Label alone is not anchor proof, but without comments it is not
            # disproof either: mark rather than offer (kittrial-5bb.290).
            shown.append(dict(row, internal=True, claimable=False))
            continue
        shown.append(row)
    return shown


#: The dependency types that block a claim. ``parent-child`` and ``related`` do not
#: (kittrial-5bb.293 item 3b: only the blocking type blocks).
BLOCKING_DEPENDENCY_TYPES = frozenset({'blocks', 'blocked-by', 'depends'})


def dependency_ref(dep):
    """(other_id, type, other_status) from one dependency entry, in either native shape.

    ``bd export`` writes ``depends_on_id``/``type`` and no status; ``bd show`` writes
    expanded issues keyed ``id``/``dependency_type`` that DO carry the other end's
    ``status`` (kittrial-5bb.293 rev3/rev4). All three are read so every surface
    answers the same fact, and a single-task view can see the block without a
    snapshot of the other end.
    """
    if not isinstance(dep, dict):
        return None, None, None
    other = dep.get('depends_on_id') or dep.get('id')
    kind = dep.get('type') or dep.get('dependency_type')
    status = dep.get('status') or dep.get('depends_on_status')
    return other, kind, status


def dependency_index(rows):
    """One id -> row map for a snapshot, so per-row checks do not rebuild it."""
    return {r.get('id'): r for r in rows or [] if isinstance(r, dict) and r.get('id')}


def open_dependency_ids(row, rows, index=None):
    """The ids of ``row``'s dependencies whose task is not closed (kittrial-5bb.293).

    A bd dependency (``bd dep``) whose other end is still open blocks the task: it
    must not look claimable. Only the blocking types block (kittrial-5bb.293 item 3b):
    ``parent-child`` and ``related`` do not. ``rows`` is the snapshot the caller
    already holds; ``index`` is its :func:`dependency_index` (built once per snapshot,
    item 2a). A dependency whose task is missing from the snapshot is unknown and is
    NOT counted as open (the claim can still be refused atomically).
    """
    if not isinstance(row, dict):
        return []
    by_id = index if index is not None else (dependency_index(rows) if rows is not None else None)
    open_ids = []
    for dep in row.get('dependencies') or []:
        other_id, kind, dep_status = dependency_ref(dep)
        if kind not in BLOCKING_DEPENDENCY_TYPES:
            continue
        other = by_id.get(other_id) if by_id is not None else None
        if isinstance(other, dict):
            status = other.get('status')
        elif dep_status is not None:
            status = dep_status
        else:
            continue
        if status != 'closed':
            open_ids.append(str(other_id))
    return open_ids


def open_dependency_sentence(open_ids):
    """One plain sentence naming the open dependency a claim is refused for."""
    if not open_ids:
        return None
    names = ', '.join('`%s`' % i for i in open_ids[:5])
    more = '' if len(open_ids) <= 5 else ' (and %d more)' % (len(open_ids) - 5)
    return 'Task is blocked by an open dependency: %s%s. Close it first.' % (names, more)


def claimable(issue_type, status, assignee, contribution_id, review_state, record_anchor=False,
              labels=None, open_dependencies=None):
    """One bounded interpretation of claimable work (kittrial-5bb.6 deliverable b).

    Advisory only: claimability is not a reservation, and an atomic claim can still
    be refused. The same interpretation is used by `work` (the `claimable` row field
    and `--claimable`), by the HTTP agent attention list, by the HTTP claim
    fall-through and by the owner-facing prompt, so none of them can disagree. A row
    is claimable when it is OPEN, unassigned, has no contribution (delivered work
    never looks unclaimed), its review state is none, and it is neither a record
    anchor nor an epic, decision, gate or job -- by TYPE, or by the `decision` LABEL,
    or a requirement record by the `requirement`/`brd-section` LABEL (kittrial-5bb.277),
    which is never claimable whatever its status, assignee or contribution.
    """
    if open_dependencies:
        return False
    if str(issue_type or 'task') in CLAIMABLE_EXCLUDED_TYPES:
        return False
    if record_anchor:
        return False
    if DECISION_LABEL in (labels or ()):
        return False
    # The project's merge slot is an internal record (kittrial-5bb.290): on real bd it
    # is a row of type task with the label gt:slot, so the type alone never excluded
    # it. `ready` drops it (drop_internal_from_ready); the predicate must agree.
    if MERGE_SLOT_LABEL in (labels or ()):
        return False
    if requirement_record(labels):
        return False
    if status!='open':
        return False
    if assignee:
        return False
    if contribution_id:
        return False
    return review_state in (None,'none')

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
                 '--proposal-limit', '--proposal-offset', '--capability-limit', '--capability-offset',
                 '--recent', '--last', '--kind'}

#: At most this many request ids are named per work item (a review record holds at most 20 items).
PENDING_REQUEST_IDS_MAX = 20

#: Bounded author/assignee explanation when independent approval is required.
APPROVAL_WAIT = 'Waiting for somebody else\'s approval of the current contribution.'

# Compact owned-work context, not a recipient or authorization inference.
CHECKPOINT_WAIT_ITEMS_MAX = 3

#: The four values `whose_move` takes on a queue item (kittrial-5bb.249). `'you'` is the
#: calling actor; the two middle values name the party that moves next. It answers the
#: one question the queue's review-state field left a worker to guess.
WHOSE_MOVE = ('you', 'reviewer', 'coordinator', 'unknown')
UNKNOWN_MOVE_TEXT = 'Whose move this is cannot be told from the review state shown here.'


def move_subject(caller, owner=None):
    """The actor whose move a work read answers for (kittrial-5bb.288).

    ``--owner ACTOR`` asks about ACTOR's queue, so its per-row ``whose_move``/``next``
    and its top-level ``your_move`` count are ACTOR's; ``--mine`` and an unfiltered
    scan answer for the caller (there ``next_actor`` names the role instead). This is
    the ONE place that decides it, so the count can never read 0 while a row assigned
    to the subject -- an open task with no checkpoint and no contribution by them --
    is in ``items``. It infers nothing: it only fixes whose queue is being read.
    """
    return caller if owner is None else owner


def _caller_move(state, task, owned, named_reviewer=False, pending_items=0, redelivered=False,
                 way_back=None):
    """`(whose_move, next)`: the caller-relative half of the one whose-move decision.

    Derived only from the review state the
    item already shows plus the facts the row already carries. No title, no comment
    and no actor is read as a claim about intent. The additive
    ``reconciliation-needed`` work state is NOT a review state, so there is no branch
    for it here: a reconciliation row's move stays its review state's move and
    ``next_actor`` comes from the same decision (review item 2/6 removed the branch
    that could never be taken).

    ``way_back`` is how the sentence tells the caller to read the review state. It
    defaults to the client command ``review TASK``; the HTTP next-actions answer passes
    this task's brief route instead, because a web agent cannot run the client command
    (kittrial-5bb.253 review, item 4). Every other word is the same in both answers.
    """
    name = str(task or 'the task')
    try:
        items = int(pending_items or 0)
    except (TypeError, ValueError, OverflowError):
        # An infinite count is not a number either (kittrial-5bb.264 item 5).
        raise ValueError('pending_items must be a number') from None
    items = max(0, items)
    plural = 'item' if items == 1 else 'items'
    back = '`review %s`' % name if way_back is None else way_back
    if named_reviewer and state in ('awaiting-review', 'legacy-review-ready'):
        return 'you', 'This is your review: read %s and record your decision.' % back
    if not owned:
        return 'unknown', UNKNOWN_MOVE_TEXT
    if state == 'changes-requested':
        if redelivered:
            # A revision that supersedes the one the items were filed on is already
            # delivered: only the responses are owed, so the worker does not redeliver
            # again (kittrial-5bb.249 review, item 3b).
            return 'you', ('This is your work now: %d outstanding review %s. Read %s and '
                           'respond to each item: the revision that supersedes this contribution '
                           'has already been delivered.' % (items, plural, back))
        return 'you', ('This is your work now: %d outstanding review %s. Read %s, deliver '
                       'a revision that supersedes this contribution and respond to each item.'
                       % (items, plural, back))
    if state in ('awaiting-review', 'legacy-review-ready'):
        return 'reviewer', "Nothing is for you to do: the next move is the reviewer's."
    if state in ('awaiting-integration', 'approved'):
        # `approved` is the web service's in-process name for the same state.
        return 'coordinator', "Nothing is for you to do: the next move is the coordinator's."
    if state == 'held':
        # An explicit integration hold, or a positively failed verification
        # (kittrial-5bb.10): the coordinator releases it or records a corrected
        # attestation, so this is not the author's move to guess at. Open requested
        # changes are still owed, though, so when there are any the author's move and the
        # sentence say BOTH facts (item 7) instead of "nothing is for you to do".
        if items:
            return 'you', ('This is your work now: %d outstanding review %s, and the contribution is held '
                           'from integration until the coordinator releases the hold or records a corrected '
                           'verification. Read %s and address each item.' % (items, plural, back))
        return 'coordinator', ("Nothing is for you to do: the contribution is held from integration "
                               "until the coordinator releases the hold or records a corrected "
                               "verification; read %s" % back)
    if state in ('withdrawn', 'superseded') and items:
        return 'you', ('This is your work now: %d outstanding review %s on a final contribution. '
                       'Read %s and deliver a new contribution that answers each item.'
                       % (items, plural, back))
    if state == 'integrated':
        # An integrated revision does not finish a task deliberately kept open.
        return 'you', ('This task stays open after its contribution was integrated. '
                       'Read %s; continue the task\'s remaining scope if any, otherwise checkpoint '
                       'that nothing remains for you. Deployment or verification may be the coordinator\'s move.' % back)
    if state in (None, 'none'):
        return 'you', ('This is your work: no contribution has been delivered yet. Read %s '
                       'for its current state and deliver one.' % back)
    return 'unknown', UNKNOWN_MOVE_TEXT


def move_decision(state, task, owned, named_reviewer=False, pending_items=0, redelivered=False,
                  assignee=None, closed=False, claimable=None, way_back=None):
    """THE one whose-move decision (kittrial-5bb.6 review item 2): `(move, next, next_actor)`.

    One function gives all three answers, so no reader can be told one mover by
    ``whose_move`` and another by ``next_actor``:

    * ``whose_move``/``next`` are the worker-facing, caller-relative answer;
      closed tasks excluded from active work get a history-only sentence;
    * ``next_actor`` is the caller-neutral ROLE of that same decision, computed by
      :func:`review_state.next_actor` -- the one projection the ``review`` read and
      the ``brief`` also use -- and is ``'unknown'`` wherever the decision cannot be
      told. It is never null; a malformed row reads ``operator``.

    ``assignee``, ``closed``, ``pending_items`` and ``claimable`` are the row facts
    that role needs; they are the same facts the shared projection passes, so a
    ``work`` row and the ``review`` read of the same task cannot disagree.

    ``way_back`` is the reference the sentence tells the caller to follow, passed through
    to :func:`_caller_move`; it changes no word of the decision (kittrial-5bb.253 review,
    item 4).
    """
    if not active_work('closed' if closed else 'open', state, pending_items):
        return ('unknown', 'This task is closed; its review and lifecycle records remain in history.',
                next_actor(state, assignee, closed, pending_items=pending_items, claimable=claimable))
    move, text = _caller_move(state, task, owned, named_reviewer, pending_items, redelivered,
                             way_back=way_back)
    return move, text, next_actor(state, assignee, closed, pending_items=pending_items,
                                  claimable=claimable)


def review_move(state, task, owned, named_reviewer=False, pending_items=0, redelivered=False,
                way_back=None):
    """`(whose_move, next)`: one plain sentence from the validated review state alone.

    The public two-answer shape (kittrial-5bb.249), unchanged. The decision it comes
    from is :func:`move_decision`, which yields ``next_actor`` as well.
    """
    move, text, _ = move_decision(state, task, owned, named_reviewer, pending_items, redelivered,
                                  way_back=way_back)
    return move, text


def is_redelivery(contribution_id, open_requests):
    """Whether every open review item was filed on an earlier contribution.

    A superseding redelivery that has not been answered: the current contribution
    replaces the one the items were filed on, so only the responses are owed
    (kittrial-5bb.249 review, item 3b). ONE rule, used by the ``work`` projection and
    by the in-process backend (kittrial-5bb.264 item 4).
    """
    return (bool(contribution_id) and bool(open_requests) and
            all(str(item.get('contribution') or '') != str(contribution_id)
                for item in open_requests))


def quoted_checkpoint_text(value):
    """One checkpoint line, safe inside the sentence's double quotes.

    A raw backslash is doubled first; ``briefing.plain_text`` then escapes the C0/C1
    controls (DEL included), the Unicode line/paragraph separators and the bidi controls,
    exactly as the rest of the answer does. The double quote and a newline are escaped
    last, so the quoted text cannot close the quote or start a second line that reads as
    a move of its own (kittrial-5bb.249 review, item 1).
    """
    from briefing import plain_text
    text = plain_text(str(value or '').replace('\\', '\\\\'))
    # A raw TAB is kept by plain_text on purpose (kittrial-5bb.264 item 1): escape it
    # here too, with the quote and the newline.
    return text.replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')


def checkpoint_wait_move_text(state):
    """The RAW text of the first blocker/dependency entry the caller recorded.

    ``None`` when there is no current checkpoint or it lists no blocker/dependency. The
    sentence escapes this raw text exactly once (kittrial-5bb.253 review, item 2);
    ``checkpoint_wait``'s own projection is already escaped for display, so quoting THAT
    would escape a control twice.
    """
    if not isinstance(state, dict) or state.get('invalid') or state.get('current') is None:
        return None
    payload = state['current'][0] if state['current'] else None
    found = [entry for entry in (payload or {}).get('open_items') or []
             if isinstance(entry, dict) and entry.get('kind') in ('blocker', 'dependency')]
    if not found:
        return None
    return {'text': found[0].get('text'), 'more': max(0, len(found) - 1)}


def waiting_move(blocker_text):
    """`next` for a caller whose own current checkpoint records a blocker or dependency.

    What is known is the caller's own record, never an inferred person (kittrial-5bb.219):
    the sentence says the caller recorded a blocker or dependency and quotes that blocker
    or dependency entry itself, escaped so it cannot read as a move. It does NOT quote the
    checkpoint's next action, which read as if that were the blocker (kittrial-5bb.253
    review, item 2). The move is still the caller's own: the row keeps ``whose_move``
    'you' and stays in ``your_move`` (kittrial-5bb.249 review, item 1).
    """
    text = blocker_text.get('text') if isinstance(blocker_text, dict) else blocker_text
    if not text:
        return None
    # Bound the quote and say when there is more than one entry (kittrial-5bb.264 item 7).
    more = ''
    if isinstance(blocker_text, dict) and blocker_text.get('more'):
        more = ' and %d more' % int(blocker_text['more'])
    quoted = quoted_checkpoint_text(text)
    if len(quoted) > 600:
        quoted = quoted[:600] + '...'
    return ('You recorded a blocker or dependency in your own checkpoint: "%s"%s.'
            % (quoted, more))


def checkpoint_wait(row, actor, state, delivered=False):
    """Project the caller's latest validated checkpoint from the existing parse.

    A later checkpoint by someone else makes this explicitly historical. Review
    delivery likewise keeps the authored context without turning it into work.
    No transport reads or checkpoint writes are performed here.
    """
    result = dict(status='unknown', checkpoint=None, author=None, is_current=None,
                  active=None, items=[], total=None, omitted=None, next_action=None)
    if not actor_holds(row.get('assignee'), actor):
        result.update(status='not-owned', active=False)
        return result
    if state is None or state['invalid']:
        return result
    comments = {str(c.get('id')): c for c in row.get('comments') or [] if isinstance(c, dict)}
    own = [(cid, p) for cid, p in state['history']
           if actor_holds(comments.get(str(cid), {}).get('author'), actor)]
    if not own:
        result.update(status='none', is_current=False, active=False, total=0, omitted=0)
        return result
    from briefing import clip, plain_text
    cid, p = own[-1]
    current = state['current'] is not None and str(state['current'][1]['id']) == str(cid)
    items = [item for item in p['open_items'] if item['kind'] in ('blocker', 'dependency')]
    bounded = lambda value, limit: clip(plain_text(value), limit)
    result.update(status='recorded', checkpoint=cid, author=actor, is_current=current,
                  active=current and bool(items) and not delivered and row.get('status') != 'closed',
                  items=[dict(id=item['id'], kind=item['kind'],
                              text=bounded(item['text'], 400), source=bounded(item['source'], 240))
                         for item in items[:CHECKPOINT_WAIT_ITEMS_MAX]],
                  total=len(items), omitted=max(0, len(items)-CHECKPOINT_WAIT_ITEMS_MAX),
                  next_action=bounded(p['next_action'], 600))
    return result


def help_requested(args):
    """True when args ask for help; side-effect free for every command."""
    for index, token in enumerate(args):
        if token in HELP_TOKENS and not (index and args[index - 1] in VALUE_OPTIONS):
            return True
    return False

def help_payload(action='work'):
    """Machine-readable help returned through the normal JSON envelope on exit 0."""
    usage = {
        'work': 'work [--mine | --owner ACTOR] [--state STATE] [--claimable] [--limit N] [--offset N] '
                '[--handoff-limit N] [--handoff-offset N] [--ref-limit N] [--ref-offset N] [--json]'
                ' | work lanes [--idle-minutes N] [--limit N] [--offset N] [--json]',
        'review': 'review TASK [--file payload.json]',
        'handoff': 'handoff TASK --file payload.json',
        'brief': 'brief TASK [--items-offset N] [--items-limit N] [--full] [--json]',
        'history': 'history TASK [--limit N | --recent N | --last N] [--since TIME] '
                   '[--since-checkpoint | --since-my-checkpoint] [--kind comment|event] [--cursor TOKEN] '
                   '[--body-budget BYTES]',
        'checkpoint': 'checkpoint TASK --file checkpoint.json [--json] | checkpoint TASK --provenance | checkpoint TASK --verify | checkpoint TASK --latest [--provenance] | checkpoint TASK --directions [--offset N] [--limit N]',
    }
    payload = {'schema_version': 1, 'contract': CONTRACT_VERSION, 'command': action,
               'usage': usage.get(action, action),
               'options': help_options(action),
               'exit_codes': {'0': 'result or help JSON on stdout',
                              '2': 'validation/transport error on stderr; stdout is not written'}}
    if action == 'work':
        import lanes
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
            'lanes-idle-minutes': '%d..%d (work lanes; the threshold is an argument of the read, not a '
                                  'setting)' % (lanes.IDLE_MINUTES_MIN, lanes.IDLE_MINUTES_MAX),
            'lanes-limit': '%d..%d (work lanes)' % (lanes.LANES_LIMIT_MIN, lanes.LANES_LIMIT_MAX),
        }
        payload['output'] = {
            'top_level': ['owner', 'total', 'your_move', 'items', 'next_offset', 'coverage', 'attention', 'guidance'],
            'guidance': 'guidance: the project\'s current standing guidance version (kittrial-5bb.99), '
                        'with present, version (null for unbound text), set_at, set_by (null when the audit record does not bind to '
                        'the text), repaired_by and repaired_at (only when a same-text repair kept that setter), '
                        'unbound (true when the text is withheld), previous_version, the calling '
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
            'item_move': 'items[].whose_move is you, reviewer, coordinator or unknown, relative to the '
                         'actor the queue is read for: the caller on --mine and an unfiltered scan, and '
                         'ACTOR on --owner ACTOR (kittrial-5bb.288). An open task assigned to that '
                         'actor with no checkpoint and no contribution by them is their move. '
                         'items[].next is one plain sentence saying the same in words, and '
                         'quotes the blocker or dependency entry of that actor\'s own checkpoint when '
                         'it records one (no person is inferred; the move stays theirs). '
                         'Items where the move is that actor\'s sort first; top-level your_move '
                         'carries their count for the filtered queue. Nothing is inferred from a title, a '
                         'comment or an actor name.',
            'item_select': 'items[].next_actor is the next-action actor as a ROLE (assignee, reviewer, '
                           'coordinator, operator, anyone, none, unknown) from the SAME decision that '
                           'gives items[].whose_move -- whose_move is the caller-relative answer, '
                           'next_actor the caller-neutral role of it, and next_actor is unknown wherever '
                           'whose_move cannot be told. It is deliberately separate from items[].owner '
                           '(the assignee) and is never a person, and it is never null: a malformed row '
                           'reads operator. items[].work_state is review_state, or '
                           'reconciliation-needed when the shared projection found recorded evidence that '
                           'must be reconciled first; items[].reconciliation_needed is true exactly for '
                           'those rows and items[].reconciliation_reasons name the mismatch (a lagging '
                           'lifecycle-scope commit or disagreeing integration facts) and clear nothing. '
                           'items[].claimable is the bounded claimability answer (open, unassigned, no '
                           'contribution, and not an epic, decision, gate or job by type or by the '
                           '`decision` label, nor a requirement record carrying the `requirement` or '
                           '`brd-section` label, nor a record anchor); `--claimable` lists only those '
                           'rows. items[].requirement_record says whether the row IS such a requirement '
                           'record: it stays visible, but is never claimable work (kittrial-5bb.277). '
                           'Advisory: claimability is not a reservation.',
            'item_fields': ['task', 'title', 'owner', 'status', 'review_state', 'whose_move', 'next',
                            'next_actor', 'work_state', 'reconciliation_needed', 'reconciliation_reasons',
                            'claimable', 'requirement_record', 'redelivered',
                            'contribution_id',
                            'commit', 'pending_review_items', 'pending_change_requests', 'open_items',
                            'blocking_items', 'checkpoint_at', 'newer_activity', 'checkpoint_wait',
                            'pending_handoff_requests',
                            'pending_handoff_total', 'pending_handoff_next_offset', 'lifecycle',
                            'lifecycle_scope', 'lifecycle_matches_contribution', 'error',
                            'deployed_delivery', 'deployed_delivery_is_current_contribution',
                            'workflow_state', 'integration', 'integration_disagreements',
                            'integration_warnings', 'review_request', 'review_requests',
                            'newer_activity_by_others', 'newer_activity_own',
                            'newer_activity_coverage', 'unresolved_directions',
                            'recommended', 'recommended_by', 'contribution_author'],
            'lanes': 'work lanes is the lanes board (kittrial-5bb.263, docs/LANE_BOARD_DESIGN.md): ONE '
                     'export plus the session registry, one row per lane keyed by the session\'s '
                     'registered name (a task with no lane reads as its assignee actor), bounded with '
                     'total/items/next_offset like this queue. Each holding row carries its held tasks '
                     '(id, title, review_state, whose_move, next_actor, work_state) and the declared '
                     'state/waiting_on/expect_by of the newest usable Kind: lane-state-v1 record with the '
                     'age of the checkpoint it names and the age of the lane\'s newest write anywhere; '
                     'inferred true marks a row whose state is not declared. Three flags are carried: '
                     'waiting_too_long (a waiting state older than --idle-minutes), expect_by_passed (a '
                     'declared expect_by in the past) and your_move_idle (the holder\'s own move with no '
                     'write for longer than the threshold). Lanes that hold nothing and wrote within 24 '
                     'hours are listed idle with the age of that write. --idle-minutes is an argument of '
                     'the read (default 15), not an installation setting. The contributor route shows a '
                     'caller only its own lane\'s row.',
        }
    elif action == 'review':
        payload['operations'] = ['read (review TASK)', 'contribute', 'request-changes',
                                 'respond', 'approve', 'withdraw', 'request-review',
                                 'resolve-item', 'decline-review', 'recommend',
                                 'attest', 'hold', 'release-hold']
        payload['notes'] = [
            'Contribution payloads use contribution = the contribution record comment_id, '
            'never a Git SHA and never latest_comment_id.',
            'A JSON file attachment is required for every operation except read.',
            'recommend records a reviewer\'s advice to approve the CURRENT contribution: '
            '{schema_version, operation, operation_id, task, contribution, commit, verdict: approve, '
            'summary, items}. It is never an approval, never changes whether approval is allowed, and '
            'takes no `previous`. Not for the contribution\'s author or the task\'s assignee; only while '
            'the contribution awaits review. The newest one by the same actor replaces their earlier one.',
            'A request-changes item may carry severity blocking or note (absent means blocking); '
            'only a blocking item holds up approval. Each pending/note item also carries the '
            'request-changes summary that asked for it.',
            'withdraw (author or coordinator, with reason) or request-review (naming a reviewer) '
            'is additive; older kits refuse the unknown operation rather than misreading it.',
            'Writing the new shapes (withdraw, request-review, resolve-item, decline-review, attest, '
            'hold, release-hold, an item severity, a request-changes summary) is refused unless this '
            'installation has review_workflow_writes on; readers here understand them either way. An '
            'operator turns it on with `admin.py review-writes on --actor OPERATOR`.',
            'attest records what a VERIFIER actually observed, kept separate from the declared '
            'delivery: {schema_version, operation, operation_id, task, previous, contribution, '
            'verdict: verified|failed|unknown, commit, base_commit, method, reason} plus optional '
            'sha256 and bytes. Only a listed verifier or a listed operator may attest, never the '
            'contribution\'s author, the task\'s assignee or their party. The operation itself reads '
            'no bundle, computes no hash and runs no git: on the host `admin.py attest-delivery` '
            'reads the bundle file, computes its SHA-256 and byte count, lists its heads and writes '
            'the record from what it found. A verified verdict is refused unless the observed commit '
            'is the declared delivered tip, the base is the declared base, and a bundle matches the '
            'declared SHA-256 with a positive byte count; an unreachable remote is recorded as '
            'unknown with its reason, never as a pass, and unknown does not hold.',
            'hold sets an explicit integration hold ({contribution, reason}) and release-hold '
            'clears exactly one open hold ({contribution, hold, reason}, hold = the hold record '
            'comment_id). Only a listed operator may set or release one; both are audited, and a '
            'hold survives retries and new revisions until it is explicitly released. An APPROVED '
            'contribution with an open hold (or a failed attestation) reads held, never '
            'awaiting-integration; a revision still awaiting review stays in the reviewers\' queue '
            'with the hold shown. attest, hold and release-hold are host-only: the web route '
            'refuses them.',
            'A text field over its limit is refused with the field, its length and the limit; for a '
            'review item or a resolution the error also names which one (its index and id). The limits '
            'are listed in `limits`.',
        ]
        import review_recommendations
        payload['limits'] = dict(review_workflow_limits(), **review_recommendations.help_limits())
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
            {'flag': '--mine', 'description': 'show only tasks owned by the requesting actor (malformed rows remain visible)'},
            {'flag': '--owner ACTOR', 'description': 'show only tasks owned by ACTOR (mutually exclusive with --mine; malformed rows remain visible)'},
            {'flag': '--state STATE', 'description': 'filter by the review state the row SHOWS: ' + ', '.join(WORK_STATES)
                                                      + ' (reconciliation-needed selects rows whose recorded evidence must '
                                                        'be reconciled, whatever review state they show). held means an '
                                                        'APPROVED contribution is held: an unapproved revision under a '
                                                        'hold still shows its own awaiting-review/changes-requested state, '
                                                        'with the hold shown on the row'},
            {'flag': '--claimable', 'description': 'bounded claimable-work view: open, unassigned, no contribution, '
                                                   'excluding epics, decisions, gates, jobs, tasks labelled decision, '
                                                   'requirement records (the requirement/brd-section labels) '
                                                   'and record anchors'},
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
            {'flag': '--full', 'description': 'add a bounded full_context block (title, owner, intent, acceptance, position, next action) with the characters the compact read clipped per field and the characters the block cap removed'},
            *common,
        ]
    if action == 'history':
        return [
            {'flag': 'TASK', 'description': 'task whose snapshot-bound history is paged'},
            {'flag': '--limit N', 'description': 'oldest-first entries per page 1..20 (default 5)'},
            {'flag': '--recent N / --last N', 'description': 'page the same snapshot newest-first; N 1..20 is the page size for that direction (the same option under two spellings); an empty selection answers total_entries 0'},
            {'flag': '--since TIME', 'description': 'only entries at or after this timezone-aware instant (inclusive)'},
            {'flag': '--since-checkpoint', 'description': 'only entries after the NEWEST VALID CHECKPOINT, BY ANYONE (the activity it did not incorporate); the answer names its author, time, coverage and unverified count'},
            {'flag': '--since-my-checkpoint', 'description': 'the same read based on this actor\'s own newest valid checkpoint'},
            {'flag': '--kind comment|event', 'description': 'only entries of this kind'},
            {'flag': '--cursor TOKEN', 'description': 'continue the exact snapshot page, direction, kind and checkpoint boundary'},
            {'flag': '--body-budget N', 'description': 'encoded body bytes per page 256..8000 (default 4000)'},
            *common,
        ]
    if action == 'checkpoint':
        return [
            {'flag': 'TASK', 'description': 'task the checkpoints belong to'},
            {'flag': '--file checkpoint.json', 'description': 'transport the checkpoint payload as text'},
            {'flag': '--provenance', 'description': 'read the current bounded provenance and activity cursor without writing; with --latest, return the record\'s retained digest map instead of counts'},
            {'flag': '--latest', 'description': 'read only the newest valid checkpoint record (identity, commit, branch, text, items, coverage and provenance COUNTS)'},
            {'flag': '--directions', 'description': 'read full digests for outstanding directions, including outside the stored windows'},
            {'flag': '--offset N / --limit N', 'description': '--directions page: offset >= 0, limit 1..100 (default 50)'},
            {'flag': '--verify', 'description': 'classify current entries using newest retained evidence in linked checkpoint order without writing'},
            {'flag': '--json', 'description': 'accepted in any position; the saved checkpoint is always returned as JSON'},
            {'flag': '-h, --help', 'description': 'return this help as JSON on stdout with exit code 0'},
        ]
    return common

class Parser(argparse.ArgumentParser):
    def error(self,message):
        hint=next((text for flag,text in MISTAKEN_FLAGS.items() if flag in message),None)
        raise ValueError(message+('; hint: '+hint if hint else ''))

def approval_wait(state, task, actor, assignee, author, independent_approval=False,
                  owners=None, named_reviewer=False, move_text=None):
    """Caller-relative waiting text, using the same next move as the work queue."""
    if state != 'awaiting-review' or not author:
        return None
    from review_workflow import same_party
    if independent_approval and actor and (
            same_party(actor,author,owners) or (assignee and same_party(actor,assignee,owners))):
        return APPROVAL_WAIT
    if move_text is not None:
        return move_text
    return review_move(state,task,bool(actor) and actor==assignee,named_reviewer)[1]


def workflow(issue,scopes=None,operators=None,reverts=None,journal=None,invalid_reverts=None,
             actor=None,independent_approval=False,owners=None):
    from review_state import project as reviewed
    result=reviewed(issue,scopes,operators,reverts,journal,invalid_reverts)
    # Additive (kittrial-5bb.115): the standing reviewer recommendations for the current
    # contribution. They sit beside the review chain and change nothing in it; every
    # reader (review, brief, work) gets them from this one place.
    from review_recommendations import block as recommendations
    result.update(recommendations(issue,result))
    from review_workflow import author_key
    named=bool(actor) and any(author_key(r.get('reviewer'))==author_key(actor)
                             for r in result.get('pending_review_requests') or [])
    result['approval_requires_other_party']=bool(independent_approval)
    result['waiting_for_approval']=approval_wait(result.get('review_state'),issue.get('id'),actor,
        issue.get('assignee'),(result.get('contribution') or {}).get('author'),
        independent_approval,owners,named)
    return result

def queue(rows,actor,args,request_dir=None, operators=None, reverts=None, scopes=None, journal=None,
          reference_attention=False, verifiers=None,independent_approval=False,owners=None,provenance_writes=True):
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
    parser.add_argument('--claimable',action='store_true')
    parser.add_argument('--limit',type=int,default=20);parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--handoff-limit',type=int,default=20);parser.add_argument('--handoff-offset',type=int,default=0)
    parser.add_argument('--ref-limit',type=int,default=20);parser.add_argument('--ref-offset',type=int,default=0)
    parser.add_argument('--proposal-limit',type=int,default=20);parser.add_argument('--proposal-offset',type=int,default=0)
    parser.add_argument('--capability-limit',type=int,default=20);parser.add_argument('--capability-offset',type=int,default=0)
    parser.add_argument('--json',action='store_true')  # output is always JSON; accepted for consistency
    a=parser.parse_args(args)
    from lifecycle_voids import Snapshot, snapshot
    if journal is not None and not isinstance(rows,Snapshot):
        rows=snapshot(rows,journal)
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
    # kittrial-5bb.288: the move is the QUEUE's, not the connection's. `--owner ACTOR`
    # is ACTOR's queue, so an open task assigned to ACTOR on which ACTOR never posted
    # reads `whose_move` 'you' and counts in `your_move`; `--mine` answers for the
    # caller; an unfiltered scan keeps the caller-relative answer and `next_actor`.
    subject=move_subject(actor,owner)
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
    checkpoint_states={}
    from review_state import is_integration_warning, reverts_by_task
    if reverts is None:
        revert_map,revert_problems=reverts_by_task(rows,operators,journal)
    else:
        revert_map,revert_problems=reverts,{}
    from reserved_comments import is_record_anchor
    from review_workflow import author_key
    from coordination import is_merge_slot
    dep_index=dependency_index(rows)
    malformed_named=[]
    unparseable_count=0
    for row in rows:
        if not isinstance(row,dict):
            unparseable_count+=1
            continue
        if row.get('malformed'):
            rid=row.get('id')
            if rid:
                if rid not in malformed_named:malformed_named.append(rid)
            else:
                unparseable_count+=1
                continue
            if record_json.selected(row, ['gt:slot'], ['event','gate']):continue
            if is_record_anchor(row):continue
            if a.state and a.state!='error':continue
            items.append({'open_items':None,'checkpoint_at':None,'newer_activity':None,
                          'pending_change_requests':[],
                          'task':rid,'title':str(row.get('title',''))[:200],'owner':None,
                          'status':'unknown','review_state':'error',
                          'contribution_id':None,'commit':None,'pending_review_items':0,
                          'pending_handoff_requests':[],
                          'pending_handoff_total':0,
                          'pending_handoff_next_offset':None,
                          'lifecycle':{},'lifecycle_scope':{},
                          'lifecycle_matches_contribution':None,
                          'deployed_delivery':None,
                          'deployed_delivery_is_current_contribution':None,
                          'deployed_live':'unknown',
                          'integration':None,'workflow_state':None,'error':row.get('error') or 'Malformed issue row',
                          'review_request':False,'review_requests':[],
                          'recommended':False,'recommended_by':[],
                          'integration_disagreements':[],'integration_warnings':[],
                          'newer_activity_by_others':None,'newer_activity_own':None,
                          'newer_activity_coverage':None,'unresolved_directions':None,
                          'waiting_for_approval':None,
                          'approval_requires_other_party':bool(independent_approval),
                          # A malformed row shows no validated review state, so whose move it
                          # is stays unknown rather than guessed (kittrial-5bb.249).
                          'whose_move':'unknown','next':UNKNOWN_MOVE_TEXT,
                          # A malformed row is never claimable and carries no next-action actor
                          # beyond the operator who must repair it (kittrial-5bb.6).
                          'claimable':False,'requirement_record':False,'work_state':'error','next_actor':'operator',
                          # A malformed row has no validated contribution, so it is never
                          # a redelivery (kittrial-5bb.264 item 2).
                          'redelivered':False,
                          'reconciliation_needed':False,'reconciliation_reasons':[]})
            continue
        # The merge slot is an internal record (kittrial-5bb.113): on real bd it is a
        # row of type task, so the type alone never excluded it.
        if row.get('issue_type') in ('event','gate') or is_merge_slot(row):continue
        # Record anchors (kittrial-5bb.64) are never work, whatever their status.
        if is_record_anchor(row):continue
        if not row.get('id'):
            unparseable_count+=1
            continue
        task_reverts=revert_map.get(row['id'],[])
        task_scopes=evidence.get(row['id']) if scopes is None else scopes.get(row['id'])
        try:review=workflow(row,task_scopes,operators=operators,reverts=task_reverts,journal=journal,
                            invalid_reverts=revert_problems.get(row['id']));state=review['review_state'];error=None
        except ValueError as e:review={};state='error';error=str(e)[:300]
        # The state main decided the closed-row rule and `--state` on: the validated state
        # BEFORE the journal override (kittrial-5bb.6 review item 1). The override changes
        # what the row SHOWS; it never moves the row into another queue, and it never
        # exempts a closed row from the closed-row rule.
        select_state=state
        if journal_errors:
            # A malformed handoff journal makes THIS row's review state an error, and the
            # row's other facts must say the same thing (kittrial-5bb.6 review item 3a):
            # the override happens before the selection facts are derived, so
            # work_state/next_actor never disagree with the state the row shows.
            state='error';error='Malformed handoff journal: '+json.dumps(journal_errors[:10],sort_keys=True)
            review=dict(review,work_state='error',next_actor='operator',
                        reconciliation_needed=False,reconciliation_reasons=[])
        if state=='error' and not review.get('next_actor'):
            # A malformed contribution-review chain (the ValueError above) reads error
            # too, and error is an operator's to repair: next_actor is never null
            # (kittrial-5bb.6 review item 2).
            review=dict(review,work_state='error',next_actor='operator',
                        reconciliation_needed=False,reconciliation_reasons=[])
        # The filterable selection state (kittrial-5bb.6): `reconciliation-needed` when the
        # shared projection found recorded evidence that must be reconciled, else the
        # effective review state. `review_state` itself is unchanged.
        work_state=review.get('work_state',state)
        from task_assignment import notices as assignment_notices
        notices=assignment_notices(row,journal.name if journal is not None else '',actor)
        if owner is not None and not actor_holds(row.get('assignee'), owner):

            # A first-class review request (kittrial-5bb.94 item 4) puts the task in
            # the NAMED reviewer's queue even though they are not its assignee.
            named=[request for request in (review.get('pending_review_requests') or [])
                   if author_key(request.get('reviewer'))==author_key(owner)]
            if not named and not (a.mine and notices['total']):continue
        # The named review requests OF THE SUBJECT: on `--mine` the caller's own, and on
        # `--owner ACTOR` ACTOR's (the queue that read answers for; kittrial-5bb.288), so a
        # row that is in the queue only because they were named says so and their move is
        # named (kittrial-5bb.94 item 4).
        review_requests=[{'request':request.get('request'),'reviewer':request.get('reviewer'),
                          'contribution':request.get('contribution'),'summary':request.get('summary'),
                          'author':request.get('author'),'timestamp':request.get('timestamp')}
                         for request in (review.get('pending_review_requests') or [])
                         if subject is not None and author_key(request.get('reviewer'))==author_key(subject)]
        review_request=bool(review_requests) and not (subject and actor_holds(row.get('assignee'), subject))
        # The integration overlay's warnings (kittrial-5bb.52): the owner decision
        # keeps any-pass-wins, and a host-issued revert is always surfaced, so both
        # the disagreement and the revert warning travel with the row.
        disagreements=review.get('integration_disagreements') or []
        integration_warnings=[w for w in review.get('warnings') or [] if is_integration_warning(w)]
        fact=facts.get(row['id'],{}).get('facts',{})
        # Closing a task clears review and integration queue entries (kittrial-5bb.210;
        # awaiting-review already did so under kittrial-5bb.94
        # item 1): the record stays in history and `review TASK` still reports it,
        # but a closed task is no longer offered to a reviewer. Outstanding
        # requested changes and a malformed
        # history stay visible: closure is not acceptance, and a broken chain must
        # still be surfaced. A WITHDRAWN (or superseded) contribution with a
        # blocking item still open stays visible for the same reason as
        # changes-requested: the item is actionable work although the revision is
        # final (kittrial-5bb.110 item 4).
        closed_open_item = bool(review.get('pending_requests'))
        if not active_work(row.get('status'), select_state, closed_open_item):
            continue
        if a.state:
            # `--state` keeps its meaning on main for every value that existed there: it
            # filters on the state main computed BEFORE the journal override, so a row the
            # journal turned into a shown error is still found under the state main showed
            # (kittrial-5bb.6 review item 1). Two additive cases: `error` additionally finds
            # the overridden rows, and `reconciliation-needed` selects the rows whose
            # recorded evidence must be reconciled.
            if a.state==RECONCILIATION_STATE:
                if not review.get('reconciliation_needed'):continue
            elif a.state=='error':
                if select_state!='error' and state!='error':continue
            elif a.state!=select_state:continue
        contribution=review.get('contribution') or {}
        scope=facts.get(row['id'],{}).get('scope') or {}
        # Name the delivery a passed deployed fact belongs to, and whether it is
        # the task's current contribution (a release can ship a superseded
        # revision). Additive fields (kittrial-5bb.95). A superseded release reads
        # deployed=unknown, so the delivery is None and `deployed_live` says why
        # (kittrial-5bb.107 rev2 item 2).
        deployed_scope=scope if (fact.get('deployed') or {}).get('value')=='passed' else None
        # The four values stay PLAIN STRINGS clipped to 160 characters, the shape the
        # field shipped with a version ago (rev2 item 6.1), so an oversized release_id
        # still cannot pass through unclipped.
        from briefing import clip
        deployed_delivery=None if deployed_scope is None else {key:str(deployed_scope.get(key) or '')[:160] for key in ('release_id','environment','source_commit','integration_commit')}
        deployed_current=None if (deployed_scope is None or not contribution.get('commit')) else deployed_scope.get('source_commit','').lower()==contribution['commit'].lower()
        deployed_live=facts.get(row['id'],{}).get('live','unknown')
        handoff_requests=[{'request_id':request['request_id'],'from_actor':request['from_actor'],
                           'to_actor':request['to_actor'],'requester':request['requester'],
                           'reason':request['reason']}
                          for request in journal_requests
                          if request.get('task')==row['id'] and request.get('status')=='pending']
        handoff_total=len(handoff_requests)
        handoff_requests=handoff_requests[a.handoff_offset:a.handoff_offset+a.handoff_limit]
        # (The malformed handoff journal was turned into this row's error state BEFORE the
        # selection facts above, so review_state, work_state and next_actor cannot disagree
        # -- kittrial-5bb.6 review item 3a. The closed-row rule and `--state` were decided
        # on `select_state`, the state before that override, exactly as main does -- item 1.)
        # Additive (kittrial-5bb.114): what an agent's attention read needs from this one
        # read. `open_items` is the number of open items in the task's latest valid
        # checkpoint (0 with none, None when its checkpoint history cannot be read);
        # `checkpoint_at` is when that checkpoint was written; `newer_activity` says
        # whether a comment from another native actor was written after it, so a blocked
        # task with nothing new can be left alone; `pending_change_requests` names the
        # request-changes records still unresolved.
        checkpoint_at=None;newer_activity=None
        try:
            from briefing import checkpoint_state, lane_state_record
            checkpoint_states[row['id']]=checkpoint_state(row,normalize=False)
            latest_checkpoint=checkpoint_states[row['id']]['current']
            open_items=len(latest_checkpoint[0]['open_items']) if latest_checkpoint else 0
            blocking_items=sum(item['kind'] in ('blocker','dependency')
                               for item in latest_checkpoint[0]['open_items']) if latest_checkpoint else 0
            if latest_checkpoint:
                comments=row.get('comments') or []
                position=next(index for index,comment in enumerate(comments)
                              if str(comment.get('id'))==str(latest_checkpoint[1].get('id')))
                checkpoint_at=latest_checkpoint[1].get('created_at')
                # The lane-state record written WITH this checkpoint (same author, naming this
                # checkpoint) is part of the checkpoint, not a later write by another actor: it
                # must not wake the task (kittrial-5bb.263 review). A record naming an older
                # checkpoint, or by another author, is ordinary activity and still wakes it.
                checkpoint_id=str(latest_checkpoint[1].get('id'))
                checkpoint_author=latest_checkpoint[1].get('author')
                newer_activity=any(comment.get('author')!=row.get('assignee')
                                   and not lane_state_record(comment,checkpoint_id,checkpoint_author)
                                   for comment in comments[position+1:])
        except (ValueError,TypeError,KeyError,StopIteration):
            open_items=None;blocking_items=None;checkpoint_at=None;newer_activity=None
        pending_change_requests=[]
        for pending in review.get('pending_requests',[]):
            if pending.get('request') not in pending_change_requests:pending_change_requests.append(pending.get('request'))
        pending_requests=review.get('pending_requests',[])
        pending_items=len(pending_requests)
        # A superseding redelivery that has not been answered: every open item was filed on a
        # contribution the current one replaces, so only the responses are owed
        # (kittrial-5bb.249 review, item 3b).
        contribution_id=contribution.get('comment_id')
        redelivered=is_redelivery(contribution_id, pending_requests)
        # The one bounded claimability answer for this row (kittrial-5bb.6 deliverable b).
        # It is computed before the move because next_actor needs it: an unassigned,
        # undelivered row may name `anyone` only when somebody could actually take it.
        row_open_deps=open_dependency_ids(row,rows,index=dep_index)
        row_claimable=claimable(row.get('issue_type'),row.get('status'),row.get('assignee'),
                                contribution.get('comment_id'),state,
                                record_anchor=is_record_anchor(row),labels=row.get('labels'),
                                open_dependencies=row_open_deps)
        # Whose move it is, one plain sentence saying so, and the caller-neutral ROLE of
        # that same decision -- ONE call, so a row can never name one mover and deny
        # another (kittrial-5bb.6 review item 2). Derived from the validated review state
        # this row already shows; no further read. `owned` is the SUBJECT's (kittrial-
        # 5bb.288): an open task assigned to the subject with no contribution and no
        # checkpoint by them reads `you` and counts in `your_move`.
        move,move_text,role=move_decision(state,row['id'],bool(subject) and actor_holds(row.get('assignee'), subject),
                                          named_reviewer=review_request,pending_items=pending_items,
                                          redelivered=redelivered,assignee=row.get('assignee'),
                                          closed=row.get('status')=='closed',claimable=row_claimable)
        # A standing hold (or a failed attestation) is shown BESIDE the state the row
        # already reads (kittrial-5bb.10 item 7): a revision still awaiting review stays in
        # the reviewers' queue, and an author with open requested changes is told the
        # changes are owed AND that the contribution is held, rather than "nothing is for
        # you to do". The `held` state's own sentence already says it.
        if state != 'held' and (review.get('holds') or review.get('verification') == 'failed'):
            from review_workflow import integration_hold_sentence
            hold_sentence=integration_hold_sentence(review)
            if hold_sentence and hold_sentence not in move_text:
                move_text=move_text+' Integration hold: '+hold_sentence+'.'
        items.append({**({'assignment_notices':notices} if notices['total'] else {}),
                      'open_items':open_items,'blocking_items':blocking_items,
                      'checkpoint_at':checkpoint_at,'newer_activity':newer_activity,
                      'pending_change_requests':pending_change_requests[:PENDING_REQUEST_IDS_MAX],
                      'task':row['id'],'title':str(row.get('title',''))[:200],'owner':row.get('assignee'),'status':row.get('status'),'review_state':state,
                      'whose_move':move,'next':move_text,
                      # Additive selection facts (kittrial-5bb.6), all from the one shared
                      # projection: the next-action ROLE (never the assignee field), the
                      # filterable work state, the reconciliation signal with its mismatching
                      # facts, and whether a claim could actually take this row.
                      'next_actor':role,'work_state':work_state,
                      'reconciliation_needed':bool(review.get('reconciliation_needed')),
                      'reconciliation_reasons':review.get('reconciliation_reasons') or [],
                      'claimable':row_claimable,
                      # Additive (kittrial-5bb.277): whether this row IS a requirement record,
                      # from the same labels, so the Tasks page and work list can mark it plainly.
                      # It stays a visible row; only claimability (above) changes.
                      'requirement_record':requirement_record(row.get('labels')),
                      # Additive (kittrial-5bb.253 item 1): the same fact the sentence above
                      # used, carried on the row so the HTTP agent builders can hand it to
                      # the one helper instead of re-deriving it without it.
                      'redelivered':redelivered,
                      'contribution_id':contribution.get('comment_id'),'commit':contribution.get('commit'),'pending_review_items':pending_items,
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
                      'deployed_live':deployed_live,
                      'integration':review.get('integration'),'workflow_state':review.get('workflow_state'),'error':error,
                      # Additive (kittrial-5bb.94 item 4): the caller's open review
                      # requests, and whether this row is in their queue because they
                      # were NAMED rather than because they own the task.
                      'review_request':review_request,'review_requests':review_requests,
                      # Additive (kittrial-5bb.115): a reviewer recommends approving the current
                      # contribution and nobody has decided yet; and who recommends it.
                      'recommended':bool(review.get('recommended')),
                      'recommended_by':[entry['author'] for entry in review.get('recommendations') or []],
                      # Additive (kittrial-5bb.115 review): who delivered the current contribution, so a
                      # reader that knows people (the web service) can apply its person rule to a row.
                      'contribution_author':(review.get('contribution') or {}).get('author'),
                      # Additive (kittrial-5bb.52): the integration disagreement entries
                      # naming both facts and both scopes, and their rendered warnings.
                      'integration_disagreements':disagreements,'integration_warnings':integration_warnings,
                      'approval_requires_other_party':bool(independent_approval),
                      'waiting_for_approval':approval_wait(state,row['id'],actor,row.get('assignee'),
                          contribution.get('author'),independent_approval,owners,review_request,move_text)})
        # Additive (kittrial-5bb.210 F0): the void snapshot's per-dimension reason is carried
        # on the row so a reader can say WHY a lifecycle dimension reads the way it does. Kept
        # BESIDE this lane's held state, not instead of it: a voided dimension and a hold are
        # independent facts (kittrial-5bb.10 merge-forward-onto-24c9d0f).
        reasons = {dim: state['reason'] for dim, state in fact.items() if state.get('reason')}
        if reasons:
            items[-1]['lifecycle_reasons'] = reasons
        # Additive (kittrial-5bb.10 item 7): a standing hold (or a verifier's reading) is
        # shown on the row even when the row stays in the reviewers' or the author's queue,
        # so a held revision is still reviewable AND says it is held. Present only when it
        # carries something, so a chain without one reads exactly as before.
        if review.get('holds'):
            items[-1]['holds']=[h['comment_id'] for h in review['holds']]
        if review.get('verification'):
            items[-1]['verification']=review['verification']
    from briefing import checkpoint_queue_fields
    task_rows={row['id']:row for row in rows if isinstance(row,dict) and row.get('id')}
    # The bounded claimable-work view (kittrial-5bb.6 deliverable b): filter to the rows
    # the one bounded predicate calls claimable. The page bounds below are unchanged.
    if a.claimable:
        items=[item for item in items if item['claimable']]
    # Reconcile EVERY row's checkpoint wait BEFORE the sort and the count, not only the rows
    # on this page (kittrial-5bb.249 review, item 2a): `checkpoint_states` is already parsed
    # for all rows, so the move and `your_move` are the queue's, never the page's. The
    # heavier per-row checkpoint fields stay on the displayed page below.
    for item in items:
        item['checkpoint_wait']=checkpoint_wait(task_rows.get(item['task'],{}),subject,
            checkpoint_states.get(item['task']),
            delivered=bool(item['contribution_id']) or item['review_state'] not in (None,'none'))
        # (kittrial-5bb.219 addition c, revised for kittrial-5bb.249 item 1 and kittrial-5bb.253
        # item 2) Where the SUBJECT's own current checkpoint (the caller's on --mine, the
        # owner's on --owner; kittrial-5bb.288) records a blocker or dependency, `next` quotes
        # that blocker or dependency ENTRY itself (its raw text, escaped once) and says no
        # person was inferred. The move stays the subject's: `whose_move` is not rewritten
        # and the row stays in `your_move`.
        if item['checkpoint_wait'].get('active'):
            wait_text=waiting_move(checkpoint_wait_move_text(checkpoint_states.get(item['task'])))
            if wait_text:
                item['next']=wait_text
    priority={'changes-requested':0,'error':1,'awaiting-review':2,'legacy-review-ready':2,'awaiting-integration':3,'held':3}
    # Items where the next move is the CALLER's sort first (kittrial-5bb.249); inside each
    # group the review-state order above is unchanged, so a revision request still leads
    # the caller's own undelivered work.
    items.sort(key=lambda r:(0 if r['whose_move']=='you' else 1,priority.get(r['review_state'],4),r['task']))
    coverage='Fresh current view; structured review takes precedence over legacy review-ready labels. Lifecycle facts remain independent; malformed handoff journals are surfaced as errors.'
    if malformed_named:
        coverage+=' Malformed issue rows: %s.'%(', '.join(malformed_named))
    if unparseable_count:
        coverage+=' %d unparseable issue row(s).'%unparseable_count
    result={'owner':owner,'total':len(items),'items':items[a.offset:a.offset+a.limit],'next_offset':a.offset+a.limit if a.offset+a.limit<len(items) else None,
            'coverage':coverage}
    # A closed task stays out of the actionable queue. Its durable assignment
    # notice still reaches both holders as bounded metadata beside that queue.
    if a.mine and journal is not None:
        from task_assignment import notices as assignment_notices
        closed_notices=[];closed_notice_total=0
        for row in rows:
            if (not isinstance(row,dict) or row.get('malformed') or row.get('status')!='closed'
                    or row.get('issue_type') in ('event','gate') or is_merge_slot(row) or is_record_anchor(row)):
                continue
            block=assignment_notices(row,journal.name,actor)
            closed_notices.extend(block['items']);closed_notice_total+=block['total']
        if closed_notice_total:
            kept=closed_notices[:20]
            result['assignment_notices']={'items':kept,'total':closed_notice_total,
                'omitted':max(0,closed_notice_total-len(kept)),
                'coverage':'Closed-task notices only; no task is reopened or offered for action. Read brief/history for omitted notices.'}
    # .1 adds the remaining checkpoint attention fields for the displayed page only; this is
    # separate from the review/handoff/HTTP queue fields and parses only displayed tasks.
    for item in result['items']:
        if item['task'] in task_rows:
            item.update(checkpoint_queue_fields(rows,task_rows[item['task']],checkpoint_states.get(item['task']),
                                                provenance_writes=provenance_writes))
    # The count of the QUEUE's items where the next move is the caller's, from the same
    # reconciled rows the sort used, so every page reports the same number
    # (kittrial-5bb.249 review, item 2a).
    own_move=sum(1 for item in items if item['whose_move']=='you')
    result={'owner':result['owner'],'total':result['total'],
            'your_move':{'count':own_move,'text':'%d of your tasks need your action'%own_move},
            **{key:value for key,value in result.items() if key not in ('owner','total')}}
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

def execute(path,actor,action,args,attachments,run,operators=None,verifiers=None,review_writes=None,
            independent_approval=False,coordinator_writes=False,owners=None,verified_web_authority=False,
            lanes_scope='all',provenance_writes=True):
    """``independent_approval``, ``coordinator_writes`` and ``owners`` are the slices
    6 and 7 settings and the project's actor -> principal map (kittrial-5bb.197),
    supplied by the endpoint; all default OFF/absent, so nothing changes for an
    installation that configures nothing. ``owners`` may be a callable, so the
    registry is read only when a rule needs it. ``verified_web_authority`` says the configured
    web authority verified an owner, coordinator, or agent with the project grant
    (kittrial-5bb.199, kittrial-5bb.209).

    ``lanes_scope`` is the route's lanes isolation (kittrial-5bb.263): ``'all'`` on the
    host route and the bound-key coordinator route, ``'own'`` for a bound key whose actor
    is not a configured operator, so a lane sees only its own board row. It defaults to
    ``'all'``, so every existing caller is unchanged."""
    # Help is recognised anywhere as a standalone token and never touches the
    # native export, the coordination lock or an attachment.
    if help_requested(args):
        return help_payload(action)
    if action in ('review','handoff'):
        # Structured output is already JSON; accept the flag consistently with brief/show/work.
        args=[token for token in args if token!='--json']
    if action in ('work','wait'):
        rows=record_json.loads_rows(run(['export','--all']))
        from reserved_comments import RECORD_ANCHOR_LABELS
        rows=record_json.classify(rows,run,sorted(RECORD_ANCHOR_LABELS)+['gt:slot'], ['event','gate'])
        from lifecycle import host_snapshot
        rows=host_snapshot(rows, run=run, project=path)
        if action=='wait':
            # The blocking wait's ONE read per poll (kittrial-5bb.285). The export is already
            # in hand: the worker form needs no further read, and the coordinator form's lane
            # board (kittrial-5bb.263) is derived from the SAME export, so a poll is one cheap
            # read - never an export per form and never a read per task.
            from waiting import parse_read_args as wait_read_args, worker_view, coordinator_view
            parsed=wait_read_args(args)
            if parsed.coordinator:
                from lanes import read as lanes_read, LANES_LIMIT_MAX
                from sessions import read_registry
                lanes_payload=lanes_read(rows,actor,read_registry(path),
                                         ['--idle-minutes',str(parsed.idle_minutes),
                                          '--limit',str(LANES_LIMIT_MAX)],
                                         operators=operators,journal=path,scope=lanes_scope)
                queue_payload=queue(rows,actor,['--limit',str(WORK_LIMIT_MAX)],path/'.handoff-requests',
                                    operators=operators,journal=path,reference_attention=False,
                                    verifiers=verifiers,independent_approval=independent_approval,
                                    owners=owners,provenance_writes=provenance_writes)
                return coordinator_view(queue_payload.get('items') or [],lanes_payload)
            queue_payload=queue(rows,actor,['--mine','--limit',str(WORK_LIMIT_MAX)],path/'.handoff-requests',
                                operators=operators,journal=path,reference_attention=False,
                                verifiers=verifiers,independent_approval=independent_approval,
                                owners=owners,provenance_writes=provenance_writes)
            return worker_view(queue_payload.get('items') or [])
        if args[:1]==['lanes']:
            # The lanes board (kittrial-5bb.263) reads the SAME export this action already
            # loaded, plus the project's session registry: one pass, no per-task comment
            # read. Its flags and aging live in lanes.py only. Preserve the host VOID
            # snapshot so lane eligibility uses the same classified lifecycle evidence.
            from lanes import read as lanes_read
            from sessions import read_registry
            return lanes_read(rows,actor,read_registry(path),args[1:],operators=operators,
                              journal=path,scope=lanes_scope)
        return queue(rows,actor,args,path/'.handoff-requests', operators=operators, journal=path,
                     reference_attention=True, verifiers=verifiers,
                     independent_approval=independent_approval,owners=owners,
                     provenance_writes=provenance_writes)
    if len(args) not in (1,2):raise ValueError('Use review TASK [--file payload.json] or handoff TASK --file payload.json')
    task=args[0]
    if action=='review' and len(args)==1:
        from briefing import task_row
        from lifecycle import read_event_rows
        rows=read_event_rows(run)
        from lifecycle import host_snapshot
        rows=host_snapshot(rows, run=run, project=path)
        issue=task_row(rows,task)
        from review_state import scopes_for
        return workflow(issue,scopes_for(rows,task),operators=operators,journal=path,
                        actor=actor,independent_approval=independent_approval,owners=owners)
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
    from lifecycle import read_event_rows
    rows=read_event_rows(run)
    from lifecycle import host_snapshot
    rows=host_snapshot(rows, run=run, project=path)
    if payload.get('operation')=='recommend':
        # A reviewer's recommendation (kittrial-5bb.115) is a record beside the review
        # chain: it has its own writer and never passes through the chain's.
        from review_recommendations import execute as recommend
        return recommend(rows,task,actor,payload,run,operators=operators,journal=path)
    from review_workflow import execute as review
    result=review(rows,task,actor,payload,run, operators=operators, journal=path,
                  review_writes=review_writes, independent_approval=independent_approval,
                  coordinator_writes=coordinator_writes, owners=owners, verified_web_authority=verified_web_authority,
                  verifiers=verifiers)
    if payload.get('operation')=='request-changes':
        # Retry repairs a label update interrupted after the durable review comment.
        run(['update',task,'--remove-label','review-ready','--json'])
    return result
