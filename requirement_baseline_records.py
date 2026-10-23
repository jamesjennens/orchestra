"""Closed records for immutable, unaccepted requirements snapshots.

Capture is read-only and assumes the caller holds the project lock. Owner/session
and replay authority belong to the service adapter, never to these record seals.
"""
import copy
import functools
import hashlib
import re
from datetime import datetime

import record_json
import requirement_governance as governance
import requirement_owner_records as owner
import requirement_records as records
from coordination import identifier
from requirements import _unique_object, canonical_bytes, content_hash
from requirement_brd import reference, render

MAX_BASELINES = 64
MAX_SNAPSHOT_BYTES = 4194304
MAX_EXPORT_BYTES = 67108864
MAX_MARKDOWN_BYTES = 4194304
NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?\Z')
DEVICES = re.compile(r'(?:con|prn|aux|nul|com[1-9]|lpt[1-9])\Z', re.I)
SNAPSHOT_FIELDS = {'schema_version', 'project', 'name', 'account_id', 'operation_id',
                   'at', 'view', 'governance_files', 'native', 'previous_current', 'source_sha256', 'sha256'}
VIEW_FIELDS = {'project', 'governance', 'items', 'decisions', 'questions'}
ITEM_FIELDS = {'id', 'kind', 'current', 'accepted', 'pending_draft', 'history',
               'acceptance', 'acceptances', 'legacy_accepted_refs', 'requirement_state',
               'state', 'reason', 'pending_author'}
NATIVE_FIELDS = {'export_sha256', 'ids_sha256', 'total', 'selected_ids'}
CONTEXT_FIELDS = {'schema_version', 'kind', 'id', 'title', 'description', 'status', 'sha256'}
RECEIPT_FIELDS = {'schema_version', 'project', 'name', 'account_id', 'operation_id',
                  'at', 'source_sha256', 'snapshot_sha256', 'files', 'references', 'sha256'}
CAPTURE_ERROR = 'A complete requirements snapshot cannot be verified. Reload or ask the host operator to reconcile it.'
SIZE_ERROR = 'A requirements snapshot exceeds the 4194304-byte limit.'


def _schema(function):
    @functools.wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, IndexError, UnicodeError, OverflowError):
            raise ValueError('Invalid requirements baseline record') from None
    return checked


def validate_pointer(value):
    _closed(value, {'schema_version', 'name', 'receipt_sha256'}, 'current pointer')
    _version(value)
    if value['name'] != baseline_name(value['name']):
        raise ValueError('Stored baseline names must be lowercase')
    governance.digest(value['receipt_sha256'])
    return value


def baseline_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value) or DEVICES.fullmatch(value):
        raise ValueError('Use a baseline name of 1 to 64 ASCII letters, digits, internal hyphens or underscores; device names are reserved.')
    return value.lower()


def _closed(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Invalid requirements baseline ' + label + ' fields')


def _version(value):
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Invalid requirements baseline schema version')


def _stamp(value):
    if not isinstance(value, str) or len(value) != 20:
        raise ValueError('Invalid requirements baseline time')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise ValueError('Invalid requirements baseline time') from None


def json_bytes(value):
    return canonical_bytes(value) + b'\n'


def byte_hash(value):
    return hashlib.sha256(value).hexdigest()


def _governance(files, project):
    if not isinstance(files, dict) or set(files) - {governance.FILE, governance.SOURCE_FILE}:
        raise ValueError('Invalid captured governance files')
    governance.validate_files(files, project)
    record = files.get(governance.FILE)
    if record is None:
        return dict(revision=0, sha256=None, mode='governed')
    return {key: record['revisions'][-1][key] for key in ('revision', 'sha256', 'mode')}


def _proof(proof, history, files, project):
    if not isinstance(proof, dict):
        raise ValueError(CAPTURE_ERROR)
    if 'account_id' in proof:
        owner.validate(proof)
        governance.validate_evidence_files(files, project, proof)
    else:
        from reserved_comments import parse_acceptance_record
        from export_requirements import ACCEPTANCE_PREFIX
        if parse_acceptance_record(ACCEPTANCE_PREFIX + canonical_bytes(proof).decode('utf-8')) != proof:
            raise ValueError('Invalid captured host requirement acceptance')
    revision = history.get(proof['revision'])
    if (revision is None or proof['id'] != revision['id']
            or proof['record_sha256'] != revision['sha256']
            or proof.get('acceptance_state', 'accepted') != revision['acceptance_state']):
        raise ValueError('Captured acceptance does not bind its exact revision')


@_schema
def validate_view(view, files):
    _closed(view, VIEW_FIELDS, 'view')
    project = governance.project_name(view['project'])
    if view['governance'] != _governance(files, project):
        raise ValueError('Captured requirements governance does not match its history')
    if not isinstance(view['items'], list):
        raise ValueError(CAPTURE_ERROR)
    ids, all_refs = set(), {}
    for item in view['items']:
        _closed(item, ITEM_FIELDS, 'item')
        identity = identifier(item['id'])
        if identity in ids or item['kind'] not in ('requirement', 'brd-section'):
            raise ValueError(CAPTURE_ERROR)
        ids.add(identity)
        history = item['history']
        if not isinstance(history, list) or not history:
            raise ValueError(CAPTURE_ERROR)
        revisions = {}
        for index, record in enumerate(history, 1):
            ref = reference(record)
            if (ref['id'] != identity or ref['revision'] != index
                    or ('key' in record) != (item['kind'] == 'requirement')):
                raise ValueError('Captured requirements history is incomplete or conflicting')
            revisions[index] = record
            all_refs[(identity, index, ref['sha256'])] = record
        if item['current'] != history[-1]:
            raise ValueError('Captured current requirement is not its latest revision')
        accepted = [record for record in history if record['acceptance_state'] == 'accepted']
        if item['accepted'] != (accepted[-1] if accepted else None):
            raise ValueError('Captured accepted wording differs from its history')
        pending = history[-1] if history[-1]['acceptance_state'] == 'draft' else None
        if item['pending_draft'] != pending:
            raise ValueError('Captured pending wording differs from its history')
        proofs = item['acceptances']
        if not isinstance(proofs, list):
            raise ValueError(CAPTURE_ERROR)
        by_revision = {}
        for proof in proofs:
            _proof(proof, revisions, files, project)
            if proof['revision'] in by_revision:
                raise ValueError('Captured requirement repeats acceptance evidence')
            by_revision[proof['revision']] = proof
        expected_legacy = [reference(record) for record in accepted if record['revision'] not in by_revision]
        if item['legacy_accepted_refs'] != expected_legacy:
            raise ValueError('Captured legacy acceptance coverage does not match its history')
        latest_proof = by_revision.get(item['accepted']['revision']) if item['accepted'] else None
        if item['acceptance'] != latest_proof:
            raise ValueError('Captured displayed acceptance differs from its evidence')
        state, reason, status = item['state'], item['reason'], item['requirement_state']
        if status not in ('active', 'withdrawn', 'superseded'):
            raise ValueError(CAPTURE_ERROR)
        if state is None:
            if reason is not None or status != 'active':
                raise ValueError('Captured terminal status has no evidence')
        else:
            owner.validate(state, state=True)
            governance.validate_evidence_files(files, project, state)
            owner.validate_reason(reason)
            bound = revisions.get(state['revision'])
            if (bound is None or state['id'] != identity or state['record_sha256'] != bound['sha256']
                    or bound['acceptance_state'] != 'accepted' or state['state'] != status
                    or reason['state_sha256'] != state['sha256']
                    or any(reason[key] != state[key] for key in ('project', 'id', 'account_id', 'at', 'operation_id'))):
                raise ValueError('Captured terminal evidence conflicts with its revision')
        author = item['pending_author']
        if author is not None:
            _closed(author, {'kind', 'text', 'omitted_chars', 'at'}, 'pending attribution')
            if (pending is None or author['kind'] not in ('account', 'contributor-name')
                    or not isinstance(author['text'], str) or len(author['text']) > 200
                    or type(author['omitted_chars']) is not int or author['omitted_chars'] < 0
                    or author['at'] is not None and not isinstance(author['at'], str)):
                raise ValueError('Invalid captured pending attribution')
    for item in view['items']:
        for record in item['history']:
            for ref in record.get('references', []):
                if not isinstance(ref, dict) or set(ref) != {'id', 'revision', 'sha256'}:
                    raise ValueError('Invalid captured exact reference')
                if (ref['id'], ref['revision'], ref['sha256']) not in all_refs:
                    raise ValueError('Captured exact reference cannot be resolved')
        successor = (item['state'] or {}).get('superseded_by')
        if successor and (successor['id'], successor['revision'], successor['sha256']) not in all_refs:
            raise ValueError('Captured successor exact reference cannot be resolved')
    for group, kind in (('decisions', 'decision'), ('questions', 'question')):
        if not isinstance(view[group], list):
            raise ValueError(CAPTURE_ERROR)
        seen = set()
        for entry in view[group]:
            _closed(entry, CONTEXT_FIELDS, 'context')
            _version(entry)
            identifier(entry['id'])
            if (entry['id'] in seen or entry['kind'] != kind
                    or entry['status'] not in ('open', 'in_progress', 'blocked', 'deferred')
                    or any(not isinstance(entry[key], str) for key in ('title', 'description'))
                    or content_hash(entry) != entry['sha256']):
                raise ValueError('Invalid captured decision or question')
            seen.add(entry['id'])
    canonical_bytes(view)
    return view


def capture(project_path, project, run, operators=None):
    """One export reused by every consumer, with a separate complete ID proof.

    A native list with --all --limit 0 proves export completeness. Native label
    and type probes classify unreadable rows; source text never grants membership.
    """
    from requirement_http import read
    cache = {}

    def frozen_run(args):
        if not args or args[0] not in ('export', 'list'):
            raise ValueError('Requirements snapshot capture is read-only')
        key = tuple(args)
        if key not in cache:
            answer = run(args)
            if not isinstance(answer, str) or len(answer.encode('utf-8')) > MAX_EXPORT_BYTES:
                raise ValueError('A native answer exceeds the 67108864-byte snapshot read limit.')
            cache[key] = answer
        return cache[key]

    raw = frozen_run(['export', '--all'])
    rows = record_json.loads_rows(raw)
    exported_ids = record_json.ids_from_native(rows)
    native_ids = record_json.native_ids(frozen_run)
    if len(exported_ids) != len(set(exported_ids)) or set(exported_ids) != native_ids:
        raise ValueError(CAPTURE_ERROR)
    files = governance.snapshot(project_path, project)
    projected = read(project_path, project, ['brd'], frozen_run, operators)
    items = []
    source_rows = {row['id']: row for row in rows}
    for item in projected['items']:
        if item.get('unreadable') or item.get('requirement_state') == 'unknown':
            raise ValueError(CAPTURE_ERROR)
        row = source_rows[item['id']]
        proofs = records.existing_acceptances(row, operators)
        accepted = [record for record in item['history'] if record['acceptance_state'] == 'accepted']
        frozen = {key: copy.deepcopy(item[key]) for key in ITEM_FIELDS - {'acceptances', 'legacy_accepted_refs'}}
        frozen['acceptances'] = [proofs[key] for key in sorted(key for key in proofs if key is not None)]
        frozen['legacy_accepted_refs'] = [reference(record) for record in accepted if record['revision'] not in proofs]
        items.append(frozen)
    classified = record_json.classify(rows, frozen_run, ('question', 'open-question'), ('decision',))
    context = {'decisions': [], 'questions': []}
    for row in classified:
        labels = row.get('labels') or []
        kind = ('decision' if row.get('issue_type') == 'decision' or record_json.selected(row, types=('decision',))
                else 'question' if set(labels).intersection(('question', 'open-question'))
                or row.get('issue_type') == 'question' else None)
        if kind is None:
            continue
        if row.get('malformed') or row.get('status') not in ('open', 'in_progress', 'blocked', 'deferred', 'closed'):
            raise ValueError(CAPTURE_ERROR)
        if row['status'] == 'closed':
            continue
        entry = dict(schema_version=1, kind=kind, id=row['id'], title=row.get('title', ''),
                     description=row.get('description', ''), status=row['status'])
        entry['sha256'] = content_hash(entry)
        context['decisions' if kind == 'decision' else 'questions'].append(entry)
    view = dict(project=project, governance=_governance(files, project), items=sorted(items, key=lambda item: item['id']),
                decisions=sorted(context['decisions'], key=lambda entry: entry['id']),
                questions=sorted(context['questions'], key=lambda entry: entry['id']))
    validate_view(view, files)
    native = dict(export_sha256=byte_hash(raw.encode('utf-8')), ids_sha256=content_hash({'ids': sorted(native_ids)}),
                  total=len(native_ids), selected_ids=sorted({item['id'] for item in items}
                    | {entry['id'] for entries in context.values() for entry in entries}))
    return dict(view=view, governance_files=files, native=native)


@_schema
def validate_snapshot(value):
    _closed(value, SNAPSHOT_FIELDS, 'snapshot')
    _version(value)
    project = governance.project_name(value['project'])
    if value['name'] != baseline_name(value['name']):
        raise ValueError('Stored baseline names must be lowercase')
    identifier(value['operation_id'])
    if not isinstance(value['account_id'], str) or not owner.HUMAN.fullmatch(value['account_id']):
        raise ValueError('A baseline cut must name its human owner account')
    _stamp(value['at'])
    if value['previous_current'] is not None:
        validate_pointer(value['previous_current'])
    if value['view']['project'] != project or value['view']['governance']['mode'] != 'simple':
        raise ValueError('A baseline cut must capture its simple project')
    validate_view(value['view'], value['governance_files'])
    native = value['native']
    _closed(native, NATIVE_FIELDS, 'native snapshot')
    governance.digest(native['export_sha256']); governance.digest(native['ids_sha256'])
    selected = sorted({item['id'] for item in value['view']['items']}
                      | {entry['id'] for group in ('decisions', 'questions') for entry in value['view'][group]})
    if (native['selected_ids'] != selected or type(native['total']) is not int or native['total'] < len(selected)):
        raise ValueError('Invalid captured native membership proof')
    source = {key: value[key] for key in ('view', 'governance_files', 'native')}
    if content_hash(source) != value['source_sha256'] or content_hash(value) != value['sha256']:
        raise ValueError('Requirements snapshot hash mismatch')
    if len(json_bytes(value)) > MAX_SNAPSHOT_BYTES:
        raise ValueError(SIZE_ERROR)
    return value


def build(project, name, account_id, operation_id, captured, at, previous_current=None):
    value = dict(schema_version=1, project=project, name=baseline_name(name), account_id=account_id,
                 operation_id=operation_id, at=at, previous_current=copy.deepcopy(previous_current),
                 **copy.deepcopy(captured))
    value['source_sha256'] = content_hash({key: value[key] for key in ('view', 'governance_files', 'native')})
    value['sha256'] = content_hash(value)
    return validate_snapshot(value)


def exact_references(value):
    refs = [reference(record) for item in value['view']['items'] for record in item['history']]
    return sorted(refs, key=lambda ref: (ref['id'], ref['revision'], ref['sha256']))


def receipt(value):
    validate_snapshot(value)
    data = dict(snapshot=json_bytes(value), markdown=render(value['view']))
    if len(data['markdown']) > MAX_MARKDOWN_BYTES:
        raise ValueError('The requirements Markdown exceeds the 4194304-byte export limit.')
    result = {key: value[key] for key in ('schema_version', 'project', 'name', 'account_id', 'operation_id', 'at', 'source_sha256')}
    result.update(snapshot_sha256=value['sha256'], files={
        'snapshot.json': dict(bytes=len(data['snapshot']), sha256=byte_hash(data['snapshot'])),
        'BRD.md': dict(bytes=len(data['markdown']), sha256=byte_hash(data['markdown']))}, references=exact_references(value))
    result['sha256'] = content_hash(result)
    return result


def validate_receipt(value, snapshot):
    _closed(value, RECEIPT_FIELDS, 'receipt')
    if value != receipt(snapshot):
        raise ValueError('Requirements baseline receipt does not match its frozen bytes')
    return value


def decode_snapshot(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError(SIZE_ERROR)
    try:
        value = record_json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        validate_snapshot(value)
        if json_bytes(value) != raw:
            raise ValueError('Requirements snapshots must be canonical UTF-8/LF')
        return value
    except (ValueError, UnicodeError, TypeError, KeyError, IndexError):
        raise ValueError('This requirements snapshot cannot be verified. Ask the host operator to restore a verified backup.') from None
