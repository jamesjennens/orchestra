"""A reviewer's recommendation on a contribution: advice, never a decision (kittrial-5bb.115).

A reviewer who finds nothing to change could only approve or say nothing, and a reviewing
agent cannot approve, so its finding lived in a chat window and the person who approves
had nothing on the task to act on. A recommendation is a record that says "I reviewed
this contribution and recommend approval", with a bounded summary of what was checked and
what was not, and optional notes.

It lives BESIDE the contribution-review chain, under its own prefix, as the integration
revert record does. That is deliberate:

* A kit that predates it does not look for the prefix, so it ignores the record
  completely: its review, brief and work reads are unchanged and no chain can fail. No
  write switch is needed, and a rollback loses only the display of the recommendation.
* It never moves the chain's ``latest_comment_id``, so a recommendation cannot make the
  owner's ``approve`` stale and reviewers do not race each other.
* It never changes whether approval is allowed, never resolves or creates an item, and
  is never an approval. Nothing in ``review_workflow`` reads it.

Because it is outside the chain, every reader validates it in full: a comment with this
prefix is shown as a recommendation only when it is a valid record, names the task's
CURRENT contribution and that contribution's commit, was written after the contribution
by someone who is neither its author nor the task's assignee (the name rule of the
follow-on gate, ``review_workflow.author_key``), and no decision on that contribution was
written after it. Anything else is ignored. So a raw comment posted on a kit that did not
reserve the prefix can never display as a recommendation unless it would have been
accepted here.

There is no operator void for a recommendation: a wrong one lapses at the next decision
or revision, and the same reviewer can replace it (the newest by one actor wins).
"""
import json
import re

import record_json
import review_workflow
from requirements import canonical_bytes

PREFIX = 'Kind: review-recommendation-v1\n'
OPERATION = 'recommend'
FIELDS = {'schema_version', 'operation', 'operation_id', 'task', 'contribution', 'commit', 'verdict', 'summary',
          'items'}
#: The only verdict. A reviewer who wants changes writes request-changes; "approve with
#: notes" is this verdict with ``items``.
VERDICTS = ('approve',)
SUMMARY_MAX = 1200
ITEM_TEXT_MAX = 1000
ITEMS_MAX = 20
PAYLOAD_MAX_BYTES = 24000
#: At most this many standing recommendations are read for one contribution (the newest).
READ_MAX = 20
#: How many of the standing recommendations are returned with their whole content.
FULL_MAX = 5
COMMIT = re.compile(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})')
#: The review states in which a recommendation may be written and stands.
OPEN_STATES = ('awaiting-review',)
#: Chain operations that are a decision on a contribution: one written after a
#: recommendation makes it lapse.
DECISIONS = ('request-changes', 'approve')


def help_limits():
    """The limits for the machine-readable ``review`` help."""
    return {'recommend summary': '1..%d characters' % SUMMARY_MAX,
            'recommend items': '0..%d notes, each text <= %d characters' % (ITEMS_MAX, ITEM_TEXT_MAX),
            'recommend verdict': ' or '.join(VERDICTS),
            'recommend payload': '<= %d KB canonical bytes' % (PAYLOAD_MAX_BYTES // 1000)}


def _text(value, name, limit):
    if not isinstance(value, str):
        raise ValueError('%s: expected text (limit %d characters)' % (name, limit))
    if len(value) > limit:
        raise ValueError('%s: %d characters, the limit is %d' % (name, len(value), limit))
    if '\x00' in value or not value.strip():
        raise ValueError('%s: must not be empty (limit %d characters)' % (name, limit))


def validate(payload, task):
    """Shape of one recommendation record. Raises ValueError."""
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError('Invalid recommendation fields: expected exactly ' + ', '.join(sorted(FIELDS)))
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1 or payload['task'] != task:
        raise ValueError('Invalid recommendation version/task')
    if payload['operation'] != OPERATION:
        raise ValueError('Invalid recommendation operation')
    review_workflow.identity(task)
    review_workflow.identity(payload['operation_id'])
    review_workflow.identity(payload['contribution'])
    if not isinstance(payload['commit'], str) or not COMMIT.fullmatch(payload['commit']):
        raise ValueError('commit: require the exact 40/64 hexadecimal commit of the contribution')
    if payload['verdict'] not in VERDICTS:
        raise ValueError('verdict: expected %s (a reviewer who wants changes writes request-changes)'
                         % ' or '.join(VERDICTS))
    _text(payload['summary'], 'summary', SUMMARY_MAX)
    items = payload['items']
    if not isinstance(items, list) or len(items) > ITEMS_MAX:
        raise ValueError('items: expected a list of at most %d notes' % ITEMS_MAX)
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {'id', 'text'}:
            raise ValueError('items[%d]: expected exactly id and text' % index)
        review_workflow.identity(item['id'])
        _text(item['text'], 'items[%d] (id %s) text' % (index, item['id']), ITEM_TEXT_MAX)
        if item['id'] in seen:
            raise ValueError('items[%d]: duplicate note id %s' % (index, item['id']))
        seen.add(item['id'])
    if len(canonical_bytes(payload)) > PAYLOAD_MAX_BYTES:
        raise ValueError('Recommendation payload: %d canonical bytes, the limit is %d'
                         % (len(canonical_bytes(payload)), PAYLOAD_MAX_BYTES))
    return payload


def check_plain_text(payload):
    """The plain-text rule of review records, on the write path and again on every read."""
    review_workflow.plain_text(payload['summary'], 'summary')
    for item in payload['items']:
        review_workflow.plain_text(item['text'], 'note text')


def _records(issue):
    """Every structurally valid recommendation comment of a task, in native order.

    ``(position, payload, comment)``. A comment that carries the prefix and is not a
    valid record is skipped and counted: it is never shown and never an error for the
    task (a malformed one must not take the task's review down).
    """
    found, invalid = [], 0
    for position, comment in enumerate(issue.get('comments') or []):
        raw = comment.get('text') if isinstance(comment, dict) else None
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        try:
            payload = record_json.loads(raw[len(PREFIX):])
            validate(payload, issue.get('id'))
            if raw != PREFIX + canonical_bytes(payload).decode():
                raise ValueError('not the canonical bytes of the record')
            # The write path refuses hidden and control characters; a record that reached the
            # task another way (an older kit's raw path, a native write) is held to the same
            # rule here, so nothing is displayed that this kit would not have accepted.
            check_plain_text(payload)
            review_workflow.identity(str(comment['id']))
            if not isinstance(comment.get('author'), str) or not comment['author'].strip():
                raise ValueError('no native author')
        except (ValueError, KeyError, TypeError):
            invalid += 1
            continue
        found.append((position, payload, comment))
    return found, invalid


def _positions(issue):
    return {str(comment.get('id')): position for position, comment in enumerate(issue.get('comments') or [])
            if isinstance(comment, dict)}


def _view(payload, comment):
    return {'comment_id': str(comment['id']), 'author': comment['author'], 'timestamp': comment.get('created_at'),
            'contribution': payload['contribution'], 'commit': payload['commit'], 'verdict': payload['verdict'],
            'summary': payload['summary'], 'items': [dict(item) for item in payload['items']]}


def standing(issue, state):
    """The recommendations that stand for the task's current contribution, newest first.

    ``state`` is the task's review projection (``review_workflow.project``). A
    recommendation stands when the task is open, its review reads awaiting-review, the
    record names the current contribution and its commit, it was written after that
    contribution by someone other than its author, and no decision on the contribution
    was written after it. One per author (the newest), at most READ_MAX.

    The task's assignee is refused when the record is WRITTEN, not here: the reader
    sees only who is assigned now, and a task reassigned to someone who had already
    recommended must not lose that recommendation (kittrial-5bb.115 review).
    """
    contribution = (state or {}).get('contribution') or {}
    if not contribution.get('comment_id') or (state or {}).get('review_state') not in OPEN_STATES:
        return []
    if issue.get('status') == 'closed':
        return []
    positions = _positions(issue)
    delivered = positions.get(str(contribution['comment_id']))
    if delivered is None:
        return []
    # The position of the newest decision on this contribution, if any is in the chain.
    decided = -1
    for comment_id, position in positions.items():
        comment = issue['comments'][position]
        raw = comment.get('text') if isinstance(comment, dict) else None
        if not isinstance(raw, str) or not raw.startswith(review_workflow.PREFIX):
            continue
        try:
            record = record_json.loads(raw[len(review_workflow.PREFIX):])
        except ValueError:
            continue
        if isinstance(record, dict) and record.get('operation') in DECISIONS \
                and record.get('contribution') == contribution['comment_id']:
            decided = max(decided, position)
    excluded = {review_workflow.author_key(contribution.get('author'))} - {None, ''}
    records, _ = _records(issue)
    newest = {}
    for position, payload, comment in records:
        if payload['contribution'] != contribution['comment_id']:
            continue
        if payload['commit'].lower() != str(contribution.get('commit') or '').lower():
            continue
        if position < delivered or position < decided:
            continue
        key = review_workflow.author_key(comment['author'])
        if key in excluded:
            continue
        newest[key] = (position, payload, comment)        # native order: a later one replaces an earlier one
    ordered = sorted(newest.values(), key=lambda entry: entry[0], reverse=True)[:READ_MAX]
    return [_view(payload, comment) for _, payload, comment in ordered]


def block(issue, state):
    """The additive reader fields: the newest standing recommendation and who recommends."""
    views = standing(issue, state)
    # Every entry names who and when. The newest FULL_MAX also carry their whole content
    # (additive): the web service leaves out a recommendation by the author's own person,
    # and then shows the next one, which it could not do from a name and a time.
    brief = lambda view: {'comment_id': view['comment_id'], 'author': view['author'], 'timestamp': view['timestamp']}
    return {'recommendation': views[0] if views else None,
            'recommendations': [dict(view) if index < FULL_MAX else brief(view) for index, view in enumerate(views)],
            'recommended': bool(views)}


def execute(rows, task, actor, payload, run, operators=None, journal=None):
    """Validate and append one recommendation. Returns the receipt and the reader block.

    Exact retries (same operation id, same payload, same actor) answer ``reconciled``.
    Nothing here writes to the review chain or changes a task field.
    """
    validate(payload, task)
    review_workflow.text(actor, 'actor', 300)
    matches = [row for row in rows if row.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]
    state = review_workflow.project(issue, operators, journal)
    records, _ = _records(issue)
    for _, stored, comment in records:
        if stored['operation_id'] == payload['operation_id']:
            if stored == payload and comment['author'] == actor:
                return dict(comment_id=str(comment['id']), reconciled=True, **block(issue, state))
            raise ValueError('Operation ID already used with different payload or actor')
    check_plain_text(payload)
    if issue.get('status') == 'closed':
        raise ValueError('The task is closed; a recommendation is for a contribution that awaits review')
    contribution = state.get('contribution') or {}
    if not contribution.get('comment_id'):
        raise ValueError('The task has no contribution to recommend')
    if payload['contribution'] != contribution['comment_id']:
        raise ValueError('The recommendation names contribution %s, which is not the task\'s current contribution; '
                         're-read the review and recommend the current one' % payload['contribution'])
    if payload['commit'].lower() != str(contribution.get('commit') or '').lower():
        raise ValueError('The recommendation names a commit that is not the current contribution\'s commit; '
                         're-read the review before recommending')
    if state.get('review_state') not in OPEN_STATES:
        raise ValueError('The contribution reads %s; a recommendation is accepted only while it awaits review'
                         % state.get('review_state'))
    key = review_workflow.author_key(actor)
    if key == review_workflow.author_key(contribution.get('author')) \
            or key == review_workflow.author_key(issue.get('assignee')):
        raise ValueError('Nobody recommends their own contribution: %s is its author or the task\'s assignee'
                         % actor)
    result = json.loads(run(['comments', 'add', task, PREFIX + canonical_bytes(payload).decode(), '--json']))
    comment = {'id': result['id'], 'text': PREFIX + canonical_bytes(payload).decode(), 'author': actor,
               'created_at': result.get('created_at')}
    preview = dict(issue, comments=list(issue.get('comments') or []) + [comment])
    return dict(comment_id=str(result['id']), reconciled=False, **block(preview, state))
