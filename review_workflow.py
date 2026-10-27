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

Staged rollout (kittrial-5bb.94 item 3). The readers in this kit understand the
new operations and fields unconditionally, but WRITING them is refused unless the
per-installation setting ``review_workflow_writes`` in the deployment
configuration (``deployment.private.json``, read by
``admin.review_workflow_writes`` and supplied by the endpoint) is on; it is OFF by
default. ``withdraw``, ``resolve-item``, ``request-review``, ``decline-review``,
an item ``severity`` and a request-changes ``summary`` are all new record shapes
an older kit fails closed on, so a deployment that may still be rolled back to
one must not write them. The coordinator turns the switch on
(``admin.py review-writes on``) once the rollback target is a kit that reads
them. See ``docs/REVIEWS.md`` and ``docs/OPERATIONS.md``.

Verifier attestations and explicit integration holds (kittrial-5bb.10) are additive
operations in the SAME chain: ``attest`` records what a verifier actually observed (the
delivered tip, the base, a bundle's byte count and SHA-256) as a record SEPARATE from the
declared delivery, and ``hold``/``release-hold`` set and release an audited integration
hold. They are new record shapes, so writing them is behind the same
``review_workflow_writes`` switch. A hold is NOT cleared by a new contribution: it
survives retries and revisions and is released only by an explicit ``release-hold``, so a
held contribution can never read ``awaiting-integration`` (its effective state is
``held``). A ``verified`` attestation that does not match the declared delivery is
refused on the write path and demoted to ``unverified`` on the read path; an unreachable
remote is an explicit ``unknown`` with a reason, never a fabricated pass.

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
import hashlib
import ntpath
import os
import posixpath
import record_json
import re
import stat
import subprocess
import tempfile
from pathlib import Path
import recovery
from coordination import is_merge_slot, merge_slot_sentence
from field_limits import check_text, describe
from requirements import canonical_bytes, content_hash

PREFIX = 'Kind: contribution-review-v1\n'
COMMON = {'schema_version', 'operation', 'operation_id', 'task', 'previous'}
EXTRA = {
    'contribute': {'repository', 'commit', 'base_commit', 'delivery', 'summary', 'supersedes'},
    'request-changes': {'contribution', 'items'},
    'respond': {'contribution', 'resolutions'},
    'approve': {'contribution', 'summary'},
    # Additive operations (kittrial-5bb.94). They are new `operation` values in the
    # same reserved chain, so a record written by an older kit keeps validating
    # unchanged and a kit that does not know an operation fails closed on that
    # chain instead of misreading it. Every new FIELD is optional on the operation
    # that carries it, so old-shaped records are never refused.
    'withdraw': {'contribution', 'reason'},
    'request-review': {'contribution', 'reviewer'},
    'resolve-item': {'contribution', 'request', 'item', 'reason'},
    # The named reviewer closes their own request (kittrial-5bb.94 item 4).
    'decline-review': {'contribution', 'request', 'reason'},
    # Verifier attestations and explicit integration holds (kittrial-5bb.10). A
    # verifier attestation is a SEPARATE record from the declared delivery: the
    # contribution keeps the commit and checksum its author declared, and this record
    # states what the verifier actually observed. An integration hold is set and
    # released by an audited record of its own.
    'attest': {'contribution', 'verdict', 'commit', 'base_commit', 'method', 'reason'},
    'hold': {'contribution', 'reason'},
    'release-hold': {'contribution', 'hold', 'reason'},
}
#: Fields an operation accepts in addition to ``EXTRA``; each is optional and
#: additive to the operation's exact legacy field set.
OPTIONAL = {
    'contribute': {'follows'},
    'request-changes': {'summary'},
    'request-review': {'summary'},
    'withdraw': {'disposition'},
    'resolve-item': {'disposition'},
    # A remote delivery has no bundle bytes, so the two observation values are
    # optional: an attestation that cannot observe them says so in its method and
    # never invents a digest (kittrial-5bb.10).
    'attest': {'sha256', 'bytes'},
}
#: The operation values that write a NEW record shape (kittrial-5bb.94 item 3,
#: extended by kittrial-5bb.10). Every one is refused unless the per-installation
#: ``review_workflow_writes`` setting is on; the readers understand them either way.
NEW_WRITE_OPERATIONS = ('withdraw', 'resolve-item', 'request-review', 'decline-review',
                        'attest', 'hold', 'release-hold')
#: At most this many OPEN review requests may stand against one requester at a
#: time, counted across every task (kittrial-5bb.94 item 4: one actor put 25 tasks
#: into another's queue in 71 seconds).
MAX_OPEN_REVIEW_REQUESTS = 10
#: The reviewer field of a request-review: the actor shape the endpoint accepts,
#: which allows ``@`` and ``/`` (actor names may contain both). The narrower
#: workflow-ID shape used for comment ids refuses them.
REVIEWER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
#: Plain text for a field another worker reads (the rule ``guidance`` applies):
#: no C0 control characters other than tab/newline/carriage return and no DEL, no
#: C1 controls, bidi controls, word joiners, BOM, Unicode tag characters or
#: variation selectors (U+E0100-U+E01EF, as guidance refuses), and a
#: ZWNJ/ZWJ only between letters. New writes also refuse the fixed Cf ranges below;
#: stored-record readers keep the legacy set so tightening the rule hides no old record.
PLAIN_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
PLAIN_INVISIBLE = re.compile('[\u0080-\u009f\u00ad\u200b\u200e\u200f\u2028\u2029\u202a-\u202e'
                             '\u2060-\u2064\u2066-\u2069\ufeff\U000E0000-\U000E007F'
                             '\U000E0100-\U000E01EF]')
PLAIN_WRITE_INVISIBLE = re.compile(
    '[\u0080-\u009f\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2\u180e'
    '\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff'
    '\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3'
    '\U0001d173-\U0001d17a\U000E0000-\U000E007F\U000E0100-\U000E01EF]')
PLAIN_JOINER = re.compile('[\u200c\u200d]')
#: Item severity on a request-changes item. Absent means ``blocking`` so every
#: record written before severity existed keeps blocking approval.
SEVERITIES = ('blocking', 'note')
#: The three readings a verifier attestation may record (kittrial-5bb.10). ``verified``
#: is only honoured when the observation matches the DECLARED delivery (commit == the
#: delivered tip, base == the declared base, and for a bundle the declared SHA-256 and a
#: positive byte count); ``failed`` states a positively disproven delivery; ``unknown``
#: states that verification could not be performed (an unreachable remote is an explicit
#: unknown, never a fabricated pass). ``failed``/``unknown`` both need a reason.
ATTEST_VERDICTS = ('verified', 'failed', 'unknown')
#: Bound on the ``method`` text of an attestation (how the verifier checked).
METHOD_LIMIT = 200
#: How many attestations one read returns (newest last). The full chain stays in
#: history; this is only the bounded projection field.
ATTEST_LIMIT = 20
#: How much of a record's own text a warning line repeats, so a 1000-character reason
#: cannot put a paragraph (or a newline) into every read.
WARNING_TEXT_LIMIT = 160
#: The reading of a contribution that carries no attestation at all.
VERIFICATION_UNVERIFIED = 'unverified'
#: How a contribution stops awaiting review: withdrawn, or marked superseded.
WITHDRAW_DISPOSITIONS = ('withdrawn', 'superseded')
#: How the requester disposes of their own item: resolve it, or leave it as a note.
RESOLVE_DISPOSITIONS = ('resolved', 'note')
#: What a resolve-item record with NO disposition means. An older kit could store an
#: explicit null, and that record still reads: it is the operation's default, which
#: RESOLVES the item. Named so the default is one greppable token rather than an inline
#: literal (kittrial-5bb.110 item 3 P3: a mutation that read a stored null as anything
#: else survived).
RESOLVE_DEFAULT = 'resolved'
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
#: The host observation of a delivered bundle (kittrial-5bb.10 review item 3). The
#: file is opened without following a symlink and must be a regular file, its first
#: line must be a git bundle signature (``# v2 git bundle`` / ``# v3 git bundle``)
#: before a single byte is hashed, the digest is computed in blocks so a large file
#: cannot be read into memory, and the file is refused outright above a stated size.
BUNDLE_MAX_BYTES = 2 * 1024 ** 3
BUNDLE_BLOCK_BYTES = 1024 * 1024
#: The bound on ``git bundle list-heads`` for the host observation, in seconds.
BUNDLE_GIT_TIMEOUT = 60
BUNDLE_SIGNATURE = re.compile(rb'# v[23] git bundle')


#: The text limits of a review record, by the field name its error uses. The validator
#: below and the `review` help both read this table (kittrial-5bb.97).
TEXT_LIMITS = {'summary': 1200, 'repository': 1000, 'remote': 1000, 'branch': 300, 'bundle path': 1000,
               'review text': 1000, 'resolution reason': 1000, 'resolution evidence': 1000,
               'assignee snapshot': 300, 'decline reason': 1000, 'withdraw reason': 1000}
#: The attestation's own text limits (kittrial-5bb.10). Kept OUT of ``TEXT_LIMITS``: that
#: map is a released, exactly-asserted shape, and an attestation's ``method``/``reason``
#: are validated with these constants directly (they are documented in docs/CLI_CONTRACT.md).
REASON_LIMIT = 1000
ITEMS_MAX = 20
PAYLOAD_MAX_BYTES = 24000


def text(value, name, limit=500):
    check_text(value, name, limit)


def limited(value, name, where=''):
    """A review-record field against TEXT_LIMITS; `where` names the item it sits in."""
    check_text(value, where + name, TEXT_LIMITS[name])


def help_limits():
    """The limits for the machine-readable `review` help."""
    limits = describe(TEXT_LIMITS)
    limits['items / resolutions'] = '1..%d per record' % ITEMS_MAX
    limits['payload'] = '<= %d KB canonical bytes' % (PAYLOAD_MAX_BYTES // 1000)
    return limits


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', value):
        raise ValueError('Invalid workflow ID')


def reviewer(value):
    """A named reviewer: an actor identity, which may contain ``@`` or ``/``."""
    if not isinstance(value, str) or not REVIEWER.fullmatch(value):
        raise ValueError('Invalid reviewer identity')
    return value


def plain_text(value, name, *, stored=False):
    """Refuse control/bidi/zero-width/tag characters in text another worker reads.

    The same rule ``guidance`` applies to its payload (kittrial-5bb.99 review
    ``small`` 4/7): a summary or reason is shown to other workers in ``review`` and
    ``brief``, so ESC, bidi controls, zero-width and tag characters must not be
    stored as instructions. New writes use the expanded fixed set. A caller reading
    a stored record passes ``stored=True`` to retain the legacy set.
    """
    if PLAIN_CONTROL.search(value):
        raise ValueError(f'{name} must be plain text (no control characters)')
    invisible = PLAIN_INVISIBLE if stored else PLAIN_WRITE_INVISIBLE
    if invisible.search(value):
        raise ValueError(f'{name} must be plain text (no bidi, zero-width, C1, tag or '
                         'variation-selector characters)')
    for match in PLAIN_JOINER.finditer(value):
        before = value[match.start() - 1] if match.start() else ''
        after = value[match.end()] if match.end() < len(value) else ''
        if not (before.isalpha() and after.isalpha()):
            raise ValueError(f'{name} must be plain text (a zero-width joiner or non-joiner is only '
                             'allowed between letters)')


def check_plain_text(payload):
    """Apply the plain-text rule to every human-authored field this write stores.

    Called by ``execute`` after ``validate`` (so the fields are known text) and
    before any native write. Reads never call it: an old record with such
    characters stays readable.
    """
    for key in ('summary', 'reason', 'method'):
        value = payload.get(key)
        if isinstance(value, str):
            plain_text(value, key)
    if payload.get('operation') == 'request-changes':
        for item in payload.get('items') or []:
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                plain_text(item['text'], 'review text')
    if payload.get('operation') == 'respond':
        for item in payload.get('resolutions') or []:
            if isinstance(item, dict):
                for key in ('reason', 'evidence'):
                    if isinstance(item.get(key), str):
                        plain_text(item[key], 'resolution ' + key)


def check_write_fields(payload):
    """Refuse an explicitly null OPTIONAL field on the WRITE path (kittrial-5bb.110 item 2).

    ``validate`` stays tolerant so a record an older kit stored with ``"disposition":
    null`` still validates and reads (``projection`` reads it as the operation default,
    never ``None``). The canonical WRITER refuses a NEW one: null is not a disposition,
    and storing it made ``review_state`` read None and a second withdraw raise a
    ``TypeError``. The HTTP service also treats a null optional field as absent and does
    not forward it, so this refusal only catches a raw payload (``review --file`` with an
    explicit null) before any native write.
    """
    op = payload.get('operation')
    if op not in ('withdraw', 'resolve-item') or 'disposition' not in payload:
        return
    if payload['disposition'] is None:
        allowed = WITHDRAW_DISPOSITIONS if op == 'withdraw' else RESOLVE_DISPOSITIONS
        raise ValueError('%s disposition must be %s or absent, not null'
                         % (op, ' or '.join(allowed)))


def new_write_field(payload):
    """The operation or FIELD a validated payload writes that is new in kittrial-5bb.94.

    Returns ``None`` for a legacy shape. A new operation names the operation; a
    request-changes carrying the additive ``summary`` or an item ``severity`` names
    that FIELD, so the switch-off refusal points at the field (item 7) instead of
    only saying ``operation/field request-changes``. Both are named when both are set.
    """
    op = payload.get('operation')
    if op in NEW_WRITE_OPERATIONS:
        return 'operation ' + str(op)
    if op == 'request-changes':
        named = []
        if 'summary' in payload:
            named.append('summary')
        if any(isinstance(item, dict) and item.get('severity') is not None
               for item in payload.get('items') or []):
            named.append('item severity')
        if named:
            return 'request-changes ' + ' and '.join(named)
    return None


def new_write_requested(payload):
    """Whether a validated payload writes a new-shaped record (item 3).

    ``withdraw``, ``resolve-item``, ``request-review`` and ``decline-review`` are
    new operation values; a request-changes ``summary`` and an item ``severity``
    are additive fields. A legacy ``{id, text}`` request-changes item is an OLD
    shape and is not gated.
    """
    return new_write_field(payload) is not None


def new_write_refusal(payload):
    """The refusal text for a new-shaped write on an installation with the switch off."""
    operation = payload.get('operation')
    boundary = ('kittrial-5bb.10 (verifier attestations and integration holds)'
                if operation in ('attest', 'hold', 'release-hold') else 'kittrial-5bb.94')
    return ('Review workflow ' + (new_write_field(payload) or str(operation)) +
            ' writes a new record shape that a kit built before ' + boundary + ' cannot read. '
            'This installation has review_workflow_writes off (the default); the readers here '
            'understand the new operations and fields already, so this is a write switch, not a '
            'read limit. An operator turns it on with `admin.py review-writes on --actor OPERATOR` '
            'on the coordination host, once the rollback target is a kit that reads the new shapes. '
            'A legacy request-changes item without severity and without a summary is unaffected.')


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


#: The most of an actor name a slice 6/7 refusal repeats (kittrial-5bb.197), the
#: same bound docs/REVIEWS.md states for the base refusal.
RULE_NAME_MAX = 80


def named_rule_actor(value):
    """An actor name as a slices 6/7 refusal may repeat it, or ``an actor``.

    A name the endpoint accepts (letters, digits and ``_.@/-``, at most
    ``RULE_NAME_MAX``) is repeated; anything else is called an actor, so a
    hand-written name cannot put escape sequences or a long line in a refusal.
    """
    if isinstance(value, str) and 0 < len(value) <= RULE_NAME_MAX and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]*', value):
        return '"%s"' % value
    return 'an actor'


def owner_map(owners):
    """The project's actor -> principal map, reading it only when a rule needs it.

    ``owners`` is a mapping, or a zero-argument callable returning one. The endpoint
    passes a callable so that a request that does not need the registry never reads
    it. Listed names need no registry for the coordinator/lifecycle naming rules,
    and exact assignees need none for their allowed facts. Independent-approval
    writers load it strictly even for a listed operator. Readers can fall back to
    names; contributing does not need ownership. A failing loader names its rule
    and recovery; ``approval_owners`` supplies the independent-approval refusal.
    """
    return owners() if callable(owners) else owners


def party_key(actor, owners=None):
    """The party an actor belongs to (kittrial-5bb.197, the task's rule 3).

    The principal the project's session registry gives the actor when it has one
    (``sessions.owners``, as slice 2 does), otherwise the actor's own name under the
    same normalisation the follows gate uses (case-folded, a ``/``-namespace suffix
    dropped). This key describes available attribution; :func:`same_party` also
    compares folded names, so binding just one name cannot evade that comparison.
    A registry that cannot be read falls back to the name-only comparison rather
    than stopping a read: the same-name refusal survives, only the principal
    refinement is unavailable.
    """
    try:
        mapping = owner_map(owners)
    except (ValueError, OSError):
        mapping = None
    if isinstance(mapping, dict):
        principal = mapping.get(actor)
        if isinstance(principal, str) and principal:
            return ('principal', principal)
    return ('name', author_key(actor))


def same_party(first, second, owners=None):
    """Equal folded names OR equal known principals identify the same party.

    Readers retain name comparison when ownership is unavailable. Writers that
    require independent approval first load the registry with :func:`approval_owners`.
    This helper is not used by the older follows or recommendation gates.
    """
    if author_key(first) == author_key(second):
        return True
    return party_key(first, owners) == party_key(second, owners)


class CoordinatorRuleRefusal(ValueError):
    """A rule refused before any effect; the endpoint marks this for HTTP callers."""


class MergeHoldRefusal(CoordinatorRuleRefusal):
    """The merge-acquire hold-gate refusal (kittrial-5bb.297): a held or unreadable
    contribution. On the endpoint's coordinate route this one refusal answers rc 2
    (the shape the web merge-slot route returns); every other CoordinatorRuleRefusal
    keeps raising exactly as before - no contract change beyond it."""


def approval_owners(owners):
    """Load ownership before an independent-approval write; never guess on failure."""
    try:
        return owner_map(owners)
    except (ValueError, OSError):
        raise CoordinatorRuleRefusal('The installation setting independent_approval is on, and the session registry '
                         'cannot be read. An operator turns the rule off with '
                         '`admin.py coordinator-rules off --rule independent_approval --actor OPERATOR`, '
                         'or repairs the session registry; nothing was changed.') from None


#: The three operations whose "never the author's or the assignee's party" check is
#: NOT switchable (kittrial-5bb.10 review item 2). The map is read for them whatever
#: the coordinator settings say, and an unreadable registry is reported rather than
#: silently degrading the comparison to names.
PARTY_OPERATIONS = ('attest', 'hold', 'release-hold')
PARTY_MAP_UNREADABLE = ('the session registry could not be read, so the parties (principals) of the actor, the '
                        'contribution author and the task assignee could not be compared; names were compared '
                        'instead')


def load_party_owners(owners):
    """Resolve the actor -> principal map for the party rule of the three operations.

    "Never the contribution author's or the task assignee's party" is a rule of the
    kit, not a per-installation setting (kittrial-5bb.10 review item 2): the endpoint
    and ``admin.py attest-delivery`` supply the project's map for ``attest``, ``hold``
    and ``release-hold`` whatever the coordinator rules say. Returns ``(mapping,
    problem)``: ``mapping`` is a plain dict :func:`same_party` can read (``{}`` when
    nothing is available), and ``problem`` is a sentence for the write's answer when a
    registry exists but cannot be read, so the caller can still compare names and SAY
    that parties could not be compared.
    """
    try:
        mapping = owner_map(owners)
    except (ValueError, OSError):
        return {}, PARTY_MAP_UNREADABLE
    return (mapping if isinstance(mapping, dict) else {}), None


def listed_operator_actor(actor, operators=None, owners=None):
    """Whether slice 7 lets ``actor`` approve or take the merge slot in this project.

    ONE rule, used by ``review_workflow`` and ``lifecycle.py`` alike (kittrial-5bb.197
    review request 01a12894, item 3a): an actor on the installation's operator
    allowlist, or an actor of a listed principal for this project, meaning an actor
    whose entry in the project's session-registry ``owners`` map names the same
    principal as some listed operator. A plain listed name is decided WITHOUT reading
    the registry, so a damaged registry never stops a listed operator; only an actor
    that is not a plain listed name needs the map. Names match by whole name and
    case, as every other check of the allowlist does; the list is the installation's,
    because the design gives no project its own operator list (rules 1 and 2 confine
    the name to its principal's projects). A project with no ``owners`` map has no
    principals and is judged on the allowlist alone. Read-only: nothing is written.
    """
    # Explicit identity collections already came from the installation. Preserve
    # their exact names; trimming one can turn a malformed entry into authority.
    # The environment/text form uses the configuration parser for its delimiters.
    if operators is None or isinstance(operators, str):
        authority = set(recovery.configured_operators(operators))
    else:
        names = tuple(operators)
        authority = {name for name in recovery.configured_operators(names) if name in names}
    if actor in authority:
        return True
    mapping = owner_map(owners)
    if not isinstance(mapping, dict):
        return False
    principal = mapping.get(actor)
    if not isinstance(principal, str) or not principal:
        return False
    return any(mapping.get(name) == principal for name in authority)


def operator_owners(project):
    """Lazy registry input for operator eligibility, independent of writer-rule flags."""
    def load():
        import sessions
        try:
            return sessions.owners(project)
        except (ValueError, OSError):
            raise ValueError('The project session registry cannot be read; bound-principal operator '
                             'eligibility cannot be proved. A listed operator repairs the registry; '
                             'no native write was attempted.') from None
    return load


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Invalid review workflow fields')


def item_fields(item):
    """One request-changes item: ``{id, text}`` plus the optional additive severity.

    An item written before severity existed has exactly ``{id, text}`` and keeps
    validating unchanged; the reader then treats it as ``blocking``.
    """
    if not isinstance(item, dict) or set(item) not in ({'id', 'text'}, {'id', 'text', 'severity'}):
        raise ValueError('Invalid review item fields')


def validate(p, task):
    if not isinstance(p, dict) or not isinstance(p.get('operation'), str) or p['operation'] not in EXTRA:
        raise ValueError('Invalid review workflow operation')
    # The required field set stays EXACT and the optional additions are a superset:
    # ``follows`` (additive follow-on relation), the server-stamped assignee
    # snapshot, a request-changes summary and the kittrial-5bb.94 item severity /
    # withdraw / request-review / resolve-item fields. A record written before any
    # of them existed has exactly the required set and keeps validating unchanged.
    required = COMMON | EXTRA[p['operation']]
    allowed = required | OPTIONAL.get(p['operation'], frozenset())
    if p['operation'] == 'approve':
        allowed = allowed | {ASSIGNEE_SNAPSHOT}
    if not required <= set(p) <= allowed:
        raise ValueError('Invalid review workflow fields')
    if type(p['schema_version']) is not int or p['schema_version'] != 1 or p['task'] != task:
        raise ValueError('Invalid review workflow version/task')
    identity(task); identity(p['operation_id'])
    if p['previous'] is not None:
        identity(p['previous'])
    op = p['operation']
    if op == 'contribute':
        limited(p['repository'], 'repository'); limited(p['summary'], 'summary')
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
            limited(d['remote'], 'remote'); limited(d['branch'], 'branch')
        elif d.get('kind') == 'bundle':
            fields(d, {'kind', 'path', 'sha256'})
            limited(d['path'], 'bundle path')
            if not isinstance(d['sha256'], str) or not re.fullmatch(r'[a-fA-F0-9]{64}', d['sha256']):
                raise ValueError('Require bundle SHA256')
        else:
            raise ValueError('Delivery must be remote or bundle')
    else:
        identity(p['contribution'])
        if op == 'approve':
            limited(p['summary'], 'summary')
            snapshot = p.get(ASSIGNEE_SNAPSHOT)
            if snapshot is not None:
                limited(snapshot, 'assignee snapshot')
        elif op == 'request-changes':
            # The summary is additive (kittrial-5bb.94 item 3): request-changes used
            # to reject it, so the reviewer's reasoning sat in an unlinked comment.
            if 'summary' in p:
                limited(p['summary'], 'summary')
            values = p['items']
            if not isinstance(values, list) or not 1 <= len(values) <= ITEMS_MAX:
                raise ValueError('Require 1..%d review items' % ITEMS_MAX)
            seen = set()
            for index, item in enumerate(values):
                # The error names WHICH item is at fault: one over-long item used to fail
                # the whole write with no pointer to it (kittrial-5bb.97).
                item_fields(item); identity(item['id'])
                limited(item['text'], 'review text', 'items[%d] (id %s) ' % (index, item['id']))
                if item.get('severity', 'blocking') not in SEVERITIES:
                    raise ValueError('Review item severity must be blocking or note')
                if item['id'] in seen:
                    raise ValueError('Duplicate review item')
                seen.add(item['id'])
        elif op == 'respond':
            values = p['resolutions']
            if not isinstance(values, list) or not 1 <= len(values) <= ITEMS_MAX:
                raise ValueError('Require 1..%d review items' % ITEMS_MAX)
            seen = set()
            for index, item in enumerate(values):
                fields(item, {'request', 'item', 'reason', 'evidence'})
                identity(item['request']); identity(item['item'])
                where = 'resolutions[%d] (item %s) ' % (index, item['item'])
                limited(item['reason'], 'resolution reason', where)
                limited(item['evidence'], 'resolution evidence', where)
                key = (item['request'], item['item'])
                if key in seen:
                    raise ValueError('Duplicate review item')
                seen.add(key)
        elif op == 'request-review':
            reviewer(p['reviewer'])
            if 'summary' in p:
                limited(p['summary'], 'summary')
        elif op == 'decline-review':
            identity(p['request'])
            limited(p['reason'], 'decline reason')
        elif op == 'attest':
            # Shape only: the cross-check against the DECLARED delivery lives in
            # ``attestation_problems``, which ``projection`` applies as a tolerant
            # demotion and ``execute`` applies as a hard refusal before any write.
            if p['verdict'] not in ATTEST_VERDICTS:
                raise ValueError('Attestation verdict must be verified, failed or unknown')
            for key in ('commit', 'base_commit'):
                if not isinstance(p[key], str) or not COMMIT_TEXT.fullmatch(p[key]):
                    raise ValueError(f'{key}: require exact 40/64 hexadecimal commit')
            check_text(p['method'], 'method', METHOD_LIMIT)
            check_text(p['reason'], 'reason', REASON_LIMIT)
            observed = p.get('sha256')
            if observed is not None and (not isinstance(observed, str)
                                         or not re.fullmatch(r'[a-fA-F0-9]{64}', observed)):
                raise ValueError('Attestation sha256 must be a 64-hex digest, or absent')
            size = p.get('bytes')
            if size is not None and (type(size) is not int or size < 0):
                raise ValueError('Attestation bytes must be a non-negative integer, or absent')
        elif op == 'hold':
            check_text(p['reason'], 'reason', REASON_LIMIT)
        elif op == 'release-hold':
            identity(p['hold'])
            check_text(p['reason'], 'reason', REASON_LIMIT)
        elif op == 'withdraw':
            limited(p['reason'], 'withdraw reason')
            # The READ path stays tolerant: a record an older kit stored with an
            # explicit null disposition still validates here and reads as the default
            # (see `projection`). The WRITE path refuses it (`check_write_fields`), so a
            # new null can never be stored (kittrial-5bb.110 item 2).
            if p.get('disposition') is not None and p['disposition'] not in WITHDRAW_DISPOSITIONS:
                raise ValueError('withdraw disposition must be withdrawn or superseded')
        elif op == 'resolve-item':
            identity(p['request']); identity(p['item'])
            limited(p['reason'], 'resolution reason')
            if p.get('disposition') is not None and p['disposition'] not in RESOLVE_DISPOSITIONS:
                raise ValueError('resolve-item disposition must be resolved or note')
    if len(canonical_bytes(p)) > PAYLOAD_MAX_BYTES:
        raise ValueError('Review workflow payload: %d canonical bytes, the limit is %d (24 KB)'
                         % (len(canonical_bytes(p)), PAYLOAD_MAX_BYTES))


def warning_text(value, limit=WARNING_TEXT_LIMIT):
    """One bounded, single-line rendering of a record's own text for a warning line."""
    if not isinstance(value, str):
        return ''
    flat = ' '.join(value.split())
    return flat if len(flat) <= limit else flat[:max(0, limit - 1)] + '\u2026'


def attestation_problems(p, contribution):
    """Why a ``verified`` attestation does NOT verify the contribution it names.

    Read as a tolerant DEMOTION by ``projection`` (a stored record whose observation no
    longer matches the delivery reads as unverified, never as verified) and as a hard
    refusal by ``execute`` before the native write, so a wrong checksum, a wrong base or
    a commit that is not the declared delivered tip can never be recorded as verified.

    A ``failed`` or ``unknown`` record asserts nothing about a match and returns no
    problems here; it is explicit by its own verdict and reason. A contribution with no
    verifiable declaration (no commit at all) fails closed: the observation cannot be
    shown to be the delivered tip.
    """
    contribution = contribution if isinstance(contribution, dict) else {}
    if p.get('verdict') != 'verified':
        return []
    problems = []
    declared = str(contribution.get('commit') or '')
    if not declared or str(p.get('commit') or '').lower() != declared.lower():
        problems.append('the observed commit is not the declared delivered commit')
    declared_base = str(contribution.get('base_commit') or '')
    if not declared_base or str(p.get('base_commit') or '').lower() != declared_base.lower():
        problems.append('the observed base is not the declared base commit')
    delivery = contribution.get('delivery') if isinstance(contribution.get('delivery'), dict) else {}
    kind = delivery.get('kind')
    if kind == 'bundle':
        declared_sha = str(delivery.get('sha256') or '')
        observed = p.get('sha256')
        if not isinstance(observed, str) or observed.lower() != declared_sha.lower():
            problems.append('the observed bundle SHA-256 is not the declared delivery checksum')
        size = p.get('bytes')
        if type(size) is not int or size <= 0:
            problems.append('a verified bundle attestation needs the delivered byte count')
    elif kind == 'remote':
        # The delivered tip IS the observed commit, and that is compared above; a remote
        # delivery declares no digest of its own, so there is nothing else to match.
        pass
    else:
        problems.append('the declared delivery is not a verifiable remote or bundle delivery')
    return problems


def attest_authority_refusal(actor, contribution, assignee, operators=None, verifiers=None, owners=None):
    """Why ``actor`` may NOT record a verifier attestation, or ``None`` when allowed.

    An attestation is a VERIFIER's observation (kittrial-5bb.10 item 3): the actor must
    be on the deployment verifiers list or be a listed operator (the ONE
    ``listed_operator_actor`` rule, so an actor of a listed operator's principal counts),
    and never the contribution's author or the task's assignee, or a party of either.
    Authorization is judged before the observation, so an author or an unlisted
    contributor cannot record a ``failed``/``unknown``/``verified`` verdict at all.
    """
    verifier_list = verifiers if verifiers is not None else ()
    listed = listed_operator_actor(actor, operators, owners) or \
        actor in recovery.configured_operators(verifier_list)
    if not listed:
        return ('attest refused: an attestation records a VERIFIER\'s observation, so only a listed '
                'verifier or a listed operator may record one, and %s is neither. An operator adds a '
                'verifier with `admin.py verifiers add ACTOR` on the coordination host.'
                % named_rule_actor(actor))
    contribution = contribution if isinstance(contribution, dict) else {}
    for role, named in (('author', contribution.get('author')), ('assignee', assignee)):
        if isinstance(named, str) and named and same_party(actor, named, owners):
            return ('attest refused: %s is the contribution %s\'s party, and nobody attests to their own '
                    'work; only another listed verifier or a listed operator may attest this delivery.'
                    % (named_rule_actor(actor), role))
    return None


def hold_authority_refusal(actor, operators=None, owners=None):
    """Why ``actor`` may NOT set or release an integration hold, or ``None``.

    A hold is a coordinator control (kittrial-5bb.10 items 4 and 6): the actor must be a
    listed operator of this project by the ONE ``listed_operator_actor`` rule (a name on
    the allowlist, or an actor of a listed operator's principal).
    """
    if listed_operator_actor(actor, operators, owners):
        return None
    return ('hold refused: an integration hold is a coordinator control, so only a listed operator of '
            'this project (or an actor of a listed operator\'s principal) may set or release one, and %s '
            'is neither.' % named_rule_actor(actor))


def read_bundle_observation(path, git=None):
    """What a verifier can OBSERVE about a delivered bundle file (kittrial-5bb.10 item 5).

    The ``attest`` operation itself reads no bundle, computes no hash and runs no git, so
    this is the host-side observation the verifier's command uses BEFORE recording the
    attestation: it reads the file's bytes, hashes them and counts them, and - when a
    ``git`` runner is supplied - lists the refs the bundle advertises with
    ``git bundle list-heads``. Returns ``{path, bytes, sha256, heads, heads_error}``;
    ``heads`` is a list of ``{commit, ref}`` (empty for a bundle with no advertised ref,
    never guessed) and ``heads_error`` names why they could not be read.

    Bounds and order (kittrial-5bb.10 review item 3), so a listed verifier cannot turn
    this into an arbitrary-file digest oracle and a planted file cannot exhaust memory:

    * the file is opened with ``O_NONBLOCK`` and ``O_NOFOLLOW`` and must be a REGULAR
      file (``fstat`` on the open descriptor), so a symlink is refused instead of
      followed and a device, fifo or directory is refused WITHOUT waiting on it: a fifo
      put in place of the bundle answers at once instead of blocking for ever (review
      item ``a-fifo-hangs``);
    * its first line must match the git bundle signature BEFORE anything is hashed: a file
      that is not a bundle is refused, so its SHA-256 and size are never returned;
    * the digest is computed in ``BUNDLE_BLOCK_BYTES`` blocks, never by reading the whole
      file into memory, and a file above ``BUNDLE_MAX_BYTES`` is refused with that stated
      limit;
    * the caller's ``git`` runner is expected to bound its own subprocess (the host
      command passes ``BUNDLE_GIT_TIMEOUT``); a runner that raises has its message kept in
      ``heads_error`` rather than propagating.
    """
    bundle = Path(path)
    # lstat first for a clear, portable refusal, then O_NOFOLLOW closes the race on hosts
    # that support it. ``is_symlink`` never follows. ``is_dir`` is asked explicitly because
    # some platforms refuse to open a directory at all (EACCES/IsADirectoryError) rather than
    # returning a descriptor ``fstat`` can classify.
    if bundle.is_symlink():
        raise ValueError('Delivered bundle must not be a symlink: %s' % bundle)
    if bundle.is_dir():
        raise ValueError('Delivered bundle is not a regular file: %s' % bundle)
    # O_NONBLOCK never waits on a fifo: without it, opening a fifo O_RDONLY blocks for
    # ever until a writer appears, so a listed verifier pointing --bundle at a fifo - or
    # a path swapped for one between the checks above and this open - hung the command
    # (kittrial-5bb.10 review item ``a-fifo-hangs``). fstat then judges the descriptor
    # that was actually opened, and the S_ISREG check below refuses the fifo at once.
    flags = (os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        handle_fd = os.open(str(bundle), flags)
    except OSError as exc:
        raise ValueError('Delivered bundle is not a readable file: %s' % bundle) from exc
    try:
        info = os.fstat(handle_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Delivered bundle is not a regular file: %s' % bundle)
        if info.st_size > BUNDLE_MAX_BYTES:
            raise ValueError('Delivered bundle is larger than the %d-byte observation limit: %s (%d bytes)'
                             % (BUNDLE_MAX_BYTES, bundle, info.st_size))
        with os.fdopen(handle_fd, 'rb') as handle:
            handle_fd = -1
            first = handle.readline(200)
            if not BUNDLE_SIGNATURE.match(first):
                raise ValueError('Delivered bundle is not a git bundle (its first line is not a bundle '
                                 'signature): %s' % bundle)
            digest = hashlib.sha256(first)
            size = len(first)
            while True:
                block = handle.read(BUNDLE_BLOCK_BYTES)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    finally:
        if handle_fd >= 0:
            os.close(handle_fd)
    observation = {'path': str(bundle), 'bytes': size, 'sha256': digest.hexdigest(),
                   'heads': [], 'heads_error': None}
    if git is None:
        observation['heads_error'] = 'no git runner was supplied, so the bundle heads were not listed'
        return observation
    try:
        raw = git(['bundle', 'list-heads', str(bundle)])
    except (ValueError, OSError) as exc:
        observation['heads_error'] = str(exc)[:200]
        return observation
    for line in str(raw or '').splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', parts[0]):
            observation['heads'].append({'commit': parts[0].lower(), 'ref': parts[1]})
    return observation


def bundle_commit_problem(observation, commit):
    """Why ``commit`` cannot be shown to exist in the observed bundle, or ``None``.

    The host observation (``read_bundle_observation``) lists the heads a bundle
    advertises. A ``verified`` attestation written by the host command is refused unless
    the observed commit IS one of those heads, which is what makes "a missing commit is
    never recorded as a pass" true on the host path: the record cannot name a commit the
    delivered bytes do not carry. Returns the reason as a sentence, or ``None`` when the
    commit is advertised (or when the caller supplied no heads to check, which the host
    command treats as a refusal for a ``verified`` verdict).
    """
    observation = observation if isinstance(observation, dict) else {}
    if observation.get('heads_error'):
        return ('the bundle heads could not be listed (%s), so the observed commit cannot be '
                'shown to exist in the bundle' % observation['heads_error'])
    heads = [str(head.get('commit') or '').lower() for head in observation.get('heads') or []
             if isinstance(head, dict)]
    if not heads:
        return 'the bundle advertises no head, so the observed commit cannot be shown to exist in it'
    if str(commit or '').lower() not in heads:
        return 'the observed commit %s is not one of the heads this bundle advertises' % commit
    return None


#: Delivered code over HTTP (docs/design/DELIVERY_CODE_OVER_HTTP.md, kittrial-5bb.373).
DELIVERY_SERVE_MAX_BYTES = 256 * 1024 * 1024
DELIVERY_NOT_AVAILABLE = 'The delivered bundle is not available on this service.'
DELIVERY_REMOTE_NO_BUNDLE = 'The delivery is remote; there is no bundle to serve.'
DELIVERY_CHECKSUM_MISMATCH = 'The delivered bundle does not match its recorded checksum.'
DELIVERY_SSH_ONLY_HTTP = (
    "Delivered bundles are served over HTTP only; on the host transport, read the "
    "contribution's recorded delivery.path directly."
)
DELIVERY_HOST_PATH = re.compile(
    r'^(?:(?![A-Za-z]:)[A-Za-z0-9][A-Za-z0-9._-]{1,63}:)?(/(?!/)[^\x00-\x1f\x7f\\]{1,400})\Z'
)
DELIVERY_WINDOWS_DRIVE_PATH = re.compile(
    r'^[A-Za-z]:[/\\](?![/\\])[^\x00-\x1f\x7f]{1,400}\Z'
)


class DeliveryUnavailableError(ValueError):
    """410 Gone: bundle is outside configured delivery_roots, missing, a symlink, or non-regular."""


class DeliveryTooLargeError(ValueError):
    """413 Payload Too Large: bundle size exceeds delivery_serve_max_bytes."""


class DeliveryCorruptError(ValueError):
    """503 Service Unavailable: bundle fails signature, SHA-256, or advertised head commit check."""


def strip_delivery_host_prefix(recorded_path):
    """Strip an optional ``HOST:`` prefix and return the absolute path, or ``None``.

    Host names are deliberately not compared (kittrial-5bb.373 precision 1). A
    ``HOST:`` prefix is stripped only when it is not a single-letter drive and the
    remainder is a POSIX absolute path (``/srv/...``). A Windows drive-letter
    absolute path (``D:\\...`` or ``D:/...``) is returned intact so its drive letter
    is never mistaken for a host prefix; drive-relative paths (``C:relative``) and
    relative or UNC paths are refused (``None``).
    """
    if not isinstance(recorded_path, str):
        return None
    match = DELIVERY_HOST_PATH.fullmatch(recorded_path)
    if match:
        return match.group(1)
    if DELIVERY_WINDOWS_DRIVE_PATH.fullmatch(recorded_path):
        return recorded_path
    return None


def _is_within_directory(child, parent):
    """Whether ``child`` resolves strictly inside ``parent`` using ``resolve()`` and ``os.path.normcase``."""
    try:
        child_resolved = Path(child).resolve()
        parent_resolved = Path(parent).resolve()
    except OSError:
        return False
    parent_norm = os.path.normcase(str(parent_resolved))
    if os.path.normcase(str(child_resolved)) == parent_norm:
        return False
    return any(
        os.path.normcase(str(ancestor)) == parent_norm
        for ancestor in child_resolved.parents
    )


def _parse_delivery_abs_path(path_str, *, allow_trailing_sep=False):
    """Parse a non-root POSIX or Windows drive-letter absolute path into ``(kind, drive, segments)``."""
    if not isinstance(path_str, str) or '\x00' in path_str:
        return None
    if path_str.startswith('/') and not path_str.startswith('//') and '\\' not in path_str:
        trimmed = path_str.rstrip('/') if (allow_trailing_sep and len(path_str) > 1) else path_str
        if trimmed == '/' or trimmed.endswith('/') or '//' in trimmed:
            return None
        segments = tuple(seg for seg in trimmed.split('/') if seg)
        if not segments or any(seg in ('.', '..') for seg in segments):
            return None
        return ('posix', '', segments)
    if len(path_str) >= 4 and path_str[0].isalpha() and path_str[1] == ':' and path_str[2] in ('/', '\\'):
        if path_str[3] in ('/', '\\'):
            return None
        drive = path_str[:2]
        rest = path_str[3:]
        if allow_trailing_sep:
            rest = rest.rstrip('/\\')
        if not rest or rest[-1] in ('/', '\\'):
            return None
        raw_segs = re.split(r'[/\\]', rest)
        if not raw_segs or any(seg == '' or seg in ('.', '..') for seg in raw_segs):
            return None
        return ('windows', drive, tuple(raw_segs))
    return None


def _step_child_component(parent_dir, part, *, case_insensitive=False):
    """Advance one path component under ``parent_dir``, matching case-insensitively for Windows drive paths on POSIX."""
    direct = parent_dir / part
    if not case_insensitive:
        return direct
    try:
        os.lstat(str(direct))
        return direct
    except OSError:
        pass
    folded = ntpath.normcase(part)
    try:
        with os.scandir(str(parent_dir)) as it:
            for entry in it:
                if ntpath.normcase(entry.name) == folded:
                    return parent_dir / entry.name
    except OSError:
        pass
    return direct


def _resolve_root_on_filesystem(base_anchor, segments, *, case_insensitive=False):
    """Resolve ``segments`` starting at ``base_anchor`` without following symlinks."""
    cur = Path(base_anchor)
    for seg in segments:
        cur = _step_child_component(cur, seg, case_insensitive=case_insensitive)
        try:
            st = os.lstat(str(cur))
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
    if not cur.is_dir():
        return None
    return cur


def _candidate_root_and_rel_parts(root_parsed, target_parsed):
    """Yield ``(root_path, walk_root, rel_parts, case_insensitive)`` candidates for ``target_parsed`` under ``root_parsed``."""
    root_kind, root_drive, root_segments = root_parsed
    target_kind, target_drive, target_segments = target_parsed

    if root_kind == 'windows' and target_kind == 'windows':
        if ntpath.normcase(root_drive) != ntpath.normcase(target_drive):
            return
        norm_seg = ntpath.normcase
        case_insensitive = os.name != 'nt'
        anchors = [root_drive + '\\'] if os.name == 'nt' else ['/']
    elif root_kind == 'posix' and target_kind == 'posix':
        norm_seg = os.path.normcase
        case_insensitive = False
        if os.name == 'nt':
            drives = []
            for sample in (os.getcwd(), tempfile.gettempdir()):
                drv = os.path.splitdrive(sample)[0]
                if drv and drv not in drives:
                    drives.append(drv)
            anchors = [drv + '\\' for drv in drives] + ['/']
        else:
            anchors = ['/']
    elif os.name == 'nt' and root_kind == 'windows' and target_kind == 'posix':
        norm_seg = ntpath.normcase
        case_insensitive = False
        anchors = [root_drive + '\\']
    elif os.name == 'nt' and root_kind == 'posix' and target_kind == 'windows':
        norm_seg = ntpath.normcase
        case_insensitive = False
        anchors = [target_drive + '\\']
    else:
        return

    for anchor in anchors:
        if case_insensitive:
            root_path = _resolve_root_on_filesystem(anchor, root_segments, case_insensitive=True)
            if root_path is None:
                continue
        else:
            root_path = Path(anchor).joinpath(*root_segments)
            if root_path.is_symlink() or not root_path.is_dir():
                continue

        if (
            len(target_segments) > len(root_segments)
            and [norm_seg(s) for s in target_segments[: len(root_segments)]]
            == [norm_seg(s) for s in root_segments]
        ):
            yield root_path, root_path, target_segments[len(root_segments) :], case_insensitive
            continue

        if os.name == 'nt':
            try:
                resolved_root = root_path.resolve()
            except OSError:
                continue
            res_parsed = _parse_delivery_abs_path(str(resolved_root), allow_trailing_sep=True)
            if res_parsed is not None:
                _, _, res_segs = res_parsed
                if (
                    len(target_segments) > len(res_segs)
                    and [norm_seg(s) for s in target_segments[: len(res_segs)]]
                    == [norm_seg(s) for s in res_segs]
                ):
                    yield root_path, resolved_root, target_segments[len(res_segs) :], False
                    continue
            if len(target_segments) > len(root_segments):
                prefix_dir = Path(anchor).joinpath(*target_segments[: len(root_segments)])
                try:
                    if (
                        not prefix_dir.is_symlink()
                        and prefix_dir.is_dir()
                        and os.path.normcase(str(prefix_dir.resolve()))
                        == os.path.normcase(str(resolved_root))
                    ):
                        yield root_path, prefix_dir, target_segments[len(root_segments) :], False
                except OSError:
                    continue


def resolve_delivery_bundle_path(recorded_path, roots):
    """Resolve ``recorded_path`` under ``roots`` without following symlinks.

    Returns ``(resolved_path, handle_fd, size)`` with an open ``O_RDONLY | O_NONBLOCK |
    O_NOFOLLOW`` regular-file descriptor, or raises :class:`DeliveryUnavailableError`
    with the single ``DELIVERY_NOT_AVAILABLE`` sentence so the route does not leak
    whether an arbitrary path exists outside ``delivery_roots``.
    """
    if not roots:
        raise DeliveryUnavailableError(DELIVERY_NOT_AVAILABLE)
    raw_path = strip_delivery_host_prefix(recorded_path)
    if raw_path is None:
        raise DeliveryUnavailableError(DELIVERY_NOT_AVAILABLE)
    target_parsed = _parse_delivery_abs_path(raw_path, allow_trailing_sep=False)
    if target_parsed is None:
        raise DeliveryUnavailableError(DELIVERY_NOT_AVAILABLE)
    flags = (
        os.O_RDONLY
        | getattr(os, 'O_NONBLOCK', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
        | getattr(os, 'O_BINARY', 0)
        | getattr(os, 'O_CLOEXEC', 0)
    )
    for root_entry in roots:
        root_parsed = _parse_delivery_abs_path(root_entry, allow_trailing_sep=True)
        if root_parsed is None:
            continue
        for root_path, walk_root, rel_parts, case_insensitive in _candidate_root_and_rel_parts(
            root_parsed, target_parsed
        ):
            if not rel_parts or any(part in ('.', '..') for part in rel_parts):
                continue
            cur = walk_root
            bad_component = False
            for part in rel_parts:
                cur = _step_child_component(cur, part, case_insensitive=case_insensitive)
                try:
                    st = os.lstat(str(cur))
                except OSError:
                    bad_component = True
                    break
                if stat.S_ISLNK(st.st_mode):
                    bad_component = True
                    break
            if bad_component:
                continue
            try:
                handle_fd = os.open(str(cur), flags)
            except OSError:
                continue
            try:
                info = os.fstat(handle_fd)
                if not stat.S_ISREG(info.st_mode):
                    os.close(handle_fd)
                    continue
                resolved_root = root_path.resolve()
                resolved_target = cur.resolve()
                if not _is_within_directory(resolved_target, resolved_root):
                    os.close(handle_fd)
                    continue
                after = os.lstat(str(resolved_target))
                if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                    os.close(handle_fd)
                    continue
                return str(resolved_target), handle_fd, info.st_size
            except OSError:
                try:
                    os.close(handle_fd)
                except OSError:
                    pass
                continue
    raise DeliveryUnavailableError(DELIVERY_NOT_AVAILABLE)


def _run_bundle_git(args):
    try:
        proc = subprocess.run(
            ['git'] + [str(a) for a in args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=BUNDLE_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(str(exc)) from exc
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or 'git bundle list-heads failed').strip())
    return proc.stdout


def verify_served_bundle(
    resolved_path,
    handle_fd,
    size,
    expected_sha256,
    expected_commit,
    max_bytes=DELIVERY_SERVE_MAX_BYTES,
    git=None,
):
    """Verify a resolved bundle fd before serving a single byte (kittrial-5bb.373).

    Checks:
      1. ``size <= max_bytes`` BEFORE reading or hashing (raises :class:`DeliveryTooLargeError`).
      2. First line matches ``BUNDLE_SIGNATURE`` (``# v2 git bundle`` / ``# v3 git bundle``).
      3. Streamed SHA-256 matches ``expected_sha256``.
      4. ``git bundle list-heads`` advertises ``expected_commit`` on the same inode.

    Leaves ``handle_fd`` rewound to offset 0 on success; closes ``handle_fd`` on any error.
    """
    try:
        if size > max_bytes:
            raise DeliveryTooLargeError(
                'The delivered bundle is %d bytes, exceeding the %d-byte serve limit (delivery_serve_max_bytes).'
                % (size, max_bytes)
            )
        stat_before = os.fstat(handle_fd)
        stat_snapshot = (
            stat_before.st_dev,
            stat_before.st_ino,
            stat_before.st_size,
            stat_before.st_mtime_ns,
            stat_before.st_ctime_ns,
        )
        os.lseek(handle_fd, 0, os.SEEK_SET)
        first_chunk = os.read(handle_fd, BUNDLE_BLOCK_BYTES)
        if not first_chunk:
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        newline_idx = first_chunk.find(b'\n')
        first_line = first_chunk[: newline_idx + 1] if newline_idx >= 0 else first_chunk[:200]
        if not BUNDLE_SIGNATURE.match(first_line):
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        digest = hashlib.sha256(first_chunk)
        total = len(first_chunk)
        while True:
            if total > max_bytes:
                raise DeliveryTooLargeError(
                    'The delivered bundle exceeds the %d-byte serve limit (delivery_serve_max_bytes).'
                    % (max_bytes,)
                )
            block = os.read(handle_fd, BUNDLE_BLOCK_BYTES)
            if not block:
                break
            total += len(block)
            digest.update(block)
        if total > max_bytes:
            raise DeliveryTooLargeError(
                'The delivered bundle is %d bytes, exceeding the %d-byte serve limit (delivery_serve_max_bytes).'
                % (total, max_bytes)
            )
        actual_sha = digest.hexdigest().lower()
        if not expected_sha256 or actual_sha != str(expected_sha256).lower():
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        info = os.fstat(handle_fd)
        if (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != stat_snapshot or total != info.st_size:
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        before_git = os.lstat(str(resolved_path))
        if (before_git.st_dev, before_git.st_ino) != (info.st_dev, info.st_ino):
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        runner = _run_bundle_git if git is None else git
        observation = {'path': str(resolved_path), 'bytes': total, 'sha256': actual_sha, 'heads': [], 'heads_error': None}
        try:
            raw = runner(['bundle', 'list-heads', str(resolved_path)])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            observation['heads_error'] = str(exc)[:200]
        else:
            for line in str(raw or '').splitlines():
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', parts[0]):
                    observation['heads'].append({'commit': parts[0].lower(), 'ref': parts[1]})
        after_git = os.lstat(str(resolved_path))
        if (after_git.st_dev, after_git.st_ino) != (info.st_dev, info.st_ino):
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        info_after = os.fstat(handle_fd)
        if (
            info_after.st_dev,
            info_after.st_ino,
            info_after.st_size,
            info_after.st_mtime_ns,
            info_after.st_ctime_ns,
        ) != stat_snapshot:
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        if bundle_commit_problem(observation, expected_commit) is not None:
            raise DeliveryCorruptError(DELIVERY_CHECKSUM_MISMATCH)
        os.lseek(handle_fd, 0, os.SEEK_SET)
        return {'bytes': total, 'sha256': actual_sha, 'stat_snapshot': stat_snapshot}
    except BaseException:
        try:
            os.close(handle_fd)
        except OSError:
            pass
        raise


def contribution_delivery_records(issue, operators=None):
    """Ordered contribution delivery records for ``issue`` after applied operator voids."""
    applied, _, _ = applied_review_voids(issue, operators)
    ordered = records(issue, set(applied))
    contributions = []
    revision = 0
    for payload, comment in ordered:
        if payload.get('operation') != 'contribute':
            continue
        revision += 1
        contributions.append({
            'task': str(issue['id']),
            'contribution': str(comment['id']),
            'revision': revision,
            'commit': payload['commit'],
            'base_commit': payload['base_commit'],
            'repository': payload['repository'],
            'author': str(comment.get('author') or ''),
            'at': str(comment.get('created_at') or ''),
            'delivery': dict(payload['delivery']),
        })
    return contributions



def integration_hold_sentence(state):
    """A sentence naming why a projected contribution is held, and who can clear it.

    ``state`` is a :func:`projection` result. Returns ``None`` when nothing holds the
    contribution. The sentence names the open hold record(s) - or the failed
    attestation record - and the authority that clears each (kittrial-5bb.10 items 3
    and 4), so every refusal (merge-acquire, a reviewed/integrated fact, a release
    selection or a release that names the task) can say both what is wrong and who can
    release it, instead of only that something is wrong.
    """
    state = state if isinstance(state, dict) else {}
    holds = [h for h in (state.get('holds') or []) if isinstance(h, dict)]
    if holds:
        ids = ', '.join(str(h.get('comment_id')) for h in holds[:5])
        return ('an open integration hold (record%s %s) is in force, and only a listed operator clears '
                'it with a release-hold record naming that hold'
                % ('s' if len(holds) != 1 else '', ids))
    if state.get('verification') == 'failed':
        attestation = state.get('attestation') if isinstance(state.get('attestation'), dict) else {}
        return ('a verifier recorded a FAILED attestation (record %s), cleared only by a later attestation '
                'from a listed verifier or a listed operator, or by a superseding revision that carries its '
                'own attestation' % (attestation.get('comment_id') or 'unknown'))
    return None


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
            p = record_json.loads(raw[len(PREFIX):]); validate(p, issue['id'])
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
            p = record_json.loads(raw[len(PREFIX):]); validate(p, issue['id'])
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


def applied_review_voids(issue, operators):
    """(applied, invalid, refused): operator voids that apply to a review chain.

    A valid void of a keyed record (kittrial-5bb.74) belongs to the anchor's kind
    (keyed_entries), which applies or reports it; it is no part of a review history.
    `applied` maps a voided comment id to its (payload, comment). Both `history` and
    `apply_revert` read the chain through this filter, so a malformed review comment an
    operator already voided no longer makes a later revert fail closed
    (kittrial-5bb.92 item 5).
    """
    voids, targets, invalid = recovery.records(issue, operators)
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
    return applied, invalid, refused


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
    applied, invalid, refused = applied_review_voids(issue, operators)
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
            from admin import read_regular_text
            from requirements import _unique_object
            entry = record_json.loads(read_regular_text(path, limit=JOURNAL_FILE_LIMIT),
                                      object_pairs_hook=_unique_object)
            canonical_bytes(entry)
            entry = validate_revert_journal_entry(entry, name)
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
            p = record_json.loads(raw[len(REVERT_PREFIX):])
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
    # projection currently reports as its passing integration. The chain is read
    # through the applied operator voids, exactly as every read is, so a malformed
    # review comment an operator already voided no longer fails this command closed
    # (kittrial-5bb.92 item 5).
    state = projection(records(issue, set(applied_review_voids(issue, operators)[0])))
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
               invalid_reverts=None, closed=False):
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
    record is never dropped silently (voids already warn).

    ``closed`` is the native task status: a closed task has no OPEN review requests
    (kittrial-5bb.94 item 4), so ``pending_review_requests`` reads empty for it.
    That is read-side only; the stored records stay in the chain.

    Additive (kittrial-5bb.94): ``note_requests`` are non-blocking request-changes
    items, ``pending_review_requests`` are open first-class review requests,
    ``declined_review_requests`` are the requests their named reviewer closed, and
    ``withdrawal`` is the withdraw/supersede record for the current contribution
    (``review_state`` is then ``withdrawn`` or ``superseded``). All are empty or
    null on a chain written before these operations existed.

    A ``withdraw`` no longer clears the contribution's unresolved ITEMS (item 1):
    they are carried to the task's next contribution exactly as a ``supersede``
    carries them, so the approve gate still refuses there. It still closes the
    contribution's review requests, and a SECOND withdraw on the same contribution
    does not flip the disposition - the first one stands and the ignored record is
    warned about (item 5), so a chain that already holds two still reads."""
    contribution = None; prior = []; pending = {}; notes = {}; reviews = {}
    declined = []; approved = False; approved_id = None; latest = None; withdrawal = None
    ignored_withdrawals = []; last_response = None
    # Verifier attestations and explicit integration holds (kittrial-5bb.10). An
    # attestation belongs to the revision it was recorded against, so it resets with a
    # new contribution; a HOLD does not: it survives retries and new revisions and is
    # cleared only by an explicit, audited ``release-hold`` record.
    attestation = None; attestations = []
    holds = {}; hold_releases = []; ignored_releases = []
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
            # A new contribution starts review again: it clears any withdrawal and
            # every review request that named the replaced revision. It does NOT clear
            # an integration HOLD (kittrial-5bb.10): a hold outlives the revision it was
            # set against and is released only by an explicit ``release-hold``, so a new
            # revision can never accidentally clear it. The verifier attestation belongs
            # to the revision, so it does reset here.
            contribution = dict(p, **metadata); approved = False; approved_id = None
            withdrawal = None; reviews = {}; attestation = None
        else:
            if not current or p['contribution'] != current:
                raise ValueError(describe_contribution_mismatch(op, p['contribution'], current, latest))
            if withdrawal is not None and op != 'withdraw':
                # A withdrawn/superseded contribution is final for its revision: the
                # only way back into review is a new contribution (above), so a
                # stray later review operation fails closed instead of reviving it.
                raise ValueError('Contribution was ' + withdrawal['disposition'] +
                                 '; deliver a new revision to continue review')
            if op == 'request-changes':
                for item in p['items']:
                    severity = item.get('severity', 'blocking')
                    entry = dict(request=cid, item=item['id'], text=item['text'],
                                 severity=severity, contribution=current, author=c['author'],
                                 timestamp=c['created_at'],
                                 # The reviewer's additive request summary travels with
                                 # each item, so `review TASK` returns it in structured
                                 # form instead of only in raw history (item 4).
                                 summary=p.get('summary'))
                    # Only a blocking item holds up approval; a note is recorded and
                    # stays visible, but never blocks (kittrial-5bb.94 item 2).
                    (notes if severity == 'note' else pending)[(cid, item['id'])] = entry
                if len(pending) + len(notes) > 20:
                    raise ValueError('At most 20 unresolved review items are allowed')
                approved = False
            elif op == 'respond':
                last_response = dict(metadata)
                for item in p['resolutions']:
                    key = (item['request'], item['item'])
                    if key in pending:
                        del pending[key]
                    elif key in notes:
                        del notes[key]
                    else:
                        raise ValueError('Resolution must reference an unresolved request/item')
                approved = False
            elif op == 'resolve-item':
                # The REQUESTER resolves (or downgrades) their own item, without the
                # task owner. The native author must be the item's requester: the
                # comparison is the normalised attribution key, so `RITA` and
                # `rita/anything` are accepted as `rita` (docs/REVIEWS.md; the
                # self-declared-actor limit of kittrial-5bb.106).
                key = (p['request'], p['item'])
                entry = pending.get(key) or notes.get(key)
                if entry is None:
                    raise ValueError('Resolution must reference an unresolved request/item')
                if author_key(c['author']) != author_key(entry['author']):
                    raise ValueError('Only the requester may resolve their own review item')
                disposition = p.get('disposition')
                if disposition is None:
                    # A stored null is the operation's DEFAULT (RESOLVE_DEFAULT), never a
                    # downgrade to a note and never the item's own severity: such a record
                    # RESOLVES the item for a blocking item and for a note item alike.
                    disposition = RESOLVE_DEFAULT
                if disposition == 'note':
                    pending.pop(key, None)
                    notes[key] = dict(entry, severity='note')
                else:
                    pending.pop(key, None); notes.pop(key, None)
                approved = False
            elif op == 'request-review':
                if any(author_key(request['reviewer']) == author_key(p['reviewer'])
                       and request['contribution'] == current for request in reviews.values()):
                    raise ValueError('A review request for that reviewer is already open')
                reviews[cid] = dict(request=cid, reviewer=p['reviewer'],
                                    summary=p.get('summary'), contribution=current,
                                    author=c['author'], timestamp=c['created_at'])
            elif op == 'decline-review':
                # The NAMED reviewer closes their own request (item 4), so it leaves
                # their `work --mine` queue instead of standing open for ever.
                request = reviews.get(p['request'])
                if request is None:
                    raise ValueError('Decline must name an open review request for this contribution')
                if author_key(request['reviewer']) != author_key(c['author']):
                    raise ValueError('Only the named reviewer may decline their own review request')
                del reviews[p['request']]
                declined.append(dict(request=p['request'], reviewer=request['reviewer'],
                                     reason=p['reason'], contribution=current,
                                     author=c['author'], timestamp=c['created_at']))
            elif op == 'attest':
                # Kept SEPARATE from the declared delivery: this entry records what the
                # verifier observed and whether it matched. A stored record that no longer
                # matches is demoted here (verified=False + problems) and never raises, so
                # an old chain keeps reading; ``execute`` refuses such a write outright.
                problems = attestation_problems(p, contribution)
                attestation = dict(comment_id=cid, contribution=current, verdict=p['verdict'],
                                   commit=p['commit'], base_commit=p['base_commit'],
                                   sha256=p.get('sha256'), bytes=p.get('bytes'),
                                   method=p['method'], reason=p['reason'],
                                   author=c['author'], timestamp=c['created_at'],
                                   verified=(p['verdict'] == 'verified' and not problems),
                                   problems=problems)
                attestations.append(attestation)
            elif op == 'hold':
                holds[cid] = dict(comment_id=cid, contribution=current, reason=p['reason'],
                                  author=c['author'], timestamp=c['created_at'])
            elif op == 'release-hold':
                # A release clears exactly the hold it names, and only while that hold is
                # open. A stray, duplicated or already-released name changes nothing and is
                # warned about, so a retry or a new revision can never clear a hold by
                # accident (kittrial-5bb.10).
                target = holds.get(p['hold'])
                if target is None:
                    ignored_releases.append(cid)
                else:
                    del holds[p['hold']]
                    hold_releases.append(dict(comment_id=cid, hold=p['hold'],
                                              contribution=current, reason=p['reason'],
                                              author=c['author'], timestamp=c['created_at']))
            elif op == 'withdraw':
                if withdrawal is not None:
                    # A second withdraw must not flip the disposition (item 5). The
                    # read stays tolerant: the first record stands and the ignored
                    # one is warned about below.
                    ignored_withdrawals.append(cid)
                else:
                    # A stored record an older kit wrote with an explicit null
                    # disposition reads as the operation default, never as None: the
                    # state, `review`, `brief` and `work` all show `withdrawn`
                    # (kittrial-5bb.110 item 2).
                    withdrawal = dict(request=cid, disposition=p.get('disposition') or 'withdrawn',
                                      reason=p['reason'], contribution=current, author=c['author'],
                                      timestamp=c['created_at'])
                    # A withdraw closes the contribution's review requests, exactly
                    # like a new contribution does. It deliberately does NOT clear
                    # the unresolved ITEMS (item 1): a supersede carries them into
                    # the next contribution, and so must a withdraw, or the approve
                    # gate could be sidestepped by withdrawing and re-contributing.
                    reviews.clear()
                    approved = False; approved_id = None
            elif op == 'approve':
                if pending:
                    raise ValueError('Cannot approve while review requests remain unresolved')
                approved = True; approved_id = cid
            if op == 'approve':
                # An approval settles the contribution: no review request for it
                # stays open, whoever recorded the approval (item 4).
                reviews.clear()
            elif op == 'request-changes':
                # A named reviewer closes their own request by requesting changes on
                # the same contribution.
                for key, request in list(reviews.items()):
                    if (request['contribution'] == p['contribution']
                            and author_key(request['reviewer']) == author_key(c['author'])):
                        del reviews[key]
        latest = cid
    if closed:
        # A closed task has no open review request: closure settles them, so
        # `review`, `brief` and `work` agree (item 4). The records stay in the chain.
        reviews = {}
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
    # Verification reading (kittrial-5bb.10): what the newest attestation for the CURRENT
    # revision actually established. A demoted record (problems) reads ``unverified``,
    # never ``verified``; no attestation at all also reads ``unverified``. A ``failed``
    # reading is a positive disproof, so it holds the contribution exactly as an explicit
    # hold does; ``unknown`` (an unreachable remote) is surfaced but never fabricated into
    # a pass and never silently treated as one.
    if attestation is None:
        verification = VERIFICATION_UNVERIFIED
    elif attestation['verified']:
        verification = 'verified'
    elif attestation['problems']:
        verification = VERIFICATION_UNVERIFIED
    else:
        verification = attestation['verdict']
    verification_failed = verification == 'failed'
    held = bool(holds) or verification_failed
    # The `held` STATE is what an APPROVED contribution shows INSTEAD OF
    # awaiting-integration (kittrial-5bb.10 items 4 and 7). A hold (or a failed
    # attestation) on a revision that is not approved does not take that revision out of
    # review: the author's open requested changes still lead, and a revision still
    # awaiting review stays in the reviewers' queue, with the hold shown beside it (the
    # `holds`/`verification` fields and the warnings). A held contribution is therefore
    # never integration-ready, while a revision under a standing hold stays reviewable.
    state = ('none' if contribution is None else
             withdrawal['disposition'] if withdrawal else
             'changes-requested' if pending else
             'held' if (held and approved) else
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
    if ignored_withdrawals:
        warnings.append('Ignored a second withdraw record on the same contribution (records: ' +
                        ', '.join(ignored_withdrawals[:5]) + '); a contribution is withdrawn once, so the '
                        'first disposition stands and the later record changed nothing')
    open_holds = [dict(h) for h in holds.values()]
    if open_holds:
        warnings.append('Integration hold(s) in force (records: ' +
                        ', '.join(h['comment_id'] for h in open_holds[:5]) +
                        '); this contribution is not integration-ready until each hold is '
                        'explicitly released with a release-hold record')
    if attestation is not None and attestation['problems']:
        warnings.append('Verifier attestation ' + attestation['comment_id'] +
                        ' does not match the declared delivery (' +
                        '; '.join(attestation['problems'][:3]) +
                        '); it is read as unverified, never as verified')
    if verification == 'failed':
        warnings.append('Verifier attestation ' + str(attestation['comment_id']) +
                        ' records the delivery as FAILED (' + warning_text(attestation['reason']) +
                        '); the contribution is held from integration until a later attestation or '
                        'a corrected delivery supersedes it')
    elif verification == 'unknown':
        warnings.append('Verifier attestation ' + str(attestation['comment_id']) +
                        ' records the delivery as an explicit UNKNOWN (' +
                        warning_text(attestation['reason']) + '); no pass is recorded and '
                        'integration must not be inferred from the declared checksum alone')
    if ignored_releases:
        warnings.append('Ignored release-hold record(s) naming no open hold: ' +
                        ', '.join(ignored_releases[:5]) + '; a release clears exactly one open hold '
                        'and changes nothing else')
    answer = dict(contribution=contribution, prior_contributions=prior, review_state=state,
                  pending_requests=list(pending.values()), latest_comment_id=latest,
                  last_response=last_response,
                  # Additive (kittrial-5bb.94): non-blocking items, open first-class
                  # review requests, the requests their named reviewer declined, and
                  # the withdrawal/supersede record for the current contribution. All
                  # are absent/empty on an old-shaped chain.
                  note_requests=list(notes.values()), pending_review_requests=list(reviews.values()),
                  declined_review_requests=declined,
                  withdrawal=withdrawal,
                  recoveries=recoveries, warnings=warnings, reverts=list(reverts or []))
    # Additive (kittrial-5bb.10) and present ONLY when they carry something, exactly as
    # the reconciliation signal does: a chain written before these operations existed then
    # reads byte-identically to before them (the bounded brief has a size budget), and an
    # absent ``verification`` means the same thing as ``unverified``. ``holds`` and
    # ``verification`` are the two a reader asks about a contribution that is not
    # integration-ready; ``attestations``/``hold_releases``/``ignored_hold_releases`` are
    # the bounded audit lists.
    if attestation is not None:
        answer['attestation'] = attestation
        answer['verification'] = verification
    if attestations:
        answer['attestations'] = attestations[-ATTEST_LIMIT:]
    if open_holds:
        answer['holds'] = open_holds
    if hold_releases:
        answer['hold_releases'] = hold_releases
    if ignored_releases:
        answer['ignored_hold_releases'] = ignored_releases
    return answer


def project(issue, operators=None, journal=None, reverts=None, invalid_reverts=None):
    """Raw workflow projection for one issue; ``journal`` is the project directory
    holding the host-issued ``.integration-reverts/`` journal (None trusts no
    revert). A caller that already resolved the per-task reverts for a whole page
    passes ``reverts``/``invalid_reverts`` so the scan is not repeated. The native
    task status is read here so a closed task reports no open review requests."""
    ordered, voids, invalid, refused, positions, reverts, invalid_reverts = history(
        issue, operators, journal, reverts=reverts, invalid_reverts=invalid_reverts)
    return projection(ordered, voids, invalid, refused, positions, reverts, invalid_reverts,
                      closed=issue.get('status') == 'closed')


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


def independent_approval_state(issue, operators=None, owners=None, journal=None):
    """``(True, None)`` when the CURRENT contribution carries an approve from another party.

    The second half of the task's rule 3 (kittrial-5bb.197, part b): ``reviewed`` and
    ``integrated`` may be recorded, and the merge slot taken, only while the CURRENT
    contribution carries a validated ``approve`` record from a party other than the
    contribution's author AND the task's assignee. The projection already resets an
    approval when a new contribution supersedes or follows the old one, so an
    approval of an OLDER contribution cannot satisfy a newer one; a void applied
    after the approval clears it the same way. The approving party is compared with
    ``same_party``: equal known principals OR equal normalised names. This reader
    remains available when the registry cannot be read; writers load ownership
    strictly before using it. Returns ``(False, reason)`` when the contribution does not qualify, so the
    caller can refuse with the setting named.
    """
    ordered, voids, invalid, refused, positions, reverts, invalid_reverts = history(
        issue, operators, journal)
    state = projection(ordered, voids, invalid, refused, positions, reverts, invalid_reverts,
                       closed=issue.get('status') == 'closed')
    contribution = state.get('contribution') or {}
    if not contribution:
        return False, 'this task has no contribution to record that fact against'
    if state.get('review_state') != 'awaiting-integration' or state.get('pending_requests'):
        return False, 'the current contribution has no standing approval from another party'
    entry = approving_entry(ordered, contribution['comment_id'])
    if entry is None:
        return False, 'the current contribution has no approval record'
    approving, approval = entry
    author = contribution.get('author')
    approver = approval.get('author')
    if same_party(approver, author, owners):
        return False, ("its approval came from the contribution author's party")
    assignee = issue.get('assignee')
    if isinstance(assignee, str) and assignee and same_party(approver, assignee, owners):
        return False, ("its approval came from the task assignee's party")
    return True, None


APPROVAL_REFUSAL = ('%s refused: the installation setting independent_approval is on, and nobody passes their '
                    'own work that way; the CURRENT contribution of %s must carry a validated approve record from '
                    'a party other than its author and the task\'s assignee before %s. %s. An operator or a '
                    'reviewer of another party approves it with `review TASK --file approve.json`, or an operator '
                    'turns the rule off with `admin.py coordinator-rules off --rule independent_approval --actor '
                    'OPERATOR`.')


def require_independent_approval(issue, what, task, operators=None, owners=None, journal=None):
    """Raise the one refusal for part (b), or return when the contribution qualifies."""
    owners = approval_owners(owners)
    allowed, reason = independent_approval_state(issue, operators, owners, journal)
    if allowed:
        return
    raise CoordinatorRuleRefusal(APPROVAL_REFUSAL % (what, task, what, reason))


def require_integrated_follow_on(payload, state, ordered, rows, task, assignee=None, operators=None,
                                 journal=None, owners=None):
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
      follow-on ``base_commit`` must be that scope's ``integration_commit`` or an
      integration commit the project recorded after it (kittrial-5bb.148; see
      ``recorded_integrations`` for what that can and cannot know).
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
    base = payload['base_commit'].lower()
    if base == evidence['integration_commit'].lower():
        return
    # Main usually moves before a follow-on is ready (kittrial-5bb.148). The base may also
    # be an integration commit this project recorded AFTER the prior one. See
    # recorded_integrations for what that can and cannot know.
    recorded, newest = recorded_integrations(rows, task, evidence, operators, journal, owners)
    found = recorded.get(base)
    if found == 'later':
        return
    if isinstance(found, tuple):
        # Recorded as an integration, but not under a listed operator's name: say who recorded
        # it and what to do, so a coordinator who is not on the operator list sees it at once.
        found, recorder = found
        unlisted = BASE_WHY_UNLISTED % recorder
    else:
        unlisted = None
    why = {None: BASE_WHY_NEVER, 'unlisted': unlisted, 'earlier': BASE_WHY_EARLIER, 'reverted': BASE_WHY_REVERTED}[found]
    raise ValueError(BASE_RULE % (evidence['integration_commit'], BASE_NEWEST % newest if newest else BASE_NONE_YET,
                                  payload['base_commit'], why))


#: The follow-on base refusal, in its parts: the rule, the newest acceptable later commit (or
#: none), the refused base and one of four reasons. The text is unchanged since
#: kittrial-5bb.155; the parts are named so that :data:`BASE_REFUSAL` is built from them.
BASE_RULE = ('Contribution base_commit must be the prior integration commit %s, or an integration commit '
             'this project recorded after it (a passed integrated fact on any task, recorded by a listed '
             'operator or carrying a valid host-issued web coordinate mark, not reverted)%s. '
             '%s is not accepted: %s')
BASE_NEWEST = '; the newest such commit is %s'
BASE_NONE_YET = '; none is recorded yet'
BASE_WHY_NEVER = 'this project has no passed integrated fact that names it as an integration commit'
BASE_WHY_EARLIER = ('this project recorded it as an integration commit before the prior integration, or in the '
                    'same second, not after it')
BASE_WHY_REVERTED = 'an operator revert names it'
BASE_WHY_UNLISTED = ('it was recorded as an integration commit by %s, who is not a listed operator of this '
                     'installation. Integrations must be recorded by a listed operator for a later base to count: '
                     'an operator adds the recorder (admin.py operators add) or records the integration')
#: How the refusal shows a recorder: the name in quotes when it is a plain name, else these words.
RECORDER_NAME = r'[A-Za-z0-9_.:@/-]{1,80}'
RECORDER_UNNAMED = 'an actor'


def _base_refusal():
    """The whole refusal as a pattern: the kit's own words around commit ids and a plain recorder name.

    The web service hands a canonical refusal on up to 200 characters, and this sentence is
    longer (kittrial-5bb.158). It may hand THIS one on whole, because a line that matches
    holds nothing but the kit's text, hexadecimal commit ids and a recorder name of the
    shape above. A line with anything else in a commit's place (a scope may name any string
    as its integration commit) does not match, and is cut as before.
    """
    commit = r'[0-9A-Fa-f]{7,64}'
    recorder = '(?:"%s"|%s)' % (RECORDER_NAME, re.escape(RECORDER_UNNAMED))
    reasons = '(?:%s)' % '|'.join([re.escape(BASE_WHY_NEVER), re.escape(BASE_WHY_EARLIER), re.escape(BASE_WHY_REVERTED),
                                   re.escape(BASE_WHY_UNLISTED).replace('%s', recorder)])
    newest = '(?:%s|%s)' % (re.escape(BASE_NEWEST).replace('%s', commit), re.escape(BASE_NONE_YET))
    pattern = re.escape(BASE_RULE)
    for part in (commit, newest, commit, reasons):
        pattern = pattern.replace('%s', part, 1)
    return re.compile(pattern)


BASE_REFUSAL = _base_refusal()


def _recorded_at(value):
    """A tracker timestamp as an aware datetime, or None when it cannot be read."""
    import datetime
    if not isinstance(value, str) or not value:
        return None
    # Python 3.10 reads a fraction of exactly three or six digits only. The tracker stamps
    # comments to the nanosecond; an event stamped that way must not read as "no time"
    # (kittrial-5bb.155), so the fraction is brought to microseconds first.
    value = re.sub(r'\.(\d+)', lambda found: '.' + (found.group(1) + '000000')[:6], value, count=1)
    try:
        stamp = datetime.datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=datetime.timezone.utc)


def recorded_integrations(rows, task, prior, operators=None, journal=None, owners=None):
    """``({commit: 'later' | 'earlier' | 'reverted'}, newest later commit or None)`` for a follow-on's base.

    ``prior`` is the integration evidence of ``task``'s prior revision (``review_state.integration``).
    A commit reads ``later`` when a passed ``integrated`` fact on ANY task of the project
    names it as its scope's ``integration_commit``, **that fact has an eligible
    recorder**, the event that recorded it was created after the event that recorded the
    prior revision's integration, and no operator revert that the host journal confirms
    names the commit, on any task. Both times are the tracker's own stamps on the event
    rows, never a caller's. The tracker stamps whole seconds, and two events stamped the
    same second cannot be ordered (the export lists rows by id, not by time), so "the same
    second" is not after: a later base recorded in the prior integration's own second is
    refused, never an earlier one accepted. A time that is missing or cannot be read is
    not "after".

    **Only an eligible recorder's fact counts** (kittrial-5bb.155/.210): a present
    listed operator/bound principal or an exact host-issued web-coordinate mark.
    Marks retain historical authority after roles and grants change; plain exports
    with no trusted host context cannot confer it. Any actor can record an
    ``integrated`` fact on a task of its own making, so a fact recorded by anybody else
    names no base: a contributor could otherwise make any commit acceptable with a decoy
    task. ``operators`` is the installation's allowlist; without an eligible operator
    or valid issued mark no later base is accepted. This is the interim rule until kittrial-5bb.106 settles who may
    write lifecycle facts. It does not stop a caller who reaches the endpoint over SSH and
    names itself as an operator: there an actor name is a declared label. Over HTTP the
    actor is the authenticated account.

    **What this cannot know.** The endpoint has no git repository, so it cannot tell that
    a recorded integration commit descends from the prior one. It knows only that the
    project recorded it, later, as an integration. Two release lines in one project, or a
    fact recorded with the wrong commit, would pass; the reviewer checks the base as for
    any contribution. A commit of main that no lifecycle fact names is never accepted.
    """
    from lifecycle import integration_evidence, host_snapshot
    import web_lifecycle_marks as marks
    if journal is not None:
        rows = host_snapshot(rows, project=journal)
    native_events = {row.get('id'): row for row in rows
                     if isinstance(row, dict) and row.get('issue_type') == 'event'}
    from review_state import reverts_by_task
    # When each event was recorded: the tracker's stamp. Two events stamped the same second
    # cannot be ordered: the export lists rows by id, not by time (measured on real bd,
    # kittrial-5bb.155), so a position in it says nothing about which came first.
    created = {row.get('id'): _recorded_at(row.get('created_at')) for row in rows
               if isinstance(row, dict) and row.get('issue_type') == 'event'}
    if owners is None and journal is not None:
        owners = operator_owners(journal)
    loaded = []
    def once_owners():
        if not loaded:
            loaded.append(owner_map(owners))
        return loaded[0]
    reverted = set()
    for found in reverts_by_task(rows, operators, journal)[0].values():
        reverted |= {str(record.get('integration_commit') or '').lower() for record in found}
    evidence = integration_evidence(rows)
    # When the prior revision's own integration was recorded: the event of its scope on this task.
    prior_time = None
    for scope in next((entry['scopes'] for entry in evidence if entry['id'] == task), []):
        if scope.get('scope_token') == prior.get('scope_token'):
            prior_time = created.get((scope.get('integrated') or {}).get('event_id'))
    recorded, newest, rank = {}, None, {'reverted': 3, 'later': 2, 'unlisted': 1, 'earlier': 0}
    for entry in evidence:
        for scope in entry['scopes']:
            fact = scope.get('integrated') or {}
            commit = str((scope.get('scope') or {}).get('integration_commit') or '').lower()
            if fact.get('value') != 'passed' or not commit:
                continue
            stamp = created.get(fact.get('event_id'))
            if commit in reverted:
                reading = 'reverted'
            elif prior_time is None or stamp is None or not stamp > prior_time:
                reading = 'earlier'
            elif marks.snapshot_eligible(rows, native_events.get(fact.get('event_id')), operators, once_owners):
                reading = 'later'
            else:
                # Later, and recorded by somebody who is not a listed operator: not a base. The
                # name is shown only when it is a plain actor name; it is caller text.
                actor = fact.get('actor')
                shown = '"%s"' % actor if isinstance(actor, str) and re.fullmatch(RECORDER_NAME, actor) \
                    else RECORDER_UNNAMED
                reading = ('unlisted', shown)
            # A commit recorded more than once: a revert wins, then a later recording by a
            # listed operator, then a later one by anybody else, then an earlier one.
            key = reading[0] if isinstance(reading, tuple) else reading
            had = recorded.get(commit)
            if had is None or rank[key] > rank[had[0] if isinstance(had, tuple) else had]:
                recorded[commit] = reading
            if reading == 'later' and (newest is None or stamp > newest[0]):
                newest = (stamp, commit)
    return recorded, (newest[1] if newest else None)


def _recorded_operator(actor, operators, owners):
    """Unreadable ownership never confers the common bound-operator eligibility."""
    try:
        return listed_operator_actor(actor, operators, owners)
    except (ValueError, OSError):
        return False


def _retry_matches(stored, incoming):
    """Whether a retried payload matches the stored record apart from server-derived fields.

    ``execute`` stamps ``assignee_at_approval`` before the append, so a caller can
    never reproduce the stored approve bytes exactly. The snapshot is ignored on
    both sides so a retry of the same operation stays idempotent even when the
    assignee changed in between.

    ``attest`` additionally has its ``previous`` derived from the chain head by the
    host command (``admin.py attest-delivery``), so an exact rerun of the same
    observation by the same actor necessarily names the record it wrote first
    instead of the record before it; that alone used to raise "Operation ID already
    used with different payload or actor" (kittrial-5bb.10 review item
    ``an-exact-rerun-and-the-load-rule``). ``previous`` is the chain-ordering
    pointer, not part of the observed facts, so it is ignored for ``attest`` ALONE:
    every other operation, and every other field of ``attest``, still refuses a
    different payload.
    """
    ignored = {ASSIGNEE_SNAPSHOT}
    if isinstance(incoming, dict) and incoming.get('operation') == 'attest':
        ignored.add('previous')
    def scrubbed(p):
        return {k: v for k, v in p.items() if k not in ignored} if isinstance(p, dict) else p
    return scrubbed(stored) == scrubbed(incoming)


def open_review_requests_by(rows, actor, operators=None, journal=None):
    """How many OPEN review requests the actor has recorded across every task.

    The cap on open requests per requester is a project-wide one (item 4): one
    actor putting 25 tasks into another's queue in 71 seconds is not a per-task
    problem. Every task's chain whose projection can be read contributes its
    ``pending_review_requests`` authored by `actor` (normalised attribution key);
    a chain that cannot be read contributes nothing rather than failing the write,
    exactly as ``work.queue`` treats an unreadable row.
    """
    total = 0
    for row in rows or []:
        if not isinstance(row, dict) or row.get('issue_type') in ('event', 'gate') or is_merge_slot(row):
            continue
        if not any(isinstance(c, dict) and isinstance(c.get('text'), str) and c['text'].startswith(PREFIX)
                   for c in row.get('comments') or []):
            continue
        try:
            state = project(row, operators, None, [], [])
        except (ValueError, KeyError, TypeError):
            continue
        total += sum(1 for request in state.get('pending_review_requests') or []
                     if author_key(request.get('author')) == author_key(actor))
    return total


def execute(rows, task, actor, payload, run, operators=None, journal=None, review_writes=None,
            independent_approval=False, coordinator_writes=False, owners=None, verified_web_authority=False,
            verifiers=None, identity_names=None):
    """Validate, CAS and append once; return receipt and projected review state.

    ``journal`` is the project directory holding the host-issued
    ``.integration-reverts/`` journal; an absent/unusable journal trusts no revert,
    so a reverted integration commit never counts as a follow-on base on a host
    that cannot prove the revert was issued there.

    ``review_writes`` is the per-installation switch for WRITING the new record
    shapes (``admin.review_workflow_writes``, supplied by the endpoint from
    ``deployment.private.json``; the default is OFF). The readers understand the
    new operations and fields either way - see the module docstring.

    ``independent_approval`` and ``coordinator_writes`` are the two per-installation
    settings of slices 6 and 7 (``admin.coordinator_rules``, supplied by the endpoint;
    both default OFF, so an installation that configures nothing behaves as before).
    ``owners`` is the project's actor -> principal map (``sessions.owners``), or a
    callable returning it so the registry is read only when a rule needs it, used by
    the principal half of both rules. For ``attest``, ``hold`` and ``release-hold`` the
    map is resolved whatever the coordinator settings say, because the party rule is not
    switchable (kittrial-5bb.10 review item 2); an unreadable registry compares names
    and the receipt carries a ``warnings`` sentence saying parties could not be compared.
    ``verified_web_authority`` says the request came through the
    web route with a verified approval capability for an owner, coordinator, or granted
    agent. The configured live authority establishes the caller (kittrial-5bb.199,
    kittrial-5bb.209), so ``coordinator_writes`` does not refuse it. Web slot operations
    use the same helper for their capability; host operations retain listed actors.
    """
    for row in rows or []:
        if isinstance(row, dict) and row.get('id') == task and is_merge_slot(row):
            raise ValueError(merge_slot_sentence(task) + '; it takes no review record')
    if isinstance(payload, dict) and payload.get('operation') == recovery.OPERATION:
        raise ValueError('Operator void records are not accepted over the contributor review transport; '
                         'an operator must use admin.py void-record on the coordination host')
    if isinstance(payload, dict) and payload.get('operation') == REVERT_OPERATION:
        raise ValueError('Integration revert records are not accepted over the contributor review '
                         'transport; an operator must use admin.py revert-record on the coordination host')
    validate(payload, task); text(actor, 'actor', 300)
    # A null optional disposition is refused on the WRITE path before anything else, so
    # the writer never stores the shape `validate` must keep reading (item 2).
    check_write_fields(payload)
    matches = [r for r in rows if r.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise ValueError('Task missing, duplicated or is an event')
    issue = matches[0]
    if issue.get('malformed'):
        raise ValueError('Task %s is malformed: %s' % (task, issue.get('error') or 'cannot parse row'))
    closed = issue.get('status') == 'closed'
    ordered, voids, invalid, refused, positions, reverts, invalid_reverts = history(issue, operators, journal)
    state = projection(ordered, voids, invalid, refused, positions, reverts, invalid_reverts, closed=closed)
    effective_state = receipt(state, rows, task)['review_state']
    # The party rule of attest/hold/release-hold is NOT one of the switchable
    # coordinator rules (kittrial-5bb.10 review item 2): resolve the actor -> principal
    # map for these three operations whatever the installation rules say. An unreadable
    # registry compares names and the answer carries `party_problem`, so the caller is
    # told parties could not be compared instead of the principal half being dropped
    # silently.
    party_problem = None
    if payload['operation'] in PARTY_OPERATIONS:
        owners, party_problem = load_party_owners(owners)
    # Exact retries remain recoverable after ownership changes or later revisions,
    # including after the write switch is turned off: the record already exists.
    for p, c in ordered:
        if p['operation_id'] == payload['operation_id']:
            if _retry_matches(p, payload) and c['author'] == actor:
                answer = dict(comment_id=str(c['id']), reconciled=True, **receipt(state, rows, task))
                if party_problem:
                    answer['warnings'] = list(answer.get('warnings') or []) + [party_problem]
                return answer
            raise ValueError('Operation ID already used with different payload or actor')
    # Staged rollout (item 3): a new-shaped write is refused unless this
    # installation turned the switch on. Reads are unaffected.
    if new_write_requested(payload) and not review_writes:
        raise ValueError(new_write_refusal(payload))
    check_plain_text(payload)
    voided_operations = _voided_operation_ids(issue, voids)
    if payload['operation_id'] in voided_operations:
        raise ValueError('Operation ID ' + payload['operation_id'] + ' belongs to a voided contribution-review '
                         'record; operator recovery removed that revision, so retry it as a new operation')
    if payload['previous'] != state['latest_comment_id']:
        raise StaleReviewPrevious(task, payload['previous'],
                                  state['latest_comment_id'], effective_state)
    # EXACT comparison only (kittrial-5bb.333): a stored assignee is never interpreted as a
    # display name at effect time. The message may LABEL an actor id with its display name --
    # actor -> name cannot move the right to another actor.
    if payload['operation'] in ('contribute', 'respond') and (not issue.get('assignee') or actor != issue['assignee']):
        assigned=issue.get('assignee')
        shown=assigned
        if isinstance(assigned,str) and assigned and identity_names:
            name=identity_names.get(assigned)
            if isinstance(name,str) and name and name!=assigned:
                shown='%s (%s)' % (name,assigned)
        raise ValueError('Only the current assigned owner may contribute/respond%s; resume or '
                         'handoff first' % ('; this task is assigned to '+shown if shown else ''))
    if payload['operation'] in ('request-changes', 'request-review', 'decline-review') and closed:
        raise ValueError('Reopen the closed task explicitly before requesting changes or a review')
    if payload['operation'] == 'withdraw':
        # An integrated contribution is settled: withdrawing it would leave the
        # integration block passing for a withdrawn revision, strand the task out of
        # `work --state integrated` and refuse a later delivery (item 2).
        if effective_state == 'integrated':
            raise ValueError('Contribution reads integrated; an integrated contribution cannot be withdrawn '
                             'or superseded (its integration block still names this commit)')
        if state.get('withdrawal') is not None:
            raise ValueError('Contribution was already ' + state['withdrawal']['disposition'] +
                             '; a second withdraw on the same contribution is refused and cannot change '
                             'the disposition (deliver a new revision instead)')
    if payload['operation'] in ('withdraw', 'request-review'):
        # A coordinator (the server-side operator allowlist the endpoint supplies)
        # may act; otherwise withdraw must come from the contribution's own author
        # and a review request from the current task owner.
        authority = recovery.configured_operators(operators)
        if actor not in authority:
            contributor = state.get('contribution') or {}
            if payload['operation'] == 'withdraw':
                if author_key(actor) != author_key(contributor.get('author')):
                    raise ValueError('Only the contribution author or a configured coordinator may '
                                     'withdraw or supersede a contribution')
            elif actor != issue.get('assignee'):
                raise ValueError('Only the current assigned owner or a configured coordinator may '
                                 'request a review')
    if payload['operation'] == 'attest':
        # WHO may attest (kittrial-5bb.10 item 3): a listed verifier or a listed operator,
        # never the contribution's author or the task's assignee or their party. Judged
        # first, so an author or an unlisted contributor cannot even record a `failed`
        # verdict that would hold somebody else's work.
        refusal = attest_authority_refusal(actor, state.get('contribution') or {},
                                           issue.get('assignee'), operators, verifiers, owners)
        if refusal is not None:
            raise CoordinatorRuleRefusal(refusal)
        # A `verified` attestation must actually verify the DECLARED delivery: the observed
        # commit must be the delivered tip, the base must be the declared base, and a bundle
        # must match the declared SHA-256 with a positive byte count. Otherwise the record
        # reads as a failure or an unknown, so record that instead of a false pass
        # (kittrial-5bb.10). Judged here, before the sole native write.
        problems = attestation_problems(payload, state.get('contribution') or {})
        if problems:
            raise ValueError('Attestation cannot read as verified: ' + '; '.join(problems) +
                             '. Record verdict "failed" or "unknown" with the reason instead, or '
                             'correct the observation')
    if payload['operation'] in ('hold', 'release-hold'):
        # An integration hold is an audited coordinator control, exactly like the manual
        # `request-changes` hold it complements; a worker does not clear another party's
        # hold. Authority is the kit's ONE "listed operator" rule (kittrial-5bb.10 item 6):
        # a name on the allowlist, or an actor of a listed operator's principal.
        refusal = hold_authority_refusal(actor, operators, owners)
        if refusal is not None:
            raise CoordinatorRuleRefusal(refusal)
    if payload['operation'] == 'release-hold':
        open_holds = [h['comment_id'] for h in state.get('holds') or []]
        if payload['hold'] not in open_holds:
            raise ValueError('release-hold must name an OPEN hold of this task (open holds: ' +
                             (', '.join(sorted(open_holds)) or 'none') + '); a released or unknown '
                             'hold cannot be released again')
    if payload['operation'] == 'request-review':
        # The self-approval refusal is unchanged and request-review may not route
        # around it by naming the contribution's own author as the reviewer.
        author = (state.get('contribution') or {}).get('author')
        if author_key(payload['reviewer']) == author_key(author):
            raise ValueError('request-review must name a reviewer other than the contribution author; '
                             'a contribution cannot be reviewed by its own author')
        open_requests = open_review_requests_by(rows, actor, operators, journal)
        if open_requests >= MAX_OPEN_REVIEW_REQUESTS:
            raise ValueError('request-review refused: ' + str(actor) + ' already has ' + str(open_requests) +
                             ' open review requests (the cap is ' + str(MAX_OPEN_REVIEW_REQUESTS) +
                             ' per requester, counted across every task); close or decline one first')
    # The assignee an approval is judged against is recorded server-side, from the
    # task row, so a caller cannot forge it to open the follow-on gate.
    if payload['operation'] == 'approve':
        # Slices 6 and 7 (kittrial-5bb.197), each behind an installation setting that
        # is OFF until it is turned on. (b) nobody passes their own work: the approver
        # may not be the contribution's author or the task's assignee by PARTY - the
        # principal the registry gives the actor where it has one, its normalised name
        # otherwise (the task's rule 3). (c) coordinator writes: only a listed actor of
        # this project may approve, unless a verified web approval (owner, coordinator, or
        # agent with the grant) already established the caller (kittrial-5bb.199, .209).
        # Judged before the snapshot is stamped and before the sole native write, so a
        # refusal writes nothing.
        if independent_approval or coordinator_writes:
            contribution=state.get('contribution') or {}
            if coordinator_writes and not verified_web_authority and not listed_operator_actor(actor,operators,owners):
                raise CoordinatorRuleRefusal('approve refused: the installation setting coordinator_writes is on, so only a '
                                 'listed operator of this project may approve a contribution (or an actor of a '
                                 'listed operator\'s principal), and %s is neither. A worker still contributes, '
                                 'responds, recommends and requests changes; an operator turns the rule off with '
                                 '`admin.py coordinator-rules off --rule coordinator_writes --actor OPERATOR`.'
                                 %named_rule_actor(actor))
            if independent_approval:
                owners = approval_owners(owners)
                if same_party(actor,contribution.get('author'),owners):
                    raise CoordinatorRuleRefusal('approve refused: the installation setting independent_approval is on, and '
                                     'nobody passes their own work; %s is the contribution\'s author\'s party, so '
                                     'it may not approve its own contribution. Another party must approve it, or an '
                                     'operator turns the rule off with `admin.py coordinator-rules off --rule '
                                     'independent_approval --actor OPERATOR`.'
                                     %named_rule_actor(actor))
                assignee=issue.get('assignee')
                if isinstance(assignee,str) and assignee and same_party(actor,assignee,owners):
                    raise CoordinatorRuleRefusal('approve refused: the installation setting independent_approval is on, and '
                                     'nobody passes their own work; %s is the task\'s assignee\'s party, so it may '
                                     'not approve the contribution on its own task. Another party must approve it, '
                                     'or an operator turns the rule off with `admin.py coordinator-rules off --rule '
                                     'independent_approval --actor OPERATOR`.'
                                     %named_rule_actor(actor))
        payload = dict(payload, **{ASSIGNEE_SNAPSHOT: issue.get('assignee')})
        validate(payload, task)
    # Validate the entire transition before the sole native mutation.
    preview_positions = dict(positions)
    preview_positions['pending-write'] = len(issue.get('comments') or [])
    preview = projection(ordered + [(payload, {'id': 'pending-write', 'author': actor, 'created_at': 'pending'})],
                         voids, invalid, refused, preview_positions, reverts, invalid_reverts, closed=closed)
    # A follow-on may only base itself on a prior revision approved by a distinct
    # native author and genuinely integrated. Resolve the approving record from the
    # chain and the integration evidence from the shared review-state projection
    # here, still before the sole native mutation, so the refusal writes nothing.
    if payload['operation'] == 'contribute' and payload.get('follows') is not None:
        require_integrated_follow_on(payload, state, ordered, rows, task, issue.get('assignee'),
                                     operators, journal, owners)
    result = json.loads(run(['comments', 'add', task, PREFIX + canonical_bytes(payload).decode(), '--json']))
    answer = dict(comment_id=str(result['id']), reconciled=False, **receipt(preview, rows, task))
    if party_problem:
        answer['warnings'] = list(answer.get('warnings') or []) + [party_problem]
    return answer


def _voided_operation_ids(issue, voids):
    """Operation ids of review records that an operator void removed."""
    found = []
    for p, _ in voids:
        raw = recovery.target_text(issue, p['target'])
        if not raw.startswith(PREFIX):
            continue
        try:
            body = record_json.loads(raw[len(PREFIX):])
        except (ValueError, TypeError):
            continue
        if isinstance(body, dict) and isinstance(body.get('operation_id'), str):
            found.append(body['operation_id'])
    return found
