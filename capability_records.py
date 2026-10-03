"""Capability records: `Kind: capability-entry-v1` on closed native anchors (.60 slice 1a).

A capability says what a piece of the system is for: its name, aliases and summary,
the requirements and design text it serves, its owner, and where its code and tests
live (docs/CAPABILITY_INDEX_DESIGN.md, the .60 design; kittrial-5bb.67 is slice 1a).
It is one more keyed-entry kind on the shared layers (`keyed_records` and
`keyed_entries`): a closed anchor labelled `capability` /
`capability:draft|accepted|superseded` / `capability-key:<slug>`, append-only
`Kind: capability-entry-v1` revisions and `Kind: capability-acceptance-v1` operator
evidence, with the same compare-and-swap, crash, inert-acceptance and per-entry
isolation rules as the reference catalog (.41).

Meaning is accepted; location is verified (.60 section 4). Slice 1a writes:

- `capability propose|revise --file` (any contributor, a draft);
- `admin.py capability-apply` (operator): a batch of `{key, revision, record_sha256}`
  under one F3 decision, one acceptance record and one receipt per item (keyed
  `(operation_id, key)`), processed in list order, resumable, with per-item results;
  or a direct accepted revision 1 (`operation: "draft"`);
- `admin.py capability-retire` (operator): the newest revision superseded by a
  successor key, with evidence; `replaces` is derived when read;
- `capability propose-alias KEY "PHRASE"` (any contributor, within caps) and
  `admin.py capability-alias-reject` (operator): `Kind: capability-alias-v1` records.

A pending alias is only ever a **candidate**: it lifts its capability in `find`, never
an exact match; an operator folds it into the next accepted revision's `aliases`, or
rejects it. Caps are keyed on the resolved person. Over the SSH endpoint the native
author of a comment is the actor the caller declared, so it proves nothing: the
endpoint route always writes `identity: unverified`, and every such proposer shares
one strict pool. Only the operator shell route (`admin.py capability-alias-propose`,
allowlist checked) writes `verified` (`operator:<actor>`), and a reader counts a
record as verified only when the record says so AND its stored native author is that
allowlisted operator (the reserved-prefix guard keeps contributors from writing such a
record raw). Attribution beyond operators arrives with kittrial-5bb.68 (.58's actor
map, and HTTP once the actor is bound to the principal), which will also set
`submitted_by_agent` (always `false` until then).
There is no demotion in slice 1a: .60 section 4's "demote" is covered by retire.

Verification (slice 1b, kittrial-5bb.69) lives in `capability_verification.py`: the
record, the two write routes and the trust rules. This module reads those records
with the entry and reports each revision's `verification` (`verified`, `reported`,
`drifted`, `unverified` or `superseded-revision`) on `get`, `list` and `find`.
Summaries and alias text are untrusted: they never enter an error message, and every
excerpt carries `trust`.
"""
import contextlib
import json
import re
import time
import unicodedata

import capability_verification as verification
import keyed_entries
import keyed_records as core
from capabilities import normalized as normalize, safe_relpath, split_pointer, stems, words
from coordination import atomic, identifier
from export_requirements import parse_json
from keyed_entries import CONTRIBUTOR_OPERATIONS, NATIVE_FAILURES, read_labelled
from recovery import configured_operators
from requirements import SHA256_TEXT, canonical_bytes, content_hash, load_json
from reserved_comments import (CAPABILITY_ACCEPTANCE_PREFIX as ACCEPTANCE_PREFIX,
                               CAPABILITY_ALIAS_PREFIX as ALIAS_PREFIX,
                               CAPABILITY_ENTRY_PREFIX as ENTRY_PREFIX, is_record_anchor)

TYPE_LABEL = 'capability'
STATE_LABEL = {'draft': 'capability:draft', 'accepted': 'capability:accepted',
               'superseded': 'capability:superseded'}
KEY_LABEL = 'capability-key:'
JOURNAL = '.capability-requests'
KEY = re.compile(r'[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+')
KEY_MAX = 80
NAME_MAX = 120
SUMMARY_MAX = 1200
ALIAS_MAX = 80
ALIASES_MAX = 32
POINTER_MAX = 400
CODE_MAX, TESTS_MAX, ANCHORS_MAX, REQUIREMENTS_MAX = 32, 32, 16, 16
TAG = re.compile(r'[a-z][a-z0-9-]{0,31}')
TAGS_MAX = 8
PHRASE_MAX = 200
FIND_LIMIT = (1, 20)
LIST_LIMIT_MAX = 100
BATCH_MAX = 100
# A pause outside the lock between batch items: flock gives no ordering guarantee, so
# without it the batch could retake the lock before a waiting writer wakes.
BATCH_YIELD_SECONDS = 0.05
ACCOUNT = re.compile(r'account:[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}')
PERSON = re.compile(r"person:[A-Za-z0-9](?:[A-Za-z0-9 _.'-]{0,94}[A-Za-z0-9_.'])?")
SESSION_MARKER = re.compile(r'session-[0-9a-f]{4}|/session[0-9]*(?:$|/)|(?:^|[^A-Za-z0-9])session[0-9]+$',
                            re.IGNORECASE)
REQUIREMENT_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
ENTRY_FIELDS = ('schema_version', 'key', 'revision', 'name', 'aliases', 'summary', 'requirements', 'anchors',
                'code', 'tests', 'owner', 'tags', 'acceptance_state', 'successor', 'sha256')
CONTENT_FIELDS = ('key', 'name', 'aliases', 'summary', 'requirements', 'anchors', 'code', 'tests', 'owner',
                  'tags')
ALIAS_FIELDS = ('schema_version', 'key', 'alias', 'normalized', 'action', 'evidence', 'reason', 'submitter',
                'at', 'sha256')
SUBMITTER_FIELDS = ('actor', 'person', 'identity', 'submitted_by_agent')
# Pending aliases allowed (.60 section 6), keyed on the resolved person.
CAP_PERSON_CAPABILITY, CAP_PERSON_PROJECT = 3, 50
CAP_UNVERIFIED_CAPABILITY, CAP_UNVERIFIED_PROJECT = 1, 10
CAP_CAPABILITY = 20
REASON_MAX = 400
ANCHOR_TITLE = 'Capability %s'
ANCHOR_DESCRIPTION = ('Capability index entry %s. Read it with `capability get %s`; its record comments are '
                      'authoritative. This anchor is not a work item.')


# -- field validation ------------------------------------------------------------------

def valid_key(value, where='key'):
    if not isinstance(value, str) or len(value) > KEY_MAX or not KEY.fullmatch(value):
        raise ValueError('%s must be a lowercase dotted key such as review.structured-contribution (at most %d '
                         'characters)' % (where, KEY_MAX))
    return value


def _bounded_text(value, where, limit):
    core.text(value, where)
    if len(value) > limit:
        raise ValueError('%s must be at most %d characters' % (where, limit))
    return value


def valid_alias_text(value, where='alias'):
    """At most 80 printable characters; no control, format or separator but the ASCII space."""
    if not isinstance(value, str) or not value.strip() or len(value) > ALIAS_MAX \
            or any(unicodedata.category(ch)[0] in 'CZ' and ch != ' ' for ch in value):
        raise ValueError('%s must be at most %d printable characters, with no control, format or separator '
                         'character other than the space' % (where, ALIAS_MAX))
    if not normalize(value):
        raise ValueError('%s must contain at least one word' % where)
    return value


def valid_owner(value, required):
    if value is None:
        if required:
            raise ValueError('an accepted capability needs an owner, account:<uid> or person:<name>')
        return value
    if not isinstance(value, str) or not (ACCOUNT.fullmatch(value) or PERSON.fullmatch(value)):
        raise ValueError('owner must be a durable identity, account:<uid> or person:<name>')
    if SESSION_MARKER.search(value):
        raise ValueError('owner must be a durable identity, not a session actor; use account:<uid> or '
                         'person:<name>')
    return value


def _pointers(value, where, limit, shapes):
    """A unique list of .61-syntax pointers; `shapes` names the allowed separators."""
    if not isinstance(value, list) or len(value) > limit or len(set(map(str, value))) != len(value):
        raise ValueError('%s must be at most %d unique pointers' % (where, limit))
    for pointer in value:
        if not isinstance(pointer, str) or len(pointer) > POINTER_MAX or \
                any(unicodedata.category(ch)[0] in 'CZ' and ch != ' ' for ch in pointer):
            raise ValueError('%s holds an invalid pointer' % where)
        rel, separator, target = split_pointer(pointer)
        if safe_relpath(rel) is None or separator not in shapes or (separator and not target.strip()):
            raise ValueError('%s holds an invalid pointer (expected %s)' % (where, ' or '.join(
                {'::': 'file::Qualified.name', '#': 'file.md#anchor', '': 'file'}[shape] for shape in shapes)))
    return value


def valid_requirements(value):
    if not isinstance(value, list) or len(value) > REQUIREMENTS_MAX:
        raise ValueError('requirements must be at most %d {key, revision?} links' % REQUIREMENTS_MAX)
    seen = set()
    for item in value:
        if not isinstance(item, dict) or not set(item) <= {'key', 'revision'} or 'key' not in item \
                or not isinstance(item['key'], str) or not REQUIREMENT_KEY.fullmatch(item['key']):
            raise ValueError('requirements entries must be {key, revision?} with a requirement key')
        if 'revision' in item:
            core.positive_int(item['revision'], 'requirements revision')
        if item['key'] in seen:
            raise ValueError('requirements must not repeat a key')
        seen.add(item['key'])
    return value


def valid_tags(value):
    if not isinstance(value, list) or len(value) > TAGS_MAX or len(set(map(str, value))) != len(value) \
            or any(not isinstance(tag, str) or not TAG.fullmatch(tag) for tag in value):
        raise ValueError('tags must be at most %d unique lowercase slugs' % TAGS_MAX)
    return value


def valid_aliases(value):
    if not isinstance(value, list) or len(value) > ALIASES_MAX:
        raise ValueError('aliases must be at most %d phrases' % ALIASES_MAX)
    for alias in value:
        valid_alias_text(alias, 'aliases entry')
    if len({normalize(alias) for alias in value}) != len(value):
        raise ValueError('aliases must not repeat a phrase')
    return value


def validate_entry(record):
    """The closed `capability-entry-v1` schema (.60 section 3.2), time-independent."""
    if not isinstance(record, dict) or set(record) != set(ENTRY_FIELDS):
        raise ValueError('capability entry has the wrong field set')
    if type(record['schema_version']) is not int or record['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    valid_key(record['key'])
    core.positive_int(record['revision'], 'revision')
    _bounded_text(record['name'], 'name', NAME_MAX)
    valid_aliases(record['aliases'])
    _bounded_text(record['summary'], 'summary', SUMMARY_MAX)
    valid_requirements(record['requirements'])
    _pointers(record['anchors'], 'anchors', ANCHORS_MAX, ('#',))
    _pointers(record['code'], 'code', CODE_MAX, ('::',))
    _pointers(record['tests'], 'tests', TESTS_MAX, ('::', ''))
    state = record['acceptance_state']
    if state not in STATE_LABEL:
        raise ValueError('acceptance_state must be draft, accepted or superseded')
    valid_owner(record['owner'], required=state != 'draft')
    valid_tags(record['tags'])
    if state == 'superseded':
        valid_key(record['successor'], 'successor')
    elif record['successor'] is not None:
        raise ValueError('successor is set only on a retirement revision')
    if content_hash(record) != record['sha256']:
        raise ValueError('capability entry content hash mismatch')
    return record


def parse_entry(body):
    if not isinstance(body, str) or not body.startswith(ENTRY_PREFIX):
        return None
    rest = body[len(ENTRY_PREFIX):]
    try:
        record = parse_json(rest)
        validate_entry(record)
        if canonical_bytes(record).decode('utf-8') != rest:
            return None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    return record


def entry_record(payload, revision, acceptance_state):
    record = {'schema_version': 1, 'key': payload['key'], 'revision': revision, 'name': payload['name'],
              'aliases': list(payload.get('aliases', [])), 'summary': payload['summary'],
              'requirements': list(payload.get('requirements', [])), 'anchors': list(payload.get('anchors', [])),
              'code': list(payload.get('code', [])), 'tests': list(payload.get('tests', [])),
              'owner': payload.get('owner'), 'tags': list(payload.get('tags', [])),
              'acceptance_state': acceptance_state, 'successor': None}
    record['sha256'] = content_hash(record)
    validate_entry(record)
    return record


def _validate_content(payload):
    for name in ('name', 'summary'):
        if name not in payload:
            raise ValueError('capability payload is missing field ' + name)
    _bounded_text(payload['name'], 'name', NAME_MAX)
    _bounded_text(payload['summary'], 'summary', SUMMARY_MAX)
    valid_aliases(payload.get('aliases', []))
    valid_requirements(payload.get('requirements', []))
    _pointers(payload.get('anchors', []), 'anchors', ANCHORS_MAX, ('#',))
    _pointers(payload.get('code', []), 'code', CODE_MAX, ('::',))
    _pointers(payload.get('tests', []), 'tests', TESTS_MAX, ('::', ''))
    valid_owner(payload.get('owner'), required=payload.get('operation') == 'draft')
    valid_tags(payload.get('tags', []))


def _pre_write(payload, run, operators=None):
    """Reads before the journal: requirement links, and a retirement's successor."""
    if payload['operation'] in ('propose', 'revise', 'draft'):
        check_requirements(payload.get('requirements') or [], run)
    if payload['operation'] == 'retire':
        check_successor(payload['key'], payload['successor'], read_key_and_retired(run, payload['successor']),
                        operators)


def check_requirements(links, run):
    """Each `{key, revision?}` names an existing requirement record (and revision)."""
    if not links:
        return
    from requirement_records import existing_revisions as requirement_revisions
    known = {}
    for row in read_labelled(run, 'requirement'):
        try:
            for revision, record in requirement_revisions(row).items():
                if record.get('key'):
                    known.setdefault(record['key'], set()).add(revision)
        except ValueError:
            continue   # a malformed requirement record cannot be linked
    bad = [link['key'] for link in links
           if link['key'] not in known or ('revision' in link and link['revision'] not in known[link['key']])]
    if bad:
        raise ValueError('Unknown requirement link(s) %s: each must name an existing requirement record key (and '
                         'revision, when given)' % ', '.join(bad))


def newest_revisions(rows, operators=None):
    """key -> the newest revision record of every readable anchor, whatever its acceptance.

    Each anchor is read as `KIND.live_row` sees it, without the comments applied
    operator voids name (`operators` is the deployment allowlist)."""
    newest = {}
    for row in rows:
        if not isinstance(row, dict) or TYPE_LABEL not in (row.get('labels') or []):
            continue
        row = KIND.live_row(row, operators)[0]
        if not is_record_anchor(row):
            continue
        try:
            revisions = KIND.existing_revisions(row)
        except ValueError:
            continue
        if revisions:
            record = revisions[max(revisions)]
            newest[record['key']] = record
    return newest


def read_key_and_retired(run, key):
    """One key's rows plus every retired capability: all `get` and a retirement need.

    `replaces` and the successor chain are found among rows labelled
    `capability:superseded`, so neither reads the whole catalog.
    """
    rows = KIND.read_key_rows(run, key)
    seen = {row['id'] for row in rows}
    retired = [task for task in KIND.listed_ids(run, ['--label', STATE_LABEL['superseded']]) if task not in seen]
    return rows + KIND.shown(run, retired)


def check_successor(key, successor, rows, operators=None):
    """The successor is an existing capability, and following successors never returns to `key`."""
    newest = newest_revisions(rows, operators)
    if successor not in newest:
        raise ValueError('Unknown successor capability %s' % successor)
    seen, current = {key}, successor
    for _ in range(64):
        if current in seen:
            raise ValueError('Retiring %s in favour of %s would create a retirement cycle' % (key, successor))
        seen.add(current)
        record = newest.get(current)
        if record is None or record.get('acceptance_state') != 'superseded':
            return
        current = record['successor']
    raise ValueError('The successor chain from %s is too long' % successor)


def _newest(entry):
    """The revision a reader describes: the accepted one, else the newest draft, else the newest of any state."""
    if not entry or entry['state'] == 'conflicted':
        return None
    return entry.get('record') or entry.get('proposed') or entry.get('newest')


# -- alias records ---------------------------------------------------------------------------

def parse_alias(body):
    """The `capability-alias-v1` record iff it passes its full schema."""
    if not isinstance(body, str) or not body.startswith(ALIAS_PREFIX):
        return None
    rest = body[len(ALIAS_PREFIX):]
    try:
        record = parse_json(rest)
        if not isinstance(record, dict) or set(record) != set(ALIAS_FIELDS):
            return None
        if type(record['schema_version']) is not int or record['schema_version'] != 1:
            return None
        valid_key(record['key'])
        valid_alias_text(record['alias'])
        if record['normalized'] != normalize(record['alias']):
            return None
        if record['action'] == 'propose':
            if record['reason'] is not None:
                return None
            if record['evidence'] is not None:
                _pointers([record['evidence']], 'evidence', 1, ('::', '#', ''))
        elif record['action'] == 'reject':
            if record['evidence'] is not None:
                return None
            _bounded_text(record['reason'], 'reason', REASON_MAX)
        else:
            return None
        submitter = record['submitter']
        if not isinstance(submitter, dict) or set(submitter) != set(SUBMITTER_FIELDS):
            return None
        identifier(submitter['actor'])
        if submitter['identity'] not in ('verified', 'unverified') or submitter['submitted_by_agent'] is not False:
            return None
        if (submitter['identity'] == 'verified') != isinstance(submitter['person'], str):
            return None
        if not isinstance(record['at'], str) or not record['at'].strip():
            return None
        if content_hash(record) != record['sha256'] or canonical_bytes(record).decode('utf-8') != rest:
            return None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    return record


def submitter_for(actor, verified):
    """The attribution block. `verified` is decided by the ROUTE, never by the actor's name:
    only the operator shell route, after its allowlist check, passes True (review 01a0fc55)."""
    return {'actor': actor, 'person': 'operator:' + actor if verified else None,
            'identity': 'verified' if verified else 'unverified', 'submitted_by_agent': False}


def alias_record(key, alias, action, actor, verified, evidence=None, reason=None):
    record = {'schema_version': 1, 'key': key, 'alias': alias, 'normalized': normalize(alias), 'action': action,
              'evidence': evidence, 'reason': reason, 'submitter': submitter_for(actor, verified),
              'at': core.now()}
    record['sha256'] = content_hash(record)
    body = ALIAS_PREFIX + canonical_bytes(record).decode('utf-8')
    if parse_alias(body) != record:
        raise ValueError('Refusing to write an alias record that does not pass its own schema')
    return record, body


def _read_alias(view, body, comment):
    """One alias record in native order; a malformed one is reported, never fatal to the entry."""
    record = parse_alias(body)
    if record is None:
        view['warnings'].append({'code': 'malformed-alias', 'detail': 'alias record %s is malformed and ignored'
                                                                      % comment.get('id')})
        return
    view.setdefault('alias_records', []).append((record, comment.get('author'), comment.get('id')))


def alias_state(view, operators):
    """(pending, rejected) alias proposals for one entry, from records in native order.

    A proposal is pending until a later reject record with the same normalised text, or
    until the accepted revision's `aliases` hold it (folded). A record counts as verified
    (and a reject takes effect) only when it says `identity: verified` AND its stored
    native author is the record's actor AND that actor is on the operator allowlist. The
    author alone is not enough: over SSH it is whatever actor the caller declared.
    """
    def trusted(record, author):
        return (record['submitter']['identity'] == 'verified' and author == record['submitter']['actor']
                and author in allowlist)

    allowlist = configured_operators(operators if operators is not None else ())
    accepted = {normalize(alias) for alias in ((view.get('record') or {}).get('aliases') or [])}
    pending, rejected = {}, set()
    for record, author, comment_id in view.get('alias_records', []):
        if record['key'] != view['key']:
            continue
        if record['action'] == 'reject':
            if trusted(record, author):
                rejected.add(record['normalized'])
                pending.pop(record['normalized'], None)
            continue
        if record['normalized'] in accepted or record['normalized'] in rejected \
                or record['normalized'] in pending:
            continue
        verified = trusted(record, author)
        pending[record['normalized']] = {'alias': record['alias'], 'normalized': record['normalized'],
                                         'person': 'operator:' + author if verified else None,
                                         'identity': 'verified' if verified else 'unverified',
                                         'actor': author, 'comment_id': comment_id, 'evidence': record['evidence']}
    return list(pending.values()), rejected


# -- the kind ---------------------------------------------------------------------------------------

KIND = keyed_entries.AnchoredKind(
    noun='capability', title='Capability', command='capability', type_label=TYPE_LABEL, state_labels=STATE_LABEL,
    key_prefix=KEY_LABEL, family='capability-', entry_prefix=ENTRY_PREFIX, acceptance_prefix=ACCEPTANCE_PREFIX,
    journal=JOURNAL, source='capability-apply', accept_action='accept a capability',
    apply_command='admin.py capability-apply', reconcile_command='admin.py capability-reconcile',
    anchor_title=ANCHOR_TITLE, anchor_description=ANCHOR_DESCRIPTION,
    close_reason='capability index anchor (not a work item)',
    valid_key=lambda value, where='key': valid_key(value, where), parse_entry=lambda body: parse_entry(body),
    validate_entry=lambda record: validate_entry(record),
    entry_record=lambda payload, revision, state: entry_record(payload, revision, state),
    validate_content=_validate_content, write_time_rules=lambda record: None,
    content_fields=CONTENT_FIELDS, pre_write=lambda payload, run, operators: _pre_write(payload, run, operators),
    extra_records={'capability-alias': _read_alias, 'capability-verification': verification.read_record},
    extra_parsers={'capability-alias': parse_alias, 'capability-verification': verification.parse},
    supports_retire=True,
)
SPEC = KIND.spec
key_label = KIND.key_label
read_rows = KIND.read_rows
existing_revisions = KIND.existing_revisions
anchor_for = KIND.anchor_for
entry_comment = KIND.entry_comment
write_payload = KIND.write_payload


def apply_native(payload, actor, run, project, operator=False, operators=None):
    """Caller holds the canonical project lock. Contributors propose and revise drafts;
    the operator route writes a direct accepted revision 1 or retires a key."""
    return KIND.apply_native(payload, actor, run, project, operator=operator, operators=operators)


def reconcile(project, operation_id, actor, reason, disposition, run, issue_id=None, operators=None):
    """Operator-only: resolve a stuck `.capability-requests/` receipt from native state. `operators`
    (the deployment allowlist) decides which operator voids apply when `complete` confirms
    the anchor holds a live record."""
    return KIND.reconcile(project, operation_id, actor, reason, disposition, run, issue_id=issue_id,
                          operators=operators)


# -- batch acceptance (.60 section 4) ----------------------------------------------------------------

BATCH_FIELDS = {'schema_version', 'operation_id', 'items', 'acceptance_state', 'acceptance'}
ITEM_FIELDS = {'key', 'revision', 'record_sha256'}


def validate_batch(payload):
    if not isinstance(payload, dict):
        raise ValueError('capability-apply payload must be an object')
    core.refuse_injected_labels(payload, 'capability operation')
    core.checked_fields(payload, BATCH_FIELDS, 'capability-apply payload')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    identifier(payload.get('operation_id'))
    if payload.get('acceptance_state') != 'accepted':
        raise ValueError('capability-apply writes accepted revisions (acceptance_state must be accepted)')
    core.validate_acceptance_shape(payload.get('acceptance'))
    core.bound_acceptance(dict(payload['acceptance'], record_sha256='0' * 64))
    items = payload.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= BATCH_MAX:
        raise ValueError('items must be a list of 1..%d {key, revision, record_sha256}' % BATCH_MAX)
    keys = set()
    for index, item in enumerate(items):
        where = 'items[%d]' % index
        if not isinstance(item, dict):
            raise ValueError(where + ' must be an object')
        core.checked_fields(item, ITEM_FIELDS, where)
        if set(item) != ITEM_FIELDS:
            raise ValueError(where + ' needs key, revision and record_sha256')
        valid_key(item['key'], where + '.key')
        core.positive_int(item['revision'], where + '.revision')
        if not isinstance(item['record_sha256'], str) or not SHA256_TEXT.match(item['record_sha256']):
            raise ValueError(where + '.record_sha256 must be the reviewed revision\'s content hash')
        if item['key'] in keys:
            raise ValueError('items must not repeat a key')
        keys.add(item['key'])
        identifier(item_operation_id(payload['operation_id'], item['key']))
    return payload


def item_operation_id(operation_id, key):
    """The per-item operation id: each item's receipt is keyed `(operation_id, key)`."""
    return '%s/%s' % (operation_id, key)


def apply_batch(payload, actor, run, project, operators=None, lock=None):
    """`admin.py capability-apply`: accept a batch under one F3 decision (.60 section 4).

    The operator allowlist is checked first. A batch receipt binds the operation id to
    the exact item list, so a changed list under the same id is refused. Items run in
    list order, each through the single core write (evidence, then the revision, then
    the label) with its own receipt keyed `(operation_id, key)`; an item whose receipt is
    complete rechecks its key for duplicate anchors before reporting `already-accepted`,
    with no native write. A refusal before an item's writes is reported
    `refused` and the batch continues; an uncertain write stops the batch (`uncertain`,
    reconcile that item), and the rest stay `not-run` until a retry.

    `lock` is a callable returning a context manager that holds the project's
    coordination lock. The batch takes it once per ITEM and releases it between items
    (review 01a0fc55 `batch-lock`), so another writer waits behind at most one item, not
    the whole batch. Each item reads only its own key and re-checks compare-and-swap
    under its own hold, so a change made between two items is seen, and refused if it
    made the reviewed revision stale. Without `lock` the caller is holding it.
    """
    core.require_configured_operator(actor, operators, KIND.spec.accept_action)
    validate_batch(payload)
    lock = lock or contextlib.nullcontext
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    keys = [item['key'] for item in payload['items']]
    with lock():
        journal = core.journal_dir(project, JOURNAL)
        receipt = core.receipt_path(journal, identity, 'capability')
        prior = load_json(receipt) if receipt.exists() else None
        if prior is not None and (prior.get('sha256') != digest or prior.get('operation') != 'apply-batch'):
            raise ValueError('Operation ID already used for a different batch; a changed list needs a new '
                             'operation ID.')
        atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor, 'operation': 'apply-batch',
                         'operation_id': payload['operation_id'], 'items': keys})
    results, stopped = [], False
    for position, item in enumerate(payload['items']):
        if stopped:
            results.append({'key': item['key'], 'result': 'not-run'})
            continue
        if position and lock is not contextlib.nullcontext:
            time.sleep(BATCH_YIELD_SECONDS)   # the lock is free here: let a waiting writer take it
        with lock():
            result = _apply_item(payload, item, actor, run, project, operators, journal)
        results.append(result)
        stopped = result['result'] == 'uncertain'
    complete = not stopped
    with lock():
        atomic(receipt, {'sha256': digest, 'status': 'complete' if complete else 'pending', 'actor': actor,
                         'operation': 'apply-batch', 'operation_id': payload['operation_id'], 'items': keys,
                         'results': {result['key']: result['result'] for result in results}})
    return {'operation_id': payload['operation_id'], 'decision_id': payload['acceptance']['decision_id'],
            'items': results, 'complete': complete}


def _apply_item(payload, item, actor, run, project, operators, journal):
    """One batch item, under one hold of the coordination lock."""
    operation_id = item_operation_id(payload['operation_id'], item['key'])
    item_receipt = core.receipt_path(journal, content_hash({'operation_id': operation_id}), 'capability')
    done = load_json(item_receipt) if item_receipt.exists() else None
    if isinstance(done, dict) and done.get('status') == 'complete':
        try:
            KIND.require_unique_key(KIND.read_key_rows(run, item['key']), item['key'])
        except ValueError as error:
            return {'key': item['key'], 'result': 'refused', 'reason': str(error)}
        return {'key': item['key'], 'result': 'already-accepted', 'revision': done.get('revision'),
                'native_id': done.get('id')}
    single = {'schema_version': 1, 'operation_id': operation_id, 'operation': 'accept', 'key': item['key'],
              'revision': item['revision'], 'record_sha256': item['record_sha256'],
              'acceptance_state': 'accepted', 'acceptance': payload['acceptance']}
    writes = []

    def counted(argv):
        if argv[:1] in (['comments'], ['update'], ['close'], ['create']) and '--dry-run' not in argv:
            writes.append(argv[0])
        return run(argv)
    try:
        outcome = KIND.apply_native(single, actor, counted, project, operator=True, operators=operators)
    except (ValueError, RuntimeError, OSError) as error:
        text = str(error)
        if 'outcome is uncertain' in text or isinstance(error, (RuntimeError, OSError)):
            return {'key': item['key'], 'result': 'uncertain',
                    'reason': 'the native write did not confirm; reconcile %s with admin.py '
                              'capability-reconcile --operation-id %s' % (item['key'], operation_id)}
        return {'key': item['key'], 'result': 'refused', 'reason': text}
    # `accepted` when this run wrote (including finishing an earlier uncertain attempt);
    # `already-accepted` when the item's accepted revision and evidence were already there.
    return {'key': item['key'], 'result': 'accepted' if writes else 'already-accepted',
            'revision': outcome['revision'], 'native_id': outcome['native_id'],
            'record_comment_id': outcome.get('record_comment_id')}


# -- aliases ---------------------------------------------------------------------------------------

def propose_alias(key, alias, actor, run, operators, evidence=None, operator=False):
    """`capability propose-alias KEY "PHRASE" [--evidence POINTER]`: a capped candidate alias.

    `operator=True` is the operator shell route (`admin.py capability-alias-propose`):
    the allowlist is checked first and the record is written `verified`. The endpoint
    route never is, whatever actor name the caller declares.

    Natively idempotent: the record itself is the receipt, so an identical pending
    proposal is returned instead of written twice. Refusals name the cap; none quotes
    another capability's text.
    """
    if operator:
        core.require_configured_operator(actor, operators, 'propose a verified capability alias')
    valid_key(key)
    valid_alias_text(alias)
    if evidence is not None:
        _pointers([evidence], 'evidence', 1, ('::', '#', ''))
    rows = KIND.read_rows(run)
    KIND.require_unique_key(rows, key)
    entries, _ = catalog(rows, operators)
    target = next((entry for entry in entries if entry['key'] == key), None)
    if target is None or target['state'] in ('malformed', 'unsupported'):
        raise ValueError('Unknown capability key %s; use capability list or capability find' % key)
    if target['state'] == 'superseded':
        raise ValueError('Capability %s is retired; propose the alias on its successor' % key)
    phrase = normalize(alias)
    for entry in entries:
        if entry['key'] == key or entry['state'] in ('malformed', 'unsupported'):
            continue
        record = _newest(entry) or {}
        taken = {normalize(entry['key']), normalize(record.get('name') or '')}
        taken |= {normalize(item) for item in ((entry.get('record') or {}).get('aliases') or [])}
        if phrase in taken:
            raise ValueError('The alias collides with the key, name or an accepted alias of capability %s'
                             % entry['key'])
    record = _newest(target) or {}
    if phrase in {normalize(key), normalize(record.get('name') or '')} | \
            {normalize(item) for item in ((target.get('record') or {}).get('aliases') or [])}:
        raise ValueError('Capability %s already matches that phrase exactly' % key)
    if phrase in {normalize(item) for item in record.get('aliases') or []}:
        raise ValueError('Capability %s already carries that phrase in its newest revision' % key)
    pending = {item['normalized']: item for item in target['aliases_pending']}
    if phrase in pending:
        return {'key': key, 'alias': pending[phrase]['alias'], 'state': 'proposed', 'reconciled': True,
                'comment_id': pending[phrase]['comment_id']}
    if phrase in target['aliases_rejected']:
        raise ValueError('That alias was rejected for capability %s by an operator' % key)
    submitter = submitter_for(actor, operator)
    mine = lambda item: (item['person'] == submitter['person']) if submitter['identity'] == 'verified' \
        else item['identity'] == 'unverified'
    on_target = [item for item in target['aliases_pending'] if mine(item)]
    in_project = [item for entry in entries for item in entry.get('aliases_pending') or [] if mine(item)]
    if len(target['aliases_pending']) >= CAP_CAPABILITY:
        raise ValueError('Capability %s already has %d pending aliases (the per-capability cap); an operator must '
                         'fold or reject some first' % (key, CAP_CAPABILITY))
    if submitter['identity'] == 'verified':
        if len(on_target) >= CAP_PERSON_CAPABILITY or len(in_project) >= CAP_PERSON_PROJECT:
            raise ValueError('You already hold the most pending aliases allowed (%d per capability, %d per '
                             'project)' % (CAP_PERSON_CAPABILITY, CAP_PERSON_PROJECT))
    elif len(on_target) >= CAP_UNVERIFIED_CAPABILITY or len(in_project) >= CAP_UNVERIFIED_PROJECT:
        raise ValueError('The shared pool for unverified proposers is full (%d per capability, %d per project) '
                         'until an operator folds or rejects pending aliases; SSH attribution beyond operators '
                         'arrives with kittrial-5bb.68' % (CAP_UNVERIFIED_CAPABILITY, CAP_UNVERIFIED_PROJECT))
    _, body = alias_record(key, alias, 'propose', actor, operator, evidence=evidence)
    raw = run(['comments', 'add', target['native_id'], body, '--json'])
    return {'key': key, 'alias': alias, 'state': 'proposed', 'reconciled': False,
            'comment_id': core._comment_id(raw), 'identity': submitter['identity']}


def reject_alias(payload, actor, run, project, operators=None):
    """`admin.py capability-alias-reject` (operator): lookup then ignores the proposal."""
    core.require_configured_operator(actor, operators, 'reject a capability alias')
    if not isinstance(payload, dict):
        raise ValueError('alias-reject payload must be an object')
    core.checked_fields(payload, {'schema_version', 'key', 'alias', 'reason'}, 'alias-reject payload')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    valid_key(payload.get('key'))
    valid_alias_text(payload.get('alias'))
    _bounded_text(payload.get('reason'), 'reason', REASON_MAX)
    rows = KIND.read_rows(run)
    KIND.require_unique_key(rows, payload['key'])
    entries, _ = catalog(rows, operators)
    target = next((entry for entry in entries if entry['key'] == payload['key']), None)
    if target is None:
        raise ValueError('Unknown capability key %s' % payload['key'])
    phrase = normalize(payload['alias'])
    if phrase in target['aliases_rejected']:
        return {'key': payload['key'], 'alias': payload['alias'], 'state': 'rejected', 'reconciled': True}
    if phrase not in {item['normalized'] for item in target['aliases_pending']}:
        raise ValueError('No pending alias with that text on capability %s' % payload['key'])
    _, body = alias_record(payload['key'], payload['alias'], 'reject', actor, True, reason=payload['reason'])
    raw = run(['comments', 'add', target['native_id'], body, '--json'])
    return {'key': payload['key'], 'alias': payload['alias'], 'state': 'rejected', 'reconciled': False,
            'comment_id': core._comment_id(raw)}


# -- reads -----------------------------------------------------------------------------------------

def entry_view(row, operators):
    """One capability anchor as readers see it. `verification_records` holds its parsed
    verification records in native order; what they mean is derived only for the entries
    a read actually shows (`verification_of`), because that can cost a native read."""
    view = KIND.entry_view(row, operators)
    if view['state'] in ('malformed', 'unsupported'):
        view.update(aliases_pending=[], aliases_rejected=set(), newest=None, verification_records=[])
    else:
        view['aliases_pending'], view['aliases_rejected'] = alias_state(view, operators)
        revisions = KIND.existing_revisions(KIND.live_row(row, operators)[0])
        view['newest'] = revisions[max(revisions)] if revisions else None
        view.setdefault('verification_records', [])
    view.pop('alias_records', None)
    return view


def read_catalog(run):
    """(rows, lifecycle rows or None): the whole catalog, in two native reads at most.

    The same read as `read_rows` (one label-filtered `bd list`, then one `bd show` up
    to CATALOG_SHOW_MAX entries or one `bd export --all` above). When it exports, it
    also keeps the lifecycle rows of that same export, so the integrated-commit test of
    `list` and `find` is answered from it: no second export and no narrow read
    (kittrial-5bb.69 review 01a0fe9e). `None` means no export was made.

    A row can be both: a capability anchor that also carries lifecycle facts (an
    integrated fact recorded on the anchor's own id). It goes into BOTH lists, so
    `list` and `find` reach the same verdict as `get`, which reads lifecycle facts
    without this split (kittrial-5bb.69 re-review, P3).
    """
    listed = json.loads(run(['list', '--label', TYPE_LABEL, '--all', '--limit', '0', '--json']) or '[]')
    ids = [row['id'] for row in listed or [] if isinstance(row, dict) and isinstance(row.get('id'), str)]
    if len(ids) <= keyed_entries.CATALOG_SHOW_MAX:
        return KIND.shown(run, ids), None
    wanted = set(ids)
    rows, lifecycle = [], []
    for line in run(['export', '--all']).splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get('id') in wanted:
            rows.append(row)
        if verification.is_lifecycle_row(row):
            lifecycle.append(row)
    return rows, lifecycle


class Trust:
    """Who is trusted to verify, and which commits are integrated, for one read."""

    def __init__(self, run=None, operators=None, verifiers=None, journal=None, export_rows=None):
        self.actors = verification.trusted_actors(operators, verifiers)
        if run is None and export_rows is None:
            self.integrated = lambda commit: False
        else:
            self.integrated = verification.Integrated(run, operators, journal, export_rows=export_rows)


def verification_of(entry, trust):
    """The `verification` block of the revision a reader describes (.60 section 5.2)."""
    trust = trust or Trust()
    if entry['state'] == 'conflicted':
        block = verification.derive([], None, trust.actors, trust.integrated)
        block['state'] = 'conflicted'
        return block
    return verification.derive(entry.get('verification_records') or [], _newest(entry), trust.actors,
                               trust.integrated)


def catalog(rows, operators):
    return KIND.catalog(rows, operators, view=entry_view)


def _excerpt(text, limit):
    from capabilities import excerpt
    return excerpt(text, limit)


def _record_view(record):
    if record is None:
        return None
    return {'revision': record['revision'], 'name': _excerpt(record['name'], NAME_MAX),
            'summary': _excerpt(record['summary'], SUMMARY_MAX), 'aliases': record['aliases'],
            'requirements': record['requirements'], 'anchors': record['anchors'], 'code': record['code'],
            'tests': record['tests'], 'owner': record['owner'], 'tags': record['tags'],
            'acceptance_state': record['acceptance_state'], 'successor': record['successor'],
            'sha256': record['sha256']}


def _pending_view(entry):
    return [{'alias': _excerpt(item['alias'], ALIAS_MAX), 'alias_state': 'proposed', 'trust': 'proposed',
             'submitter': {'person': item['person'], 'identity': item['identity']}}
            for item in entry['aliases_pending']]


def _replaces(entries, key):
    return sorted(entry['key'] for entry in entries
                  if entry['state'] == 'superseded' and (entry['record'] or {}).get('successor') == key)


def get(rows, key, operators, trust=None):
    entry = KIND.find_entry(rows, key, operators, view=entry_view)
    entries, _ = catalog(rows, operators)
    checked = verification.public(verification_of(entry, trust)) \
        if entry['state'] not in ('malformed', 'unsupported', 'conflicted') else None
    return {'schema_version': 1, 'key': key, 'state': entry['state'], 'native_id': entry['native_id'],
            'trust': 'conflicted' if entry['state'] == 'conflicted' else 'accepted' if entry['record'] else 'draft',
            'anchors': entry.get('duplicate_anchors', []),
            'record': _record_view(entry['record']), 'record_comment_id': entry['record_comment_id'],
            'acceptance': entry['acceptance'], 'acceptance_inert': entry['acceptance_inert'],
            'inert_operator': entry['inert_operator'], 'proposed': _record_view(entry['proposed']),
            'proposed_comment_id': entry['proposed_comment_id'], 'aliases_pending': _pending_view(entry),
            'replaces': _replaces(entries, key),
            'resolved': (entry['record'] or {}).get('successor') if entry['state'] == 'superseded' else None,
            'verification': checked,
            'warnings': entry['warnings'][:10],
            'coverage': 'newest accepted revision and its acceptance evidence; the newest draft after it is in '
                        'proposed; verification describes the accepted revision, or the newest draft when none '
                        'is accepted, and is verified only by an operator or listed verifier'}


def _list_item(entry, trust=None, pointers=False):
    source = entry.get('candidate') or entry['record'] or entry['proposed'] or {}
    item = {'key': entry['key'], 'name': (source.get('name') or '')[:NAME_MAX], 'state': entry['state'],
            'trust': 'conflicted' if entry['state'] == 'conflicted' else 'accepted' if entry['record'] else 'draft',
            'owner': source.get('owner'),
            'tags': source.get('tags') or [], 'revision': source.get('revision'), 'native_id': entry['native_id'],
            'aliases_pending': len(entry['aliases_pending']), 'acceptance_inert': entry['acceptance_inert'],
            'verification': 'conflicted' if entry['state'] == 'conflicted' else verification_of(entry, trust)['state']}
    if entry['state'] == 'conflicted':
        item['anchor_trust'] = entry['anchor_trust']
    if pointers:
        # What `capability check` needs to verify this row's revision in a checkout.
        checked = source if entry['state'] != 'conflicted' else {}
        item.update(record_sha256=checked.get('sha256'), code=checked.get('code') or [],
                    tests=checked.get('tests') or [], anchors=checked.get('anchors') or [])
    return item


def list_entries(rows, options, operators, trust=None):
    entries, incomplete = catalog(rows, operators)
    good = [entry for entry in entries if entry['state'] not in ('malformed', 'unsupported')]
    state = options.get('state') or 'all'
    if state != 'all':
        good = [entry for entry in good if entry['state'] == state]
    if options.get('owner'):
        good = [entry for entry in good if (_newest(entry) or {}).get('owner') == options['owner']]
    for tag in options.get('tags') or []:
        good = [entry for entry in good if tag in ((_newest(entry) or {}).get('tags') or [])]
    good.sort(key=lambda entry: entry['key'] or '')
    offset, limit = options.get('offset', 0), options.get('limit', 20)
    return {'schema_version': 1, 'total': len(good),
            'items': [_list_item(entry, trust, options.get('pointers', False))
                      for entry in good[offset:offset + limit]],
            'next_offset': offset + limit if offset + limit < len(good) else None,
            'coverage': KIND.coverage(entries, incomplete, 'one row per unambiguous key; every conflicted anchor '
                                                           'is shown without a selected record')}


def _exact_phrases(entry):
    """(names, accepted aliases): the normalised phrases `find` matches exactly for one entry.

    The names are the key and the name of the revision a reader describes (the accepted
    one, else the newest draft); the aliases are those of the accepted revision only.
    """
    record = entry.get('candidate') or _newest(entry) or {}
    names = {normalize(entry['key']), normalize(record.get('name') or '')}
    accepted_aliases = {normalize(item) for item in ((entry.get('record') or {}).get('aliases') or [])}
    return names, accepted_aliases


def exact_index(rows, operators):
    """{normalised phrase: [{key, trust, state}]}: every phrase `find` would now match exactly.

    The same rule as `find` (`_exact_phrases`), over every readable entry, accepted
    records first. `capability misses` uses it to mark the misses that now resolve.
    """
    entries, _ = catalog(rows, operators)
    index = {}
    for entry in sorted((entry for entry in entries if entry['state'] not in ('malformed', 'unsupported', 'conflicted')),
                        key=lambda entry: (entry['record'] is None, entry['key'])):
        names, accepted_aliases = _exact_phrases(entry)
        for phrase in sorted(names | accepted_aliases):
            if phrase:
                index.setdefault(phrase, []).append({'key': entry['key'],
                                                     'trust': 'accepted' if entry['record'] else 'draft',
                                                     'state': entry['state']})
    return index


def find(rows, phrase, operators, limit=5, trust=None):
    """`capability find PHRASE`: records only, scored like .61's lookup (.60 section 7).

    Exact matches come only from keys, names and accepted aliases (of the accepted
    revision, or of a draft's own name/key); a pending alias only lifts its capability
    among the candidates. Accepted and draft records are both returned, trust-marked.
    """
    if not isinstance(phrase, str) or len(phrase) > PHRASE_MAX:
        raise ValueError('phrase: expected text up to %d characters' % PHRASE_MAX)
    from capabilities import clean
    text = clean(phrase)
    if not text:
        raise ValueError('find needs a nonempty phrase')
    if not FIND_LIMIT[0] <= limit <= FIND_LIMIT[1]:
        raise ValueError('--limit: expected %d..%d' % FIND_LIMIT)
    key = normalize(text)
    wanted = stems(text)
    entries, incomplete = catalog(rows, operators)
    exact, conflicted, scored = [], [], []
    for entry in entries:
        if entry['state'] in ('malformed', 'unsupported') or entry['key'] is None:
            continue
        record = entry.get('candidate') or _newest(entry) or {}
        names, accepted_aliases = _exact_phrases(entry)
        if text == entry['key'] or key in names | accepted_aliases:
            (conflicted if entry['state'] == 'conflicted' else exact).append(entry)
            continue
        pending = {item['normalized'] for item in entry['aliases_pending']}
        draft_aliases = {normalize(item) for item in record.get('aliases') or []} - accepted_aliases
        vocabulary = stems(' '.join([entry['key'].replace('.', ' ').replace('-', ' '), record.get('name') or '',
                                     ' '.join(record.get('aliases') or [])] + [item['alias'] for item in
                                                                             entry['aliases_pending']]))
        overlap = len(wanted & vocabulary) / len(wanted) if wanted else 0.0
        summary_overlap = len(wanted & stems(record.get('summary') or '')) / len(wanted) if wanted else 0.0
        score = max(overlap, 0.5 * summary_overlap)
        if key in pending or key in draft_aliases:
            score = max(score, 0.95)   # a candidate lift, never an exact match
        if score >= 0.2:
            scored.append((score, entry))
    exact.sort(key=lambda entry: (entry['record'] is None, entry['key']))
    groups = {tuple(anchor['native_id'] for anchor in entry['duplicate_anchors']) for entry in conflicted}
    conflicted = [entry for entry in entries if entry['state'] == 'conflicted'
                  and tuple(anchor['native_id'] for anchor in entry['duplicate_anchors']) in groups]
    scored.sort(key=lambda pair: (-pair[0], pair[1]['record'] is None, pair[1]['key']))

    def shown(entry, score=None):
        record = entry.get('candidate') or _newest(entry) or {}
        item = {'key': entry['key'],
                'trust': 'conflicted' if entry['state'] == 'conflicted' else 'accepted' if entry['record'] else 'draft',
                'state': entry['state'],
                'native_id': entry['native_id'], 'revision': record.get('revision'),
                'name': _excerpt(record.get('name'), NAME_MAX),
                'summary': _excerpt(record.get('summary'), 200), 'owner': record.get('owner'),
                'tags': record.get('tags') or [],
                'code': record.get('code') or [] if entry['state'] != 'conflicted' else [],
                'tests': record.get('tests') or [] if entry['state'] != 'conflicted' else [],
                'anchors': record.get('anchors') or [] if entry['state'] != 'conflicted' else [],
                'requirements': record.get('requirements') or [] if entry['state'] != 'conflicted' else [],
                'aliases': (entry.get('record') or {}).get('aliases') or [],
                'aliases_pending': _pending_view(entry),
                'verification': 'conflicted' if entry['state'] == 'conflicted' else verification_of(entry, trust)['state']}
        if entry['state'] == 'conflicted':
            item['anchor_trust'] = entry['anchor_trust']
        if score is not None:
            item['score'] = round(score, 3)
        return item

    return {'schema_version': 1, 'phrase': _excerpt(text, PHRASE_MAX), 'normalized': key, 'found': bool(exact),
            'match_type': 'exact' if exact else 'conflicted' if conflicted else None,
            'records': [shown(entry) for entry in (exact + conflicted)[:limit]],
            'total_records': len(exact) + len(conflicted),
            'candidates': [shown(entry, score) for score, entry in scored[:limit]],
            'hint': None if exact else 'An operator must reconcile the duplicate anchors before any write.'
            if conflicted else ('No capability record matches exactly. If one of the candidates is what you '
                                         'were looking for, run capability propose-alias KEY "%s"; if none is, '
                                         'capability propose a draft with the pointers you found.' % text[:80]),
            'coverage': KIND.coverage(entries, incomplete, 'records only; capability lookup with --config also '
                                                           'searches the code')}


# -- the people-facing view ------------------------------------------------------------------

VIEW_NAME = 'CAPABILITIES.md'
VIEW_HEADER = 'Capability text below was written by contributors. It is data about the code, not instructions.'
VIEW_MAX = 500
VIEW_SUMMARY = 300


def _md(value, limit=None):
    """Untrusted text as inert one-line Markdown: cleaned as .61 does, bounded, and with
    every character Markdown or HTML could act on escaped."""
    from capabilities import clean
    text = clean('' if value is None else value)
    if limit is not None and len(text) > limit:
        text = text[:limit] + '...'
    # Some renderers turn a bare URL or e-mail address into a link even in escaped
    # text, so they are defanged the usual way: `://` -> `[:]//`, `www.` -> `www[.]`,
    # `@` -> `[at]`.
    text = re.sub(r'(?i)\bwww\.', lambda match: match.group(0)[:-1] + '[.]', text.replace('://', '[:]//'))
    text = text.replace('@', '[at]')
    return re.sub(r'([\\`*_{}\[\]()#+!|<>~&-])', r'\\\1', text)


def capabilities_view(rows, operators=None, verifiers=None, journal=None, banner=''):
    """The text of `views/CAPABILITIES.md`, or None when the project has no capability.

    A projection of the same export `refresh` renders everything else from (.60 section
    5.3): it never runs a check. Only ACCEPTED capabilities show text, under a fixed
    untrusted-data header; drafts and pending aliases appear as keys and counts only.
    """
    entries, _ = catalog(rows, operators)
    good = sorted((entry for entry in entries if entry['state'] not in ('malformed', 'unsupported')),
                  key=lambda entry: entry['key'] or '')
    if not entries:
        return None
    trust = Trust(None, operators, verifiers, journal, export_rows=rows)
    accepted = [entry for entry in good if entry['state'] == 'accepted']
    drafts = [entry for entry in good if entry['state'] == 'draft-only' or entry['proposed']]
    retired = [entry for entry in good if entry['state'] == 'superseded']
    conflicts = [entry for entry in good if entry['state'] == 'conflicted']
    pending = [(entry['key'], len(entry['aliases_pending'])) for entry in good if entry['aliases_pending']]
    lines = ['# Capabilities\n\n', banner, VIEW_HEADER + '\n\n',
             'Accepted: %d. Drafts awaiting acceptance: %d. Capabilities with pending aliases: %d. Retired: %d. '
             'Unreadable: %d. Conflicted anchors: %d.\n\n' % (len(accepted), len(drafts), len(pending), len(retired),
                                       len(entries) - len(good), len(conflicts)),
             'Read one with `capability get KEY`. `verified` means an operator or listed verifier recorded a '
             'passing check; `reported` is an unconfirmed report; `drifted` means a pointer was reported '
             'missing.\n\n']
    for entry in accepted[:VIEW_MAX]:
        record = entry['record']
        block = verification_of(entry, trust)
        checked = block['state']
        if block['verified_at']:
            at = block['verified_at']
            checked += ' at commit %s (%s), checked %s by %s' % (
                at['commit'][:12], 'integrated' if at['integrated'] else 'not an integrated commit',
                _md(at['checked_at']), _md(at['person']))
        elif block['drift']:
            checked += ' at commit %s: %d pointer(s) reported missing' % (block['drift']['commit'][:12],
                                                                         len(block['drift']['missing']))
        lines.append('## %s: %s\n\n' % (_md(entry['key']), _md(record['name'], NAME_MAX)))
        lines.append('- Summary: %s\n' % _md(record['summary'], VIEW_SUMMARY))
        lines.append('- Owner: %s\n' % _md(record['owner']))
        lines.append('- State: accepted, revision %d. Verification: %s\n' % (record['revision'], checked))
        for label, values in (('Requirements', ['%s%s' % (link['key'], ' r%d' % link['revision']
                                                          if link.get('revision') else '')
                                                for link in record['requirements']]),
                              ('Design anchors', record['anchors']), ('Code', record['code']),
                              ('Tests', record['tests'])):
            if values:
                lines.append('- %s: %s\n' % (label, ', '.join(_md(value, POINTER_MAX) for value in values)))
        lines.append('\n')
    if len(accepted) > VIEW_MAX:
        lines.append('%d more accepted capabilities: use `capability list --state accepted`.\n\n'
                     % (len(accepted) - VIEW_MAX))
    if drafts:
        lines.append('## Drafts awaiting acceptance (keys only)\n\n'
                     + ''.join('- %s\n' % _md(entry['key']) for entry in drafts[:VIEW_MAX]) + '\n')
    if pending:
        lines.append('## Pending aliases (keys and counts only)\n\n'
                     + ''.join('- %s: %d\n' % (_md(key), count) for key, count in pending[:VIEW_MAX]) + '\n')
    if retired:
        lines.append('## Retired (keys only)\n\n'
                     + ''.join('- %s, replaced by %s\n' % (_md(entry['key']), _md(entry['record'].get('successor')))
                               for entry in retired[:VIEW_MAX]) + '\n')
    if conflicts:
        lines.append('## Conflicted anchors (no selected record)\n\n'
                     + ''.join('- %s: %s (%s); operator reconciliation required\n' % (
                         _md(entry['key']), _md(entry['native_id']), entry['anchor_trust'])
                               for entry in conflicts[:VIEW_MAX]) + '\n')
    return ''.join(lines)


# -- attention: `work` and `brief` (kittrial-5bb.76) ------------------------------------------

# An accepted capability whose current revision has had no trusted pass for this many
# days since its acceptance counts as `unverified_stale` (coordinator answer (c)).
UNVERIFIED_STALE_DAYS = 30
ATTENTION_SCAN_MAX = 1000
BRIEF_MAX = 3
CAPABILITY_ACTIONS = (  # (count key, priority, kind, reason)
    ('conflicted', 0, 'capability-conflict', 'A capability key has ambiguous duplicate anchors; an operator '
                                         'must reconcile them before any write.'),
    ('malformed', 1, 'capability-repair', 'A capability record cannot be read and needs an operator repair.'),
    ('drifted', 1, 'capability-drift', 'A capability pointer was reported missing; fix the record or the code, then '
                                       'verify it at an integrated commit.'),
    ('draft_pending', 2, 'capability-accept', 'A capability draft waits for an operator to accept it.'),
    ('alias_pending', 2, 'capability-alias', 'A proposed alias waits for an operator to fold or reject it.'),
    ('unverified_stale', 3, 'capability-verify', 'An accepted capability has had no trusted verification for '
                                                 'more than %d days.' % UNVERIFIED_STALE_DAYS),
    ('reported_only', 3, 'capability-verify-report', 'An accepted capability has passing reports but no trusted '
                                                     'verification.'),
)


def _days_since(stamp, now):
    import calendar
    try:
        return max(0, int((now - calendar.timegm(time.strptime(stamp, core.STAMP))) // 86400))
    except (TypeError, ValueError):
        return None


def _flags(entry, block, now):
    """The attention flags of one readable capability."""
    flags = []
    if entry['state'] == 'accepted':
        if block['state'] == 'drifted':
            flags.append('drifted')
        else:
            age = _days_since((entry.get('acceptance') or {}).get('at'), now)
            if block['state'] != 'verified' and age is not None and age > UNVERIFIED_STALE_DAYS:
                flags.append('unverified_stale')
            if block['state'] == 'reported':
                flags.append('reported_only')
    if entry['state'] == 'draft-only' or entry.get('proposed') is not None:
        flags.append('draft_pending')
    if entry.get('aliases_pending'):
        flags.append('alias_pending')
    return flags


def _attention_title(value):
    from agent_prompts import TITLE_LIMIT, label
    from capabilities import clean
    text = clean(value or '')
    return {'text': label(value),
            'omitted_chars': len(text) - TITLE_LIMIT + 1 if len(text) > TITLE_LIMIT else 0}


def work_attention(rows, actor, operators, project_name, verifiers=None, project=None, limit=20, offset=0,
                   now=None):
    """`attention.capability_index` for `work` (.60 section 8), in the agent attention shape.

    Counts always; `items` only for an actor on the deployment operator allowlist (an
    owner reads their own with `capability list --owner`; owner routing comes with the
    actor-map wiring). Computed from the export `work` already made, including the
    integrated-commit test, so it adds no native read. Reading changes nothing.
    """
    import calendar
    from agent_prompts import label
    now = now if now is not None else calendar.timegm(time.gmtime())
    trust = Trust(None, operators, verifiers, project, export_rows=rows)
    entries, incomplete = catalog(rows, operators)
    scanned = sorted(entries, key=lambda entry: str(entry['key'] or entry['native_id']))[:ATTENTION_SCAN_MAX]
    counts = {'conflicted': 0, 'drifted': 0, 'reported_only': 0, 'unverified_stale': 0, 'alias_pending': 0, 'draft_pending': 0,
              'malformed': 0, 'total': 0}
    first, flagged = {}, []
    conflict_groups = set()
    for entry in scanned:
        if entry['state'] == 'conflicted':
            group = tuple(anchor['native_id'] for anchor in entry['duplicate_anchors'])
            if group in conflict_groups:
                continue
            conflict_groups.add(group)
            counts['conflicted'] += 1
            counts['total'] += 1
            first.setdefault('conflicted', entry)
            flagged.append((entry, {'state': 'conflicted'}, ['conflicted']))
            continue
        if entry['state'] in ('malformed', 'unsupported'):
            counts['malformed'] += 1
            counts['total'] += 1
            first.setdefault('malformed', entry)
            continue
        if entry['state'] == 'superseded':
            continue
        block = verification_of(entry, trust)
        flags = _flags(entry, block, now)
        for flag in flags:
            counts[flag] += len(entry['aliases_pending']) if flag == 'alias_pending' else 1
            first.setdefault(flag, entry)
        if flags:
            counts['total'] += 1
            flagged.append((entry, block, flags))
    base = '/v1/projects/%s' % project_name
    actions = []
    for name, priority, kind, reason in CAPABILITY_ACTIONS:
        if counts[name]:
            entry = first[name]
            key = entry['key']
            # Lookup labels replace dots with hyphens and cannot be inverted
            # unambiguously. An unreadable key needs the catalog repair view.
            command = 'capability get %s' % key if key else 'capability list --state all'
            link = '%s/capabilities/%s' % (base, key) if key else '%s/capabilities?state=all' % base
            actions.append({'priority': priority, 'kind': kind, 'project': project_name,
                            'task': entry['native_id'], 'reason': reason,
                            'links': {'capability': link},
                            'label': {'text': command, 'omitted_chars': 0},
                            'token': 'capability.get' if key else 'capability.list'})
    actions.sort(key=lambda action: (action['priority'], str(action['project']), str(action['task'])))
    state = ('conflicted' if counts['conflicted'] else 'malformed' if counts['malformed'] else 'drifted' if counts['drifted'] else
             'pending' if counts['draft_pending'] or counts['alias_pending'] else
             'stale' if counts['unverified_stale'] or counts['reported_only'] else 'clear')
    phrases = (('conflicted', '{} key(s) with ambiguous duplicate anchors'),
               ('drifted', '{} capability(ies) drifted'), ('draft_pending', '{} draft(s) waiting for acceptance'),
               ('alias_pending', '{} alias(es) waiting for an operator'),
               ('unverified_stale', '{} accepted but not verified for over %d days' % UNVERIFIED_STALE_DAYS),
               ('reported_only', '{} with reports but no trusted verification'), ('malformed', '{} unreadable'))
    parts = [text.format(counts[name]) for name, text in phrases if counts[name]]
    operator = actor in configured_operators(operators if operators is not None else ())
    order = {'conflicted': -1, 'drifted': 0, 'draft_pending': 1, 'alias_pending': 2, 'unverified_stale': 3, 'reported_only': 4}
    flagged.sort(key=lambda item: (min(order[flag] for flag in item[2]), item[0]['key'] or ''))
    page = flagged[offset:offset + limit] if operator else []
    items = []
    for entry, block, flags in page:
        record = _newest(entry) or {}
        items.append({'kind': 'capability', 'key': entry['key'], 'task': entry['native_id'], 'state': entry['state'],
                      'verification': block['state'], 'flags': flags,
                      'owner': label(record['owner']) if record.get('owner') is not None else None,
                      'aliases_pending': len(entry['aliases_pending']),
                      'accepted_days': _days_since((entry.get('acceptance') or {}).get('at'), now),
                      'title': dict(_attention_title(record.get('name')),
                                    trust='accepted' if entry['record'] else 'draft')})
        if entry['state'] == 'conflicted':
            items[-1]['anchors'] = entry['duplicate_anchors']
            items[-1]['title']['trust'] = 'conflicted'
    result = {'state': state, 'summary': ('; '.join(parts) + '.') if parts else 'No capability needs attention.',
              'counts': counts, 'actions': actions,
              'truncated': (not operator and bool(flagged)) or offset + limit < len(flagged)
              or len(entries) > ATTENTION_SCAN_MAX,
              'computed_at': time.strftime(core.STAMP, time.gmtime(now)), 'items': items,
              'next_offset': offset + limit if operator and offset + limit < len(flagged) else None}
    notes = []
    if any(any(warning['code'] == 'duplicate-key' for warning in entry['warnings']) for entry in entries):
        notes.append(KIND.coverage(entries, [], 'Catalog warnings'))
    if not operator and flagged:
        notes.append('%d item(s) for operators (actors on the deployment operator allowlist); an owner reads '
                     'their own with capability list --owner' % len(flagged))
    if incomplete:
        notes.append('%d incomplete anchor(s)' % len(incomplete))
    if len(entries) > ATTENTION_SCAN_MAX:
        notes.append('only the first %d capabilities were scanned' % ATTENTION_SCAN_MAX)
    if notes:
        result['coverage'] = '; '.join(notes)
    return result


def brief_attention(rows, task_row, operators, verifiers=None, project=None, limit=BRIEF_MAX):
    """At most 3 `capability` items for a brief (.60 section 8): accepted capabilities whose
    tags match the task's labels, drifted first, then by key. Server-derived text only."""
    labels = set((task_row or {}).get('labels') or [])
    trust = Trust(None, operators, verifiers, project, export_rows=rows)
    entries, _ = catalog(rows, operators)
    chosen = []
    for entry in entries[:ATTENTION_SCAN_MAX]:
        if entry['state'] != 'accepted' or not labels & set(entry['record'].get('tags') or []):
            continue
        chosen.append((entry, verification_of(entry, trust)['state']))
    chosen.sort(key=lambda pair: (pair[1] != 'drifted', pair[0]['key']))
    items = [{'kind': 'capability', 'key': entry['key'], 'trust': 'accepted', 'verification': state,
              'title': _attention_title(entry['record']['name']),
              'text': 'Capability %s is tagged for this task (verification: %s).' % (entry['key'], state),
              'source': 'capability get ' + entry['key']}
             for entry, state in chosen[:limit]]
    return {'attention': items, 'attention_total': len(chosen), 'attention_more': (len(chosen) - len(items)) or None}


def help_payload():
    return {'schema_version': 1, 'action': 'capability', 'contract': 'cli-contract-v1',
            'usage': ['capability get KEY', 'capability list [--tag TAG]... [--owner IDENTITY] '
                      '[--state draft-only|accepted|superseded|all] [--limit N] [--offset N] [--pointers]',
                      'capability find PHRASE [--limit N]', 'capability misses [--limit N]',
                      'capability propose --file entry.json',
                      'capability revise --file entry.json',
                      'capability propose-alias KEY PHRASE [--evidence POINTER]',
                      'capability verify --file verification.json'],
            'local': ['capability lookup PHRASE', 'capability resolve POINTER...', 'capability index',
                      'capability check --repo PATH [--key KEY]... [--record | --payloads FILE]'],
            'telemetry': 'Each find is counted per project, and a find with no exact match also records its '
                         'normalised phrase, a count and first/last seen times; no actor is stored. Read it '
                         'with capability misses.',
            'limits': {'find_limit': list(FIND_LIMIT), 'phrase': PHRASE_MAX, 'name': NAME_MAX,
                       'summary': SUMMARY_MAX, 'alias': ALIAS_MAX, 'pointer': POINTER_MAX,
                       'code': CODE_MAX, 'tests': TESTS_MAX, 'anchors': ANCHORS_MAX,
                       'requirements': REQUIREMENTS_MAX, 'tags': TAGS_MAX,
                       'pending_alias_caps': {'verified_person': [CAP_PERSON_CAPABILITY, CAP_PERSON_PROJECT],
                                              'unverified_pool': [CAP_UNVERIFIED_CAPABILITY,
                                                                  CAP_UNVERIFIED_PROJECT],
                                              'per_capability': CAP_CAPABILITY},
                       'verification': {'results': verification.RESULTS_MAX,
                                        'open_failing_reports': {
                                            'verified_person': verification.CAP_PERSON_FAILING,
                                            'unverified_pool': verification.CAP_UNVERIFIED_FAILING},
                                        'untrusted_passing_reports_per_revision':
                                            verification.CAP_UNTRUSTED_REVISION}},
            'operator': ['admin.py capability-apply PROJECT --actor OPERATOR --file batch.json',
                         'admin.py capability-verify PROJECT --actor OPERATOR_OR_VERIFIER --file payloads.json',
                         'admin.py verifiers list|add|remove [ACTOR] [--confirm-revoke]',
                         'admin.py capability-retire PROJECT --actor OPERATOR --file retire.json',
                         'admin.py capability-alias-propose PROJECT --actor OPERATOR --file alias.json',
                         'admin.py capability-alias-reject PROJECT --actor OPERATOR --file reject.json',
                         'admin.py capability-reconcile PROJECT --operation-id ID --actor OPERATOR --reason TEXT '
                         '--disposition complete|failed|released [--issue-id ID]',
                         'admin.py void-record PROJECT --actor OPERATOR --file void.json',
                         'admin.py anchor-release PROJECT --kind capability --issue-id ID --actor OPERATOR '
                         '--reason TEXT']}


def _options(args, allowed):
    options = {'tags': [], 'limit': 20, 'offset': 0}
    index = 0
    while index < len(args):
        token = args[index]
        if token == '--json':
            index += 1
            continue
        if token == '--pointers' and token in allowed:
            options['pointers'] = True
            index += 1
            continue
        if token not in allowed or index + 1 >= len(args):
            raise ValueError('capability: unknown or incomplete option %s' % token)
        value = args[index + 1]
        name = allowed[token]
        if name == 'tag':
            options['tags'].append(valid_tags([value])[0])
        elif name in ('limit', 'offset'):
            if not re.fullmatch(r'[0-9]{1,6}', value):
                raise ValueError('capability: --%s must be a number' % name)
            options[name] = int(value)
        else:
            options[name] = value
        index += 2
    return options


READ_COMMANDS = ('get', 'list', 'find')
WRITE_COMMANDS = CONTRIBUTOR_OPERATIONS + ('propose-alias', 'verify')


def read(args, run, operators, verifiers=None, journal=None):
    """`capability get|list|find|--help`: read-only, one label-filtered read, no lock.

    `verifiers` is the deployment verifiers list and `journal` the project directory
    (it holds the host revert journal); both feed the verification trust rules. The
    integrated-commit test reads nothing unless an entry being shown has a trusted
    passing verification.

    `capability misses` is answered by the endpoint itself (it needs the project
    directory): see capability_misses.
    """
    if not args or args[0] == 'help' or any(token in ('--help', '-h') for token in args):
        return help_payload()
    command, rest = args[0], args[1:]
    if command == 'get':
        trust = Trust(run, operators, verifiers, journal)
        rest = [token for token in rest if token != '--json']
        if len(rest) != 1:
            raise ValueError('capability get takes exactly one KEY')
        valid_key(rest[0])
        return get(read_key_and_retired(run, rest[0]), rest[0], operators, trust)
    if command == 'list':
        options = _options(rest, {'--tag': 'tag', '--owner': 'owner', '--state': 'state', '--limit': 'limit',
                                  '--offset': 'offset', '--pointers': 'pointers'})
        if not 1 <= options['limit'] <= LIST_LIMIT_MAX:
            raise ValueError('capability list: --limit must be 1..%d' % LIST_LIMIT_MAX)
        if options.get('state', 'all') not in ('draft-only', 'accepted', 'superseded', 'all'):
            raise ValueError('capability list: --state must be draft-only, accepted, superseded or all')
        if options.get('owner') is not None:
            valid_owner(options['owner'], required=True)
        rows, lifecycle = read_catalog(run)
        return list_entries(rows, options, operators, Trust(run, operators, verifiers, journal, lifecycle))
    if command == 'find':
        if not rest or rest[0].startswith('--'):
            raise ValueError('capability find takes a PHRASE')
        phrase, options = rest[0], _options(rest[1:], {'--limit': 'limit'})
        rows, lifecycle = read_catalog(run)
        return find(rows, phrase, operators, limit=options['limit'] if '--limit' in rest else 5,
                    trust=Trust(run, operators, verifiers, journal, lifecycle))
    raise ValueError('capability: unknown command %s; use get, list, find, misses, propose, revise, '
                     'propose-alias or verify' % command)


def write(args, attachments, actor, run, project, operators, verifiers=None):
    """`capability propose|revise --file`, `capability propose-alias` and `capability
    verify --file`: contributor writes. A verification written here is always
    `unverified` (see capability_verification)."""
    if args[0] == 'verify':
        payload = write_payload(args, attachments, commands=('verify',))
        payload.pop('operation', None)
        return verification.verify(payload, actor, run, operators=operators, verifiers=verifiers, journal=project)
    if args[0] == 'propose-alias':
        rest = [token for token in args[1:] if token != '--json']
        evidence = None
        if '--evidence' in rest:
            position = rest.index('--evidence')
            if position + 1 >= len(rest):
                raise ValueError('--evidence needs a POINTER')
            evidence = rest[position + 1]
            del rest[position:position + 2]
        if len(rest) != 2:
            raise ValueError('use capability propose-alias KEY "PHRASE" [--evidence POINTER]')
        return propose_alias(rest[0], rest[1], actor, run, operators, evidence=evidence)
    payload = write_payload(args, attachments)
    return apply_native(payload, actor, run, project, operators=operators)
