"""Operator void records for malformed reserved machine history.

A comment that claims a reserved machine format but fails schema validation used
to make every read and every new write on the task fail, leaving destructive
native-database editing as the only escape. A void record is an append-only
native comment, written only by the operator CLI, that names one offending
comment, preserves its exact original bytes and their SHA-256, and carries the
disposition, reason, target identity and - through the native comment - the
operator actor and timestamp. Nothing is deleted or rewritten: the original
stays in native history, in history reads and in rendered views, so the incident
remains inspectable and a reviewer can audit the decision.

A malformed, forged or stale void record never takes effect. It is ignored and
reported, exactly like an invalid checkpoint, so recovery can never dead-end on
its own bookkeeping and a void can never suppress history by accident. Authority
is bound to *server-side operator configuration*, not to the payload's own
`operator` string and not merely to the native comment author: the record names
the operator that issued it, its stored native author must match that name, and
that author must be on the operator allowlist the coordination endpoint
supplies. A contributor cannot make a void valid by naming their own actor (or
any actor) as the operator, so a forged or self-authored raw void is inert even
if the generic raw-write guard is bypassed. The void prefix is also reserved on
the contributor transport. Applying a void to a record that is part of the
contribution chain the surviving records currently form is refused by the owning
module, so an unauthorised void cannot remove a current approval. Reads require
a void to follow its target in native order, so a void planted before its target
cannot reorder or reconcile history. The owning module additionally lets a void
target an `integration-revert` record, to retract an operator revert; a revert is
not part of the contribution chain, so it is not protected by it, and the owning
module refuses to retract a revert the host did not issue.

The same void repairs a keyed record on a closed reference or capability anchor
(kittrial-5bb.74; .41 section 3.7): its target kinds are KEYED_KIND_PREFIXES, and
`keyed_entries.AnchoredKind` is the owning module. Its readers and writers drop a
comment an applied void names, under this module's trust rule (`records`), and a
void of a record the entry reads is refused there, exactly as a void of the
current contribution chain is refused here.
"""
import hashlib
import json
import os
import re
from requirements import canonical_bytes

PREFIX = 'Kind: record-void-v1\n'
OPERATION = 'void-record'
DISPOSITIONS = ('void',)
# Voidable target kinds. ``contribution-review`` reconciles a malformed,
# duplicate or conflicting review record. ``integration-revert`` RETRACTS an
# operator integration revert (kittrial-5bb.52 review item
# ``retraction-and-same-commit``): the owning module refuses to void a revert the
# host did not issue, and it requires the retraction to be host-journaled before
# the native write. The prefix is repeated here rather than imported because
# review_workflow imports this module; test_recovery pins the two together.
REVIEW_KIND_PREFIXES = {'contribution-review': 'Kind: contribution-review-v1\n',
                        'integration-revert': 'Kind: integration-revert-v1\n'}
# Keyed-record kinds on closed anchors (kittrial-5bb.74). The owning module is
# keyed_entries.AnchoredKind: a kind belongs to the anchor kind whose record family
# (`reference-`, `capability-`) it starts with. The prefixes are repeated rather
# than imported because reserved_comments imports this module; test_recovery pins
# them to reserved_comments.
# `reference-entry` has two versions this kit reads (v2: an attestation authority,
# kittrial-5bb.98); a void names the kind, and the target's bytes say which version.
KEYED_KIND_PREFIXES = {'reference-entry': ('Kind: reference-entry-v1\n', 'Kind: reference-entry-v2\n',
                                           'Kind: reference-entry-v3\n'),
                       'reference-acceptance': 'Kind: reference-acceptance-v1\n',
                       'capability-entry': 'Kind: capability-entry-v1\n',
                       'capability-acceptance': 'Kind: capability-acceptance-v1\n',
                       'capability-verification': 'Kind: capability-verification-v1\n',
                       'capability-alias': 'Kind: capability-alias-v1\n'}
# Extension point for the proposal kinds of kittrial-5bb.68 (`requirement-proposal`,
# `proposal-disposition`, `contribution-settings`). They are listed here, with the
# prefixes reserved_comments already reserves, only together with a reader that
# drops voided comments the way AnchoredKind does. Until then a void naming one is
# refused as an unsupported target kind, so no void is written that no reader honours.
PROPOSAL_KIND_PREFIXES = {}
KIND_PREFIXES = dict(REVIEW_KIND_PREFIXES, **KEYED_KIND_PREFIXES, **PROPOSAL_KIND_PREFIXES)
FIELDS = {'schema_version', 'operation', 'operation_id', 'task', 'target', 'target_kind',
          'target_sha256', 'original', 'reason', 'disposition', 'operator'}
ORIGINAL_LIMIT = 60000
BYTE_LIMIT = 80000
OPERATORS_ENV = 'ORCHESTRA_OPERATORS'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def identity(value, label='Invalid void record ID'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', value):
        raise ValueError(label)
    return value


def configured_operators(value=None):
    """Server-side operator authority for void records.

    The authorized identities are deployment configuration the endpoint
    supplies (or, for direct library use and host commands, the
    ORCHESTRA_OPERATORS environment variable). An empty or unconfigured
    authority authorizes nobody: validity is never read from the void payload's
    own `operator` field, so a contributor naming their own actor as operator
    gains nothing. Entries that can never satisfy the void schema's operator
    identity are ignored rather than making every read on the task fail.
    """
    if value is None:
        value = os.environ.get(OPERATORS_ENV, '')
    if value is None:
        found = ()
    elif isinstance(value, str):
        found = re.split(r'[,\s]+', value)
    else:
        try:
            found = list(value)
        except TypeError:
            raise ValueError('Operator configuration must be text or a collection of identities') from None
    result = set()
    for item in found:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            result.add(identity(item.strip(), 'Invalid configured operator identity'))
        except ValueError:
            continue
    return frozenset(result)


def text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}: expected nonempty text up to {limit} characters')
    return value


def issued(p, task):
    """Validate one void payload. Raises for anything that is not an exact record."""
    if not isinstance(p, dict) or set(p) != FIELDS:
        raise ValueError('Invalid operator void record fields')
    if type(p['schema_version']) is not int or p['schema_version'] != 1 or p['task'] != task:
        raise ValueError('Invalid operator void record version/task')
    if p['operation'] != OPERATION:
        raise ValueError('Invalid operator void record operation')
    identity(task, 'Invalid void record task')
    identity(p['operation_id'], 'Invalid void record operation ID')
    identity(p['target'], 'Invalid void record target')
    identity(p['operator'], 'Invalid void record operator')
    if p['target_kind'] not in KIND_PREFIXES:
        raise ValueError('Unsupported operator void target kind')
    if p['disposition'] not in DISPOSITIONS:
        raise ValueError('Invalid operator void disposition')
    if not isinstance(p['target_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', p['target_sha256']):
        raise ValueError('Require the original record SHA256')
    if not isinstance(p['original'], str) or not p['original'].strip() or '\x00' in p['original']:
        raise ValueError('Preserve the exact original record bytes')
    if len(p['original']) > ORIGINAL_LIMIT:
        raise ValueError(f'Original record exceeds {ORIGINAL_LIMIT} characters; split the incident')
    if digest(p['original']) != p['target_sha256']:
        raise ValueError('Void record original bytes do not match target_sha256')
    text(p['reason'], 'void reason', 1000)
    if len(canonical_bytes(p)) > BYTE_LIMIT:
        raise ValueError('Void record exceeds 80 KB')


def validate(p, task):
    return issued(p, task)


def claims_kind(raw, kind):
    """True when the target's bytes claim the record `kind` in a version this kit reads
    (v1, and v2 of `reference-entry`).

    A keyed kind is matched through the reserved-prefix view its readers use
    (`reserved_comments`: one leading BOM dropped, CRLF folded), so a lookalike that
    reads as a malformed record of that kind can be voided (kittrial-5bb.74 review). A
    review kind keeps the exact prefix: its reader does not read a lookalike as a
    record at all. An unknown or newer version never matches.
    """
    prefix = KIND_PREFIXES[kind]
    if raw.startswith(prefix):
        return True
    if kind not in KEYED_KIND_PREFIXES:
        return False
    from reserved_comments import _reserved_prefix_view   # it imports this module
    return _reserved_prefix_view(raw).startswith(prefix)


def preserves(raw, p):
    """True when the void payload still carries the target's exact current bytes."""
    return raw == p['original'] and digest(raw) == p['target_sha256']


def authorize(actor, operators):
    """Refuse an issuing actor outside the server-side operator allowlist, before any read.

    Shared by every owning module's void write (review_workflow.apply_void and
    keyed_entries.AnchoredKind.apply_void). An unconfigured allowlist authorizes nobody.
    """
    text(actor, 'actor', 300)
    authority = configured_operators(operators)
    if not authority:
        raise ValueError('No operator allowlist is configured on the coordination host; add the acting '
                         'operator to deployment.private.json before recording a void')
    if actor not in authority:
        raise ValueError('Actor ' + actor + ' is not a server-side configured operator; only a configured '
                         'operator may record a void')


def bound(payload, actor, task):
    """The payload to write: bound to its issuing operator and validated for `task`."""
    if not isinstance(payload, dict):
        raise ValueError('Invalid operator void record')
    payload = dict(payload)
    payload.setdefault('operator', actor)
    if payload['operator'] != actor:
        raise ValueError('Void record operator must match the issuing actor')
    validate(payload, task)
    return payload


def earlier(voids, payload, actor):
    """The identical void already written, as (payload, comment), or None.

    `voids` are the valid voids `records` read on the task. A retry of the same
    operation is idempotent; the same operation ID with a different payload or actor,
    or a second void of one target, is refused before any native write.
    """
    for p, c in voids:
        if p['operation_id'] == payload['operation_id']:
            if p == payload and c.get('author') == actor:
                return p, c
            raise ValueError('Void operation ID already used with different payload or actor')
        if p['target'] == payload['target']:
            raise ValueError('Another operator void record already targets ' + p['target'])
    return None


def target_text(issue, target):
    """Exact stored bytes of the named comment; one match required."""
    found = [c for c in issue.get('comments') or [] if str(c.get('id')) == target]
    if len(found) != 1:
        raise ValueError('Operator void target comment is missing or duplicated: ' + target)
    raw = found[0].get('text', '')
    if not isinstance(raw, str):
        raise ValueError('Operator void target comment has no text: ' + target)
    return raw


def records(issue, operators=None):
    """Read the void records on one issue as (voids, targets, invalid).

    `voids`/`targets` hold only records that are individually valid, whose
    target still exists with exactly the preserved bytes and matching kind,
    whose declared operator matches the native comment's author, whose author is
    on the server-side operator allowlist, and which follow their target in
    native order. Validity is therefore bound to configured authority, not to
    the payload shape or the raw write path: a void whose native author is not a
    configured operator never takes effect even if a contributor wrote it. An
    unconfigured allowlist authorizes nobody. Anything else is listed in
    `invalid` and has no effect on the projection.
    """
    authority = configured_operators(operators)
    comments = issue.get('comments') or []
    order = {}
    for position, comment in enumerate(comments):
        order.setdefault(str(comment.get('id')), position)
    voids = []
    targets = {}
    invalid = []
    operations = set()
    for comment in comments:
        raw = comment.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        cid = str(comment.get('id'))
        try:
            p = json.loads(raw[len(PREFIX):])
            issued(p, issue['id'])
            identity(cid, 'Invalid void record comment ID')
            author = text(comment.get('author'), 'void record author', 300)
            text(comment.get('created_at'), 'void record timestamp', 100)
            if author != p['operator']:
                raise ValueError('Operator void record provenance does not match its native author')
            if author not in authority:
                raise ValueError('Operator void record author is not a configured operator')
            if p['target'] in targets or p['operation_id'] in operations:
                raise ValueError('Conflicting operator void record')
            original = target_text(issue, p['target'])
            if not preserves(original, p):
                raise ValueError('Operator void record does not preserve the exact target bytes')
            if not claims_kind(original, p['target_kind']):
                raise ValueError('Operator void target is not a ' + p['target_kind'] + ' record')
            target_position = order.get(p['target'])
            if target_position is None or order.get(cid, len(comments)) <= target_position:
                raise ValueError('Operator void record must follow its target in native order')
        except (ValueError, KeyError, TypeError):
            invalid.append(cid)
            continue
        operations.add(p['operation_id'])
        targets[p['target']] = (p, comment)
        voids.append((p, comment))
    return voids, targets, invalid
