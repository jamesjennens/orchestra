"""Contributor-level requirement draft/revise records with controlled labels.

One locked native operation creates or selects a requirement (or BRD narrative)
record, applies only the controlled `requirement`/`brd-section` type label and
the `requirement:draft`/`requirement:accepted` state label, and posts exactly
one `Kind: requirement-revision-v1` comment. Labels are never caller-supplied:
the operation derives them from the record kind and acceptance state, so an
arbitrary label write is refused rather than silently rewritten.

Authority split (REQUIREMENTS_CONTRACT F3). The contributor route may only write
*draft* revisions. Acceptance of a record and demotion of an accepted record back
to draft are owner/operator-only and require F3 acceptance evidence: an
`acceptance` object naming owners/approvers plus a decision id and evidence. The
operator route is `admin.py requirement-apply`.

Selection is closed. `draft` with `task=` only accepts records already carrying
the `requirement`/`brd-section` type label (or records this command created), and
`revise` is bound to the record's existing kind and key: cross-kind changes and
key swaps are refused before any native write.

Idempotency is native-first. A created record carries `request:<hash>` and
`request-content:<hash>` labels (the create-child convention), and the revision
comment itself is the per-revision anchor for selected records. A local receipt
under `.requirement-requests/` is only a recovery cache: it is deliberately not
part of the coordination backup, because the native record remains authoritative.
A native `bd create --dry-run` preflight and the revision check run BEFORE any
receipt is written, so a refusal reserves nothing; a receipt stays pending only
for an uncertain real-write failure and can be completed from native state with
`admin.py requirement-reconcile`.

The operator-only `backfill` path applies the same controlled labels to existing
records (for example records created through create-child that lack them) and
never writes a revision comment. Backfilling a record to `requirement:accepted`
requires an evidence field.

Payload schemas are closed. `schema_version` is the integer 1.
"""
import argparse
import json
import time
from pathlib import Path

from coordination import atomic, identifier
from export_requirements import REVISION_PREFIX, parse_json, revision_comment
from requirements import (ACCEPTANCE_FIELDS, canonical_bytes, content_hash,
                          load_json)

OPERATIONS = ('draft', 'revise')
KIND_TYPE_LABEL = {'requirement': 'requirement', 'brd-section': 'brd-section'}
STATE_LABEL = {'draft': 'requirement:draft', 'accepted': 'requirement:accepted'}
TYPE_LABELS = frozenset(KIND_TYPE_LABEL.values())
CONTROLLED_LABELS = frozenset(KIND_TYPE_LABEL.values()) | frozenset(STATE_LABEL.values())
FIELDS = {'schema_version', 'operation_id', 'operation', 'kind', 'task', 'parent',
          'title', 'key', 'description', 'revision', 'acceptance_state',
          'acceptance'}
ACCEPTANCE_EVIDENCE_FIELDS = tuple(name for name in ACCEPTANCE_FIELDS
                                   if name != 'manifest_sha256')
BACKFILL_FIELDS = {'schema_version', 'operation_id', 'records'}
BACKFILL_RECORD_FIELDS = {'task', 'kind', 'acceptance_state', 'evidence'}
JOURNAL = '.requirement-requests'
BACKFILL_JOURNAL = '.requirement-backfills'
RECONCILE_RELEASABLE = ('failed', 'released')


def _refuse_injected_labels(payload, where='requirement payload'):
    """Fail closed on caller-supplied labels before any other validation."""
    if not isinstance(payload, dict):
        return
    if 'labels' in payload or 'add_labels' in payload:
        raise ValueError('Labels are controlled by the %s; arbitrary label writes are refused. '
                         'The operation derives the type/state labels from kind and acceptance_state.' % where)
    if 'decided_by' in payload:
        raise ValueError('decided_by is proposed in kittrial-pth.25 (change-proposal, not accepted) and '
                         'is refused rather than written as an unvalidated revision field.')


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(where + ' must be a nonempty string')
    return value


def _positive_int(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(where + ' must be a positive integer (not boolean)')
    return value


def _checked_fields(payload, allowed, where):
    extra = sorted(set(payload) - set(allowed))
    if extra:
        raise ValueError('%s has unknown field(s): %s' % (where, ', '.join(extra)))


def _validate_acceptance_shape(acceptance):
    """Structural check for caller-supplied F3 acceptance evidence.

    `manifest_sha256` is not caller-supplied: the command binds the acceptance
    object to the exact revision content hash it is about to write.
    """
    if not isinstance(acceptance, dict):
        raise ValueError('acceptance must be an object naming owners, approvers, policy, '
                         'decision_id and evidence')
    extra = sorted(set(acceptance) - set(ACCEPTANCE_EVIDENCE_FIELDS))
    if extra:
        raise ValueError('acceptance has unknown field(s): %s (manifest_sha256 is bound by the command)'
                         % ', '.join(extra))
    for name in ACCEPTANCE_EVIDENCE_FIELDS:
        if name not in acceptance:
            raise ValueError('acceptance is missing field ' + name)
    return acceptance


def bind_acceptance(acceptance, record):
    """Bind F3 evidence to the exact revision content hash via the contract validator."""
    full = dict(acceptance)
    full['manifest_sha256'] = record['sha256']
    from requirements import _validate_acceptance
    try:
        _validate_acceptance(full, record)
    except ValueError as exc:
        raise ValueError('invalid acceptance evidence: %s' % exc) from None
    return full


def validate_payload(payload):
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
    if payload['operation'] == 'draft':
        if payload['acceptance_state'] != 'draft':
            raise ValueError('draft creates a draft revision (acceptance_state must be draft)')
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


def read_rows(run):
    return [json.loads(line) for line in run(['export', '--all']).splitlines() if line.strip()]


def find(rows, task):
    return next((row for row in rows if row.get('id') == task), None)


def _journal(project, name):
    journal = Path(project) / name
    if journal.is_symlink():
        raise ValueError('Refusing %s: the path is a symlink; no native write was attempted.' % name)
    try:
        journal.mkdir(exist_ok=True)
    except OSError as exc:
        raise ValueError('Cannot create %s: %s' % (name, exc))
    if not journal.is_dir():
        raise ValueError('%s is not a directory; refusing to write a receipt.' % name)
    return journal


def _receipt_path(journal, identity):
    receipt = journal / (identity + '.json')
    if receipt.is_symlink():
        raise ValueError('Refusing requirement receipt %s: it is a symlink; no native write was attempted.'
                         % receipt.name)
    return receipt


def existing_revisions(row):
    """Validated revision-comment ledger for one record; malformed input fails closed.

    Reuses reserved_comments.parse_requirement_record, the guard's own full
    schema + canonical-bytes + content-hash validation, so an existing revision
    is accepted only when it is exactly what a legitimate writer could post.
    """
    from reserved_comments import parse_requirement_record
    found = {}
    task = row.get('id')
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            raise ValueError('malformed comment on requirement record ' + str(task))
        text = comment.get('text')
        if not isinstance(text, str) or not text.startswith(REVISION_PREFIX):
            continue
        record = parse_requirement_record(text)
        if record is None:
            raise ValueError('malformed requirement revision comment on ' + str(task))
        if record.get('id') != task:
            raise ValueError('requirement revision comment on %s belongs to another record' % (task,))
        prior = found.get(record['revision'])
        if prior is not None and prior != record:
            raise ValueError('conflicting content for one id/revision: ' + str(task))
        found[record['revision']] = record
    return found


def latest_revision(existing):
    return existing[max(existing)] if existing else None


def existing_kind(row):
    """The record's controlled type label, or None when it is untyped."""
    present = TYPE_LABELS & set(row.get('labels') or [])
    if not present:
        return None
    if len(present) > 1:
        raise ValueError('Record %s carries conflicting type labels (%s); operator reconciliation required'
                         % (row.get('id'), ', '.join(sorted(present))))
    return next(iter(present))


def require_typed(row, payload):
    """Selection is closed: only records already typed requirement/brd-section."""
    kind = existing_kind(row)
    if kind is None:
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


def check_key_unique(rows, payload, task):
    """Requirement keys are unique across the project's requirement records."""
    if payload['kind'] != 'requirement':
        return
    for row in rows:
        if row.get('id') == task:
            continue
        if 'requirement' not in (row.get('labels') or []):
            continue
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
    desired = controlled(kind, acceptance_state)
    present = set(current or [])
    changed = False
    for label in sorted(CONTROLLED_LABELS - desired):
        if label in present:
            run(['update', task, '--remove-label', label, '--json'])
            changed = True
    for label in sorted(desired - present):
        run(['update', task, '--add-label', label, '--json'])
        changed = True
    return changed


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
    if 'requirement:accepted' in (row.get('labels') or []):
        return 'accepted'
    record = latest_revision(existing)
    if record is not None and record.get('acceptance_state') == 'accepted':
        return 'accepted'
    return 'draft'


def _check_acceptance(payload, existing, record, operator, row):
    """Enforce the contributor-drafts-only / operator-F3-evidence split.

    Returns the acceptance object bound to the new revision hash, or None.
    """
    target = payload['acceptance_state']
    current = _current_acceptance(row, existing)
    acceptance = payload.get('acceptance')
    if not operator:
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
                         'evidence (manifest_sha256 is bound to the revision by the command).')
    return None


def _prior_state(prior, digest, actor):
    """Classify an existing receipt for this operation ID.

    Returns 'none', 'same' (identical retry) or 'reusable' (operator released or
    marked failed). A conflict raises before any native write.
    """
    if not isinstance(prior, dict):
        return 'none'
    if prior.get('actor') and prior['actor'] != actor:
        raise ValueError('Operation ID already used by actor %r; use a new operation ID or have the operator '
                         'reconcile it.' % (prior['actor'],))
    if prior.get('status') in RECONCILE_RELEASABLE:
        return 'reusable'
    if prior.get('sha256') == digest:
        return 'same'
    raise ValueError('Operation ID already used for different content; use a new operation ID or have the '
                     'operator reconcile it.')


def _uncertain(message, action):
    return ValueError('%s The native %s did not confirm; the outcome is uncertain, so this operation ID stays '
                      'pending. Inspect native state and complete it with admin.py requirement-reconcile before '
                      'retrying.' % (message, action))


def apply_native(payload, actor, run, project, operator=False):
    """Caller holds the canonical project lock; run(argv) invokes pinned bd.

    `operator=True` is the owner/operator route (admin.py requirement-apply);
    only it may accept a record or demote an accepted record, and only with F3
    acceptance evidence. The contributor route may only write draft revisions.
    """
    validate_payload(payload)
    if not operator and payload['acceptance_state'] == 'accepted':
        raise ValueError('Contributors may only draft requirement records; accepting a record is '
                         'owner/operator-only and requires F3 acceptance evidence. Use a draft revision, or ask '
                         'the operator to accept it with admin.py requirement-apply.')
    revision = payload.get('revision', 1)
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    journal = _journal(project, JOURNAL)
    receipt = _receipt_path(journal, identity)
    prior = load_json(receipt) if receipt.exists() else None
    state = _prior_state(prior, digest, actor)
    reusable = state == 'reusable'
    reconciled = state == 'same'
    if prior is not None and prior.get('id') and payload.get('task') and not reusable \
            and prior['id'] != payload['task']:
        raise ValueError('Operation ID already used for a different record')
    rows = read_rows(run)
    created = False
    task = (prior.get('id') if prior and not reusable and prior.get('id') else None) or payload.get('task')
    # Key uniqueness is checked against the pre-write export so a duplicate key
    # is refused with zero native writes (the created record would otherwise be
    # written first and rejected after the fact).
    check_key_unique(rows, payload, task)
    if task is None:
        request_label = 'request:' + identity
        matches = [row for row in rows if request_label in (row.get('labels') or [])]
        if len(matches) > 1:
            raise ValueError('Duplicate native requirement records; operator reconciliation required')
        if matches:
            if 'request-content:' + digest not in (matches[0].get('labels') or []):
                raise ValueError('Native requirement content mismatch')
            task = matches[0]['id']
            reconciled = True
        else:
            if prior is not None and not reusable:
                raise ValueError('Reserved requirement request has no visible record; outcome uncertain. '
                                 'Operator must reconcile before any new request; do not allocate another ID.')
            labels = sorted(controlled(payload['kind'], payload['acceptance_state'])
                            | {request_label, 'request-content:' + digest})
            args = ['create', '--title', payload['title'], '--parent', payload['parent'],
                    '--description', payload['description'], '--type', 'task',
                    '--no-inherit-labels', '--labels', ','.join(labels), '--json']
            # Preflight the identical create natively BEFORE any receipt or
            # native mutation: a bad parent or invalid description is refused
            # here, so a refusal reserves nothing and cannot burn the ID.
            run(args + ['--dry-run'])
            atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor,
                             'operation': payload['operation'], 'revision': revision})
            try:
                raw = run(args)
            except (ValueError, OSError) as refusal:
                raise _uncertain(str(refusal), 'create') from None
            try:
                issue = json.loads(raw)
            except (TypeError, ValueError):
                issue = None
            if not isinstance(issue, dict) or not issue.get('id'):
                raise _uncertain('Create response was not a record.', 'create')
            task = issue['id']
            created = True
            atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor, 'id': task,
                             'created': True, 'operation': payload['operation'], 'revision': revision})
            rows = read_rows(run)
    row = find(rows, task)
    if row is None:
        if prior is not None:
            raise ValueError('Recorded native requirement record %s is not visible; outcome uncertain. '
                             'Operator must reconcile this operation ID.' % task)
        raise ValueError('Unknown requirement record: ' + task)
    if not created:
        require_typed(row, payload)
        if prior is not None and prior.get('created') \
                and 'request-content:' + digest not in (row.get('labels') or []):
            raise ValueError('Native requirement content mismatch')
    record = requirement_record(payload, task, revision)
    existing = existing_revisions(row)
    require_bound_key(task, payload, existing)
    _check_revision(payload, revision, existing, record, task)
    bound = _check_acceptance(payload, existing, record, operator, row)
    if prior is None or prior.get('status') != 'complete' or created:
        # A pending receipt is written only immediately before a real native
        # write, so every refusal above reserves nothing.
        atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor, 'id': task,
                         'created': created, 'operation': payload['operation'], 'revision': revision})
    if revision not in existing:
        try:
            run(['comments', 'add', task, revision_comment(record), '--json'])
        except (ValueError, OSError) as refusal:
            raise _uncertain(str(refusal), 'revision comment') from None
    apply_controlled_labels(run, task, row.get('labels') or [], payload['kind'], payload['acceptance_state'])
    complete = {'sha256': digest, 'status': 'complete', 'actor': actor, 'id': task,
                'operation': payload['operation'], 'revision': revision}
    if bound is not None:
        complete['acceptance'] = bound
    atomic(receipt, complete)
    result = {'id': task, 'kind': payload['kind'], 'revision': revision,
              'acceptance_state': payload['acceptance_state'],
              'labels': sorted(controlled(payload['kind'], payload['acceptance_state'])),
              'created': created, 'reconciled': reconciled}
    if bound is not None:
        result['acceptance'] = bound
    return result


def backfill(payload, actor, run, project):
    """Operator-only: apply controlled labels to existing records, no revision comment."""
    validate_backfill(payload)
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    journal = _journal(project, BACKFILL_JOURNAL)
    receipt = _receipt_path(journal, identity)
    prior = load_json(receipt) if receipt.exists() else None
    if prior is not None and prior.get('sha256') != digest:
        raise ValueError('Operation ID already used for different content or actor')
    rows = read_rows(run)
    plan, changed = [], False
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
    if changed:
        for entry in payload['records']:
            row = find(rows, entry['task'])
            apply_controlled_labels(run, entry['task'], row.get('labels') or [],
                                    entry['kind'], entry['acceptance_state'])
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
    identifier(operation_id)
    identifier(actor)
    if disposition not in ('failed', 'released', 'complete'):
        raise ValueError('Disposition must be failed, released or complete')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('A reconciliation reason is required')
    if disposition == 'complete' and not issue_id:
        raise ValueError('A complete disposition requires an explicit --issue-id naming the native record to confirm')
    if issue_id is not None and disposition != 'complete':
        raise ValueError('An issue ID applies only to a complete disposition')
    if issue_id is not None and (not isinstance(issue_id, str) or not issue_id.strip()):
        raise ValueError('Invalid issue ID')
    journal = _journal(project, JOURNAL)
    identity = content_hash({'operation_id': operation_id})
    receipt = _receipt_path(journal, identity)
    if not receipt.exists():
        raise ValueError('No requirement operation receipt exists for that operation ID')
    prior = load_json(receipt)
    if not isinstance(prior, dict) or not isinstance(prior.get('status'), str):
        raise ValueError('Malformed requirement receipt; inspect before reconciling')
    at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    audit = prior.get('reconciliation') or {}
    if prior['status'] in RECONCILE_RELEASABLE:
        same = (audit.get('actor') == actor and audit.get('reason') == reason
                and audit.get('disposition') == disposition)
        if same:
            return {'operation_id': operation_id, 'status': prior['status'], 'reconciled': False,
                    'already': True, 'reconciliation': audit}
        raise ValueError('Reconciliation conflict: operation %s is already %s by actor %r (reason %r); the '
                         'recorded audit is kept.' % (operation_id, prior['status'], audit.get('actor'),
                                                      audit.get('reason')))
    if prior['status'] == 'complete':
        same = (audit.get('actor') == actor and audit.get('reason') == reason
                and audit.get('disposition') == 'complete' and prior.get('id') == issue_id)
        if same:
            return {'operation_id': operation_id, 'status': 'complete', 'reconciled': False,
                    'already': True, 'id': prior.get('id'), 'reconciliation': audit}
        raise ValueError('Reconciliation conflict: operation %s is already complete by actor %r (reason %r, '
                         'id %r); the recorded audit is kept.' % (operation_id, audit.get('actor'),
                                                                  audit.get('reason'), prior.get('id')))
    if prior['status'] != 'pending':
        raise ValueError('Requirement operation is not pending; nothing to reconcile')
    rows = read_rows(run)
    found = [row for row in rows if 'request:' + identity in (row.get('labels') or [])]
    if len(found) > 1:
        raise ValueError('Duplicate native request records; manual operator reconciliation required')
    if disposition == 'complete':
        if prior.get('id') and prior['id'] != issue_id:
            raise ValueError('--issue-id %s does not match the receipt record %s' % (issue_id, prior['id']))
        if found and found[0].get('id') != issue_id:
            raise ValueError('--issue-id %s does not match the labelled native record %s'
                             % (issue_id, found[0].get('id')))
        if not found and not prior.get('id'):
            raise ValueError('No labelled native record exists for this request; completion needs one, so use '
                             '--disposition failed or released')
        row = find(rows, issue_id)
        if row is None:
            raise ValueError('Could not confirm native record %s; refusing to complete an unconfirmed receipt'
                             % issue_id)
        audit = {'actor': actor, 'reason': reason, 'disposition': 'complete', 'at': at,
                 'completed_from': 'native', 'issue': {'id': issue_id, 'title': row.get('title')}}
        updated = {'sha256': prior.get('sha256'), 'status': 'complete', 'actor': prior.get('actor') or actor,
                   'id': issue_id, 'reconciliation': audit}
        for name in ('operation', 'revision', 'created', 'acceptance'):
            if name in prior:
                updated[name] = prior[name]
        atomic(receipt, updated)
        return {'operation_id': operation_id, 'status': 'complete', 'reconciled': True, 'id': issue_id,
                'issue': audit['issue'], 'reconciliation': audit}
    if prior.get('id'):
        raise ValueError('The receipt records native record %s; releasing would discard the binding, so '
                         'complete it with --disposition complete --issue-id instead.' % prior['id'])
    if found:
        raise ValueError('A native record already carries request:%s; complete the receipt instead of '
                         'releasing it.' % identity)
    audit = {'actor': actor, 'reason': reason, 'disposition': disposition, 'at': at}
    updated = {'sha256': prior.get('sha256'), 'status': disposition, 'actor': prior.get('actor'),
               'reconciliation': audit}
    for name in ('operation', 'revision', 'error'):
        if name in prior:
            updated[name] = prior[name]
    if 'error' not in updated:
        updated['error'] = ('Released by the operator after confirming no native record was created.'
                            if disposition == 'released' else
                            'Marked failed by the operator after confirming no native record was created.')
    atomic(receipt, updated)
    return {'operation_id': operation_id, 'status': disposition, 'reconciled': True, 'reconciliation': audit}


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
