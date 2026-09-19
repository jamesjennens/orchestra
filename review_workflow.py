"""Append-only contribution delivery and actionable review requests.

The endpoint must hold the project coordination lock across execute(). Native
comment authors supply attribution; actor labels are not authentication.
"""
import json
import re
from requirements import canonical_bytes

PREFIX = 'Kind: contribution-review-v1\n'
COMMON = {'schema_version', 'operation', 'operation_id', 'task', 'previous'}
EXTRA = {
    'contribute': {'repository', 'commit', 'base_commit', 'delivery', 'summary', 'supersedes'},
    'request-changes': {'contribution', 'items'},
    'respond': {'contribution', 'resolutions'},
    'approve': {'contribution', 'summary'},
}


def text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}: expected nonempty text up to {limit} characters')


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', value):
        raise ValueError('Invalid workflow ID')


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Invalid review workflow fields')


def validate(p, task):
    if not isinstance(p, dict) or not isinstance(p.get('operation'), str) or p['operation'] not in EXTRA:
        raise ValueError('Invalid review workflow operation')
    fields(p, COMMON | EXTRA[p['operation']])
    if type(p['schema_version']) is not int or p['schema_version'] != 1 or p['task'] != task:
        raise ValueError('Invalid review workflow version/task')
    identity(task); identity(p['operation_id'])
    if p['previous'] is not None:
        identity(p['previous'])
    op = p['operation']
    if op == 'contribute':
        text(p['repository'], 'repository', 1000); text(p['summary'], 'summary', 1200)
        for key in ('commit', 'base_commit'):
            if not isinstance(p[key], str) or not re.fullmatch(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})', p[key]):
                raise ValueError(f'{key}: require exact 40/64 hexadecimal commit')
        if p['supersedes'] is not None:
            identity(p['supersedes'])
        d = p['delivery']
        if not isinstance(d, dict):
            raise ValueError('Invalid delivery')
        if d.get('kind') == 'remote':
            fields(d, {'kind', 'remote', 'branch'})
            text(d['remote'], 'remote', 1000); text(d['branch'], 'branch', 300)
        elif d.get('kind') == 'bundle':
            fields(d, {'kind', 'path', 'sha256'})
            text(d['path'], 'bundle path', 1000)
            if not isinstance(d['sha256'], str) or not re.fullmatch(r'[a-fA-F0-9]{64}', d['sha256']):
                raise ValueError('Require bundle SHA256')
        else:
            raise ValueError('Delivery must be remote or bundle')
    else:
        identity(p['contribution'])
        if op == 'approve':
            text(p['summary'], 'summary', 1200)
        else:
            values = p['items'] if op == 'request-changes' else p['resolutions']
            if not isinstance(values, list) or not 1 <= len(values) <= 20:
                raise ValueError('Require 1..20 review items')
            seen = set()
            for item in values:
                if op == 'request-changes':
                    fields(item, {'id', 'text'}); identity(item['id']); text(item['text'], 'review text', 1000)
                    key = item['id']
                else:
                    fields(item, {'request', 'item', 'reason', 'evidence'})
                    identity(item['request']); identity(item['item'])
                    text(item['reason'], 'resolution reason', 1000); text(item['evidence'], 'resolution evidence', 1000)
                    key = (item['request'], item['item'])
                if key in seen:
                    raise ValueError('Duplicate review item')
                seen.add(key)
    if len(canonical_bytes(p)) > 24000:
        raise ValueError('Review workflow payload exceeds 24 KB')


def records(issue):
    found = {}; operations = set()
    for c in issue.get('comments') or []:
        raw = c.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        try:
            p = json.loads(raw[len(PREFIX):]); validate(p, issue['id'])
            cid = str(c['id']); identity(cid)
            text(c.get('author'), 'native author', 300)
            text(c.get('created_at'), 'native timestamp', 100)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('Malformed contribution-review history; operator reconciliation required') from exc
        if cid in found or p['operation_id'] in operations:
            raise ValueError('Duplicate workflow comment/operation ID')
        operations.add(p['operation_id']); found[cid] = (p, c)
    ordered = []; previous = None
    while found:
        children = [cid for cid, (p, _) in found.items() if p['previous'] == previous]
        if len(children) != 1:
            raise ValueError('Conflicting or unlinked contribution-review history')
        previous = children[0]; ordered.append(found.pop(previous))
    return ordered


def describe_contribution_mismatch(op, supplied, current, latest):
    """Actionable refusal for a review operation that names the wrong revision.

    A caller confusing a task's `latest_comment_id` with the contribution
    record's `comment_id` must be told which id is which; the older
    "must reference current contribution revision" text did not distinguish
    "no contribution exists yet" from "this id is the latest comment" from
    "this id belongs to an older revision".
    """
    expected = (f'the current contribution id is {current}' if current
                else 'no current contribution has been recorded yet')
    # `latest` is the newest record in the chain, and may itself be the operation
    # being validated, so only trust it when it is a distinct earlier record.
    if supplied and latest and latest == supplied and latest != current:
        return (f'Review operation {op} supplied {supplied}, which is the task latest comment id; '
                f'reference the current contribution instead ({expected}).')
    return f'Review operation {op} supplied {supplied}, but {expected}.'

def projection(ordered):
    contribution = None; pending = {}; approved = False; latest = None
    for p, c in ordered:
        cid = str(c['id']); op = p['operation']
        metadata = {'comment_id': cid, 'author': c['author'], 'timestamp': c['created_at']}
        current = contribution['comment_id'] if contribution else None
        if op == 'contribute':
            if p['supersedes'] != current:
                raise ValueError('Contribution must explicitly supersede the current revision')
            contribution = dict(p, **metadata); approved = False
        else:
            if not current or p['contribution'] != current:
                raise ValueError(describe_contribution_mismatch(op, p['contribution'], current, latest))
            if op == 'request-changes':
                for item in p['items']:
                    pending[(cid, item['id'])] = dict(request=cid, item=item['id'], text=item['text'],
                        contribution=current, author=c['author'], timestamp=c['created_at'])
                if len(pending) > 20:
                    raise ValueError('At most 20 unresolved review items are allowed')
                approved = False
            elif op == 'respond':
                for item in p['resolutions']:
                    key = (item['request'], item['item'])
                    if key not in pending:
                        raise ValueError('Resolution must reference an unresolved request/item')
                    del pending[key]
                approved = False
            elif op == 'approve':
                if pending:
                    raise ValueError('Cannot approve while review requests remain unresolved')
                approved = True
        latest = cid
    state = ('none' if contribution is None else 'changes-requested' if pending else
             'awaiting-integration' if approved else 'awaiting-review')
    return dict(contribution=contribution, review_state=state,
                pending_requests=list(pending.values()), latest_comment_id=latest, warnings=[])


def project(issue):
    return projection(records(issue))


def execute(rows, task, actor, payload, run):
    """Validate, CAS and append once; return receipt and projected review state."""
    validate(payload, task); text(actor, 'actor', 300)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]; ordered = records(issue); state = projection(ordered)
    # Exact retries remain recoverable after ownership changes or later revisions.
    for p, c in ordered:
        if p['operation_id'] == payload['operation_id']:
            if p == payload and c['author'] == actor:
                return dict(comment_id=str(c['id']), reconciled=True, review_state=state['review_state'])
            raise ValueError('Operation ID already used with different payload or actor')
    if payload['previous'] != state['latest_comment_id']:
        raise ValueError('Stale review workflow previous; reread brief')
    if payload['operation'] in ('contribute', 'respond') and (not issue.get('assignee') or actor != issue['assignee']):
        raise ValueError('Only the current assigned owner may contribute/respond; resume or handoff first')
    if payload['operation'] == 'request-changes' and issue.get('status') == 'closed':
        raise ValueError('Reopen the closed task explicitly before requesting changes')
    # Validate the entire transition before the sole native mutation.
    preview = projection(ordered + [(payload, {'id': 'pending-write', 'author': actor, 'created_at': 'pending'})])
    result = json.loads(run(['comments', 'add', task, PREFIX + canonical_bytes(payload).decode(), '--json']))
    return dict(comment_id=str(result['id']), reconciled=False, review_state=preview['review_state'])
