"""Contributor-level requirement draft/revise records with controlled labels.

One locked native operation creates or selects a requirement (or BRD narrative)
record, applies only the controlled `requirement`/`brd-section` type label and
the `requirement:draft`/`requirement:accepted` state label, and posts exactly
one `Kind: requirement-revision-v1` comment (plus, only on an operator
acceptance or demotion, one `Kind: requirement-acceptance-v1` evidence record).
Labels are never caller-supplied: the operation derives them from the record
kind and acceptance state, so an arbitrary label write is refused rather than
silently rewritten.

Authority split (REQUIREMENTS_CONTRACT F3). The contributor route may only write
*draft* revisions and must not carry an `acceptance` object at all: a draft is
unaccepted by definition, so a contributor acceptance is refused before any
native read or write. Acceptance of a record and demotion of an accepted record
back to draft are owner/operator-only and require F3 acceptance evidence: an
`acceptance` object naming owners/approvers plus a decision id and evidence. The
operator route is `admin.py requirement-apply`. An operator acceptance writes a
durable `Kind: requirement-acceptance-v1` record on the native record, bound to
the exact revision content hash (`record_sha256`), so the decision is visible to
every reader and survives a restore; the publication-level `manifest_sha256`
stays owned by `requirements.py`/`publish_brd.py`, and `publication_acceptance`
rebinds a record decision to a manifest when a BRD is published.

Selection is closed for contributors. Their `draft` with `task=` only accepts
records already carrying the `requirement`/`brd-section` type label (or records
this command created). The operator may accept revision 1 on an untyped task
with no prior revisions and F3 evidence. `revise` is bound to the record's
existing kind and key: cross-kind changes and key swaps are refused before any
native write. Revise follows the trusted-team
model: any contributor actor may revise any draft (see
docs/REQUIREMENTS_INTEGRATION.md); the actor string is an attribution, not an
authenticated owner identity.

Idempotency is native-first. A created record carries `request:<hash>` and
`request-content:<hash>` labels (the create-child convention), and the revision
comment itself is the per-revision anchor for selected records. The local
receipts under `.requirement-requests/` and `.requirement-backfills/` are the
operator's recovery cache and are captured by the coordination backup/restore
sidecar, while the native record (revision comment plus acceptance record)
remains authoritative. A native `bd create --dry-run` preflight and the revision
check run BEFORE any receipt is written, so a refusal reserves nothing; a receipt
stays pending only for an uncertain real-write failure and can be completed from
native state with `admin.py requirement-reconcile`.

The operator-only `backfill` path applies the same controlled labels to existing
records (for example records created through create-child that lack them) and
never writes a revision comment. Backfilling a record to `requirement:accepted`
requires an evidence field and also writes the durable
`requirement-acceptance-v1` evidence record (bound to the latest revision when
one exists). A record whose latest revision comment says `draft` is refused:
relabelling it accepted behind that ledger would contradict the authoritative
revision, so acceptance goes through `admin.py requirement-apply` instead.

An operator acceptance writes the acceptance evidence comment BEFORE the
accepted revision comment and the `requirement:accepted` label, so an uncertain
evidence write can never leave a record that reads as accepted with no
acceptance evidence.

Payload schemas are closed. `schema_version` is the integer 1.
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('requirement_records.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import json
import re
import time
from pathlib import Path

import keyed_records as core
import record_json
from coordination import atomic, identifier
from export_requirements import (ACCEPTANCE_PREFIX, REVISION_PREFIX, parse_json,
                                 revision_comment)
from keyed_records import (ACCEPTANCE_EVIDENCE_FIELDS, RECEIPT_STATUSES,
                           RECONCILE_RELEASABLE, bind_acceptance, bound_acceptance,
                           find, latest_revision, read_rows, validate_receipt)
from requirements import canonical_bytes, content_hash, load_json

OPERATIONS = ('draft', 'revise')
KIND_TYPE_LABEL = {'requirement': 'requirement', 'brd-section': 'brd-section'}
STATE_LABEL = {'draft': 'requirement:draft', 'accepted': 'requirement:accepted'}
TYPE_LABELS = frozenset(KIND_TYPE_LABEL.values())
CONTROLLED_LABELS = frozenset(KIND_TYPE_LABEL.values()) | frozenset(STATE_LABEL.values())
FIELDS = {'schema_version', 'operation_id', 'operation', 'kind', 'task', 'parent',
          'title', 'key', 'description', 'revision', 'acceptance_state',
          'acceptance'}
BACKFILL_FIELDS = {'schema_version', 'operation_id', 'records'}
BACKFILL_RECORD_FIELDS = {'task', 'kind', 'acceptance_state', 'evidence'}
JOURNAL = '.requirement-requests'
BACKFILL_JOURNAL = '.requirement-backfills'


def _refuse_injected_labels(payload, where='requirement payload'):
    """Fail closed on caller-supplied labels before any other validation."""
    core.refuse_injected_labels(payload, where)
    if isinstance(payload, dict) and 'decided_by' in payload:
        raise ValueError('decided_by is proposed in kittrial-pth.25 (change-proposal, not accepted) and '
                         'is refused rather than written as an unvalidated revision field.')


_text = core.text
_positive_int = core.positive_int
_checked_fields = core.checked_fields
_validate_acceptance_shape = core.validate_acceptance_shape


def _require_configured_operator(actor, operators, action):
    """The deployment operator allowlist check (keyed_records.require_configured_operator)."""
    core.require_configured_operator(actor, operators, action)


def publication_acceptance(acceptance, manifest):
    """Rebind a record-level acceptance to a validated manifest for publish_brd.

    `requirements.validate_manifest` (and therefore `publish_brd`) consumes an
    acceptance object whose `manifest_sha256` equals the manifest hash. The
    record-level object stores `record_sha256` instead, so this bridge verifies
    that the manifest really contains the accepted record at the accepted
    revision/hash and returns the object the publisher's own validator accepts.
    """
    bound_acceptance(acceptance)
    from requirements import _validate_record
    if not isinstance(manifest, dict):
        raise ValueError('manifest must be an object')
    digest = acceptance['record_sha256']
    matches = []
    for group in ('narrative', 'requirements'):
        for record in manifest.get(group) or []:
            if not isinstance(record, dict):
                continue
            if record.get('sha256') == digest:
                _validate_record(record, 'manifest.%s' % group, has_key='key' in record)
                matches.append(record)
    if len(matches) != 1:
        raise ValueError('the manifest does not contain exactly one record matching the accepted '
                         'revision hash %s' % digest)
    full = {name: acceptance[name] for name in ACCEPTANCE_EVIDENCE_FIELDS}
    full['manifest_sha256'] = manifest.get('sha256')
    if not isinstance(full['manifest_sha256'], str):
        raise ValueError('manifest has no sha256 to bind the acceptance to')
    from requirements import _validate_acceptance
    try:
        _validate_acceptance(full, manifest)
    except ValueError as exc:
        raise ValueError('invalid publication acceptance: %s' % exc) from None
    return full


def validate_payload(payload, operator=False):
    if not isinstance(payload, dict):
        raise ValueError('requirement payload must be an object')
    _refuse_injected_labels(payload)
    _checked_fields(payload, FIELDS, 'requirement payload')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    for name in ('operation_id', 'operation', 'kind', 'title', 'description', 'acceptance_state'):
        if name not in payload:
            raise ValueError('requirement payload is missing field ' + name)
    identifier(payload['operation_id'])
    if payload['operation'] not in OPERATIONS:
        raise ValueError('operation must be one of ' + ', '.join(OPERATIONS))
    if payload['kind'] not in KIND_TYPE_LABEL:
        raise ValueError('kind must be one of ' + ', '.join(sorted(KIND_TYPE_LABEL)))
    if payload['acceptance_state'] not in STATE_LABEL:
        raise ValueError('acceptance_state must be one of ' + ', '.join(sorted(STATE_LABEL)))
    _text(payload['title'], 'title')
    _text(payload['description'], 'description')
    if payload['kind'] == 'requirement':
        if 'key' not in payload:
            raise ValueError('a requirement record needs a nonempty key')
        _text(payload['key'], 'key')
    elif 'key' in payload:
        raise ValueError('a brd-section record must not carry a requirement key')
    if 'task' in payload:
        identifier(payload['task'])
    if 'parent' in payload:
        identifier(payload['parent'])
    if 'revision' in payload:
        _positive_int(payload['revision'], 'revision')
    if 'acceptance' in payload:
        _validate_acceptance_shape(payload['acceptance'])
        if operator:
            # Check F3 semantics before a new accepted revision allocates a
            # native task; bind the real revision hash after the task ID is
            # known. A dummy well-formed hash suffices for this preflight.
            bound_acceptance(dict(payload['acceptance'], record_sha256='0' * 64))
    if payload['operation'] == 'draft':
        if payload['acceptance_state'] != 'draft':
            if not operator:
                raise ValueError('draft creates a draft revision (acceptance_state must be draft)')
            if 'acceptance' not in payload:
                raise ValueError('An accepted first revision requires F3 acceptance evidence')
        if 'revision' in payload and payload['revision'] != 1:
            raise ValueError('draft creates revision 1; use revise for later revisions')
        if 'task' not in payload and 'parent' not in payload:
            raise ValueError('draft without an existing task needs a parent job')
        if 'task' in payload and 'parent' in payload:
            raise ValueError('draft selects an existing task or creates one, not both')
    else:
        if 'task' not in payload:
            raise ValueError('revise needs the existing requirement task id')
        if 'parent' in payload:
            raise ValueError('revise must not carry a parent job')
        if 'revision' not in payload:
            raise ValueError('revise needs the next revision number')
    return payload


def validate_backfill(payload):
    if not isinstance(payload, dict):
        raise ValueError('backfill payload must be an object')
    _refuse_injected_labels(payload, 'requirement backfill')
    _checked_fields(payload, BACKFILL_FIELDS, 'backfill payload')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    if 'operation_id' not in payload:
        raise ValueError('backfill payload is missing field operation_id')
    identifier(payload['operation_id'])
    records = payload.get('records')
    if not isinstance(records, list) or not records:
        raise ValueError('backfill records must be a nonempty list')
    seen = set()
    for index, entry in enumerate(records):
        where = 'backfill records[%d]' % index
        if not isinstance(entry, dict):
            raise ValueError(where + ' must be an object')
        _refuse_injected_labels(entry, 'requirement backfill')
        _checked_fields(entry, BACKFILL_RECORD_FIELDS, where)
        for name in ('task', 'kind', 'acceptance_state'):
            if name not in entry:
                raise ValueError(where + ' is missing field ' + name)
        identifier(entry['task'])
        if entry['kind'] not in KIND_TYPE_LABEL:
            raise ValueError(where + '.kind must be one of ' + ', '.join(sorted(KIND_TYPE_LABEL)))
        if entry['acceptance_state'] not in STATE_LABEL:
            raise ValueError(where + '.acceptance_state must be one of ' + ', '.join(sorted(STATE_LABEL)))
        if entry['acceptance_state'] == 'accepted':
            if 'evidence' not in entry:
                raise ValueError(where + ' accepts a record, so it needs an evidence field '
                                 '(the decision/evidence pointer for the acceptance)')
            _text(entry['evidence'], where + '.evidence')
        elif 'evidence' in entry:
            raise ValueError(where + '.evidence applies only to an accepted record')
        if entry['task'] in seen:
            raise ValueError('backfill repeats record ' + entry['task'])
        seen.add(entry['task'])
    return payload


def controlled(kind, acceptance_state):
    return {KIND_TYPE_LABEL[kind], STATE_LABEL[acceptance_state]}


def _journal(project, name):
    return core.journal_dir(project, name)


def _receipt_path(journal, identity):
    return core.receipt_path(journal, identity, 'requirement')


def existing_revisions(row):
    """Validated revision-comment ledger for one record; malformed input fails closed.

    Reuses reserved_comments.parse_requirement_record, the guard's own full
    schema + canonical-bytes + content-hash validation, so an existing revision
    is accepted only when it is exactly what a legitimate writer could post.
    """
    from reserved_comments import parse_requirement_record
    return core.existing_ledger(row, REVISION_PREFIX, parse_requirement_record,
                                'requirement', 'revision', 'revision')


def _host_acceptances(row, operators=None):
    """Validated durable acceptance ledger for one record; malformed fails closed.

    Keyed by the bound revision (None for a backfill record with no revision
    comment), so a retry recognises its own evidence and a second, different
    decision for the same revision is refused rather than silently rewriting
    the operator's acceptance. When `operators` is given, only evidence whose
    stored native author is on that allowlist and equals the record's `operator`
    takes part; a contributor-planted acceptance must not stop the operator
    writing their own (kittrial-5bb.92 item 1).
    """
    from reserved_comments import parse_acceptance_record
    keep = None
    if operators is not None:
        allowed = set(core.configured_operators(operators))

        def keep(comment, record):
            # The one definition of live evidence (keyed_entries.evidence_is_live).
            from keyed_entries import evidence_is_live
            return evidence_is_live(record, comment.get('author'), allowed)
    return core.existing_ledger(row, ACCEPTANCE_PREFIX, parse_acceptance_record,
                                'requirement', 'acceptance', 'revision', keep=keep)


def existing_acceptances(row, operators=None):
    """One evidence resolver for host and authenticated owner decisions.

    Catalog acceptance authority is unchanged. Only requirement evidence learns
    the owner kind; each owner decision binds its native human author at write
    time, regardless of later membership or governance changes.
    """
    from requirement_owner_records import existing_acceptances as owner_acceptances
    result = dict(_host_acceptances(row, operators))
    for revision, evidence in owner_acceptances(row).items():
        if revision in result and (result[revision]['record_sha256'] != evidence['record_sha256']
                                   or result[revision].get('decision') != evidence['decision']):
            raise ValueError('Conflicting host and owner requirement acceptance evidence')
        result[revision] = evidence
    return result


def resolved_acceptance(row, record, operators=None):
    """Exact revision's acceptance, retaining legacy captured-snapshot reads.

    An owner-family claim always needs valid, author-bound evidence. Neither an
    accepted label nor an accepted revision can substitute for that evidence.
    Older host-only snapshots retain their declared v1 acceptance semantics.
    """
    from requirement_owner_records import ACCEPTANCE_PREFIX as owner_prefix, STATE_PREFIX, HUMAN, parse
    from reserved_comments import _reserved_prefix_view
    owner_claim = False
    for comment in row.get('comments') or []:
        body = comment.get('text', '') if isinstance(comment, dict) else ''
        if (isinstance(body, str) and body.startswith(REVISION_PREFIX)
                and HUMAN.fullmatch(str(comment.get('author', '')))):
            from reserved_comments import parse_requirement_record
            claimed = parse_requirement_record(body)
            if claimed is not None and claimed['revision'] == record['revision']:
                owner_claim = True
        view = _reserved_prefix_view(body) if isinstance(body, str) else ''
        if view.startswith('Kind: requirement-owner-'):
            owner_claim = True
            if view != body:
                raise ValueError('Malformed owner requirement evidence')
            if not body.startswith((owner_prefix, STATE_PREFIX)):
                raise ValueError('Unsupported owner requirement evidence')
            if body.startswith(STATE_PREFIX):
                # Reserved future state evidence cannot make a record accepted.
                # An unimplemented state operation is never silently ignored.
                if parse(body, state=True) is None:
                    raise ValueError('Malformed owner requirement state evidence')
                raise ValueError('Owner requirement state is not supported by this kit')
    evidence = existing_acceptances(row, operators).get(record['revision'])
    if record.get('id') != row.get('id') or record.get('acceptance_state') != 'accepted':
        return False
    if evidence is not None:
        return (evidence['record_sha256'] == record.get('sha256')
                and evidence.get('acceptance_state', 'accepted') == 'accepted')
    return not owner_claim


def acceptance_evidence(bound, task, revision, acceptance_state, actor, at=None):
    """The durable native acceptance record bound to one revision hash.

    Returns (record, body). `at` defaults to now; pass it in when a single
    operation writes more than one record so they share a timestamp.
    """
    if at is None:
        at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    decision = {name: bound[name] for name in ACCEPTANCE_EVIDENCE_FIELDS}
    record = {'schema_version': 1, 'source': 'requirement-apply', 'id': task,
              'revision': revision, 'record_sha256': bound['record_sha256'],
              'acceptance_state': acceptance_state, 'decision': decision,
              'evidence': None, 'operator': actor, 'at': at}
    record['sha256'] = content_hash(record)
    return record, _acceptance_body(record)


def backfill_evidence(task, evidence, binding, actor, at=None):
    """The durable native evidence record for one operator backfill acceptance."""
    if at is None:
        at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    record = {'schema_version': 1, 'source': 'requirement-backfill', 'id': task,
              'revision': binding['revision'] if binding else None,
              'record_sha256': binding['sha256'] if binding else None,
              'acceptance_state': 'accepted', 'decision': None,
              'evidence': evidence, 'operator': actor, 'at': at}
    record['sha256'] = content_hash(record)
    return record, _acceptance_body(record)


def _acceptance_body(record):
    """Self-check a durable acceptance record before it is written natively."""
    from reserved_comments import parse_acceptance_record
    body = ACCEPTANCE_PREFIX + canonical_bytes(record).decode('utf-8')
    if parse_acceptance_record(body) != record:
        raise ValueError('Refusing to write acceptance evidence that does not pass its own schema')
    return body


def existing_kind(row):
    """The record's controlled type label, or None when it is untyped."""
    present = TYPE_LABELS & set(row.get('labels') or [])
    if not present:
        return None
    if len(present) > 1:
        raise ValueError('Record %s carries conflicting type labels (%s); operator reconciliation required'
                         % (row.get('id'), ', '.join(sorted(present))))
    return next(iter(present))


def require_typed(row, payload, allow_untyped=False):
    """Selection is typed, except an operator's accepted first revision on a task."""
    kind = existing_kind(row)
    if kind is None:
        if allow_untyped and row.get('issue_type') == 'task':
            return
        raise ValueError('Record %s is not a requirement/brd-section record; refusing to relabel it. '
                         'Only records already typed requirement or brd-section (or created by this command) '
                         'may be selected; type an existing record with admin.py requirement-backfill.'
                         % (row.get('id'),))
    if kind != payload['kind']:
        raise ValueError('Record %s is a %s record and cannot be revised as %s; cross-kind changes are '
                         'refused.' % (row.get('id'), kind, payload['kind']))


def existing_key(existing):
    keys = {record['key'] for record in existing.values() if isinstance(record.get('key'), str)}
    if len(keys) > 1:
        raise ValueError('Requirement record has conflicting keys across revisions; operator reconciliation required')
    return next(iter(keys)) if keys else None


def require_bound_key(task, payload, existing):
    """A keyed record stays keyed to the same key; a key swap is refused."""
    if payload['kind'] != 'requirement':
        return
    prior = existing_key(existing)
    if prior is not None and prior != payload['key']:
        raise ValueError('Requirement record %s is keyed %s; revising it as key %s is refused '
                         '(the key is bound to the record).' % (task, prior, payload['key']))


def uniqueness_rows(run):
    rows = record_json.classify(read_rows(run), run, ['requirement'])
    for row in rows:
        if not row.get('malformed') or not record_json.selected(row, ['requirement']):
            continue
        # Native issue metadata is not part of this independent comment read.
        # Only the validated immutable ledger can establish the bound key.
        comments = record_json.loads(run(['comments', row['id'], '--json']) or '[]')
        if not isinstance(comments, list) or any(not isinstance(c, dict) or c.get('issue_id') != row['id']
                                                 for c in comments):
            raise ValueError('Cannot read requirement ledger for %s; operator reconciliation required' % row['id'])
        row['comments'] = comments
    return rows


def check_key_unique(rows, payload, task):
    """Requirement keys are unique across the project's requirement records."""
    if payload['kind'] != 'requirement':
        return
    for row in rows:
        if row.get('id') == task:
            continue
        if 'requirement' not in (row.get('labels') or []):
            continue
        if row.get('malformed'):
            key = existing_key(existing_revisions(row))
            if key is not None and key != payload['key']:
                continue
            raise ValueError('Cannot verify requirement key uniqueness: anchor %s could not be parsed'
                             % (row.get('id') or ''))
        record = latest_revision(existing_revisions(row))
        if record is not None and record.get('key') == payload['key']:
            raise ValueError('Requirement key %s is already used by record %s; requirement keys must be unique.'
                             % (payload['key'], row.get('id')))


def requirement_record(payload, task, revision):
    record = {'id': task, 'title': payload['title'], 'description': payload['description'],
              'revision': revision, 'acceptance_state': payload['acceptance_state']}
    if payload['kind'] == 'requirement':
        record['key'] = payload['key']
    record['sha256'] = content_hash(record)
    return record


def apply_controlled_labels(run, task, current, kind, acceptance_state):
    return core.apply_controlled_labels(run, task, current, SPEC, controlled(kind, acceptance_state))


def _check_revision(payload, revision, existing, record, task):
    """Refuse non-monotonic or conflicting revisions before any native write."""
    previous = sorted(existing)
    if payload['operation'] == 'draft':
        if not previous:
            return
        if revision not in existing:
            raise ValueError('draft cannot add revision %d to existing requirement %s; use revise' % (revision, task))
        if existing[revision] != record:
            raise ValueError('revision %d already exists with different content on %s; use revise' % (revision, task))
        return
    if not previous:
        raise ValueError('revise needs an existing requirement revision on %s; use draft first' % (task,))
    latest = previous[-1]
    if revision in existing:
        if existing[revision] != record:
            raise ValueError('revision %d already exists with different content on %s' % (revision, task))
        return
    if revision < latest:
        raise ValueError('revise must not rewrite earlier revision %d on %s; the next revision is %d'
                         % (revision, task, latest + 1))
    if revision != latest + 1:
        raise ValueError('revise must write revision %d on %s (requested %d)' % (latest + 1, task, revision))


def _current_acceptance(row, existing):
    """Whether the record is currently accepted, from state label and revisions."""
    from requirement_owner_records import HUMAN
    if any(isinstance(c, dict) and isinstance(c.get('text'), str)
           and (c['text'].startswith('Kind: requirement-owner-')
                or (c['text'].startswith(REVISION_PREFIX) and HUMAN.fullmatch(str(c.get('author', '')))))
           for c in row.get('comments') or []):
        newest = latest_revision(existing)
        return 'accepted' if newest is not None and resolved_acceptance(row, newest) else 'draft'
    if 'requirement:accepted' in (row.get('labels') or []):
        return 'accepted'
    record = latest_revision(existing)
    if record is not None and record.get('acceptance_state') == 'accepted':
        return 'accepted'
    return 'draft'


def _check_acceptance(payload, existing, record, operator, row):
    """Enforce the contributor-drafts-only / operator-F3-evidence split.

    Returns the acceptance object bound to the new revision hash, or None.
    A contributor never carries an acceptance object at all: the contract says
    a draft must not have one, so it is refused rather than validated, bound and
    echoed (kittrial-pth.26 review item 4).
    """
    target = payload['acceptance_state']
    current = _current_acceptance(row, existing)
    acceptance = payload.get('acceptance')
    if not operator:
        if acceptance is not None:
            raise ValueError('A contributor draft must not carry an acceptance object: drafts are '
                             'unaccepted by definition, and only the owner/operator route '
                             '(admin.py requirement-apply) may record F3 acceptance evidence. Remove '
                             'the acceptance field and submit a draft revision.')
        if target == 'accepted':
            raise ValueError('Contributors may only draft requirement records; accepting a record is '
                             'owner/operator-only and requires F3 acceptance evidence (named owners plus '
                             'decision/evidence ids). Use admin.py requirement-apply with an acceptance object.')
        if current == 'accepted':
            raise ValueError('A contributor may not demote an accepted requirement record back to draft; '
                             'demotion is owner/operator-only and requires F3 acceptance evidence. '
                             'Use admin.py requirement-apply with an acceptance object.')
    if acceptance is not None:
        return bind_acceptance(acceptance, record)
    if target == 'accepted' or current == 'accepted':
        raise ValueError('Accepting a requirement record or demoting an accepted record requires F3 acceptance '
                         'evidence: an `acceptance` object with owners, approvers, policy, decision_id and '
                         'evidence (record_sha256 is bound to the revision by the command).')
    return None


_prior_state = core.prior_state


def _uncertain(message, action):
    return core.uncertain(message, action, SPEC)


def _refuse_before_journal(payload, operator):
    if not operator and payload['acceptance_state'] == 'accepted':
        raise ValueError('Contributors may only draft requirement records; accepting a record is '
                         'owner/operator-only and requires F3 acceptance evidence. Use a draft revision, or ask '
                         'the operator to accept it with admin.py requirement-apply.')
    if not operator and payload.get('acceptance') is not None:
        # Fail closed BEFORE the journal directory or any native read/write, so
        # a refused contributor acceptance reserves and writes nothing at all.
        raise ValueError('A contributor draft must not carry an acceptance object: drafts are unaccepted '
                         'by definition, and F3 acceptance evidence is owner/operator-only '
                         '(admin.py requirement-apply).')


def _create_args(payload, request_label, content_label):
    # A new accepted record starts as draft until its F3 evidence and accepted
    # revision have both been written.
    labels = sorted(controlled(payload['kind'], 'draft') | {request_label, content_label})
    return ['create', '--title', payload['title'], '--parent', payload['parent'],
            '--description', payload['description'], '--type', 'task',
            '--no-inherit-labels', '--labels', ','.join(labels), '--json']


def _require_selectable(row, payload, operator, existing):
    allow_untyped = (operator and payload['operation'] == 'draft'
                     and payload['acceptance_state'] == 'accepted' and not existing)
    require_typed(row, payload, allow_untyped=allow_untyped)


def _result(payload, task, revision, record, created, reconciled, bound, comment_id=None):
    result = {'id': task, 'kind': payload['kind'], 'revision': revision,
              'acceptance_state': payload['acceptance_state'],
              'labels': sorted(controlled(payload['kind'], payload['acceptance_state'])),
              'created': created, 'reconciled': reconciled}
    if bound is not None:
        result['acceptance'] = bound
    return result


SPEC = core.RecordSpec(
    kind='requirement', noun='requirement', type_labels=TYPE_LABELS, state_labels=STATE_LABEL,
    revision_prefix=REVISION_PREFIX, acceptance_prefix=ACCEPTANCE_PREFIX, journal=JOURNAL,
    key_regex=None, fields=FIELDS, allow_accepted_first_revision=True, supports_retire=False,
    accept_action='accept a requirement record', apply_command='admin.py requirement-apply',
    reconcile_command='admin.py requirement-reconcile',
    validate=lambda payload, operator: validate_payload(payload, operator=operator),
    refuse_before_journal=_refuse_before_journal,
    explicit_task=lambda payload: payload.get('task'),
    read_rows=lambda run, payload=None: read_rows(run) if payload is None else uniqueness_rows(run),
    read_created=lambda run, task, payload: read_rows(run),
    resolve_task=lambda rows, payload, operator: None,
    check_key_unique=lambda rows, payload, task: check_key_unique(rows, payload, task),
    create_revision=lambda payload: payload.get('revision', 1),
    create_args=_create_args,
    after_create=lambda run, task: None,
    prepare_row=lambda run, row: None,
    existing_revisions=lambda row: existing_revisions(row),
    require_selectable=_require_selectable,
    build_record=lambda payload, task, existing: (
        payload.get('revision', 1), requirement_record(payload, task, payload.get('revision', 1))),
    require_bound_key=lambda task, payload, existing: require_bound_key(task, payload, existing),
    check_revision=lambda payload, revision, existing, record, task: _check_revision(
        payload, revision, existing, record, task),
    check_acceptance=lambda payload, existing, record, operator, row: _check_acceptance(
        payload, existing, record, operator, row),
    acceptance_evidence=lambda bound, task, revision, record, actor: acceptance_evidence(
        bound, task, revision, record['acceptance_state'], actor),
    existing_acceptances=lambda row: existing_acceptances(row),
    live_acceptances=lambda row, operators: existing_acceptances(row, operators),
    revision_comment=revision_comment,
    apply_labels=lambda run, task, current, payload, record: apply_controlled_labels(
        run, task, current, payload['kind'], payload['acceptance_state']),
    result=_result,
)


def apply_native(payload, actor, run, project, operator=False, operators=None):
    """Caller holds the canonical project lock; run(argv) invokes pinned bd.

    `operator=True` is the owner/operator route (admin.py requirement-apply);
    only it may accept a record or demote an accepted record, and only with F3
    acceptance evidence. The contributor route may only write draft revisions
    and must not carry an acceptance object. An operator acceptance writes a
    durable `Kind: requirement-acceptance-v1` record bound to the exact
    revision hash beside the revision comment, so the evidence survives a
    restore and is visible to every other reader. The evidence comment is
    written before the accepted revision comment and the state label, so a
    failed evidence write leaves the record reading as draft rather than
    accepted without evidence.

    `operators` is the deployment operator allowlist (kittrial-5bb.65). The
    admin CLI supplies `operators(root, strict=True)`; an actor outside it is
    refused before any journal or native write, exactly as `void-record` does.
    The steps are the shared keyed-record core's (keyed_records.apply_native)
    with this module's REQUIREMENT spec.
    """
    return core.apply_native(payload, actor, run, project, SPEC, operator=operator,
                             operators=operators)


def backfill(payload, actor, run, project, operators=None):
    """Operator-only: apply controlled labels to existing records, no revision comment.

    Backfilling a record to `requirement:accepted` also writes the durable
    `requirement-acceptance-v1` evidence record (bound to the latest revision
    when the record has one), so the operator's evidence is visible on the
    record itself and survives a restore, not only in `.requirement-backfills`.

    `operators` is the deployment operator allowlist (kittrial-5bb.65). The
    admin CLI supplies `operators(root, strict=True)`; an actor outside it is
    refused before any journal or native write.
    """
    _require_configured_operator(actor, operators, 'backfill requirement records')
    validate_backfill(payload)
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    journal = _journal(project, BACKFILL_JOURNAL)
    receipt = _receipt_path(journal, identity)
    prior = load_json(receipt) if receipt.exists() else None
    if prior is not None and prior.get('sha256') != digest:
        raise ValueError('Operation ID already used for different content or actor')
    rows = read_rows(run)
    at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    plan, changed, evidence_writes = [], False, []
    for entry in payload['records']:
        row = find(rows, entry['task'])
        if row is None:
            raise ValueError('Unknown requirement record: ' + entry['task'])
        present = set(row.get('labels') or [])
        desired = controlled(entry['kind'], entry['acceptance_state'])
        added = sorted(desired - present)
        removed = sorted((CONTROLLED_LABELS & present) - desired)
        if added or removed:
            changed = True
        plan.append({'task': entry['task'], 'added': added, 'removed': removed})
        # Plan the durable evidence before any native write: a conflicting or
        # malformed existing record fails closed with zero writes.
        if entry['acceptance_state'] == 'accepted':
            binding = latest_revision(existing_revisions(row))
            # A record whose latest revision comment says draft must not be
            # relabelled accepted behind that ledger (kittrial-pth.26 rev3 review
            # backfill-latest-draft): the state label would then contradict the
            # authoritative revision. Accept it through requirement-apply with F3
            # evidence (which writes a new accepted revision), or backfill draft.
            if binding is not None and binding.get('acceptance_state') != 'accepted':
                raise ValueError(
                    'Refusing to backfill %s to accepted: its latest revision comment is %r, and '
                    'the revision ledger stays authoritative. Accept it with admin.py '
                    'requirement-apply and F3 acceptance evidence (a new accepted revision), or '
                    'backfill it as a draft.' % (entry['task'], binding.get('acceptance_state')))
            record, body = backfill_evidence(entry['task'], entry['evidence'], binding, actor, at)
            prior_evidence = existing_acceptances(row, operators).get(record['revision'])
            if prior_evidence is not None:
                if prior_evidence.get('source') == 'requirement-apply':
                    # A real operator F3 decision is already recorded for this
                    # revision; the backfill leaves the stronger evidence alone.
                    continue
                if (prior_evidence.get('source') != 'requirement-backfill'
                        or prior_evidence.get('evidence') != entry['evidence']):
                    raise ValueError('Record %s already carries different acceptance evidence; '
                                     'operator reconciliation required.' % entry['task'])
                continue
            evidence_writes.append((entry['task'], body))
    if changed:
        for entry in payload['records']:
            row = find(rows, entry['task'])
            apply_controlled_labels(run, entry['task'], row.get('labels') or [],
                                    entry['kind'], entry['acceptance_state'])
    for task, body in evidence_writes:
        run(['comments', 'add', task, body, '--json'])
    record = {'sha256': digest, 'status': 'complete', 'actor': actor,
              'records': [entry['task'] for entry in payload['records']]}
    evidence = {entry['task']: entry['evidence'] for entry in payload['records']
                if 'evidence' in entry}
    if evidence:
        record['evidence'] = evidence
    atomic(receipt, record)
    return {'records': plan, 'changed': changed, 'reconciled': prior is not None and not changed}


def reconcile(project, operation_id, actor, reason, disposition, run, issue_id=None):
    """Operator-only: resolve a stuck requirement receipt from native state.

    * `complete` requires an explicit `--issue-id` and confirms that exact
      native record (and, for created records, its `request:` label) before the
      receipt is marked complete.
    * `failed`/`released` is allowed only when the receipt records no native
      record and no record carries the request label, so a released operation ID
      can be resubmitted.
    * Repeating an identical reconciliation is idempotent.
    """
    return core.reconcile(project, operation_id, actor, reason, disposition, run, SPEC,
                          issue_id=issue_id)


def _cli_payload(path, operation):
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError('payload must be a JSON object')
    if 'operation' in data:
        raise ValueError('the payload must not set operation; use the draft or revise subcommand')
    return dict(data, operation=operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in OPERATIONS:
        command = sub.add_parser(name, help='%s a requirement revision' % name)
        for option in ('config', 'project', 'actor', 'file'):
            command.add_argument('--' + option, required=True)
    args = parser.parse_args()
    try:
        from client import request
        payload = _cli_payload(args.file, args.command)
        result = request(load_json(args.config), args.project, args.actor,
                         [canonical_bytes(payload).decode()], action='requirement')
        if result['returncode']:
            raise ValueError(result['stderr'])
        print(result['stdout'], end='')
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc))


if __name__ == '__main__':
    main()
