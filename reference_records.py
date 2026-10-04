"""Reference catalog records: `Kind: reference-entry-v1` on closed native anchors.

A reference entry states an operational fact and its authority ("what is the
authority for X, where does it live, when must it be re-checked"). Each entry is
one native `task` issue - an *anchor* - created and **closed** by `ref propose`
before its first revision, carrying the controlled labels `reference` and
`reference:draft|accepted|superseded`, the lookup label `reference-key:<key with
. as ->` and the `request:`/`request-content:` idempotency labels. Its revisions
are append-only `Kind: reference-entry-v1` comments; operator acceptance is a
separate `Kind: reference-acceptance-v1` evidence comment bound to the accepted
revision's content hash (docs/REFERENCE_CATALOG_DESIGN.md, the .41 design;
kittrial-5bb.66 is its slice 1).

Writes go through the shared keyed-record core (keyed_records.py) and the shared
closed-anchor entry layer (keyed_entries.py, kittrial-5bb.67) with this module's
REFERENCE kind:

- `propose` (contributor) creates the closed anchor and revision 1 as a draft;
- `revise` (contributor) appends the next draft revision, compare-and-swap on
  `revision` and `expected_sha256`; on an accepted entry the accepted pointer does
  not move until an operator accepts;
- `accept` (operator, `admin.py reference-apply`) names the newest draft it
  reviewed by `revision` and `record_sha256` and writes revision+1 with identical
  content and `acceptance_state: accepted`, after the F3 evidence;
- `draft` with `acceptance_state: accepted` (operator) writes a direct accepted
  revision 1.

Reads (`ref get`, `ref list`, the work/brief attention) recognise an entry only
through `reserved_comments.is_record_anchor`, unchanged. A crash between creating
an anchor and posting its first record leaves an ordinary closed row (labelled, no
record): the same operation's retry finishes it, another operation proposing that
key is refused, and readers report it in `coverage` as incomplete. Every read is
per entry: one malformed or unsupported entry is reported and never fails the
read. An acceptance counts only while its evidence comment's stored native author
equals the evidence `operator` and is on the live deployment operator allowlist;
otherwise the entry reads `draft-only` with `acceptance_inert: true` and raises an
`acceptance-inert` attention item to approvers. The native author is self-declared
over SSH, so the reserved-prefix guard is what keeps contributors from writing
evidence; acceptance is deliberately not bound to the host receipt, because a read
must not depend on a local journal (.41 section 10.2). docs/OPERATIONS.md states the
residual risk and the scan to run before first use.

Reads cost what they touch: `ref get` and every write read only their own key (one
`bd list` by lookup label, one `bd show`); only `ref list` and reconcile read the
catalog, with one `bd show` up to CATALOG_SHOW_MAX entries and one export above.

A malformed entry reads `malformed` until the operator repairs it with `void-record`
(the .41 design's section 3.7; kittrial-5bb.74): `keyed_entries` leaves out a comment an
applied void names, on every read and write, and refuses a void of a record the entry
reads. An anchor that holds no record and whose propose cannot be re-run is closed and
its key freed by `admin.py anchor-release`.

An operational fact (which host runs what, what the owner said, how a backup is taken)
has no repository file or page to point at. Its authority is an `attestation`: who
observed or stated it, on what date, on what basis (`host-check` or `owner-statement`)
and how (kittrial-5bb.98). A revision with that authority is written as
`Kind: reference-entry-v2` (`schema_version` 2); every other revision stays v1, so a
kit before this one reads everything it read before and reports an attested entry as
a record it does not support. It is accepted only by the operator route, with more
asked of the operator (`attestation_acceptance_rules`), and every read marks it
`authority_kind: attested` with a sentence saying it cannot be checked against a
repository, and whether it is accepted.

Statements are untrusted text: they never enter an error message, and excerpts
carry `trust`. The due-soon window is fixed at 30 days (a per-project setting is
deferred to slice 2 with its configuration store).
"""
import calendar
import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urlsplit

import keyed_entries
import keyed_records as core
from briefing import clip
from export_requirements import parse_json
from keyed_entries import (ACCEPTANCE_RECORD_FIELDS, CATALOG_SHOW_MAX, CONTRIBUTOR_OPERATIONS,
                           COVERAGE_IDS, NATIVE_FAILURES, OPERATOR_OPERATIONS, SHOW_CHUNK,
                           WRITTEN_BY_THE_OPERATION, all_missing)
from recovery import configured_operators
from requirements import SHA256_TEXT, canonical_bytes, content_hash
from reserved_comments import (REFERENCE_ACCEPTANCE_PREFIX as ACCEPTANCE_PREFIX,
                               REFERENCE_ENTRY_PREFIX as ENTRY_PREFIX,
                               REFERENCE_ENTRY_V2_PREFIX as ENTRY_V2_PREFIX)

TYPE_LABEL = 'reference'
STATE_LABEL = {'draft': 'reference:draft', 'accepted': 'reference:accepted',
               'superseded': 'reference:superseded'}
KEY_LABEL = 'reference-key:'
JOURNAL = '.reference-requests'
KEY = re.compile(r'[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+')
KEY_MAX = 80
TAG = re.compile(r'[a-z][a-z0-9-]{0,31}')
TAGS_MAX = 12
TITLE_MAX = 200
STATEMENT_MAX = 2000
URL_MAX = 2048
PATH_MAX = 512
ANCHOR_MAX = 200
ISSUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}')
COMMIT = re.compile(r'[0-9a-f]{40}')
ACCOUNT = re.compile(r'account:[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}')
PERSON = re.compile(r"person:[A-Za-z0-9](?:[A-Za-z0-9 _.'-]{0,94}[A-Za-z0-9_.'])?")
# The kit's session actors are `session-<uuid>` (sessions.py); older clients wrote
# `name/sessionN`. A session is never a durable owner (.41 section 3.4).
SESSION_MARKER = re.compile(r'session-[0-9a-f]{4}|/session[0-9]*(?:$|/)|(?:^|[^A-Za-z0-9])session[0-9]+$',
                            re.IGNORECASE)
# The one due-soon window, in days. A per-project `reference_due_soon_days`
# (1..365) needs a configuration store, which is a new sidecar path and so its own
# tolerant-reader step; it is a slice-2 setting.
DUE_SOON_DAYS = 30
REVIEW_BY_MAX_MONTHS = 24
# An attestation authority (kittrial-5bb.98). `basis` is a closed set of two: someone
# looked at a live system, or the owner said so. A rule somebody set for themselves is
# neither; it belongs in a decision issue or a repository document.
ATTESTATION_BASES = ('host-check', 'owner-statement')
ATTESTATION_LINE_MAX = 300
# An operational fact goes stale faster than code: an attested entry is reviewed within
# this many months of its observation, and is not accepted on an observation older than it.
ATTESTED_REVIEW_MONTHS = 6
# What a reader is told the authority is. `repository` and `url` are pointers a person
# can follow; `attested` is provenance only.
AUTHORITY_KINDS = {'repo-path': 'repository', 'url': 'url', 'attestation': 'attested'}
ENTRY_FIELDS = ('schema_version', 'key', 'revision', 'title', 'statement', 'authority', 'owner',
                'review_by', 'tags', 'decisions', 'acceptance_state', 'successor', 'origin', 'sha256')
CONTENT_FIELDS = ('key', 'title', 'statement', 'authority', 'owner', 'review_by', 'tags', 'decisions')
LIST_LIMIT_MAX = 100
ANCHOR_TITLE = 'Reference %s'
ANCHOR_DESCRIPTION = ('Reference catalog entry %s. Read it with `ref get %s`; its record comments are '
                      'authoritative. This anchor is not a work item.')
DUE_ORDER = {'expired': 0, 'due-soon': 1, 'unset': 2, 'ok': 3}


def today():
    return date(*time.gmtime()[:3])


# -- field validation ------------------------------------------------------------------

def valid_key(value, where='key'):
    if not isinstance(value, str) or len(value) > KEY_MAX or not KEY.fullmatch(value):
        raise ValueError('%s must be a lowercase dotted key such as calendar.trading (at most %d characters)'
                         % (where, KEY_MAX))
    return value


def _bounded_text(value, where, limit):
    core.text(value, where)
    if len(value) > limit:
        raise ValueError('%s must be at most %d characters' % (where, limit))
    return value


def _date(value, where):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError(where + ' must be a YYYY-MM-DD date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(where + ' must be a real calendar date') from None


def valid_owner(value, where='owner'):
    """A durable person/account identity; a session actor is refused (.41 3.4)."""
    if not isinstance(value, str) or not (ACCOUNT.fullmatch(value) or PERSON.fullmatch(value)):
        raise ValueError('%s must be a durable identity, account:<uid> or person:<name>' % where)
    if SESSION_MARKER.search(value):
        raise ValueError('%s must be a durable identity, not a session actor; use account:<uid> or '
                         'person:<name>' % where)
    return value


def _one_line(value, where, limit):
    _bounded_text(value, where, limit)
    if any(ord(character) < 32 for character in value):
        raise ValueError(where + ' must be one line')
    return value


def valid_authority(value):
    """Exactly one closed `repo-path`, `url` or `attestation` object (.41 3.6), time-independent rules."""
    if not isinstance(value, dict):
        raise ValueError('authority must be an object of type repo-path, url or attestation')
    kind = value.get('type')
    if kind == 'repo-path':
        core.checked_fields(value, ('type', 'path', 'commit', 'anchor'), 'authority')
        path = value.get('path')
        if not isinstance(path, str) or not path or len(path) > PATH_MAX or '\\' in path \
                or path.startswith(('/', '~')) or re.match(r'[A-Za-z]:', path) \
                or any(part in ('', '.', '..') for part in path.split('/')):
            raise ValueError('authority.path must be a relative / path inside the repository, with no .. '
                             'segment, ~ or drive')
        if 'commit' in value and (not isinstance(value['commit'], str) or not COMMIT.fullmatch(value['commit'])):
            raise ValueError('authority.commit must be a 40-character lowercase hex revision')
        if 'anchor' in value:
            _bounded_text(value['anchor'], 'authority.anchor', ANCHOR_MAX)
            if any(ord(character) < 32 for character in value['anchor']):
                raise ValueError('authority.anchor must be one line')
    elif kind == 'url':
        core.checked_fields(value, ('type', 'url', 'retrieved'), 'authority')
        url = value.get('url')
        if not isinstance(url, str) or len(url) > URL_MAX:
            raise ValueError('authority.url must be an https URL of at most %d characters' % URL_MAX)
        try:
            parts = urlsplit(url)
        except ValueError:
            raise ValueError('authority.url must be an https URL') from None
        if parts.scheme != 'https' or not parts.hostname or '@' in parts.netloc \
                or any(ord(character) <= 32 for character in url):
            raise ValueError('authority.url must be an https URL with a host and no userinfo')
        _date(value.get('retrieved'), 'authority.retrieved')
    elif kind == 'attestation':
        core.checked_fields(value, ('type', 'basis', 'by', 'observed', 'how', 'source'), 'authority')
        if value.get('basis') not in ATTESTATION_BASES:
            raise ValueError('authority.basis must be host-check (someone looked at a live system) or '
                             'owner-statement (the owner said so)')
        valid_owner(value.get('by'), 'authority.by')
        _date(value.get('observed'), 'authority.observed')
        _one_line(value.get('how'), 'authority.how', ATTESTATION_LINE_MAX)
        if 'source' in value:
            _one_line(value['source'], 'authority.source', ATTESTATION_LINE_MAX)
    else:
        raise ValueError('authority.type must be repo-path, url or attestation')
    return value


def attested(record):
    return record['authority']['type'] == 'attestation'


def valid_tags(value):
    if not isinstance(value, list) or len(value) > TAGS_MAX or len(set(map(str, value))) != len(value) \
            or any(not isinstance(tag, str) or not TAG.fullmatch(tag) for tag in value):
        raise ValueError('tags must be at most %d unique lowercase slugs' % TAGS_MAX)
    return value


def valid_decisions(value):
    if not isinstance(value, list) or len(set(map(str, value))) != len(value) \
            or any(not isinstance(item, str) or not ISSUE_ID.fullmatch(item) for item in value):
        raise ValueError('decisions must be unique native decision issue ids')
    return value


def validate_entry(record):
    """The closed entry schema, time-independent (a record stays valid as days pass)."""
    if not isinstance(record, dict) or set(record) != set(ENTRY_FIELDS):
        raise ValueError('reference entry has the wrong field set')
    if type(record['schema_version']) is not int or record['schema_version'] not in (1, 2):
        raise ValueError('schema_version must be the integer 1, or 2 for an attestation authority')
    valid_key(record['key'])
    core.positive_int(record['revision'], 'revision')
    _bounded_text(record['title'], 'title', TITLE_MAX)
    _bounded_text(record['statement'], 'statement', STATEMENT_MAX)
    valid_authority(record['authority'])
    # One form per content: version 2 is the record with an attestation authority, and
    # only that, so no revision can be written both ways.
    if (record['schema_version'] == 2) != attested(record):
        raise ValueError('schema_version 2 is the entry record with an attestation authority, and only that')
    valid_owner(record['owner'])
    if record['review_by'] is not None:
        _date(record['review_by'], 'review_by')
    valid_tags(record['tags'])
    valid_decisions(record['decisions'])
    state = record['acceptance_state']
    if state not in STATE_LABEL:
        raise ValueError('acceptance_state must be draft, accepted or superseded')
    if state == 'superseded':
        valid_key(record['successor'], 'successor')
    elif record['successor'] is not None:
        raise ValueError('successor is set only on a retirement revision')
    if state != 'draft':
        if record['review_by'] is None:
            raise ValueError('an accepted revision needs review_by')
        if record['authority']['type'] == 'repo-path' and 'commit' not in record['authority']:
            raise ValueError('an accepted repo-path authority needs its pinned commit')
    origin = record['origin']
    if origin != {'type': 'authored'}:
        if not isinstance(origin, dict) or set(origin) != {'type', 'path', 'digest'} \
                or origin.get('type') != 'import' or not isinstance(origin.get('path'), str) \
                or not origin['path'] or not isinstance(origin.get('digest'), str) \
                or not SHA256_TEXT.match(origin['digest']):
            raise ValueError('origin must be {"type":"authored"} or an import origin')
    if content_hash(record) != record['sha256']:
        raise ValueError('reference entry content hash mismatch')
    return record


def parse_entry(body):
    """The entry record iff the comment is exactly what a legitimate writer posts."""
    prefix = next((item for item in (ENTRY_PREFIX, ENTRY_V2_PREFIX)
                   if isinstance(body, str) and body.startswith(item)), None)
    if prefix is None:
        return None
    rest = body[len(prefix):]
    try:
        record = parse_json(rest)
        validate_entry(record)
        if canonical_bytes(record).decode('utf-8') != rest or entry_prefix_for(record) != prefix:
            return None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    return record


def entry_prefix_for(record):
    """The record kind a revision is written under: v2 for an attestation authority."""
    return ENTRY_V2_PREFIX if record['schema_version'] == 2 else ENTRY_PREFIX


# -- payloads ------------------------------------------------------------------------------

def _validate_content(payload):
    """The reference content fields of a propose, revise or direct payload."""
    for name in ('title', 'statement', 'authority', 'owner'):
        if name not in payload:
            raise ValueError('reference payload is missing field ' + name)
    _bounded_text(payload['title'], 'title', TITLE_MAX)
    _bounded_text(payload['statement'], 'statement', STATEMENT_MAX)
    valid_authority(payload['authority'])
    valid_owner(payload['owner'])
    if payload.get('review_by') is not None:
        _date(payload['review_by'], 'review_by')
    valid_tags(payload.get('tags', []))
    valid_decisions(payload.get('decisions', []))


def _write_time_rules(record):
    """Rules that depend on today's date, checked only when a revision is written."""
    current = today()
    limit = _months_ahead(current, REVIEW_BY_MAX_MONTHS)
    authority = record['authority']
    if authority['type'] == 'attestation':
        # Before the review-date rules: a stale observation makes every allowed review
        # date a past one, and the refusal should name the cause.
        observed = _date(authority['observed'], 'authority.observed')
        if observed > current:
            raise ValueError('authority.observed must not be later than today')
        stale = _months_ahead(observed, ATTESTED_REVIEW_MONTHS)
        if record['review_by'] is not None and _date(record['review_by'], 'review_by') > stale:
            raise ValueError('review_by must be at most %d months after authority.observed (%s) for an '
                             'attestation' % (ATTESTED_REVIEW_MONTHS, stale))
        if record['acceptance_state'] == 'accepted' and stale < current:
            raise ValueError('authority.observed %s is more than %d months old; check the fact again and '
                             'revise the entry with the new observation before it is accepted'
                             % (authority['observed'], ATTESTED_REVIEW_MONTHS))
    if record['review_by'] is not None:
        review_by = _date(record['review_by'], 'review_by')
        if review_by > limit:
            raise ValueError('review_by must be at most %d months ahead (%s)' % (REVIEW_BY_MAX_MONTHS, limit))
        if record['acceptance_state'] == 'accepted' and review_by < current:
            raise ValueError('review_by %s is in the past; an accepted revision needs a future review date'
                             % record['review_by'])
    if authority['type'] == 'url' and _date(authority['retrieved'], 'authority.retrieved') > current:
        raise ValueError('authority.retrieved must not be later than today')


def _months_ahead(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def entry_record(payload, revision, acceptance_state, origin=None):
    version = 2 if isinstance(payload['authority'], dict) and payload['authority'].get('type') == 'attestation' else 1
    record = {'schema_version': version, 'key': payload['key'], 'revision': revision,
              'title': payload['title'], 'statement': payload['statement'],
              'authority': payload['authority'], 'owner': payload['owner'],
              'review_by': payload.get('review_by'), 'tags': list(payload.get('tags', [])),
              'decisions': list(payload.get('decisions', [])), 'acceptance_state': acceptance_state,
              'successor': None, 'origin': origin or {'type': 'authored'}}
    record['sha256'] = content_hash(record)
    validate_entry(record)
    return record


def check_decisions(payload, run):
    """Every cited decision resolves to an issue typed `decision` or labelled `decision`."""
    wanted = list(payload.get('decisions') or [])
    if not wanted or payload['operation'] == 'accept':
        return
    # `bd list --id` returns only the rows that exist (an empty list when none do), so
    # an unknown link is this refusal and never a native failure.
    shown = json.loads(run(['list', '--id', ','.join(wanted), '--all', '--limit', '0', '--json']) or '[]')
    found = {row.get('id'): row for row in shown or [] if isinstance(row, dict)}
    bad = [item for item in wanted
           if item not in found or not (found[item].get('issue_type') == 'decision'
                                        or 'decision' in (found[item].get('labels') or []))]
    if bad:
        raise ValueError('Unknown decision link(s) %s: each must be an existing issue of type decision or '
                         'labelled decision' % ', '.join(bad))


def attestation_acceptance_rules(record, acceptance, run):
    """What an operator must show to accept an attested revision (kittrial-5bb.98).

    A repository authority is pinned to a commit and a URL to a retrieval date; an
    attestation has nothing the kit can resolve, so the acceptance carries the weight:

    - `acceptance.evidence` is one line saying what the accepting operator did to check
      the fact (re-ran the check: command, host, date; or confirmed the statement with the
      person named in `by`). The kit checks that it is there and is one line, not what it says;
    - `acceptance.decision_id` names an existing native issue of type `decision` or
      labelled `decision`: nothing else anchors the entry;
    - for an `owner-statement`, the identity in `by` is one of `acceptance.owners`.

    The date rules (the observation is not in the future, is at most
    ATTESTED_REVIEW_MONTHS old, and `review_by` falls within that many months of it) are
    `_write_time_rules`. Refusals run before any write of the acceptance.
    """
    if not attested(record):
        return
    authority = record['authority']
    evidence = acceptance['evidence']
    if len(evidence) > STATEMENT_MAX or any(ord(character) < 32 for character in evidence):
        raise ValueError('acceptance.evidence for an attested entry must be one line saying what the operator '
                         'did to check the fact (at most %d characters)' % STATEMENT_MAX)
    if authority['basis'] == 'owner-statement' and authority['by'] not in acceptance['owners']:
        raise ValueError('An owner-statement attestation is accepted only with the person who made the statement '
                         '(authority.by) among acceptance.owners')
    decision = acceptance['decision_id']
    if not ISSUE_ID.fullmatch(decision):
        raise ValueError('acceptance.decision_id must be a native decision issue id for an attested entry')
    shown = json.loads(run(['list', '--id', decision, '--all', '--limit', '0', '--json']) or '[]')
    found = next((row for row in shown or [] if isinstance(row, dict) and row.get('id') == decision), None)
    if found is None or not (found.get('issue_type') == 'decision' or 'decision' in (found.get('labels') or [])):
        raise ValueError('acceptance.decision_id must name an existing issue of type decision or labelled '
                         'decision for an attested entry: nothing else anchors an operational fact')


KIND = keyed_entries.AnchoredKind(
    noun='reference', title='Reference', command='ref', type_label=TYPE_LABEL, state_labels=STATE_LABEL,
    key_prefix=KEY_LABEL, family='reference-', entry_prefix=ENTRY_PREFIX, acceptance_prefix=ACCEPTANCE_PREFIX,
    journal=JOURNAL, source='reference-apply', accept_action='accept a reference entry',
    apply_command='admin.py reference-apply', reconcile_command='admin.py reference-reconcile',
    anchor_title=ANCHOR_TITLE, anchor_description=ANCHOR_DESCRIPTION,
    close_reason='reference catalog anchor (not a work item)',
    valid_key=lambda value, where='key': valid_key(value, where), parse_entry=lambda body: parse_entry(body),
    validate_entry=lambda record: validate_entry(record),
    entry_record=lambda payload, revision, state: entry_record(payload, revision, state),
    validate_content=_validate_content, write_time_rules=lambda record: _write_time_rules(record),
    content_fields=CONTENT_FIELDS, pre_write=lambda payload, run, operators: check_decisions(payload, run),
    entry_prefixes=(ENTRY_PREFIX, ENTRY_V2_PREFIX), entry_prefix_for=lambda record: entry_prefix_for(record),
    acceptance_rules=lambda record, acceptance, run: attestation_acceptance_rules(record, acceptance, run),
)
SPEC = KIND.spec
PROPOSE_FIELDS, ACCEPT_FIELDS, DIRECT_FIELDS = KIND.propose_fields, KIND.accept_fields, KIND.direct_fields
key_label = KIND.key_label
read_rows = KIND.read_rows
read_key_rows = KIND.read_key_rows
existing_revisions = KIND.existing_revisions
existing_acceptances = KIND.existing_acceptances
anchor_for = KIND.anchor_for
parse_acceptance = KIND.parse_acceptance
entry_comment = KIND.entry_comment
acceptance_evidence = KIND.acceptance_evidence
write_payload = KIND.write_payload


def validate_payload(payload, operator=False):
    return KIND.validate_payload(payload, operator=operator)


def apply_native(payload, actor, run, project, operator=False, operators=None):
    """Caller holds the canonical project lock; run(argv) invokes pinned bd.

    The contributor route (`operator=False`) proposes and revises drafts. The operator
    route (`admin.py reference-apply`) accepts the newest draft it reviewed, or writes
    a direct accepted revision 1, with F3 evidence; `operators` is the deployment
    allowlist, checked before any journal or native read on that route. On both routes
    it decides which operator voids apply (the endpoint supplies it to contributors).
    """
    return KIND.apply_native(payload, actor, run, project, operator=operator, operators=operators)


def apply_batch(payload, actor, run, project, operators=None, lock=None):
    """`admin.py reference-apply` with `items`: accept several reviewed drafts under one F3
    decision, as `capability-apply` does (`AnchoredKind.apply_batch`). Each item is one
    ordinary accept with its own receipt and its own hold of the coordination lock; a
    refused item does not stop the others."""
    return KIND.apply_batch(payload, actor, run, project, operators=operators, lock=lock)


def reconcile(project, operation_id, actor, reason, disposition, run, issue_id=None, operators=None):
    """Operator-only: resolve a stuck `.reference-requests/` receipt from native state. `operators`
    (the deployment allowlist) decides which operator voids apply when `complete` confirms
    the anchor holds a live record."""
    return KIND.reconcile(project, operation_id, actor, reason, disposition, run, issue_id=issue_id,
                          operators=operators)


# -- the catalog as readers see it ---------------------------------------------------------------

def due(review_by, current=None):
    if review_by is None:
        return 'unset'
    current = current or today()
    day = date.fromisoformat(review_by)
    if day < current:
        return 'expired'
    if day <= current + timedelta(days=DUE_SOON_DAYS):
        return 'due-soon'
    return 'ok'


def entry_view(row, operators, current=None):
    """One anchor as readers see it, plus its review-by class. Never raises: a bad entry reads malformed."""
    view = KIND.entry_view(row, operators)
    view['due'] = due(view['record']['review_by'], current) if view['record'] is not None else 'unset'
    return view


def catalog(rows, operators, current=None):
    """(entries, incomplete ids) over reference-labelled rows; ordinary labelled tasks are skipped."""
    return KIND.catalog(rows, operators, view=lambda row, operators: entry_view(row, operators, current))


_coverage = KIND.coverage


def authority_kind(record):
    """`repository`, `url` or `attested`: what kind of authority a reader is looking at."""
    return AUTHORITY_KINDS.get(((record or {}).get('authority') or {}).get('type'))


def authority_note(record, accepted):
    """The sentence every read shows beside an attestation, else None.

    Server-derived from validated fields only (the identity, the date and the basis), never
    the free-text `how` or `source`. `accepted` is whether the reader is presenting this
    revision as the accepted record; a draft attestation is always marked not accepted.
    """
    if record is None or not attested(record):
        return None
    authority = record['authority']
    who = 'Attested by %s on %s (%s)' % (authority['by'], authority['observed'], authority['basis'])
    if accepted:
        return who + ', accepted by an operator. It is provenance, not a pointer: it cannot be checked against a repository.'
    return ('NOT ACCEPTED. ' + who + '; no operator has accepted it, so it is a lead and not authority. '
            'It cannot be checked against a repository.')


def _record_view(record, accepted=False):
    if record is None:
        return None
    view = {'revision': record['revision'], 'title': clip(record['title'], TITLE_MAX),
            'statement': clip(record['statement'], STATEMENT_MAX), 'authority': record['authority'],
            'authority_kind': authority_kind(record),
            'owner': record['owner'], 'review_by': record['review_by'], 'tags': record['tags'],
            'decisions': record['decisions'], 'successor': record['successor'],
            'acceptance_state': record['acceptance_state'], 'sha256': record['sha256']}
    note = authority_note(record, accepted)
    if note is not None:
        view['authority_note'] = note
    return view


def get(rows, key, operators, current=None):
    """`ref get KEY`: found through the lookup label, then the record's own key must match.

    A malformed or unsupported entry is returned with that state (its key is known only
    from the label); an anchor with no record yet, or an unknown key, is a refusal.
    """
    entry = KIND.find_entry(rows, key, operators, view=lambda row, operators: entry_view(row, operators, current))
    return {'schema_version': 1, 'key': key, 'state': entry['state'], 'native_id': entry['native_id'],
            'record': _record_view(entry['record'], accepted=True), 'record_comment_id': entry['record_comment_id'],
            'acceptance': entry['acceptance'], 'acceptance_inert': entry['acceptance_inert'],
            'inert_operator': entry['inert_operator'],
            'replaces': [], 'proposed': _record_view(entry['proposed']),
            'proposed_comment_id': entry['proposed_comment_id'],
            'due': 'conflicted' if entry['state'] == 'conflicted' else entry['due'], 'resolved': None,
            'warnings': entry['warnings'][:10],
            'trust': 'conflicted' if entry['state'] == 'conflicted' else 'accepted' if entry['record'] else 'draft',
            'anchors': entry.get('duplicate_anchors', []),
            'coverage': 'newest accepted revision and its acceptance evidence; unresolved drafts are returned '
                        'in proposed'}


def _list_item(entry):
    source = entry.get('candidate') or entry['record'] or entry['proposed'] or {}
    item = {'key': entry['key'], 'title': (source.get('title') or '')[:TITLE_MAX], 'state': entry['state'],
            'owner': source.get('owner'), 'review_by': source.get('review_by') if entry['record'] else None,
            'proposed_review_by': (entry['proposed'] or {}).get('review_by'),
            'due': 'conflicted' if entry['state'] == 'conflicted' else entry['due'], 'tags': source.get('tags') or [],
            'revision': source.get('revision'), 'native_id': entry['native_id'],
            'acceptance_inert': entry['acceptance_inert'],
            # What kind of authority the listed revision carries, and whether that revision
            # is the accepted record: a draft attestation is never shown unmarked.
            'authority_kind': authority_kind(source), 'authority_accepted': entry['record'] is not None}
    note = authority_note(source or None, entry['record'] is not None)
    if note is not None:
        item['authority_note'] = note
    if entry['state'] == 'conflicted':
        item.update(trust='conflicted', anchor_trust=entry['anchor_trust'])
    return item


def list_entries(rows, options, operators, current=None):
    entries, incomplete = catalog(rows, operators, current)
    good = [entry for entry in entries if entry['state'] not in ('malformed', 'unsupported')]
    state = options.get('state') or 'all'
    if state != 'all':
        good = [entry for entry in good if entry['state'] == state]
    if options.get('owner'):
        good = [entry for entry in good if (entry['record'] or entry['proposed'] or {}).get('owner')
                == options['owner']]
    for tag in options.get('tags') or []:
        good = [entry for entry in good if tag in ((entry['record'] or entry['proposed'] or {}).get('tags') or [])]
    if options.get('due'):
        good = [entry for entry in good if entry['due'] == options['due']]
    if options.get('authority'):
        good = [entry for entry in good
                if authority_kind(entry.get('candidate') or entry['record'] or entry['proposed'])
                == options['authority']]
    good.sort(key=lambda entry: (DUE_ORDER.get(entry['due'], 9), entry['key'] or ''))
    offset, limit = options.get('offset', 0), options.get('limit', 20)
    page = good[offset:offset + limit]
    return {'schema_version': 1, 'total': len(good), 'items': [_list_item(entry) for entry in page],
            'next_offset': offset + limit if offset + limit < len(good) else None,
            'coverage': _coverage(entries, incomplete,
                                  'one row per key: newest accepted/superseded revision, or newest draft when no '
                                  'revision is accepted')}


# -- attention (work and brief) ------------------------------------------------------------------

def work_attention(rows, actor, operators, current=None, limit=20, offset=0):
    """`attention.reference_review` for `work`: project-wide counts always; items only for an approver.

    On the SSH route an approver is an actor on the deployment operator allowlist; an
    entry owner is matched only through `ref list --owner` (.41 7.1). Draft-only
    entries are counted as `unset` and listed only by `ref list --state draft-only`.
    """
    entries, incomplete = catalog(rows, operators, current)
    counts = {'conflicted': 0, 'expired': 0, 'due_soon': 0, 'unset': 0, 'acceptance_inert': 0, 'malformed': 0, 'total': 0}
    candidates = []
    conflict_groups = set()
    for entry in entries:
        if entry['state'] == 'conflicted':
            group = tuple(anchor['native_id'] for anchor in entry['duplicate_anchors'])
            if group in conflict_groups:
                continue
            conflict_groups.add(group)
            counts['conflicted'] += 1
            counts['total'] += 1
            candidates.append(dict(_attention_item(entry), due='conflicted', anchors=entry['duplicate_anchors']))
            continue
        if entry['state'] in ('malformed', 'unsupported'):
            counts['malformed'] += 1
            continue
        flagged = False
        if entry['acceptance_inert']:
            # An inert acceptance is reported here, never silently dropped back to a draft.
            counts['acceptance_inert'] += 1
            candidates.append(dict(_attention_item(entry), due='acceptance-inert',
                                   inert_operator=entry['inert_operator']))
            flagged = True
        if entry['state'] == 'draft-only':
            if not flagged:
                counts['unset'] += 1
                flagged = True
        elif entry['due'] in ('expired', 'due-soon'):
            counts['expired' if entry['due'] == 'expired' else 'due_soon'] += 1
            candidates.append(_attention_item(entry))
            flagged = True
        counts['total'] += flagged
    approver = actor in configured_operators(operators if operators is not None else ())
    order = {'conflicted': -1, 'acceptance-inert': 0, 'expired': 1, 'due-soon': 2}
    candidates.sort(key=lambda item: (order.get(item['due'], 9), item['key'] or ''))
    items = candidates[offset:offset + limit] if approver else []
    block = dict(counts, items=items,
                 truncated=(not approver and bool(candidates)) or offset + limit < len(candidates),
                 next_offset=offset + limit if approver and offset + limit < len(candidates) else None)
    if not approver and candidates:
        block['coverage'] = ('%d item%s for approvers (actors on the deployment operator allowlist); owners use '
                             'ref list --owner' % (len(candidates), '' if len(candidates) == 1 else 's'))
    if incomplete:
        block['incomplete'] = len(incomplete)
    if any(any(warning['code'] == 'duplicate-key' for warning in entry['warnings']) for entry in entries):
        block['coverage'] = KIND.coverage(entries, [], block.get('coverage', 'Catalog warnings'))
    return block


def _attention_item(entry):
    from agent_prompts import label
    record = entry['record'] or entry['proposed'] or {}
    return {'key': entry['key'], 'review_by': record.get('review_by'), 'due': entry['due'],
            'owner': label(record['owner']) if record.get('owner') is not None else None,
            'state': entry['state'], 'revision': record.get('revision'),
            'authority_kind': authority_kind(record), 'authority_accepted': entry['record'] is not None}


def _attention_title(value):
    from agent_prompts import TITLE_LIMIT, label
    from capabilities import clean
    text = clean(value or '')
    return {'text': label(value),
            'omitted_chars': len(text) - TITLE_LIMIT + 1 if len(text) > TITLE_LIMIT else 0}


def brief_attention(rows, task_row, operators, current=None, limit=3):
    """At most 3 `reference-review` items for a brief: tag matches plus expired/due-soon, expired first."""
    entries, _ = catalog(rows, operators, current)
    labels = set((task_row or {}).get('labels') or [])
    chosen = []
    for entry in entries:
        if entry['state'] not in ('accepted', 'superseded') or entry['record'] is None:
            continue
        tagged = bool(labels & set(entry['record']['tags']))
        if tagged or entry['due'] in ('expired', 'due-soon'):
            chosen.append(entry)
    chosen.sort(key=lambda entry: (DUE_ORDER.get(entry['due'], 9), entry['key']))
    items = [{'kind': 'reference-review', 'key': entry['key'], 'due': entry['due'],
              'review_by': entry['record']['review_by'], 'trust': 'accepted',
              'authority_kind': authority_kind(entry['record']),
              'title': _attention_title(entry['record']['title']),
              'text': _attention_text(entry), 'source': 'ref get ' + entry['key']}
             for entry in chosen[:limit]]
    more = len(chosen) - len(items)
    return {'attention': items, 'attention_total': len(chosen), 'attention_more': more or None}


def _attention_text(entry):
    """Server-derived text only: the key, the due class and the date - never the statement."""
    name = 'Attested reference' if attested(entry['record']) else 'Reference'
    if entry['due'] == 'expired':
        return '%s %s is past its review date (%s).' % (name, entry['key'], entry['record']['review_by'])
    if entry['due'] == 'due-soon':
        return '%s %s is due for review by %s.' % (name, entry['key'], entry['record']['review_by'])
    return '%s %s is tagged for this task.' % (name, entry['key'])


# -- the `ref` client action ------------------------------------------------------------------------

def help_payload():
    return {'schema_version': 1, 'action': 'ref', 'contract': 'cli-contract-v1',
            'usage': ['ref get KEY', 'ref list [--tag TAG]... [--owner IDENTITY] '
                      '[--state draft-only|accepted|superseded|all] [--due expired|due-soon|unset] '
                      '[--authority repository|url|attested] [--limit N] [--offset N]', 'ref propose --file entry.json', 'ref revise --file entry.json'],
            'limits': {'list_limit': [1, LIST_LIMIT_MAX], 'title': TITLE_MAX, 'statement': STATEMENT_MAX,
                       'tags': TAGS_MAX, 'key': KEY_MAX, 'due_soon_days': DUE_SOON_DAYS,
                       'attestation_line': ATTESTATION_LINE_MAX, 'attested_review_months': ATTESTED_REVIEW_MONTHS,
                       'batch_items': keyed_entries.BATCH_MAX},
            'authority': {'types': sorted(AUTHORITY_KINDS), 'attestation_bases': list(ATTESTATION_BASES),
                          'kinds': 'authority_kind is repository, url or attested; an attested entry is '
                                   'provenance (who observed or stated it, when and how), accepted only by an '
                                   'operator, and cannot be checked against a repository'},
            'operator': ['admin.py reference-apply PROJECT --actor OPERATOR --file acceptance.json',
                         'admin.py reference-apply PROJECT --actor OPERATOR --file batch.json  '
                         '(a batch: {schema_version, operation_id, acceptance_state, acceptance, items: '
                         '[{key, revision, record_sha256}]})',
                         'admin.py reference-reconcile PROJECT --operation-id ID --actor OPERATOR '
                         '--reason TEXT --disposition complete|failed|released [--issue-id ID]',
                         'admin.py void-record PROJECT --actor OPERATOR --file void.json',
                         'admin.py anchor-release PROJECT --kind reference --issue-id ID --actor OPERATOR '
                         '--reason TEXT']}


def parse_list_options(args):
    options = {'tags': [], 'limit': 20, 'offset': 0}
    values = {'--tag': 'tag', '--owner': 'owner', '--state': 'state', '--due': 'due', '--limit': 'limit',
              '--offset': 'offset', '--authority': 'authority'}
    index = 0
    while index < len(args):
        token = args[index]
        if token == '--json':
            index += 1
            continue
        if token not in values or index + 1 >= len(args):
            raise ValueError('ref list: unknown or incomplete option %s' % token)
        value = args[index + 1]
        name = values[token]
        if name == 'tag':
            options['tags'].append(valid_tags([value])[0])
        elif name in ('limit', 'offset'):
            if not re.fullmatch(r'[0-9]{1,6}', value):
                raise ValueError('ref list: --%s must be a number' % name)
            options[name] = int(value)
        else:
            options[name] = value
        index += 2
    if not 1 <= options['limit'] <= LIST_LIMIT_MAX:
        raise ValueError('ref list: --limit must be 1..%d' % LIST_LIMIT_MAX)
    if options.get('state', 'all') not in ('draft-only', 'accepted', 'superseded', 'all'):
        raise ValueError('ref list: --state must be draft-only, accepted, superseded or all')
    if options.get('due') not in (None, 'expired', 'due-soon', 'unset'):
        raise ValueError('ref list: --due must be expired, due-soon or unset')
    if options.get('authority') not in (None,) + tuple(AUTHORITY_KINDS.values()):
        raise ValueError('ref list: --authority must be repository, url or attested')
    if options.get('owner') is not None:
        valid_owner(options['owner'])
    return options


def read(args, run, operators):
    """`ref get KEY` / `ref list ...` / `ref --help`: read-only. `get` reads only its key
    (`read_key_rows`); `list` reads the catalog (`read_rows`)."""
    if not args or args[0] in ('--help', '-h', 'help'):
        return help_payload()
    command, rest = args[0], args[1:]
    if command == 'get':
        rest = [token for token in rest if token != '--json']
        if len(rest) != 1:
            raise ValueError('ref get takes exactly one KEY')
        valid_key(rest[0])
        return get(read_key_rows(run, rest[0]), rest[0], operators)
    if command == 'list':
        return list_entries(read_rows(run), parse_list_options(rest), operators)
    raise ValueError('ref: unknown command %s; use get, list, propose or revise' % command)
