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

Writes go through the shared keyed-record core (keyed_records.py) with this
module's REFERENCE spec:

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
`acceptance-inert` attention item to approvers.

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

import keyed_records as core
from briefing import clip
from coordination import identifier
from export_requirements import parse_json
from recovery import configured_operators
from requirements import SHA256_TEXT, canonical_bytes, content_hash
from reserved_comments import (REFERENCE_ACCEPTANCE_PREFIX as ACCEPTANCE_PREFIX,
                               REFERENCE_ENTRY_PREFIX as ENTRY_PREFIX, is_record_anchor,
                               record_comment_kind)

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
ENTRY_FIELDS = ('schema_version', 'key', 'revision', 'title', 'statement', 'authority', 'owner',
                'review_by', 'tags', 'decisions', 'acceptance_state', 'successor', 'origin', 'sha256')
ACCEPTANCE_RECORD_FIELDS = ('schema_version', 'source', 'id', 'key', 'revision', 'record_sha256',
                            'acceptance_state', 'decision', 'operator', 'at', 'sha256')
CONTENT_FIELDS = ('key', 'title', 'statement', 'authority', 'owner', 'review_by', 'tags', 'decisions')
PROPOSE_FIELDS = frozenset(('schema_version', 'operation_id', 'operation', 'revision',
                            'expected_sha256') + CONTENT_FIELDS)
ACCEPT_FIELDS = frozenset(('schema_version', 'operation_id', 'operation', 'key', 'revision',
                           'record_sha256', 'acceptance_state', 'acceptance'))
DIRECT_FIELDS = PROPOSE_FIELDS | {'acceptance_state', 'acceptance'}
CONTRIBUTOR_OPERATIONS = ('propose', 'revise')
OPERATOR_OPERATIONS = ('accept', 'draft')
WRITTEN_BY_THE_OPERATION = ('acceptance_state', 'successor', 'sha256', 'acceptance', 'labels')
LIST_LIMIT_MAX = 100
COVERAGE_IDS = 10
SHOW_CHUNK = 50
ANCHOR_TITLE = 'Reference %s'
ANCHOR_DESCRIPTION = ('Reference catalog entry %s. Read it with `ref get %s`; its record comments are '
                      'authoritative. This anchor is not a work item.')
DUE_ORDER = {'expired': 0, 'due-soon': 1, 'unset': 2, 'ok': 3}


def key_label(key):
    return KEY_LABEL + key.replace('.', '-')


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


def valid_owner(value):
    """A durable person/account identity; a session actor is refused (.41 3.4)."""
    if not isinstance(value, str) or not (ACCOUNT.fullmatch(value) or PERSON.fullmatch(value)):
        raise ValueError('owner must be a durable identity, account:<uid> or person:<name>')
    if SESSION_MARKER.search(value):
        raise ValueError('owner must be a durable identity, not a session actor; use account:<uid> or '
                         'person:<name>')
    return value


def valid_authority(value):
    """Exactly one closed `repo-path` or `url` object (.41 3.6), time-independent rules."""
    if not isinstance(value, dict):
        raise ValueError('authority must be an object of type repo-path or url')
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
    else:
        raise ValueError('authority.type must be repo-path or url')
    return value


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
    if type(record['schema_version']) is not int or record['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    valid_key(record['key'])
    core.positive_int(record['revision'], 'revision')
    _bounded_text(record['title'], 'title', TITLE_MAX)
    _bounded_text(record['statement'], 'statement', STATEMENT_MAX)
    valid_authority(record['authority'])
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


def parse_acceptance(body):
    """The `reference-acceptance-v1` evidence iff it passes its full schema."""
    if not isinstance(body, str) or not body.startswith(ACCEPTANCE_PREFIX):
        return None
    rest = body[len(ACCEPTANCE_PREFIX):]
    try:
        record = parse_json(rest)
        if not isinstance(record, dict) or set(record) != set(ACCEPTANCE_RECORD_FIELDS):
            return None
        if type(record['schema_version']) is not int or record['schema_version'] != 1:
            return None
        if record['source'] != 'reference-apply':
            return None
        if not isinstance(record['id'], str) or not ISSUE_ID.fullmatch(record['id']):
            return None
        valid_key(record['key'])
        core.positive_int(record['revision'], 'revision')
        if not isinstance(record['record_sha256'], str) or not SHA256_TEXT.match(record['record_sha256']):
            return None
        if record['acceptance_state'] not in ('accepted', 'superseded'):
            return None
        decision = record['decision']
        if not isinstance(decision, dict) or set(decision) != set(core.ACCEPTANCE_EVIDENCE_FIELDS):
            return None
        core.bound_acceptance(dict(decision, record_sha256=record['record_sha256']))
        identifier(record['operator'])
        if not isinstance(record['at'], str) or not record['at'].strip():
            return None
        if content_hash(record) != record['sha256'] or canonical_bytes(record).decode('utf-8') != rest:
            return None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    return record


def entry_comment(record):
    body = ENTRY_PREFIX + canonical_bytes(record).decode('utf-8')
    if parse_entry(body) != record:
        raise ValueError('Refusing to write a reference revision that does not pass its own schema')
    return body


def acceptance_evidence(bound, task, revision, record, actor, at=None):
    """The durable `reference-acceptance-v1` record bound to one revision hash."""
    decision = {name: bound[name] for name in core.ACCEPTANCE_EVIDENCE_FIELDS}
    evidence = {'schema_version': 1, 'source': 'reference-apply', 'id': task, 'key': record['key'],
                'revision': revision, 'record_sha256': bound['record_sha256'],
                'acceptance_state': record['acceptance_state'], 'decision': decision,
                'operator': actor, 'at': at or core.now()}
    evidence['sha256'] = content_hash(evidence)
    body = ACCEPTANCE_PREFIX + canonical_bytes(evidence).decode('utf-8')
    if parse_acceptance(body) != evidence:
        raise ValueError('Refusing to write acceptance evidence that does not pass its own schema')
    return evidence, body


# -- payloads ------------------------------------------------------------------------------

def validate_payload(payload, operator=False):
    if not isinstance(payload, dict):
        raise ValueError('reference payload must be an object')
    core.refuse_injected_labels(payload, 'reference operation')
    operation = payload.get('operation')
    allowed_operations = OPERATOR_OPERATIONS if operator else CONTRIBUTOR_OPERATIONS
    if operation not in allowed_operations:
        raise ValueError('operation must be one of ' + ', '.join(allowed_operations))
    if not operator:
        supplied = [name for name in WRITTEN_BY_THE_OPERATION if name in payload]
        if supplied:
            raise ValueError('%s %s written by the operation, never caller-supplied; acceptance is the '
                             'operator route (admin.py reference-apply).'
                             % (', '.join(supplied), 'is' if len(supplied) == 1 else 'are'))
    fields = {'propose': PROPOSE_FIELDS, 'revise': PROPOSE_FIELDS, 'accept': ACCEPT_FIELDS,
              'draft': DIRECT_FIELDS}[operation]
    core.checked_fields(payload, fields, 'reference payload')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    if 'operation_id' not in payload:
        raise ValueError('reference payload is missing field operation_id')
    identifier(payload['operation_id'])
    if 'key' not in payload:
        raise ValueError('reference payload is missing field key')
    valid_key(payload['key'])
    if operation == 'accept':
        for name in ('revision', 'record_sha256', 'acceptance_state', 'acceptance'):
            if name not in payload:
                raise ValueError('reference-apply needs ' + name)
        core.positive_int(payload['revision'], 'revision')
        if not isinstance(payload['record_sha256'], str) or not SHA256_TEXT.match(payload['record_sha256']):
            raise ValueError('record_sha256 must be the content hash of the revision being accepted')
    else:
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
        revision = payload.get('revision', 1 if operation != 'revise' else None)
        if operation == 'revise':
            if revision is None:
                raise ValueError('revise needs the next revision number')
            core.positive_int(revision, 'revision')
            if revision < 2:
                raise ValueError('revise writes revision 2 or later; use propose for revision 1')
            digest = payload.get('expected_sha256')
            if not isinstance(digest, str) or not SHA256_TEXT.match(digest):
                raise ValueError('revise needs expected_sha256, the content hash of the newest revision '
                                 'it replaces')
        else:
            if revision != 1:
                raise ValueError('%s creates revision 1; use revise for later revisions' % operation)
            if payload.get('expected_sha256') is not None:
                raise ValueError('%s creates a new entry, so expected_sha256 must be null' % operation)
    if operator:
        if payload.get('acceptance_state') != 'accepted':
            raise ValueError('reference-apply writes an accepted revision (acceptance_state must be accepted)')
        core.validate_acceptance_shape(payload.get('acceptance'))
        core.bound_acceptance(dict(payload['acceptance'], record_sha256='0' * 64))
    if operation != 'accept':
        # Every content rule, including the date rules, is checked here, BEFORE the
        # anchor is created: a refusal after the create would leave an anchor with no
        # record. (An acceptance's content is the reviewed draft's; it is checked
        # before its writes, on an anchor that already exists.)
        state = 'accepted' if operation == 'draft' else 'draft'
        _write_time_rules(entry_record(payload, payload.get('revision', 1), state))
    return payload


def _write_time_rules(record):
    """Rules that depend on today's date, checked only when a revision is written."""
    current = today()
    limit = _months_ahead(current, REVIEW_BY_MAX_MONTHS)
    if record['review_by'] is not None:
        review_by = _date(record['review_by'], 'review_by')
        if review_by > limit:
            raise ValueError('review_by must be at most %d months ahead (%s)' % (REVIEW_BY_MAX_MONTHS, limit))
        if record['acceptance_state'] == 'accepted' and review_by < current:
            raise ValueError('review_by %s is in the past; an accepted revision needs a future review date'
                             % record['review_by'])
    authority = record['authority']
    if authority['type'] == 'url' and _date(authority['retrieved'], 'authority.retrieved') > current:
        raise ValueError('authority.retrieved must not be later than today')


def _months_ahead(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def entry_record(payload, revision, acceptance_state, origin=None):
    record = {'schema_version': 1, 'key': payload['key'], 'revision': revision,
              'title': payload['title'], 'statement': payload['statement'],
              'authority': payload['authority'], 'owner': payload['owner'],
              'review_by': payload.get('review_by'), 'tags': list(payload.get('tags', [])),
              'decisions': list(payload.get('decisions', [])), 'acceptance_state': acceptance_state,
              'successor': None, 'origin': origin or {'type': 'authored'}}
    record['sha256'] = content_hash(record)
    validate_entry(record)
    return record


# -- native reads ----------------------------------------------------------------------------

def read_rows(run):
    """The reference-labelled rows with their comments, in two native reads.

    One label-filtered `bd list` (no comments) and one `bd show ... --include-comments`
    per chunk of SHOW_CHUNK rows - never a full export (kittrial-5bb.71: a full export
    of a large project costs seconds and over 100 MB).
    """
    listed = json.loads(run(['list', '--label', TYPE_LABEL, '--all', '--limit', '0', '--json']) or '[]')
    ids = [row['id'] for row in listed or [] if isinstance(row, dict) and isinstance(row.get('id'), str)]
    rows = []
    for start in range(0, len(ids), SHOW_CHUNK):
        chunk = ids[start:start + SHOW_CHUNK]
        try:
            shown = json.loads(run(['show', *chunk, '--json', '--include-comments']) or '[]')
        except ValueError as error:
            if 'no issues found' in str(error):
                continue   # every row of the chunk was deleted after the list
            raise
        shown = shown if isinstance(shown, list) else [shown]
        rows.extend(row for row in shown if isinstance(row, dict) and row.get('id') in chunk)
    return rows


def _key_labels(row):
    return [label for label in row.get('labels') or [] if isinstance(label, str) and label.startswith(KEY_LABEL)]


def _entry_belongs(record, row):
    return key_label(record['key']) in _key_labels(row)


def existing_revisions(row):
    return core.existing_ledger(row, ENTRY_PREFIX, parse_entry, 'reference', 'revision', 'revision',
                                belongs=_entry_belongs)


def existing_acceptances(row):
    return core.existing_ledger(row, ACCEPTANCE_PREFIX, parse_acceptance, 'reference', 'acceptance',
                                'revision', belongs=lambda record, row: record['id'] == row.get('id')
                                and key_label(record['key']) in _key_labels(row))


def anchor_for(rows, key):
    """(row, status) for a key: status is `entry`, `incomplete` or None."""
    label = key_label(key)
    for row in rows:
        if TYPE_LABEL not in (row.get('labels') or []) or label not in _key_labels(row):
            continue
        if not is_record_anchor(row):
            return row, 'incomplete'
        return row, 'entry'
    return None, None


# -- spec hooks ------------------------------------------------------------------------------

def _resolve_task(rows, payload, operator):
    if payload['operation'] in ('propose', 'draft'):
        return None
    row, status = anchor_for(rows, payload['key'])
    if row is None:
        raise ValueError('Unknown reference key %s; use ref list to see the catalog, or ref propose to '
                         'create it.' % payload['key'])
    if status == 'incomplete':
        raise ValueError('Reference key %s is held by anchor %s, which has no revision record yet (an '
                         'interrupted propose); re-run that propose with its operation_id, or ask the operator '
                         'to reconcile it.' % (payload['key'], row['id']))
    return row['id']


def _check_key_unique(rows, payload, task):
    if task is not None:
        return
    label = key_label(payload['key'])
    request = 'request:' + content_hash({'operation_id': payload['operation_id']})
    for row in rows:
        if TYPE_LABEL not in (row.get('labels') or []) or label not in _key_labels(row):
            continue
        if request in (row.get('labels') or []):
            continue   # this operation's own interrupted anchor; the retry finishes it
        if not is_record_anchor(row):
            raise ValueError('Reference key %s is held by anchor %s, which has no revision record yet (an '
                             'interrupted propose by another operation); that operation must be re-run with '
                             'its operation_id, or reconciled by the operator.' % (payload['key'], row['id']))
        revisions = existing_revisions(row)
        keys = {record['key'] for record in revisions.values()}
        if payload['key'] in keys or not keys:
            raise ValueError('Reference key %s already exists (%s); use ref revise.' % (payload['key'], row['id']))
        raise ValueError('Reference key %s collides with existing key %s (both use the lookup label %s); '
                         'choose a different key.' % (payload['key'], sorted(keys)[0], label))


def _create_args(payload, request_label, content_label):
    labels = sorted({TYPE_LABEL, STATE_LABEL['draft'], key_label(payload['key']), request_label,
                     content_label})
    return ['create', '--title', ANCHOR_TITLE % payload['key'],
            '--description', ANCHOR_DESCRIPTION % (payload['key'], payload['key']), '--type', 'task',
            '--no-inherit-labels', '--labels', ','.join(labels), '--json']


def _close(run, task):
    run(['close', task, '--reason', 'reference catalog anchor (not a work item)', '--json'])


def _prepare_row(run, row):
    """Close an anchor whose create committed but whose close did not (a retry)."""
    if row.get('status') != 'closed':
        _close(run, row['id'])


def _require_selectable(row, payload, operator, existing):
    if TYPE_LABEL not in (row.get('labels') or []) or key_label(payload['key']) not in _key_labels(row):
        raise ValueError('Record %s is not the reference anchor for key %s' % (row.get('id'), payload['key']))
    unsupported = [kind for kind in (record_comment_kind(comment.get('text'))
                                     for comment in row.get('comments') or [] if isinstance(comment, dict))
                   if kind and kind[0].startswith('reference-') and kind[2] == 'unsupported']
    if unsupported:
        raise ValueError('Reference %s carries a record kind this kit does not support (%s-v%s); an operator '
                         'must handle it with a kit that does.' % (payload['key'], unsupported[0][0],
                                                                   unsupported[0][1]))


def _build_record(payload, task, existing):
    operation = payload['operation']
    if operation == 'propose':
        record = entry_record(payload, 1, 'draft')
    elif operation == 'draft':
        record = entry_record(payload, 1, 'accepted')
    elif operation == 'revise':
        record = entry_record(payload, payload['revision'], 'draft')
    else:
        reviewed = existing.get(payload['revision'])
        if reviewed is None:
            raise ValueError('Reference %s has no revision %d to accept' % (payload['key'], payload['revision']))
        record = {name: value for name, value in reviewed.items() if name != 'sha256'}
        record.update(revision=payload['revision'] + 1, acceptance_state='accepted', successor=None)
        record['sha256'] = content_hash(record)
        validate_entry(record)
    _write_time_rules(record)
    return record['revision'], record


def _require_bound_key(task, payload, existing):
    keys = {record['key'] for record in existing.values()}
    if len(keys) > 1:
        raise ValueError('Reference anchor %s has conflicting keys across revisions; operator reconciliation '
                         'required' % task)
    if keys and keys != {payload['key']}:
        raise ValueError('Reference anchor %s is keyed %s; a key swap is refused' % (task, next(iter(keys))))


def _check_revision(payload, revision, existing, record, task):
    operation = payload['operation']
    newest = max(existing) if existing else None
    if revision in existing:
        if existing[revision] == record:
            return   # an identical retry of a completed write
        if operation == 'accept':
            raise ValueError('Revision %d of %s is not the newest (revision %d exists); review the newest '
                             'revision and accept that one' % (payload['revision'], payload['key'], newest))
        raise ValueError('revision %d of %s already exists with different content' % (revision, payload['key']))
    if operation in ('propose', 'draft'):
        if newest is not None:
            raise ValueError('Reference key %s already exists; use ref revise' % payload['key'])
        return
    if operation == 'revise':
        if revision != newest + 1:
            raise ValueError('revise must write revision %d of %s (requested %d)'
                             % (newest + 1, payload['key'], revision))
        if existing[newest]['sha256'] != payload['expected_sha256']:
            raise ValueError('expected_sha256 does not match revision %d of %s; re-read it with ref get and '
                             'revise from the current content' % (newest, payload['key']))
        return
    # accept: the reviewed revision must still be the newest, with the reviewed hash.
    if newest != payload['revision']:
        raise ValueError('Revision %d of %s is not the newest (revision %d exists); review the newest revision '
                         'and accept that one' % (payload['revision'], payload['key'], newest))
    if existing[payload['revision']]['sha256'] != payload['record_sha256']:
        raise ValueError('record_sha256 does not match revision %d of %s' % (payload['revision'], payload['key']))


def _check_acceptance(payload, existing, record, operator, row):
    if not operator:
        return None
    return core.bind_acceptance(payload['acceptance'], record)


def _apply_labels(run, task, current, payload, record):
    accepted = record['acceptance_state'] == 'accepted' or STATE_LABEL['accepted'] in (current or [])
    desired = {TYPE_LABEL, STATE_LABEL['accepted' if accepted else 'draft']}
    core.apply_controlled_labels(run, task, current, SPEC, desired)


def _result(payload, task, revision, record, created, reconciled, bound, comment_id=None):
    result = {'key': payload['key'], 'revision': revision, 'native_id': task,
              'record_comment_id': comment_id, 'state': record['acceptance_state'],
              'created': created, 'reconciled': reconciled}
    if bound is not None:
        result['acceptance'] = bound
    return result


def _refuse_before_journal(payload, operator):
    return None


def _create_revision(payload):
    if payload['operation'] == 'accept':
        return payload['revision'] + 1
    return payload.get('revision', 1)


SPEC = core.RecordSpec(
    kind='reference', noun='reference', type_labels={TYPE_LABEL}, state_labels=STATE_LABEL,
    revision_prefix=ENTRY_PREFIX, acceptance_prefix=ACCEPTANCE_PREFIX, journal=JOURNAL, key_regex=KEY,
    fields=PROPOSE_FIELDS, allow_accepted_first_revision=True, supports_retire=False,
    accept_action='accept a reference entry', apply_command='admin.py reference-apply',
    reconcile_command='admin.py reference-reconcile',
    validate=lambda payload, operator: validate_payload(payload, operator=operator),
    refuse_before_journal=_refuse_before_journal,
    explicit_task=lambda payload: None,
    read_rows=read_rows,
    resolve_task=_resolve_task,
    check_key_unique=_check_key_unique,
    create_revision=_create_revision,
    create_args=_create_args,
    after_create=_close,
    prepare_row=_prepare_row,
    existing_revisions=existing_revisions,
    require_selectable=_require_selectable,
    build_record=_build_record,
    require_bound_key=_require_bound_key,
    check_revision=_check_revision,
    check_acceptance=_check_acceptance,
    acceptance_evidence=lambda bound, task, revision, record, actor: acceptance_evidence(
        bound, task, revision, record, actor),
    existing_acceptances=existing_acceptances,
    revision_comment=entry_comment,
    apply_labels=_apply_labels,
    result=_result,
)


def check_decisions(payload, run):
    """Every cited decision resolves to an issue typed `decision` or labelled `decision`."""
    wanted = list(payload.get('decisions') or [])
    if not wanted or payload['operation'] == 'accept':
        return
    try:
        shown = json.loads(run(['show', *wanted, '--json']) or '[]')
    except ValueError:
        shown = []
    shown = shown if isinstance(shown, list) else [shown]
    found = {row.get('id'): row for row in shown if isinstance(row, dict)}
    bad = [item for item in wanted
           if item not in found or not (found[item].get('issue_type') == 'decision'
                                        or 'decision' in (found[item].get('labels') or []))]
    if bad:
        raise ValueError('Unknown decision link(s) %s: each must be an existing issue of type decision or '
                         'labelled decision' % ', '.join(bad))


def apply_native(payload, actor, run, project, operator=False, operators=None):
    """Caller holds the canonical project lock; run(argv) invokes pinned bd.

    The contributor route (`operator=False`) proposes and revises drafts. The operator
    route (`admin.py reference-apply`) accepts the newest draft it reviewed, or writes
    a direct accepted revision 1, with F3 evidence; `operators` is the deployment
    allowlist, checked before any journal or native read.
    """
    if operator:
        core.require_configured_operator(actor, operators, SPEC.accept_action)
    validate_payload(payload, operator=operator)
    check_decisions(payload, run)
    return core.apply_native(payload, actor, run, project, SPEC, operator=operator, operators=operators)


def _confirm_anchor(row):
    if not is_record_anchor(row):
        raise ValueError('Anchor %s has no reference revision record yet; re-run the original ref propose '
                         'with the same operation_id to finish it, then reconcile.' % row.get('id'))


def reconcile(project, operation_id, actor, reason, disposition, run, issue_id=None):
    """Operator-only: resolve a stuck `.reference-requests/` receipt from native state."""
    return core.reconcile(project, operation_id, actor, reason, disposition, run, SPEC,
                          issue_id=issue_id, confirm=_confirm_anchor)


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
    """One anchor as readers see it. Never raises: a bad entry reads malformed."""
    current = current or today()
    view = {'key': None, 'native_id': row.get('id'), 'state': None, 'record': None,
            'record_comment_id': None, 'acceptance': None, 'acceptance_inert': False,
            'inert_operator': None, 'proposed': None, 'proposed_comment_id': None,
            'due': 'unset', 'warnings': []}
    try:
        revisions, acceptances = {}, {}
        for comment in row.get('comments') or []:
            body = comment.get('text') if isinstance(comment, dict) else None
            kind = record_comment_kind(body)
            if not kind or not kind[0].startswith('reference-'):
                continue
            if kind[2] == 'unsupported':
                view.update(state='unsupported')
                view['warnings'].append({'code': 'unsupported-record',
                                         'detail': '%s-v%s is newer than this kit' % (kind[0], kind[1])})
                return view
            if kind[0] == 'reference-entry':
                record = parse_entry(body)
                if record is None or not _entry_belongs(record, row):
                    raise ValueError('malformed reference revision')
                prior = revisions.get(record['revision'])
                if prior is not None and prior[0] != record:
                    raise ValueError('conflicting content for one revision')
                revisions[record['revision']] = (record, comment.get('id'))
            else:
                record = parse_acceptance(body)
                if record is None or record['id'] != row.get('id'):
                    raise ValueError('malformed reference acceptance evidence')
                acceptances.setdefault(record['revision'], []).append((record, comment.get('author')))
        if not revisions:
            raise ValueError('no reference revision record')
        keys = {record['key'] for record, _ in revisions.values()}
        if len(keys) != 1:
            raise ValueError('conflicting keys across revisions')
        view['key'] = next(iter(keys))
        allowlist = configured_operators(operators if operators is not None else ())
        chosen = inert = None
        for number in sorted(revisions, reverse=True):
            record, comment_id = revisions[number]
            if record['acceptance_state'] == 'draft':
                continue
            evidence = [(item, author) for item, author in acceptances.get(number, [])
                        if item['record_sha256'] == record['sha256']
                        and item['acceptance_state'] == record['acceptance_state'] and item['key'] == record['key']]
            if not evidence:
                view['warnings'].append({'code': 'accepted-without-evidence',
                                         'detail': 'revision %d reads accepted but has no acceptance evidence'
                                                   % number})
                continue
            live = [(item, author) for item, author in evidence
                    if author == item['operator'] and author in allowlist]
            if live:
                chosen = (number, live[0][0])
                break
            if inert is None:
                inert = (number, evidence[0][0]['operator'])
        if chosen is not None:
            number, item = chosen
            record, comment_id = revisions[number]
            view.update(record=record, record_comment_id=comment_id, state=record['acceptance_state'],
                        due=due(record['review_by'], current),
                        acceptance=dict(item['decision'], record_sha256=item['record_sha256'],
                                        operator=item['operator'], at=item['at']))
        else:
            view['state'] = 'draft-only'
        if inert is not None and (chosen is None or inert[0] > chosen[0]):
            view.update(acceptance_inert=True, inert_operator=inert[1])
            view['warnings'].append({'code': 'acceptance-inert',
                                     'detail': 'revision %d was accepted by %s, who is no longer on the '
                                               'deployment operator allowlist' % inert})
        floor = chosen[0] if chosen is not None else 0
        drafts = [number for number, (record, _) in revisions.items()
                  if record['acceptance_state'] == 'draft' and number > floor]
        if drafts:
            record, comment_id = revisions[max(drafts)]
            view.update(proposed=record, proposed_comment_id=comment_id)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        view.update(state='malformed', record=None, acceptance=None, proposed=None)
        view['warnings'].append({'code': 'malformed', 'detail': str(error)[:200]})
    return view


def catalog(rows, operators, current=None):
    """(entries, incomplete ids) over reference-labelled rows; ordinary labelled tasks are skipped."""
    entries, incomplete = [], []
    for row in rows:
        if not isinstance(row, dict) or TYPE_LABEL not in (row.get('labels') or []):
            continue
        if not is_record_anchor(row):
            if _key_labels(row):
                incomplete.append(row.get('id'))
            continue
        entries.append(entry_view(row, operators, current))
    return entries, incomplete


def _coverage(entries, incomplete, base):
    notes = [base]
    for state in ('malformed', 'unsupported'):
        ids = [entry['native_id'] for entry in entries if entry['state'] == state]
        if ids:
            notes.append('%d entr%s skipped as %s (%s)' % (len(ids), 'y' if len(ids) == 1 else 'ies', state,
                                                           ', '.join(ids[:COVERAGE_IDS])))
    if incomplete:
        notes.append('%d incomplete anchor%s with no record yet (%s)' % (
            len(incomplete), '' if len(incomplete) == 1 else 's', ', '.join(incomplete[:COVERAGE_IDS])))
    return '; '.join(notes)


def _record_view(record):
    if record is None:
        return None
    return {'revision': record['revision'], 'title': clip(record['title'], TITLE_MAX),
            'statement': clip(record['statement'], STATEMENT_MAX), 'authority': record['authority'],
            'owner': record['owner'], 'review_by': record['review_by'], 'tags': record['tags'],
            'decisions': record['decisions'], 'successor': record['successor'],
            'acceptance_state': record['acceptance_state'], 'sha256': record['sha256']}


def get(rows, key, operators, current=None):
    """`ref get KEY`: found through the lookup label, then the record's own key must match.

    A malformed or unsupported entry is returned with that state (its key is known only
    from the label); an anchor with no record yet, or an unknown key, is a refusal.
    """
    valid_key(key)
    label = key_label(key)
    incomplete = []
    entry = None
    for row in rows:
        if not isinstance(row, dict) or TYPE_LABEL not in (row.get('labels') or []) \
                or label not in _key_labels(row):
            continue
        if not is_record_anchor(row):
            incomplete.append(row.get('id'))
            continue
        view = entry_view(row, operators, current)
        if view['key'] in (key, None):
            entry = view
            break
    if entry is None:
        if incomplete:
            raise ValueError('Reference key %s has no revision record yet (incomplete anchor %s); re-run its '
                             'propose or ask the operator to reconcile it' % (key, incomplete[0]))
        raise ValueError('Unknown reference key %s; use ref list to see the catalog' % key)
    return {'schema_version': 1, 'key': key, 'state': entry['state'], 'native_id': entry['native_id'],
            'record': _record_view(entry['record']), 'record_comment_id': entry['record_comment_id'],
            'acceptance': entry['acceptance'], 'acceptance_inert': entry['acceptance_inert'],
            'inert_operator': entry['inert_operator'],
            'replaces': [], 'proposed': _record_view(entry['proposed']),
            'proposed_comment_id': entry['proposed_comment_id'], 'due': entry['due'], 'resolved': None,
            'warnings': entry['warnings'][:10], 'trust': 'accepted' if entry['record'] else 'draft',
            'coverage': 'newest accepted revision and its acceptance evidence; unresolved drafts are returned '
                        'in proposed'}


def _list_item(entry):
    source = entry['record'] or entry['proposed'] or {}
    return {'key': entry['key'], 'title': (source.get('title') or '')[:TITLE_MAX], 'state': entry['state'],
            'owner': source.get('owner'), 'review_by': source.get('review_by') if entry['record'] else None,
            'proposed_review_by': (entry['proposed'] or {}).get('review_by'),
            'due': entry['due'], 'tags': source.get('tags') or [],
            'revision': source.get('revision'), 'native_id': entry['native_id'],
            'acceptance_inert': entry['acceptance_inert']}


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
    counts = {'expired': 0, 'due_soon': 0, 'unset': 0, 'acceptance_inert': 0, 'malformed': 0, 'total': 0}
    candidates = []
    for entry in entries:
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
    order = {'acceptance-inert': 0, 'expired': 1, 'due-soon': 2}
    candidates.sort(key=lambda item: (order.get(item['due'], 9), item['key']))
    items = candidates[offset:offset + limit] if approver else []
    block = dict(counts, items=items,
                 truncated=(not approver and bool(candidates)) or offset + limit < len(candidates),
                 next_offset=offset + limit if approver and offset + limit < len(candidates) else None)
    if not approver and candidates:
        block['coverage'] = ('%d item%s for approvers (actors on the deployment operator allowlist); owners use '
                             'ref list --owner' % (len(candidates), '' if len(candidates) == 1 else 's'))
    if incomplete:
        block['incomplete'] = len(incomplete)
    return block


def _attention_item(entry):
    record = entry['record'] or entry['proposed'] or {}
    return {'key': entry['key'], 'review_by': record.get('review_by'), 'due': entry['due'],
            'owner': record.get('owner'), 'state': entry['state'], 'revision': record.get('revision')}


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
              'title': clip(entry['record']['title'], TITLE_MAX),
              'text': _attention_text(entry), 'source': 'ref get ' + entry['key']}
             for entry in chosen[:limit]]
    more = len(chosen) - len(items)
    return {'attention': items, 'attention_total': len(chosen), 'attention_more': more or None}


def _attention_text(entry):
    """Server-derived text only: the key, the due class and the date - never the statement."""
    if entry['due'] == 'expired':
        return 'Reference %s is past its review date (%s).' % (entry['key'], entry['record']['review_by'])
    if entry['due'] == 'due-soon':
        return 'Reference %s is due for review by %s.' % (entry['key'], entry['record']['review_by'])
    return 'Reference %s is tagged for this task.' % entry['key']


# -- the `ref` client action ------------------------------------------------------------------------

def help_payload():
    return {'schema_version': 1, 'action': 'ref', 'contract': 'cli-contract-v1',
            'usage': ['ref get KEY', 'ref list [--tag TAG]... [--owner IDENTITY] '
                      '[--state draft-only|accepted|superseded|all] [--due expired|due-soon|unset] '
                      '[--limit N] [--offset N]', 'ref propose --file entry.json', 'ref revise --file entry.json'],
            'limits': {'list_limit': [1, LIST_LIMIT_MAX], 'title': TITLE_MAX, 'statement': STATEMENT_MAX,
                       'tags': TAGS_MAX, 'key': KEY_MAX, 'due_soon_days': DUE_SOON_DAYS},
            'operator': ['admin.py reference-apply PROJECT --actor OPERATOR --file acceptance.json',
                         'admin.py reference-reconcile PROJECT --operation-id ID --actor OPERATOR '
                         '--reason TEXT --disposition complete|failed|released [--issue-id ID]']}


def parse_list_options(args):
    options = {'tags': [], 'limit': 20, 'offset': 0}
    values = {'--tag': 'tag', '--owner': 'owner', '--state': 'state', '--due': 'due', '--limit': 'limit',
              '--offset': 'offset'}
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
    if options.get('owner') is not None:
        valid_owner(options['owner'])
    return options


def read(args, run, operators):
    """`ref get KEY` / `ref list ...` / `ref --help`: read-only, one label-filtered read."""
    if not args or args[0] in ('--help', '-h', 'help'):
        return help_payload()
    command, rest = args[0], args[1:]
    if command == 'get':
        rest = [token for token in rest if token != '--json']
        if len(rest) != 1:
            raise ValueError('ref get takes exactly one KEY')
        return get(read_rows(run), rest[0], operators)
    if command == 'list':
        return list_entries(read_rows(run), parse_list_options(rest), operators)
    raise ValueError('ref: unknown command %s; use get, list, propose or revise' % command)


def write_payload(args, attachments):
    """The payload of `ref propose|revise --file entry.json` (the client's attachment transport)."""
    command, rest = args[0], [token for token in args[1:] if token != '--json']
    if command not in CONTRIBUTOR_OPERATIONS:
        raise ValueError('ref: unknown command %s' % command)
    if len(rest) != 1 or not rest[0].startswith('@attachment:'):
        raise ValueError('ref %s takes --file entry.json' % command)
    item = (attachments or {}).get(rest[0].partition(':')[2])
    if not isinstance(item, dict) or item.get('flag') not in ('--file', '-f') or not isinstance(item.get('text'), str):
        raise ValueError('ref %s takes --file entry.json' % command)
    payload = parse_json(item['text'])
    if not isinstance(payload, dict):
        raise ValueError('reference payload must be an object')
    if 'operation' in payload:
        raise ValueError('the payload must not set operation; use the propose or revise command')
    return dict(payload, operation=command)
