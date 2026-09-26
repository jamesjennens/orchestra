"""Append-only contribution delivery and actionable review requests.

The endpoint must hold the project coordination lock across execute(). Native
comment authors supply attribution; actor labels are not authentication.
"""
import json
import re
import recovery
from lifecycle import project_facts
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
    if p['operation'] == 'contribute':
        # `follows` is optional on purpose: payloads and chains written before the
        # additive follow-on relation existed must keep validating unchanged.
        expected = COMMON | EXTRA['contribute']
        if set(p) not in (expected, expected | {'follows'}):
            raise ValueError('Invalid review workflow fields')
    else:
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
        follows = p.get('follows')
        if follows is not None:
            identity(follows)
        if p['supersedes'] is not None and follows is not None:
            raise ValueError('Contribution may supersede or follow the current revision, not both')
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


def records(issue, voided=None):
    voided = set(voided or ())
    found = {}; operations = set()
    for c in issue.get('comments') or []:
        raw = c.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        cid = str(c['id'])
        if cid in voided:
            continue
        try:
            p = json.loads(raw[len(PREFIX):]); validate(p, issue['id'])
            identity(cid)
            text(c.get('author'), 'native author', 300)
            text(c.get('created_at'), 'native timestamp', 100)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('Malformed contribution-review history; operator reconciliation required '
                             '(an operator may void comment ' + cid + ' with admin.py void-record)') from exc
        if cid in found or p['operation_id'] in operations:
            raise ValueError('Duplicate workflow comment/operation ID')
        operations.add(p['operation_id']); found[cid] = (p, c)
    return chain(found, voided)


def chain(found, voided=()):
    """Link records by their explicit `previous` reference; never silently re-link."""
    voided = set(voided or ()); ordered = []; previous = None
    while found:
        children = [cid for cid, (p, _) in found.items() if p['previous'] == previous]
        if len(children) != 1:
            dangling = sorted((cid, p['previous']) for cid, (p, _) in found.items() if p['previous'] in voided)
            if dangling:
                raise ValueError('Contribution-review record(s) ' +
                                 ', '.join(cid + ' -> ' + previous_id for cid, previous_id in dangling) +
                                 ' still reference voided record(s); void those downstream records explicitly '
                                 'or deliver a revision that repairs the chain')
            raise ValueError('Conflicting or unlinked contribution-review history')
        previous = children[0]; ordered.append(found.pop(previous))
    return ordered


class StaleReviewPrevious(ValueError):
    """A new review operation named a `previous` that is no longer the chain head.

    The current head is returned with the refusal so a caller does not need a blind
    extra fetch to discover it. `latest_comment_id` and `review_state` describe the
    head at rejection time; they do not carry the chain content, so a caller must
    still re-read before deciding. `detail` is the transport-neutral structured
    error: the CLI prints it as one canonical JSON line on stderr and an HTTP
    transport returns it as the 409 body.
    """

    code = 'stale-previous'
    http_status = 409

    def __init__(self, task, supplied, latest_comment_id, review_state):
        self.task = task
        self.supplied_previous = supplied
        self.latest_comment_id = latest_comment_id
        self.review_state = review_state
        self.detail = {'code': self.code, 'http_status': self.http_status, 'task': task,
                       'supplied_previous': supplied, 'latest_comment_id': latest_comment_id,
                       'review_state': review_state}
        head = 'null (no review record exists yet)' if latest_comment_id is None else latest_comment_id
        super().__init__(
            'Stale review workflow previous; reread brief. Current latest_comment_id is '
            f'{head} and review_state is {review_state}; re-read the chain content before '
            'deciding.\n' + canonical_bytes(self.detail).decode('utf-8'))


def effective(issue, voided):
    """Individually valid non-voided records; malformed/duplicate ones are excluded."""
    found = {}; operations = set()
    for c in issue.get('comments') or []:
        raw = c.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        cid = str(c['id'])
        if cid in voided:
            continue
        try:
            p = json.loads(raw[len(PREFIX):]); validate(p, issue['id'])
            identity(cid)
            text(c.get('author'), 'native author', 300)
            text(c.get('created_at'), 'native timestamp', 100)
        except (ValueError, KeyError, TypeError):
            continue
        if p['operation_id'] in operations:
            continue
        operations.add(p['operation_id']); found[cid] = (p, c)
    return found


def protected(issue, voided, target):
    """Comment ids in the chain the surviving records currently form.

    A void may not remove one of these: recovery reconciles malformed, duplicate
    or conflicting records, it never suppresses a revision or an approval that
    the surviving records still link into place. The chain is walked greedily
    from the head and stops at the first ambiguity, so an already-unlinked
    downstream record stays voidable while the effective prefix stays protected.
    """
    surviving = effective(issue, set(voided) - {target})
    reached = []; previous = None
    while True:
        children = [cid for cid, (p, _) in surviving.items() if p['previous'] == previous]
        if len(children) != 1:
            break
        previous = children[0]; reached.append(previous)
    return set(reached)


def check_void(issue, payload, voided):
    """Whole-transition check for one operator void before any native mutation."""
    raw = recovery.target_text(issue, payload['target'])
    if not raw.startswith(PREFIX):
        raise ValueError('Operator void target is not a contribution-review record: ' + payload['target'])
    if not recovery.preserves(raw, payload):
        raise ValueError('Operator void record must preserve the exact current bytes of ' + payload['target'])
    if payload['target'] in protected(issue, voided, payload['target']):
        raise ValueError('Operator void refused for ' + payload['target'] + ': that record is part of the '
                         'contribution history the surviving records currently form; voids only reconcile '
                         'malformed, duplicate or conflicting records')


def history(issue, operators=None):
    """Apply operator voids; return (ordered, voids, invalid, refused, positions).

    A well-formed void that would remove a record the surviving history still
    forms is *refused*: it has no effect on the projection and is surfaced as a
    refused recovery instead of making every read on the task fail. `invalid`
    lists void comments that are malformed, stale, not bound to their native
    author, or authored by someone outside the server-side operator allowlist
    `operators` (the endpoint supplies it; None falls back to the host
    ORCHESTRA_OPERATORS configuration). `positions` maps comment id to native
    order so a void's position relative to an approval is observable.

    Raises when the history cannot be reconciled even after applied voids,
    including when surviving records still reference a voided revision.
    """
    voids, targets, invalid = recovery.records(issue, operators)
    applied = {}
    refused = []
    for p, c in voids:
        try:
            check_void(issue, p, set(targets))
        except ValueError as exc:
            refused.append((p, c, str(exc)))
            continue
        applied[p['target']] = (p, c)
    ordered = records(issue, set(applied))
    positions = {str(c.get('id')): i for i, c in enumerate(issue.get('comments') or [])}
    return ordered, list(applied.values()), invalid, refused, positions


def apply_void(rows, task, actor, payload, run, operator=False, operators=None):
    """Append one operator void record; operator is supplied only by the admin CLI.

    The issuing operator is bound into the record so reads can verify native
    provenance, and the issuing actor must be on the server-side operator
    allowlist the host supplies (deployment configuration or
    ORCHESTRA_OPERATORS). A payload that names a different operator than the
    issuing actor, an actor outside the allowlist, or an unconfigured allowlist
    is refused before any native mutation.
    """
    if not operator:
        raise ValueError('Operator void records are not authorized over the contributor review transport; '
                         'an operator must use admin.py void-record on the coordination host')
    text(actor, 'actor', 300)
    authority = recovery.configured_operators(operators)
    if not authority:
        raise ValueError('No operator allowlist is configured on the coordination host; add the acting '
                         'operator to deployment.private.json before recording a void')
    if actor not in authority:
        raise ValueError('Actor ' + actor + ' is not a server-side configured operator; only a configured '
                         'operator may record a void')
    if not isinstance(payload, dict):
        raise ValueError('Invalid operator void record')
    payload = dict(payload)
    payload.setdefault('operator', actor)
    if payload['operator'] != actor:
        raise ValueError('Void record operator must match the issuing actor')
    recovery.validate(payload, task)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]; voids, targets, _ = recovery.records(issue, operators)
    for p, c in voids:
        if p['operation_id'] == payload['operation_id']:
            if p == payload and c.get('author') == actor:
                return dict(comment_id=str(c['id']), reconciled=True, target=p['target'])
            raise ValueError('Void operation ID already used with different payload or actor')
        if p['target'] == payload['target']:
            raise ValueError('Another operator void record already targets ' + p['target'])
    check_void(issue, payload, set(targets))
    # Validate the whole transition before the sole native mutation.
    result = json.loads(run(['comments', 'add', task, recovery.PREFIX + canonical_bytes(payload).decode(), '--json']))
    return dict(comment_id=str(result['id']), reconciled=False, target=payload['target'])


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

def receipt(state, rows, task):
    """The shared projection for a review receipt: effective state plus raw state.

    A review write returns the SAME effective state the reads (`review TASK`,
    `brief`, `work`) report, so an approval whose scoped integration evidence was
    recorded earlier is receipted as `integrated`, not `awaiting-integration`.
    `workflow_state` and `integration` stay additive and describe how that state
    was derived.
    """
    from review_state import effective, scopes_for
    answer = effective(state, scopes_for(rows, task))
    return {key: answer[key] for key in ('review_state', 'workflow_state', 'integration')}


def projection(ordered, voids=None, invalid=None, refused=None, positions=None):
    """Project the chain. ``prior_contributions`` keeps every revision the current
    one replaced visible, tagged with its ``relation`` (``follows`` additive or
    ``supersedes``), so a follow-on never removes the prior revision's record from
    the chain. The prior revision's scoped lifecycle facts are not re-scoped: they
    stay recorded in lifecycle history under their own scope."""
    contribution = None; prior = []; pending = {}; approved = False; approved_id = None; latest = None
    for p, c in ordered:
        cid = str(c['id']); op = p['operation']
        metadata = {'comment_id': cid, 'author': c['author'], 'timestamp': c['created_at']}
        current = contribution['comment_id'] if contribution else None
        if op == 'contribute':
            follows = p.get('follows')
            if follows is not None:
                if follows != current:
                    raise ValueError('Contribution must follow the current revision')
                relation = 'follows'
            elif p['supersedes'] is not None:
                if p['supersedes'] != current:
                    raise ValueError('Contribution must explicitly supersede or follow the current revision')
                relation = 'supersedes'
            elif current is not None:
                raise ValueError('Contribution must explicitly supersede or follow the current revision')
            else:
                relation = None
            if contribution is not None:
                prior.append(dict(contribution, relation=relation))
            contribution = dict(p, **metadata); approved = False; approved_id = None
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
                approved = True; approved_id = cid
        latest = cid
    void_list = list(voids or [])
    # A void is an operator repair of a broken history. An approval recorded
    # before the void is not a fresh review of the repaired history, so it must
    # not silently become eligible for integration: a void applied after the
    # latest approval forces a fresh approval. Without native order (positions
    # unavailable) the conservative reading is that every void post-dates it.
    stale_approval = []
    if approved:
        approval_position = positions.get(approved_id) if positions else None
        for p, c in void_list:
            void_position = positions.get(str(c['id'])) if positions else None
            if approval_position is None or void_position is None or void_position > approval_position:
                stale_approval.append(p['target'])
        if stale_approval:
            approved = False
    state = ('none' if contribution is None else 'changes-requested' if pending else
             'awaiting-integration' if approved else 'awaiting-review')
    recoveries = [{'comment_id': str(c['id']), 'disposition': p['disposition'], 'target': p['target'],
                   'target_kind': p['target_kind'], 'target_sha256': p['target_sha256'],
                   'original_chars': len(p['original']), 'author': c['author'],
                   'timestamp': c['created_at'], 'reason': p['reason'], 'applied': True,
                   'invalidates_approval': p['target'] in stale_approval} for p, c in void_list]
    for p, c, refusal in (refused or []):
        recoveries.append({'comment_id': str(c['id']), 'disposition': 'refused', 'target': p['target'],
                           'target_kind': p['target_kind'], 'target_sha256': p['target_sha256'],
                           'original_chars': len(p['original']), 'author': c['author'],
                           'timestamp': c['created_at'], 'reason': p['reason'], 'applied': False,
                           'refusal': refusal})
    warnings = []
    applied_targets = [r['target'] for r in recoveries if r['applied']]
    if applied_targets:
        warnings.append('Operator void record(s) applied to: ' + ', '.join(applied_targets[:5]) +
                        ' (targets are excluded from the review chain; their original bytes remain in history)')
    refused_targets = [r['target'] for r in recoveries if not r['applied']]
    if refused_targets:
        warnings.append('Operator void record(s) refused and ignored (they would suppress the surviving '
                        'contribution history): ' + ', '.join(refused_targets[:5]))
    if stale_approval:
        warnings.append('Operator void record(s) applied after the latest approval (targets: ' +
                        ', '.join(stale_approval[:5]) + '); a fresh approval is required before integration')
    invalid = list(invalid or [])
    if invalid:
        warnings.append('Malformed or stale operator void comments ignored: ' + ', '.join(invalid[:5]))
    return dict(contribution=contribution, prior_contributions=prior, review_state=state,
                pending_requests=list(pending.values()), latest_comment_id=latest,
                recoveries=recoveries, warnings=warnings)


def project(issue, operators=None):
    ordered, voids, invalid, refused, positions = history(issue, operators)
    return projection(ordered, voids, invalid, refused, positions)


def scoped_integration(fact, commit):
    """Return the integration commit iff ``fact`` records a passed ``integrated``
    lifecycle fact scoped to ``commit``'s source revision, else ``None``."""
    scope = (fact or {}).get('scope') or {}
    if not scope or (scope.get('source_commit') or '').lower() != commit.lower():
        return None
    if ((fact or {}).get('facts') or {}).get('integrated', {}).get('value') != 'passed':
        return None
    return scope.get('integration_commit') or None


def require_integrated_follow_on(payload, state, fact):
    """Refuse an additive follow-on whose prior revision is not integrated.

    ``follows`` asserts the reviewer's base is already integrated, so the prior
    contribution must carry a passed ``integrated`` fact scoped to its own
    ``source_commit`` and the follow-on ``base_commit`` must equal that scope's
    ``integration_commit``. This is a transition rule checked in the write path
    before the sole native ``comments add``, so a refused follow-on writes nothing.
    Append-only history is never re-litigated against later lifecycle evidence, so
    read-only projections stay readable when facts move to a new scope. When no
    scoped lifecycle evidence is available at all (``fact`` is ``None``) the
    minimum rule applies: the prior must be approved with nothing pending, and
    anything else fails closed.
    """
    if payload.get('operation') != 'contribute' or payload.get('follows') is None:
        return
    prior = state.get('contribution') or {}
    if not prior:
        raise ValueError('Contribution must follow the current revision')
    if fact is None:
        if state.get('review_state') != 'awaiting-integration' or state.get('pending_requests'):
            raise ValueError('Contribution follows a revision without scoped integration evidence; '
                             'integrate the prior contribution and record its integration commit first')
        return
    integration = scoped_integration(fact, prior['commit'])
    if integration is None:
        raise ValueError('Contribution follows a revision that is not integrated; require a passed '
                         'integrated lifecycle fact scoped to the prior contribution commit before an '
                         'additive follow-on can use it as its base')
    if payload['base_commit'].lower() != integration.lower():
        raise ValueError('Contribution base_commit must equal the prior integration commit ' + integration)


def execute(rows, task, actor, payload, run, operators=None):
    """Validate, CAS and append once; return receipt and projected review state."""
    if isinstance(payload, dict) and payload.get('operation') == recovery.OPERATION:
        raise ValueError('Operator void records are not accepted over the contributor review transport; '
                         'an operator must use admin.py void-record on the coordination host')
    validate(payload, task); text(actor, 'actor', 300)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]; ordered, voids, invalid, refused, positions = history(issue, operators)
    state = projection(ordered, voids, invalid, refused, positions)
    effective_state = receipt(state, rows, task)['review_state']
    # Exact retries remain recoverable after ownership changes or later revisions.
    for p, c in ordered:
        if p['operation_id'] == payload['operation_id']:
            if p == payload and c['author'] == actor:
                return dict(comment_id=str(c['id']), reconciled=True, **receipt(state, rows, task))
            raise ValueError('Operation ID already used with different payload or actor')
    voided_operations = _voided_operation_ids(issue, voids)
    if payload['operation_id'] in voided_operations:
        raise ValueError('Operation ID ' + payload['operation_id'] + ' belongs to a voided contribution-review '
                         'record; operator recovery removed that revision, so retry it as a new operation')
    if payload['previous'] != state['latest_comment_id']:
        raise StaleReviewPrevious(task, payload['previous'],
                                  state['latest_comment_id'], effective_state)
    if payload['operation'] in ('contribute', 'respond') and (not issue.get('assignee') or actor != issue['assignee']):
        raise ValueError('Only the current assigned owner may contribute/respond; resume or handoff first')
    if payload['operation'] == 'request-changes' and issue.get('status') == 'closed':
        raise ValueError('Reopen the closed task explicitly before requesting changes')
    # Validate the entire transition before the sole native mutation.
    preview_positions = dict(positions)
    preview_positions['pending-write'] = len(issue.get('comments') or [])
    preview = projection(ordered + [(payload, {'id': 'pending-write', 'author': actor, 'created_at': 'pending'})],
                         voids, invalid, refused, preview_positions)
    # A follow-on may only base itself on an integrated prior revision. Resolve the
    # scoped lifecycle evidence here, still before the sole native mutation, so the
    # refusal happens with zero writes.
    if payload['operation'] == 'contribute' and payload.get('follows') is not None:
        fact = next((r for r in project_facts(rows) if r.get('id') == task), None)
        require_integrated_follow_on(payload, state, fact)
    result = json.loads(run(['comments', 'add', task, PREFIX + canonical_bytes(payload).decode(), '--json']))
    return dict(comment_id=str(result['id']), reconciled=False, **receipt(preview, rows, task))


def _voided_operation_ids(issue, voids):
    """Operation ids of review records that an operator void removed."""
    found = []
    for p, _ in voids:
        raw = recovery.target_text(issue, p['target'])
        if not raw.startswith(PREFIX):
            continue
        try:
            body = json.loads(raw[len(PREFIX):])
        except (ValueError, TypeError):
            continue
        if isinstance(body, dict) and isinstance(body.get('operation_id'), str):
            found.append(body['operation_id'])
    return found
