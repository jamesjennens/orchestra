"""Append-only contribution delivery and actionable review requests.

The endpoint must hold the project coordination lock across execute(). Native
comment authors supply attribution; actor labels are not authentication, so the
follow-on gate compares normalised attribution keys (case-folded, with any
``/``-namespace suffix dropped) and judges the assignee as it was when the
approval was recorded: ``execute`` stamps the optional ``assignee_at_approval``
snapshot on an ``approve`` record. A record with no usable snapshot -- absent
because it was written before the field existed, or an explicit null written while
the task was unassigned -- falls back to the current assignee, which is the
pre-snapshot fail-closed reading.

Rollback compatibility: the snapshot is additive for readers, not for writers. A
kit built before the field existed validates an ``approve`` record against the
exact pre-snapshot field set, so it treats a record that carries
``assignee_at_approval`` as malformed and refuses the whole chain ("Malformed
contribution-review history; operator reconciliation required"). Deploying this
change and later rolling the kit back is therefore safe only while no approve
record with the snapshot has been written; once one exists the older kit cannot
read that task until an operator voids the affected record or the tolerant reader
is restored. ``docs/REVIEWS.md`` records the hazard, the operator remedy and the
staged alternative (a tolerant reader in one release, the writer in the next).
See also ``ASSIGNEE_SNAPSHOT`` below.

Revert records (``Kind: integration-revert-v1``) are deliberately a SEPARATE
record kind, not a sixth contribution-review operation. They are written only by
the operator CLI (``admin.py revert-record``), never over the contributor review
transport, and no existing reader looks for their prefix, so an old kit ignores
them completely: reads keep working and no chain validation can fail. That is
rollback-safe by construction, with one documented limit -- an old kit cannot
SEE that a contribution was reverted (fail-open, not fail-closed). See
``docs/REVIEWS.md``.

A revert comment alone is NOT authority. On the SSH/endpoint path the stored
native author is the self-declared request actor, so until this prefix was
reserved on the contributor transport a contributor could post a body that read
as an operator-authored revert, and a forged record stayed honoured after the
kit was rolled forward again (kittrial-5bb.52 review item
``forged-pre-deploy-reverts``). The fix is the host-issued revert journal: while
``revert-record`` holds the project coordination lock it also writes one entry
per revert to ``<project>/.integration-reverts/<sha256>.json``, and
``revert_records`` honours a native revert comment ONLY when its matching entry
is present. A contributor can post a comment; only a process holding the lock on
the coordination host can write the journal, so a forged or rolled-back record
with no entry is ignored (fail-closed). The journal path is whitelisted, backed
up and validated with the other coordination journals (``admin.py``), and the
required deployment order is documented in ``docs/REVIEWS.md``.
"""
import json
import re
from pathlib import Path
import recovery
from requirements import canonical_bytes, content_hash

PREFIX = 'Kind: contribution-review-v1\n'
COMMON = {'schema_version', 'operation', 'operation_id', 'task', 'previous'}
EXTRA = {
    'contribute': {'repository', 'commit', 'base_commit', 'delivery', 'summary', 'supersedes'},
    'request-changes': {'contribution', 'items'},
    'respond': {'contribution', 'resolutions'},
    'approve': {'contribution', 'summary'},
}
# Optional additive field on an ``approve`` record: the task assignee at the moment
# the approval was written. Stamped server-side by ``execute``; approve records
# written before this field existed keep validating without it, and a null value is
# treated like a missing one (fall back to the current assignee). Adding the field
# is not rollback-safe: a kit that predates it validates the approve field set
# exactly and refuses a record that carries it. See the module docstring and
# docs/REVIEWS.md.
ASSIGNEE_SNAPSHOT = 'assignee_at_approval'
# The audited revert record. A separate reserved kind (see the module docstring),
# written only by the operator CLI; ``issued_revert`` is its exact schema and
# ``revert_records`` its tolerant reader.
REVERT_PREFIX = 'Kind: integration-revert-v1\n'
REVERT_OPERATION = 'revert-record'
REVERT_FIELDS = {'schema_version', 'operation', 'operation_id', 'task', 'contribution',
                 'integration_commit', 'revert_commit', 'reason',
                 'evidence', 'operator'}
REVERT_REQUIRED_FIELDS = REVERT_FIELDS - {'evidence'}
REVERT_REASON_LIMIT = 1000
REVERT_EVIDENCE_LIMIT = 8
REVERT_BYTE_LIMIT = 24000
#: Host-issued revert journal inside one project directory. ``revert-record``
#: writes one entry per revert (and one per retraction) under the project lock it
#: already holds; ``revert_records`` honours a native revert comment only when its
#: matching entry is present. See the module docstring and ``docs/REVIEWS.md``.
JOURNAL_DIR = '.integration-reverts'
JOURNAL_REVERT = 'integration-revert'
JOURNAL_RETRACTION = 'integration-revert-retraction'
JOURNAL_FIELDS = {
    JOURNAL_REVERT: frozenset({'schema_version', 'kind', 'task', 'contribution',
                               'integration_commit', 'revert_commit', 'operation_id',
                               'operator', 'actor', 'comment_id', 'payload', 'sha256'}),
    JOURNAL_RETRACTION: frozenset({'schema_version', 'kind', 'task',
                                   'target_revert_comment_id', 'operation_id', 'operator',
                                   'actor', 'comment_id', 'payload', 'sha256'}),
}
#: Bound on one journal file, so a planted giant file cannot be read into memory.
JOURNAL_FILE_LIMIT = 256000
COMMIT_TEXT = re.compile(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})')


def text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}: expected nonempty text up to {limit} characters')


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', value):
        raise ValueError('Invalid workflow ID')


def author_key(value):
    """Normalised attribution key for a native author or an assignee label.

    Native authors are attribution, not authentication (see the module docstring):
    the transport has no identity system, so ``Worker``/``worker`` and
    ``worker/sub``/``worker`` name the same attributing principal. The follow-on
    gate applies this key to both sides of its author and assignee comparisons.

    Only case and the ``/``-namespace suffix are normalised. ``worker@host``,
    ``worker-2`` and ``worker.`` stay distinct from ``worker``, while
    ``team/alice`` and ``team/bob`` both reduce to ``team`` and are therefore
    treated as one author. Those extra refusals are deliberate, they apply on the
    HTTP path too (where the author is the authenticated principal), and they
    change nothing about the comparison being attribution rather than
    authentication.
    """
    if not isinstance(value, str):
        return value
    return value.strip().casefold().partition('/')[0]


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
        expected = COMMON | EXTRA[p['operation']]
        if p['operation'] == 'approve':
            # The assignee snapshot is additive: approve records written before it
            # existed keep validating, and the server stamps it before the append.
            if set(p) not in (expected, expected | {ASSIGNEE_SNAPSHOT}):
                raise ValueError('Invalid review workflow fields')
        else:
            fields(p, expected)
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
            snapshot = p.get(ASSIGNEE_SNAPSHOT)
            if snapshot is not None:
                text(snapshot, 'assignee snapshot', 300)
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
    """Whole-transition check for one operator void before any native mutation.

    Two target kinds are voidable. A contribution-review record is voided to
    reconcile a malformed, duplicate or conflicting one, and is refused when the
    surviving records still link it into the contribution history. An
    ``integration-revert`` record is voided to RETRACT an operator revert
    (kittrial-5bb.52 review item ``retraction-and-same-commit``); a revert is not
    part of the contribution chain, so it is never protected by it, and the void
    only has to preserve its exact bytes. ``apply_void`` additionally requires the
    retraction to be host-journaled, so a void can only retract a revert the host
    itself issued.
    """
    raw = recovery.target_text(issue, payload['target'])
    if payload['target_kind'] == 'integration-revert':
        if not raw.startswith(REVERT_PREFIX):
            raise ValueError('Operator void target is not an integration revert record: ' + payload['target'])
        if not recovery.preserves(raw, payload):
            raise ValueError('Operator void record must preserve the exact current bytes of ' + payload['target'])
        return
    if not raw.startswith(PREFIX):
        raise ValueError('Operator void target is not a contribution-review record: ' + payload['target'])
    if not recovery.preserves(raw, payload):
        raise ValueError('Operator void record must preserve the exact current bytes of ' + payload['target'])
    if payload['target'] in protected(issue, voided, payload['target']):
        raise ValueError('Operator void refused for ' + payload['target'] + ': that record is part of the '
                         'contribution history the surviving records currently form; voids only reconcile '
                         'malformed, duplicate or conflicting records')


def history(issue, operators=None, journal=None, reverts=None, invalid_reverts=None):
    """Apply operator voids; return the ordered chain and every derived list.

    A well-formed void that would remove a record the surviving history still
    forms is *refused*: it has no effect on the projection and is surfaced as a
    refused recovery instead of making every read on the task fail. `invalid`
    lists void comments that are malformed, stale, not bound to their native
    author, or authored by someone outside the server-side operator allowlist
    `operators` (the endpoint supplies it; None falls back to the host
    ORCHESTRA_OPERATORS configuration). `positions` maps comment id to native
    order so a void's position relative to an approval is observable.

    `reverts` are the separately-prefixed, operator-audited integration revert
    records that the HOST ISSUED (see ``revert_records``: a matching entry in the
    ``.integration-reverts/`` journal under `journal` is required, and a
    host-journaled retraction removes one). They are NOT part of the contribution
    chain and never affect its linking; an invalid one is ignored and returned in
    `invalid_reverts` rather than raising, so every read can surface it. A caller
    that already read them once per page (``work.queue``) passes `reverts` (and
    `invalid_reverts`) so the scan is not repeated.

    Raises when the history cannot be reconciled even after applied voids,
    including when surviving records still reference a voided revision.
    """
    voids, targets, invalid = recovery.records(issue, operators)
    # A valid void of a keyed record (kittrial-5bb.74) belongs to the anchor's kind
    # (keyed_entries), which applies or reports it; it is no part of a review history.
    voids = [(p, c) for p, c in voids if p['target_kind'] in recovery.REVIEW_KIND_PREFIXES]
    targets = {target: entry for target, entry in targets.items()
               if entry[0]['target_kind'] in recovery.REVIEW_KIND_PREFIXES}
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
    if reverts is None:
        reverts, invalid_reverts = revert_records(issue, operators, journal,
                                                  voids=list(applied.values()))
    else:
        reverts = list(reverts)
        invalid_reverts = list(invalid_reverts or [])
    return (ordered, list(applied.values()), invalid, refused, positions, reverts,
            invalid_reverts)


def apply_void(rows, task, actor, payload, run, operator=False, operators=None, journal=None):
    """Append one operator void record; operator is supplied only by the admin CLI.

    The issuing operator is bound into the record so reads can verify native
    provenance, and the issuing actor must be on the server-side operator
    allowlist the host supplies (deployment configuration or
    ORCHESTRA_OPERATORS). A payload that names a different operator than the
    issuing actor, an actor outside the allowlist, or an unconfigured allowlist
    is refused before any native mutation.

    A void whose target kind is ``integration-revert`` RETRACTS that revert
    (kittrial-5bb.52 review item ``retraction-and-same-commit``). The retraction is
    host-issued too: the target must already be a host-journaled revert
    (``host_revert_target``), the journal directory must be usable BEFORE the
    native write, and the retraction entry is persisted after it. An interrupted
    retraction is completed by re-running the same operation id.
    """
    if not operator:
        raise ValueError('Operator void records are not authorized over the contributor review transport; '
                         'an operator must use admin.py void-record on the coordination host')
    recovery.authorize(actor, operators)
    payload = recovery.bound(payload, actor, task)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]; voids, targets, _ = recovery.records(issue, operators)
    prior = recovery.earlier(voids, payload, actor)
    if prior is not None:
        p, c = prior
        persisted_retraction(issue, p, str(c['id']), actor, operators, journal)
        return dict(comment_id=str(c['id']), reconciled=True, target=p['target'])
    check_void(issue, payload, set(targets))
    return _write_void(issue, payload, actor, run, operators, journal)


def _write_void(issue, payload, actor, run, operators, journal):
    """One validated void: prove the retraction is journalable, then write natively.

    The journal directory is opened before the native write, so a retraction this
    host cannot journal is refused with no write at all; every honoured retraction
    is therefore host-issued.
    """
    task = issue['id']
    retract = host_revert_target(issue, payload, operators, journal)
    if retract is not None:
        open_revert_journal(journal)
    result = json.loads(run(['comments', 'add', task,
                             recovery.PREFIX + canonical_bytes(payload).decode(), '--json']))
    comment_id = str(result['id'])
    if retract is not None:
        entry = revert_journal_entry(JOURNAL_RETRACTION, payload, comment_id, actor, task=task,
                                     target_revert_comment_id=retract)
        try:
            publish_revert_journal(journal, entry)
        except ValueError as exc:
            raise ValueError('Operator void comment ' + comment_id + ' was written but its host retraction '
                             'entry could not be persisted (' + str(exc) + '); the revert stands until it '
                             'is: re-run the same void-record with operation_id '
                             + str(payload['operation_id']) + ' to persist the entry') from None
    return dict(comment_id=comment_id, reconciled=False, target=payload['target'])


def persisted_retraction(issue, payload, comment_id, actor, operators, journal):
    """Complete an interrupted retraction for an already-written void; else a no-op."""
    retract = host_revert_target(issue, payload, operators, journal, honoured_only=False)
    if retract is None:
        return
    publish_revert_journal(journal, revert_journal_entry(JOURNAL_RETRACTION, payload, comment_id,
                                                         actor, task=issue['id'],
                                                         target_revert_comment_id=retract))


def issued_revert(p, task):
    """Validate one revert payload. Raises for anything that is not an exact record.

    The payload is the audit trail: WHO (``operator``, bound to the native comment
    author and to the server-side allowlist by ``apply_revert``/``revert_records``),
    WHICH integration commit is being reverted (``integration_commit``), the
    evidence for the revert (``revert_commit`` plus optional ``evidence``
    pointers) and WHY (``reason``). ``evidence`` is optional so "the revert commit
    OR evidence" is enough; every other field is required. ``operator`` is
    included in ``REVERT_REQUIRED_FIELDS`` here because the admin route stamps it
    before the write; callers that validate an unstamped payload add it first.
    """
    if not isinstance(p, dict) or not REVERT_REQUIRED_FIELDS <= set(p) <= REVERT_FIELDS:
        raise ValueError('Invalid integration revert record fields')
    if type(p['schema_version']) is not int or p['schema_version'] != 1 or p['task'] != task:
        raise ValueError('Invalid integration revert record version/task')
    if p['operation'] != REVERT_OPERATION:
        raise ValueError('Invalid integration revert record operation')
    identity(task)
    identity(p['operation_id'])
    identity(p['contribution'])
    identity(p['operator'])
    for key in ('integration_commit', 'revert_commit'):
        if not isinstance(p[key], str) or not COMMIT_TEXT.fullmatch(p[key]):
            raise ValueError(f'{key}: require exact 40/64 hexadecimal commit')
    text(p['reason'], 'revert reason', REVERT_REASON_LIMIT)
    evidence = p.get('evidence')
    if evidence is not None:
        if (not isinstance(evidence, list) or len(evidence) > REVERT_EVIDENCE_LIMIT
                or any(not isinstance(item, str) or not item.strip() or len(item) > 500
                       for item in evidence)):
            raise ValueError(f'evidence: require at most {REVERT_EVIDENCE_LIMIT} nonempty pointers of '
                             'up to 500 characters')
    if len(canonical_bytes(p)) > REVERT_BYTE_LIMIT:
        raise ValueError('Integration revert record exceeds 24 KB')


def revert_body(p):
    """Canonical native comment body for one validated revert payload."""
    return REVERT_PREFIX + canonical_bytes(p).decode()


def validate_revert_journal_entry(entry, name=None):
    """Validate one host-issued revert journal entry, and optionally its file name.

    Shared by the reader (``host_revert_journal``) and by the backup/restore
    validator (``admin.validate_coordination_files``), so a backup can never carry
    an entry the reader would have to guess about. Raises ``ValueError`` for
    anything that is not exactly what the host writer emits: the entry binds the
    revert identity (task, contribution, integration commit, revert commit,
    operation id, operator, issuing actor) to the native comment id it was written
    for, and carries the exact canonical payload plus a SHA-256 over it. The file
    name must be ``<that sha256>.json``, which is what makes the entry
    path-verifiable after a restore.
    """
    if not isinstance(entry, dict):
        raise ValueError('Invalid integration revert journal entry')
    kind = entry.get('kind')
    if kind not in JOURNAL_FIELDS or set(entry) != JOURNAL_FIELDS[kind]:
        raise ValueError('Invalid integration revert journal fields')
    if type(entry['schema_version']) is not int or entry['schema_version'] != 1:
        raise ValueError('Invalid integration revert journal version')
    identity(entry['task'])
    identity(entry['operation_id'])
    identity(entry['comment_id'])
    identity(entry['operator'])
    identity(entry['actor'])
    payload = entry['payload']
    if kind == JOURNAL_REVERT:
        issued_revert(payload, entry['task'])
        for key in ('contribution', 'integration_commit', 'revert_commit'):
            if entry[key] != payload[key]:
                raise ValueError('Integration revert journal entry does not match its payload')
        if entry['operation_id'] != payload['operation_id'] or entry['operator'] != payload['operator']:
            raise ValueError('Integration revert journal entry does not match its payload')
    else:
        recovery.issued(payload, entry['task'])
        identity(entry['target_revert_comment_id'])
        if (payload['target_kind'] != 'integration-revert'
                or payload['target'] != entry['target_revert_comment_id']
                or payload['operation_id'] != entry['operation_id']
                or payload['operator'] != entry['operator']):
            raise ValueError('Integration revert retraction journal entry does not match its payload')
    digest = content_hash(payload)
    if entry['sha256'] != digest:
        raise ValueError('Integration revert journal entry hash does not match its payload')
    if name is not None and name != digest + '.json':
        raise ValueError('Integration revert journal path mismatch')
    return entry


def revert_journal_entry(kind, payload, comment_id, actor, **bound):
    """One validated host journal entry for a revert or a retraction.

    ``bound`` carries the identity fields that are specific to the entry kind
    (``task``/``contribution``/``integration_commit``/``revert_commit`` for a
    revert, ``task``/``target_revert_comment_id`` for a retraction). The SHA-256 is
    over the exact canonical payload the native comment carries, so the entry
    cannot be re-pointed at different bytes.
    """
    entry = dict(bound, schema_version=1, kind=kind, operation_id=payload['operation_id'],
                 operator=payload['operator'], actor=actor, comment_id=comment_id,
                 payload=payload, sha256=content_hash(payload))
    validate_revert_journal_entry(entry)
    return entry


def journal_directory(project):
    """The host revert journal directory inside one project directory, or None.

    ``None`` (no project directory, an absent path, a symlink, or a path that is
    not a directory) means the caller cannot PROVE a revert was host-issued, so no
    revert is trusted. Fail-closed on the read side.
    """
    if project is None:
        return None
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink() or not directory.is_dir():
        return None
    return directory


def revert_journal_writable(directory):
    """Prove ``directory`` accepts a write, or ``ValueError`` so nothing is written.

    ``mkdir(exist_ok=True)`` only proves the path exists; a directory that exists
    but is not writable used to let ``revert-record`` (and the retraction path)
    write the native comment first and then die with a raw ``PermissionError``
    from the journal write (kittrial-5bb.52 review item ``smaller``). The probe
    write is the real check: ``os.access`` lies for a privileged process, while a
    create-and-remove in the directory fails exactly when the journal write would.
    The probe name cannot collide with an entry (``<sha256>.json``) and is removed
    again, so a reader never sees it.
    """
    from uuid import uuid4
    probe = directory / ('.writable-' + uuid4().hex + '.tmp')
    try:
        probe.write_text('probe', encoding='utf-8')
    except OSError as exc:
        raise ValueError('Refusing ' + JOURNAL_DIR + ': the directory is not writable (%s); '
                         'no native write was attempted' % (exc,)) from None
    try:
        probe.unlink()
    except OSError:
        pass
    return directory


def open_revert_journal(project):
    """The journal directory for a WRITER; refuses when it cannot be used.

    Mirrors ``requirement_records._journal``: a symlink is refused outright and the
    directory is created when missing, so a host that cannot persist the entry
    refuses BEFORE its native write instead of recording a revert no reader would
    honour. A directory that exists but cannot be written is refused the same way
    (``revert_journal_writable``), so the host never emits a native revert or
    retraction comment it cannot journal.
    """
    if project is None:
        raise ValueError('Recording an integration revert requires the project directory that holds the '
                         'host-issued ' + JOURNAL_DIR + ' journal; none was supplied')
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink():
        raise ValueError('Refusing ' + JOURNAL_DIR + ': the path is a symlink; no native write was attempted')
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise ValueError('Cannot create ' + JOURNAL_DIR + ': %s' % (exc,))
    if not directory.is_dir():
        raise ValueError(JOURNAL_DIR + ' is not a directory; refusing to record an integration revert')
    return revert_journal_writable(directory)


def publish_revert_journal(project, entry):
    """Persist one validated journal entry atomically; returns its path."""
    from coordination import atomic
    directory = open_revert_journal(project)
    path = directory / (entry['sha256'] + '.json')
    if path.is_symlink():
        raise ValueError('Refusing integration revert journal entry ' + path.name
                         + ': it is a symlink; no journal write was attempted')
    atomic(path, entry)
    return path


def host_revert_journal(project):
    """Read the host-issued revert journal as ``(reverts, retractions, invalid)``.

    Fail-closed: an absent, unreadable or symlinked journal directory yields three
    empty results, so no revert is trusted. Entries are keyed by the native comment
    id they were written for -- ``reverts`` by the revert comment id, ``retractions``
    by the reverted comment id -- and each entry records the task it belongs to, so
    ``revert_records`` can ignore another task's records. A malformed, misnamed or
    conflicting entry is ignored and reported as ``.integration-reverts/<name>`` in
    ``invalid``; one bad file can never make every read on the task fail.
    """
    reverts = {}
    retractions = {}
    invalid = []
    directory = journal_directory(project)
    if directory is None:
        return reverts, retractions, invalid
    try:
        names = sorted(path.name for path in directory.glob('*.json'))
    except OSError:
        return reverts, retractions, invalid
    for name in names:
        path = directory / name
        try:
            if path.is_symlink():
                raise ValueError('symlink')
            if path.stat().st_size > JOURNAL_FILE_LIMIT:
                raise ValueError('oversized')
            entry = validate_revert_journal_entry(
                json.loads(path.read_text(encoding='utf-8')), name)
        except (OSError, ValueError, TypeError, KeyError):
            invalid.append(JOURNAL_DIR + '/' + name)
            continue
        if entry['kind'] == JOURNAL_REVERT:
            # A retraction is keyed by the REVERTED comment id, which is the same id
            # this entry is keyed by, so a legitimately retracted revert must not be
            # treated as a conflict here.
            if entry['comment_id'] in reverts:
                invalid.append(JOURNAL_DIR + '/' + name)
                continue
            reverts[entry['comment_id']] = entry
        else:
            if entry['target_revert_comment_id'] in retractions:
                invalid.append(JOURNAL_DIR + '/' + name)
                continue
            retractions[entry['target_revert_comment_id']] = entry
    return reverts, retractions, invalid


def revert_comments(issue):
    """Every reserved-prefix revert comment as ``(records, invalid)``, UNTRUSTED.

    This is what is on the native issue, not what the host issued: no operator
    allowlist and no journal are applied. ``revert_records`` is the trusted reader;
    ``apply_revert`` uses this raw list only so its own interrupted write (native
    comment written, journal entry not yet persisted) stays reconcilable.
    """
    comments = issue.get('comments') or []
    order = {}
    for position, comment in enumerate(comments):
        order.setdefault(str(comment.get('id')), position)
    records = []
    invalid = []
    for comment in comments:
        raw = comment.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(REVERT_PREFIX):
            continue
        cid = str(comment.get('id'))
        try:
            p = json.loads(raw[len(REVERT_PREFIX):])
            issued_revert(p, issue['id'])
            identity(cid)
            author = comment.get('author')
            text(author, 'revert record author', 300)
            text(comment.get('created_at'), 'revert record timestamp', 100)
            records.append(dict(p, comment_id=cid, order=order.get(cid, len(comments)),
                                author=author, timestamp=comment.get('created_at')))
        except (ValueError, KeyError, TypeError):
            invalid.append(cid)
            continue
    return records, invalid


def revert_records(issue, operators=None, journal=None, voids=None, host=None):
    """Read the HOST-ISSUED revert records on one issue as ``(reverts, invalid)``.

    Tolerant about malformed input, fail-closed about authority: a revert comment is
    honoured only when ALL of these hold -- the record is well formed, its declared
    operator matches its native author, that author is on the server-side operator
    allowlist, it does not conflict with another honoured revert, and the host
    journal (``journal``: the project directory that holds
    ``.integration-reverts/``) carries the matching entry whose canonical payload
    and SHA-256 bind this exact comment (kittrial-5bb.52 review item
    ``forged-pre-deploy-reverts``). A comment that claims to be a revert but has no
    host entry is IGNORED, never honoured.

    ``voids`` is the already-applied operator void list (``history`` supplies it).
    A revert whose comment id is named by a HOST-JOURNALED retraction that is backed
    by one of those applied voids is excluded: that is the retraction path of review
    item ``retraction-and-same-commit``. A retraction the reader cannot verify is
    reported and the revert STANDS, because dropping it would fail open.

    The journal is per PROJECT, so only entries whose recorded task is THIS issue are
    consulted (and only they can be reported as orphaned); another task's revert
    records neither apply here nor warn here.

    This reader deliberately does NOT re-check that the named contribution exists or
    that the named integration commit is still the reported one: ``apply_revert``
    performs both checks before it writes, and every honoured record therefore
    passed them when the host issued it. Everything else is listed in ``invalid``
    (comment ids, plus ``.integration-reverts/<name>`` for an unusable journal
    entry) so it is surfaced on every read instead of dropped silently. An
    unconfigured allowlist authorizes nobody.

    Each returned entry carries ``comment_id``, ``order`` (native position),
    ``operator``, ``contribution``, ``integration_commit``, ``revert_commit``,
    ``reason``, ``evidence``, ``author`` and ``timestamp``.
    """
    authority = recovery.configured_operators(operators)
    entries, retractions, invalid = host or host_revert_journal(journal)
    # The journal lives in the PROJECT directory, so it holds every task's records:
    # only the entries that belong to THIS task are consulted, and only those can
    # be reported as orphaned. Cross-task entries are invisible here (otherwise
    # every task read would warn about every other task's reverts).
    task = issue.get('id')
    entries = {cid: entry for cid, entry in entries.items() if entry['task'] == task}
    retractions = {target: entry for target, entry in retractions.items() if entry['task'] == task}
    invalid = list(invalid)
    records, malformed = revert_comments(issue)
    invalid.extend(malformed)
    applied = {}
    if voids is not None:
        applied = {str(c.get('id')): p for p, c in voids}
    else:
        # A retraction is honoured only against a void this reader can validate
        # under the same operator authority, and an unbacked retraction attempt
        # must be surfaced here exactly as it is on the void-aware path.
        applied = {str(c.get('id')): p for p, c in recovery.records(issue, operators)[0]}
    native = {str(c.get('id')) for c in issue.get('comments') or []}
    reverts = []
    operations = set()
    targets = set()
    seen = set()
    for record in records:
        cid = record['comment_id']
        seen.add(cid)
        try:
            author = record['author']
            if author != record['operator']:
                raise ValueError('Integration revert record provenance does not match its native author')
            if author not in authority:
                raise ValueError('Integration revert record author is not a configured operator')
            entry = entries.get(cid)
            if entry is None:
                raise ValueError('Integration revert record has no host-issued journal entry')
            if {k: v for k, v in record.items() if k in REVERT_FIELDS} != entry['payload']:
                raise ValueError('Integration revert record does not match its host journal entry')
            retraction = retractions.get(cid)
            if retraction is not None:
                void = applied.get(retraction['comment_id'])
                if void is not None and void == retraction['payload'] and void.get('target') == cid:
                    # Retracted by an applied, host-journaled operator void.
                    continue
                invalid.append(JOURNAL_DIR + '/' + retraction['sha256'] + '.json')
            if (record['operation_id'] in operations
                    or (record['contribution'], record['integration_commit'].lower()) in targets):
                raise ValueError('Conflicting integration revert record')
            operations.add(record['operation_id'])
            targets.add((record['contribution'], record['integration_commit'].lower()))
            reverts.append(dict(record))
        except (ValueError, KeyError, TypeError):
            invalid.append(cid)
            continue
    # A host entry the native issue no longer carries (a partial restore) is not a
    # silent no-op: report it, so an operator learns that a host-issued revert is
    # not being honoured.
    for cid in entries:
        if cid not in native:
            invalid.append(JOURNAL_DIR + '/' + entries[cid]['sha256'] + '.json')
    for target in retractions:
        if target not in seen:
            invalid.append(JOURNAL_DIR + '/' + retractions[target]['sha256'] + '.json')
    # An APPLIED void that targets a revert but has no host retraction entry is an
    # unbacked retraction attempt (a raw void comment, or a rollback): the revert
    # stands and the attempt is surfaced instead of silently doing nothing.
    for comment_id, void in applied.items():
        if void.get('target_kind') != 'integration-revert':
            continue
        retraction = retractions.get(void.get('target'))
        if retraction is None or retraction['comment_id'] != comment_id:
            invalid.append(comment_id)
    return reverts, invalid


def host_revert_target(issue, payload, operators=None, journal=None, honoured_only=True):
    """The host-issued revert comment id a void retracts, or None for another kind.

    Raises when the void claims an integration-revert target the host journal does
    not hold, so a void can never retract an unaudited or forged revert record.
    ``honoured_only`` additionally requires the revert to be currently honoured;
    the retry path passes False, because the first retraction already removed it
    from the honoured set and the retry must stay idempotent.
    """
    if not isinstance(payload, dict) or payload.get('target_kind') != 'integration-revert':
        return None
    target = payload.get('target')
    entries, _, _ = host_revert_journal(journal)
    if target not in entries:
        raise ValueError('Operator void target is not a host-issued integration revert record: '
                         + str(target))
    if honoured_only:
        honoured, _ = revert_records(issue, operators, journal)
        if not any(record['comment_id'] == target for record in honoured):
            raise ValueError('Operator void target is not a host-issued integration revert record: '
                             + str(target))
    return target


def apply_revert(rows, task, actor, payload, run, operator=False, operators=None, journal=None):
    """Append one audited revert record; operator is supplied only by the admin CLI.

    Follows ``apply_void``: the issuing operator is bound into the record so reads
    can verify native provenance, and the issuing actor must be on the server-side
    operator allowlist the host supplies (deployment configuration or
    ORCHESTRA_OPERATORS). A payload that names a different operator than the
    issuing actor, an actor outside the allowlist, or an unconfigured allowlist is
    refused before any native mutation, and the contributor review transport never
    reaches this function at all.

    The host-issued journal entry is written under the same project lock, after the
    sole native mutation: ``journal`` is the project directory holding
    ``.integration-reverts/``. The directory is opened BEFORE the native write, so a
    host that cannot persist the entry refuses with no write at all, and a failure
    between the two writes leaves the record inert (the reader honours only
    journaled records) with a retry of the same operation id persisting the missing
    entry instead of writing a second comment.

    The named contribution must exist in the chain and the named
    ``integration_commit`` must currently be the contribution's effective passing
    integration commit, read from the shared projection, so a revert can never be
    a silent no-op and can never be recorded for work that is not integrated. A
    later ``integrated=passed`` under a different integration commit re-integrates
    the work: the revert removes exactly one recorded commit.

    The operation id MUST be unpredictable (a fresh random value per revert):
    an EXACT retry of one operation id with the same actor and payload adopts the
    earlier native comment and journals that one instead of writing a second
    record, which is what makes an interrupted write reconcilable (kittrial-5bb.52
    review item ``smaller``). That adoption is payload-bound, but a predictable id
    such as a counter lets a planted same-author, same-payload comment be adopted
    by the real retry, so docs and examples use random ids.
    """
    if not operator:
        raise ValueError('Integration revert records are not authorized over the contributor review '
                         'transport; an operator must use admin.py revert-record on the coordination host')
    text(actor, 'actor', 300)
    authority = recovery.configured_operators(operators)
    if not authority:
        raise ValueError('No operator allowlist is configured on the coordination host; add the acting '
                         'operator to deployment.private.json before recording a revert')
    if actor not in authority:
        raise ValueError('Actor ' + actor + ' is not a server-side configured operator; only a configured '
                         'operator may record a revert')
    if not isinstance(payload, dict):
        raise ValueError('Invalid integration revert record')
    payload = dict(payload)
    payload.setdefault('operator', actor)
    if payload['operator'] != actor:
        raise ValueError('Revert record operator must match the issuing actor')
    issued_revert(payload, task)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]
    # Refuse before the native write when the host journal cannot be used.
    open_revert_journal(journal)

    def entry_for(comment_id):
        return revert_journal_entry(JOURNAL_REVERT, payload, comment_id, actor, task=task,
                                    contribution=payload['contribution'],
                                    integration_commit=payload['integration_commit'],
                                    revert_commit=payload['revert_commit'])

    # An interrupted earlier attempt (native comment written, journal entry not yet
    # persisted) is reconciled by persisting the missing entry, not by writing a
    # second comment.
    raw, _ = revert_comments(issue)
    for record in raw:
        if record['operation_id'] != payload['operation_id']:
            continue
        if (record['author'] == actor
                and {k: v for k, v in record.items() if k in REVERT_FIELDS} == payload):
            publish_revert_journal(journal, entry_for(record['comment_id']))
            return dict(comment_id=record['comment_id'], reconciled=True,
                        contribution=record['contribution'],
                        integration_commit=record['integration_commit'])
        raise ValueError('Revert operation ID already used with different payload or actor')
    existing, _ = revert_records(issue, operators, journal)
    for p in existing:
        if (p['contribution'] == payload['contribution']
                and p['integration_commit'].lower() == payload['integration_commit'].lower()):
            raise ValueError('Another integration revert record already reverts '
                             + payload['integration_commit'] + ' for contribution ' + payload['contribution'])
    # Validate the whole transition before the sole native mutation: the named
    # contribution must exist and the named commit must be the one the shared
    # projection currently reports as its passing integration.
    state = projection(records(issue))
    known = [c for c in [state.get('contribution')] + list(state.get('prior_contributions') or [])
             if isinstance(c, dict) and c.get('comment_id') == payload['contribution']]
    if not known:
        raise ValueError('Revert record must name the current or a prior contribution of this task: '
                         + payload['contribution'])
    from review_state import integration, reverts_for, scopes_for
    evidence = integration(known[0], scopes_for(rows, task),
                           reverts_for(rows, task, operators, journal))
    if evidence['fact'] != 'passed' or not evidence['integration_commit']:
        raise ValueError('Revert record requires a contribution that is currently integrated; '
                         'record integrated=failed lifecycle evidence instead')
    if payload['integration_commit'].lower() != evidence['integration_commit'].lower():
        raise ValueError('Revert record must name the contribution integration commit being reverted ('
                         + evidence['integration_commit'] + ')')
    result = json.loads(run(['comments', 'add', task, revert_body(payload), '--json']))
    comment_id = str(result['id'])
    try:
        publish_revert_journal(journal, entry_for(comment_id))
    except ValueError as exc:
        raise ValueError('Integration revert comment ' + comment_id + ' was written but its host journal '
                         'entry could not be persisted (' + str(exc) + '); the record is inert until it is: '
                         're-run the same revert-record with operation_id ' + payload['operation_id']
                         + ' to persist the entry') from None
    return dict(comment_id=comment_id, reconciled=False,
                contribution=payload['contribution'],
                integration_commit=payload['integration_commit'])


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


def projection(ordered, voids=None, invalid=None, refused=None, positions=None, reverts=None,
               invalid_reverts=None):
    """Project the chain. ``prior_contributions`` keeps every revision the current
    one replaced visible, tagged with its ``relation`` (``follows`` additive or
    ``supersedes``), so a follow-on never removes the prior revision's record from
    the chain. The prior revision's scoped lifecycle facts are not re-scoped: they
    stay recorded in lifecycle history under their own scope.

    ``reverts`` is the validated, HOST-ISSUED operator revert list (``history``
    returns it); it is carried on the result as the additive ``reverts`` key so the
    shared integration overlay can subtract exactly the reverted integration commit.
    ``invalid_reverts`` names the revert comments and journal entries that were
    ignored, and is surfaced as a warning so an unauthorized or unhosted revert
    record is never dropped silently (voids already warn)."""
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
            # Only a void of a CONTRIBUTION-REVIEW record repairs the chain. A void
            # of an integration-revert record retracts a revert (kittrial-5bb.52
            # item 2) and is not a repair of the reviewed history, so it must not
            # invalidate an earlier approval.
            if p.get('target_kind') != 'contribution-review':
                continue
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
    invalid_reverts = list(invalid_reverts or [])
    if invalid_reverts:
        warnings.append('Integration revert record(s) ignored (malformed, not host-issued, not a configured '
                        'operator, conflicting, or a journal entry with no native record); they change no '
                        'read: ' + ', '.join(invalid_reverts[:5]))
    return dict(contribution=contribution, prior_contributions=prior, review_state=state,
                pending_requests=list(pending.values()), latest_comment_id=latest,
                recoveries=recoveries, warnings=warnings, reverts=list(reverts or []))


def project(issue, operators=None, journal=None, reverts=None, invalid_reverts=None):
    """Raw workflow projection for one issue; ``journal`` is the project directory
    holding the host-issued ``.integration-reverts/`` journal (None trusts no
    revert). A caller that already resolved the per-task reverts for a whole page
    passes ``reverts``/``invalid_reverts`` so the scan is not repeated."""
    ordered, voids, invalid, refused, positions, reverts, invalid_reverts = history(
        issue, operators, journal, reverts=reverts, invalid_reverts=invalid_reverts)
    return projection(ordered, voids, invalid, refused, positions, reverts, invalid_reverts)


def approving_entry(ordered, contribution_id):
    """The ``(payload, native comment)`` pair of the LAST ``approve`` naming a revision.

    A later contribution resets the approval and the projection refuses to approve
    while requests are unresolved, so the last ``approve`` naming this contribution
    is the record the projection used. The payload carries the server-stamped
    ``assignee_at_approval`` snapshot when the record has one, so the gate can judge
    the assignee as it was then rather than as it is now.
    """
    found = None
    for p, c in ordered:
        if p['operation'] == 'approve' and p['contribution'] == contribution_id:
            found = (p, c)
    return found


def approving_record(ordered, contribution_id):
    """The native ``approve`` comment the shared projection counted for a revision.

    The native author on that comment is the attribution the follow-on gate
    checks; it is the transport's record of who actually wrote the approval. Use
    ``approving_entry`` when the approving payload (and its assignee snapshot) is
    needed as well.
    """
    entry = approving_entry(ordered, contribution_id)
    return entry[1] if entry else None


def require_integrated_follow_on(payload, state, ordered, rows, task, assignee=None, operators=None,
                                 journal=None):
    """Refuse an additive follow-on whose prior revision is not genuinely approved.

    ``follows`` asserts that the reviewer's base is already integrated, so the gate
    requires three independent things before the sole native ``comments add``:

    * the raw append-only chain must show the prior revision **approved with no
      unresolved requests** (``awaiting-integration``);
    * that approving record's **normalised native author** must differ from both the
      prior contribution's author and the task assignee **as it was when the
      approval was recorded**. Native comment authors supply *attribution, not
      authentication*, on the SSH/endpoint path: a caller can label itself anything,
      so this check refuses the contributor's own approval (and the assignee's)
      without pretending to be an identity system. Attribution keys are compared
      case-folded with any ``/``-namespace suffix dropped, so ``Worker`` and
      ``worker/sub`` cannot pass as a distinct author. The assignee comes from the
      ``assignee_at_approval`` snapshot the server stamped on the approve record, so
      a handoff after the approval neither opens nor closes the gate; a record with
      no usable snapshot -- absent (legacy) or an explicit null written while the
      task was unassigned -- falls back to the caller's current assignee (fail
      closed as before), so an unassign/approve/take-back cycle cannot open the
      gate. The HTTP ``CAP_APPROVE`` capability is the
      real approval authority: there the native author is bound to the
      authenticated principal, so a worker credential cannot approve at all;
    * the shared per-scope integration evidence (kittrial-5bb.24,
      ``review_state.integration`` over ``review_state.scopes_for``) must record a
      passed ``integrated`` fact for the prior contribution's FULL commit, and the
      follow-on ``base_commit`` must equal that scope's ``integration_commit``.
      Any scope order is accepted, so recording a newer scope for other work does
      not make a genuinely integrated prior un-followable -- the older defect that
      read only the task's single current ``lifecycle`` scope.

    There is deliberately no ``fact is None`` fallback: ``scopes_for`` returns an
    empty list when no scoped evidence is trusted, and ``integration`` then reports
    ``fact='unknown'``, which the check below refuses. A self-recorded
    ``integrated=passed`` can therefore never open the gate on its own.
    """
    if payload.get('operation') != 'contribute' or payload.get('follows') is None:
        return
    prior = state.get('contribution') or {}
    if not prior:
        raise ValueError('Contribution must follow the current revision')
    if state.get('review_state') != 'awaiting-integration' or state.get('pending_requests'):
        raise ValueError('Contribution follows a revision that is not approved; a reviewer must '
                         'approve the prior contribution with no unresolved requests before an '
                         'additive follow-on can use it as its base')
    entry = approving_entry(ordered, prior['comment_id'])
    if entry is None:
        raise ValueError('Contribution follows a revision that is not approved; a reviewer must '
                         'approve the prior contribution with no unresolved requests before an '
                         'additive follow-on can use it as its base')
    approving, approval = entry
    author = approval.get('author')
    if author_key(author) == author_key(prior.get('author')):
        raise ValueError('Contribution follows a revision approved by its own author; an additive '
                         'follow-on requires an approving record whose native author is neither the '
                         'prior contribution author nor the task assignee')
    # Judge the assignee as it was when the approval was recorded. The snapshot is
    # stamped server-side. A record with no usable snapshot -- absent (written
    # before the field existed) or an explicit null (written while the task was
    # unassigned) -- falls back to the caller's current assignee, the pre-snapshot
    # fail-closed reading, so an unassign/approve/take-back cycle cannot open the
    # gate.
    approve_assignee = approving.get(ASSIGNEE_SNAPSHOT) or assignee
    if approve_assignee and author_key(author) == author_key(approve_assignee):
        raise ValueError('Contribution follows a revision approved by the task assignee; an additive '
                         'follow-on requires an approving record whose native author is neither the '
                         'prior contribution author nor the task assignee')
    from review_state import integration, reverts_for, scopes_for
    evidence = integration(prior, scopes_for(rows, task),
                           reverts_for(rows, task, operators, journal))
    if evidence['fact'] != 'passed' or not evidence['integration_commit']:
        raise ValueError('Contribution follows a revision that is not integrated; require a passed '
                         'integrated lifecycle fact scoped to the prior contribution commit, read from '
                         'the shared review-state projection (an operator-reverted integration commit '
                         'does not count), before an additive follow-on can use it as its base')
    if payload['base_commit'].lower() != evidence['integration_commit'].lower():
        raise ValueError('Contribution base_commit must equal the prior integration commit '
                         + evidence['integration_commit'])


def _retry_matches(stored, incoming):
    """Whether a retried payload matches the stored record apart from the snapshot.

    ``execute`` stamps ``assignee_at_approval`` before the append, so a caller can
    never reproduce the stored approve bytes exactly. The snapshot is ignored on
    both sides so a retry of the same operation stays idempotent even when the
    assignee changed in between.
    """
    def scrubbed(p):
        return {k: v for k, v in p.items() if k != ASSIGNEE_SNAPSHOT} if isinstance(p, dict) else p
    return scrubbed(stored) == scrubbed(incoming)


def execute(rows, task, actor, payload, run, operators=None, journal=None):
    """Validate, CAS and append once; return receipt and projected review state.

    ``journal`` is the project directory holding the host-issued
    ``.integration-reverts/`` journal; an absent/unusable journal trusts no revert,
    so a reverted integration commit never counts as a follow-on base on a host
    that cannot prove the revert was issued there.
    """
    if isinstance(payload, dict) and payload.get('operation') == recovery.OPERATION:
        raise ValueError('Operator void records are not accepted over the contributor review transport; '
                         'an operator must use admin.py void-record on the coordination host')
    if isinstance(payload, dict) and payload.get('operation') == REVERT_OPERATION:
        raise ValueError('Integration revert records are not accepted over the contributor review '
                         'transport; an operator must use admin.py revert-record on the coordination host')
    validate(payload, task); text(actor, 'actor', 300)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]
    ordered, voids, invalid, refused, positions, reverts, invalid_reverts = history(issue, operators, journal)
    state = projection(ordered, voids, invalid, refused, positions, reverts, invalid_reverts)
    effective_state = receipt(state, rows, task)['review_state']
    # Exact retries remain recoverable after ownership changes or later revisions.
    for p, c in ordered:
        if p['operation_id'] == payload['operation_id']:
            if _retry_matches(p, payload) and c['author'] == actor:
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
    # The assignee an approval is judged against is recorded server-side, from the
    # task row, so a caller cannot forge it to open the follow-on gate.
    if payload['operation'] == 'approve':
        payload = dict(payload, **{ASSIGNEE_SNAPSHOT: issue.get('assignee')})
        validate(payload, task)
    # Validate the entire transition before the sole native mutation.
    preview_positions = dict(positions)
    preview_positions['pending-write'] = len(issue.get('comments') or [])
    preview = projection(ordered + [(payload, {'id': 'pending-write', 'author': actor, 'created_at': 'pending'})],
                         voids, invalid, refused, preview_positions, reverts, invalid_reverts)
    # A follow-on may only base itself on a prior revision approved by a distinct
    # native author and genuinely integrated. Resolve the approving record from the
    # chain and the integration evidence from the shared review-state projection
    # here, still before the sole native mutation, so the refusal writes nothing.
    if payload['operation'] == 'contribute' and payload.get('follows') is not None:
        require_integrated_follow_on(payload, state, ordered, rows, task, issue.get('assignee'),
                                     operators, journal)
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
