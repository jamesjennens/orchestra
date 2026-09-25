"""Contributor-level requirement draft/revise records with controlled labels.

One locked native operation creates or selects a requirement (or BRD narrative)
record, applies only the controlled `requirement`/`brd-section` type label and
the `requirement:draft`/`requirement:accepted` state label, and posts exactly
one `Kind: requirement-revision-v1` comment. Labels are never caller-supplied:
the operation derives them from the record kind and acceptance state, so an
arbitrary label write is refused rather than silently rewritten.

Idempotency is native-first. A created record carries `request:<hash>` and
`request-content:<hash>` labels (the create-child convention), and the revision
comment itself is the per-revision anchor for selected records. A local receipt
under `.requirement-requests/` is only a recovery cache: it is deliberately not
part of the coordination backup, because the native record remains authoritative.

The operator-only `backfill` path applies the same controlled labels to existing
records (for example records created through create-child that lack them) and
never writes a revision comment.

Payload schemas are closed. `schema_version` is the integer 1.
"""
import argparse
import json
from pathlib import Path

from coordination import atomic, identifier
from export_requirements import REVISION_PREFIX, parse_json, revision_comment
from requirements import canonical_bytes, content_hash, load_json

OPERATIONS = ('draft', 'revise')
KIND_TYPE_LABEL = {'requirement': 'requirement', 'brd-section': 'brd-section'}
STATE_LABEL = {'draft': 'requirement:draft', 'accepted': 'requirement:accepted'}
CONTROLLED_LABELS = frozenset(KIND_TYPE_LABEL.values()) | frozenset(STATE_LABEL.values())
FIELDS = {'schema_version', 'operation_id', 'operation', 'kind', 'task', 'parent',
          'title', 'key', 'description', 'revision', 'acceptance_state'}
BACKFILL_FIELDS = {'schema_version', 'operation_id', 'records'}
BACKFILL_RECORD_FIELDS = {'task', 'kind', 'acceptance_state'}


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


def existing_revisions(row):
    """Validated revision-comment ledger for one record; malformed input fails closed."""
    found = {}
    task = row.get('id')
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            raise ValueError('malformed comment on requirement record ' + str(task))
        text = comment.get('text')
        if not isinstance(text, str) or not text.startswith(REVISION_PREFIX):
            continue
        try:
            record = parse_json(text[len(REVISION_PREFIX):])
        except (ValueError, TypeError):
            raise ValueError('malformed requirement revision comment on ' + str(task)) from None
        if not isinstance(record, dict) or not {'id', 'revision', 'sha256'} <= set(record):
            raise ValueError('malformed requirement revision comment on ' + str(task))
        if record['id'] != task:
            raise ValueError('requirement revision comment on %s belongs to another record' % (task,))
        _positive_int(record['revision'], 'revision')
        if content_hash(record) != record['sha256']:
            raise ValueError('requirement revision content hash mismatch on ' + str(task))
        prior = found.get(record['revision'])
        if prior is not None and prior != record:
            raise ValueError('conflicting content for one id/revision: ' + str(task))
        found[record['revision']] = record
    return found


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


def apply_native(payload, actor, run, project):
    """Caller holds the canonical project lock; run(argv) invokes pinned bd."""
    validate_payload(payload)
    revision = payload.get('revision', 1)
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    journal = Path(project) / '.requirement-requests'
    journal.mkdir(exist_ok=True)
    receipt = journal / (identity + '.json')
    prior = load_json(receipt) if receipt.exists() else None
    if prior is not None and prior.get('sha256') != digest:
        raise ValueError('Operation ID already used for different content or actor')
    if prior is not None and prior.get('id') and payload.get('task') and prior['id'] != payload['task']:
        raise ValueError('Operation ID already used for a different record')
    reconciled = prior is not None
    rows = read_rows(run)
    created = False
    task = (prior.get('id') if prior and prior.get('id') else None) or payload.get('task')
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
            if prior is not None:
                raise ValueError('Reserved requirement request has no visible record; outcome uncertain. '
                                 'Operator must reconcile before any new request; do not allocate another ID.')
            atomic(receipt, {'sha256': digest, 'status': 'pending'})
            labels = sorted(controlled(payload['kind'], payload['acceptance_state'])
                            | {request_label, 'request-content:' + digest})
            args = ['create', '--title', payload['title'], '--parent', payload['parent'],
                    '--description', payload['description'], '--type', 'task',
                    '--no-inherit-labels', '--labels', ','.join(labels), '--json']
            issue = json.loads(run(args))
            if not isinstance(issue, dict) or not issue.get('id'):
                raise ValueError('Create response uncertain; reconcile the same operation ID')
            task = issue['id']
            created = True
            atomic(receipt, {'sha256': digest, 'status': 'created', 'id': task})
            rows = read_rows(run)
    else:
        if find(rows, task) is None:
            raise ValueError('Unknown requirement record: ' + task)
        if prior is None:
            atomic(receipt, {'sha256': digest, 'status': 'pending', 'id': task})
    row = find(rows, task)
    if row is None:
        raise ValueError('Unknown requirement record: ' + task)
    record = requirement_record(payload, task, revision)
    existing = existing_revisions(row)
    _check_revision(payload, revision, existing, record, task)
    if revision not in existing:
        run(['comments', 'add', task, revision_comment(record), '--json'])
    apply_controlled_labels(run, task, row.get('labels') or [], payload['kind'], payload['acceptance_state'])
    atomic(receipt, {'sha256': digest, 'status': 'complete', 'id': task})
    return {'id': task, 'kind': payload['kind'], 'revision': revision,
            'acceptance_state': payload['acceptance_state'],
            'labels': sorted(controlled(payload['kind'], payload['acceptance_state'])),
            'created': created, 'reconciled': reconciled}


def backfill(payload, actor, run, project):
    """Operator-only: apply controlled labels to existing records, no revision comment."""
    validate_backfill(payload)
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    journal = Path(project) / '.requirement-backfills'
    journal.mkdir(exist_ok=True)
    receipt = journal / (identity + '.json')
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
    atomic(receipt, {'sha256': digest, 'status': 'complete',
                     'records': [entry['task'] for entry in payload['records']]})
    return {'records': plan, 'changed': changed, 'reconciled': prior is not None and not changed}


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
