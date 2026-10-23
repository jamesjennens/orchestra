"""Immutable named requirements views, installed under the caller's project lock.

This is not an authority mechanism: the owner adapter checks live session,
current membership and simple mode before effects AND before cached replay.
Record hashes detect damage; somebody who can edit host files is trusted.
"""
import os
import re
import secrets
import stat
from pathlib import Path

import record_json
import requirement_baseline_records as schema
import requirement_governance as governance
import requirement_owner_records as owner
from coordination import identifier
from requirements import _unique_object, canonical_bytes, content_hash
from requirement_brd import render

DIRECTORY = '.requirements-baselines'
RESTORE_MARKER = '.requirements-baselines-restore.json'
PREFIX = DIRECTORY + '/'
FILES = {'snapshot.json', 'BRD.md', 'receipt.json'}
STAGE = re.compile(r'\.pending-[0-9a-f]{64}\Z')
TEMP = re.compile(r'\.pointer-[0-9a-f]{32}\.tmp\Z')
REPAIR = 'This requirements baseline cannot be verified. Ask the host operator to restore a verified backup.'
NOT_FOUND = 'Requirements baseline was not found.'
FULL = 'This project has reached the limit of 64 baselines or unfinished cuts.'
CONFLICT = 'That baseline name already belongs to a different cut. Choose a new name.'
SOURCE_FIELDS = {'schema_version', 'project', 'restored_from', 'baselines', 'sha256'}
MAX_RECEIPT_BYTES = schema.MAX_SNAPSHOT_BYTES


def _reparse(info):
    return bool(getattr(info, 'st_file_attributes', 0) & 1024)


def _directory(path, create=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            path.mkdir(mode=0o700)
            info = path.lstat()
        except OSError:
            raise ValueError(REPAIR) from None
    except OSError:
        raise ValueError(REPAIR) from None
    if not stat.S_ISDIR(info.st_mode) or _reparse(info):
        raise ValueError(REPAIR)
    return path


def _root(project_path, create=False):
    project_path = Path(project_path)
    if os.path.lexists(project_path / RESTORE_MARKER):
        raise ValueError('Requirements baseline restore is incomplete. Retry the exact verified restore before reading or cutting baselines.')
    if _directory(project_path) is None:
        raise ValueError(REPAIR)
    return _directory(project_path / DIRECTORY, create)


def _read(path, limit, missing=False):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or _reparse(info):
            raise ValueError(REPAIR)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode) or _reparse(opened)
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError(REPAIR)
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(REPAIR)
        return raw
    except FileNotFoundError:
        if missing:
            return None
        raise ValueError(REPAIR) from None
    except OSError:
        raise ValueError(REPAIR) from None


def _json(raw):
    try:
        value = record_json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        if schema.json_bytes(value) != raw:
            raise ValueError(REPAIR)
        return value
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(REPAIR) from None


def _write_once(path, raw):
    prior = _read(path, len(raw), missing=True)
    if prior is not None:
        if prior != raw:
            raise ValueError(REPAIR)
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    except OSError:
        raise ValueError(REPAIR) from None


def _entries(base):
    names, stages, temps = [], [], []
    if base is None:
        return names, stages
    try:
        for index, path in enumerate(base.iterdir()):
            if index >= schema.MAX_BASELINES * 2 + 2:
                raise ValueError(FULL)
            if path.name in ('current.json', 'source.json'):
                continue
            if STAGE.fullmatch(path.name):
                stages.append(path.name)
            elif TEMP.fullmatch(path.name):
                temps.append(path.name)
            elif path.name == schema.baseline_name(path.name):
                names.append(path.name)
            else:
                raise ValueError(REPAIR)
    except (OSError, ValueError):
        raise ValueError(REPAIR) from None
    if len(names) + len(stages) > schema.MAX_BASELINES or len(temps) > schema.MAX_BASELINES:
        raise ValueError(FULL)
    return sorted(names), sorted(stages)


@schema._schema
def _source_record(value, project):
    schema._closed(value, SOURCE_FIELDS, 'restore binding'); schema._version(value)
    if value['project'] != project:
        raise ValueError(REPAIR)
    governance.project_name(project); governance.project_name(value['restored_from'])
    entries = value['baselines']
    if not isinstance(entries, dict) or not entries or len(entries) > schema.MAX_BASELINES:
        raise ValueError(REPAIR)
    for name, binding in entries.items():
        if name != schema.baseline_name(name):
            raise ValueError(REPAIR)
        schema._closed(binding, {'project', 'receipt_sha256'}, 'restored baseline')
        governance.project_name(binding['project']); governance.digest(binding['receipt_sha256'])
    if content_hash(value) != value['sha256']:
        raise ValueError(REPAIR)
    return value


def _source(base, project):
    raw = _read(base / 'source.json', MAX_RECEIPT_BYTES, missing=True)
    return _source_record(_json(raw), project) if raw is not None else None


def _artifact(base, name):
    path = _directory(base / name)
    if path is None:
        raise ValueError(NOT_FOUND)
    try:
        snapshot_raw = _read(path / 'snapshot.json', schema.MAX_SNAPSHOT_BYTES)
        snapshot = schema.decode_snapshot(snapshot_raw)
        receipt_raw = _read(path / 'receipt.json', MAX_RECEIPT_BYTES)
        markdown = _read(path / 'BRD.md', schema.MAX_MARKDOWN_BYTES)
        receipt = schema.validate_receipt(_json(receipt_raw), snapshot, markdown=markdown)
        if snapshot['name'] != name:
            raise ValueError(REPAIR)
        return dict(snapshot=snapshot, receipt=receipt, markdown=markdown,
                    receipt_sha256=schema.byte_hash(receipt_raw))
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError(REPAIR) from None


def _belongs(artifact, project, binding):
    snapshot = artifact['snapshot']
    entries = binding['baselines'] if binding else {}
    wanted = dict(project=snapshot['project'], receipt_sha256=artifact['receipt_sha256'])
    if snapshot['name'] in entries:
        if entries[snapshot['name']] != wanted:
            raise ValueError(REPAIR)
    elif snapshot['project'] != project:
        raise ValueError(REPAIR)


def get(project_path, project, name):
    governance.project_name(project); name = schema.baseline_name(name)
    base = _root(project_path)
    if base is None:
        raise ValueError(NOT_FOUND)
    artifact = _artifact(base, name)
    _belongs(artifact, project, _source(base, project))
    return artifact


def _pointer(base):
    raw = _read(base / 'current.json', 1024, missing=True)
    return schema.validate_pointer(_json(raw)) if raw is not None else None


def current(project_path, project):
    base = _root(project_path)
    if base is None:
        return None
    pointer = _pointer(base)
    if pointer is None:
        return None
    artifact = get(project_path, project, pointer['name'])
    if artifact['receipt_sha256'] != pointer['receipt_sha256']:
        raise ValueError(REPAIR)
    return pointer


def list_baselines(project_path, project):
    governance.project_name(project)
    base = _root(project_path)
    names, stages = _entries(base)
    items = []
    for name in names:
        try:
            artifact = get(project_path, project, name)
            snapshot = artifact['snapshot']
            items.append(dict(name=name, at=snapshot['at'], source_project=snapshot['project'],
                              receipt_sha256=artifact['receipt_sha256'], readable=True))
        except ValueError:
            items.append(dict(name=name, readable=False, message=REPAIR))
    try:
        pointer, error = current(project_path, project), None
    except ValueError:
        pointer, error = None, REPAIR
    return dict(items=items, total=len(items), current=pointer, current_error=error,
                unfinished=len(stages), limit=schema.MAX_BASELINES)


def _publish_pointer(base, snapshot, receipt_sha256):
    wanted = dict(schema_version=1, name=snapshot['name'], receipt_sha256=receipt_sha256)
    existing = _pointer(base)
    if existing == wanted or existing != snapshot['previous_current']:
        # An old request replay never rewinds the last cut. Time is not order.
        return
    temporary = base / ('.pointer-' + secrets.token_hex(16) + '.tmp')
    try:
        _write_once(temporary, schema.json_bytes(wanted))
        os.replace(temporary, base / 'current.json')
    except OSError:
        raise ValueError(REPAIR) from None
    finally:
        # Only our own exact unique temporary; unlink never follows its target.
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mark_write(run):
    if hasattr(run, 'attempted_write'):
        run.attempted_write = True
    if hasattr(run, 'wrote'):
        run.wrote = True


def cut(project_path, context, name, operation_id, run, at, operators=None):
    """Caller holds project/effect locks and has freshly checked owner authority."""
    from requirement_http import OwnerContext
    name = schema.baseline_name(name); identifier(operation_id)
    if (not isinstance(context, OwnerContext) or not owner.HUMAN.fullmatch(context.account_id)
            or context.role != 'owner' or context.agent_id is not None
            or context.governance['mode'] != 'simple'
            or governance.current(project_path, context.project) != context.governance):
        raise ValueError('Owner baseline context is stale or unavailable')
    base = _root(project_path)
    names, stages = _entries(base)
    if base is not None:
        binding = _source(base, context.project)
        if binding is not None and set(binding['baselines']) - set(names):
            raise ValueError(REPAIR)
    previous = current(project_path, context.project)
    stage_name = '.pending-' + content_hash(dict(project=context.project, account_id=context.account_id,
                                               operation_id=operation_id))
    if name in names:
        artifact = get(project_path, context.project, name)
        snapshot = artifact['snapshot']
        if any(snapshot[key] != value for key, value in dict(project=context.project, account_id=context.account_id,
                                                            name=name, operation_id=operation_id).items()):
            raise ValueError(CONFLICT)
        _mark_write(run)
        _publish_pointer(base, snapshot, artifact['receipt_sha256'])
        return dict(name=name, receipt_sha256=artifact['receipt_sha256'], source_sha256=snapshot['source_sha256'],
                    snapshot_sha256=snapshot['sha256'], created=False)
    # The outer account-bound operation journal also binds the entire request.
    # Retain a local cross-name check so direct checked adapter calls cannot reuse
    # the same complete cut under another name.
    for existing_name in names:
        existing_path = _directory(base / existing_name)
        if existing_path is None:
            raise ValueError(REPAIR)
        entry = _json(_read(existing_path / 'receipt.json', MAX_RECEIPT_BYTES))
        # A readable canonical name is required BEFORE considering isolation.
        # Missing/ambiguous identity or a receipt naming the selected target is
        # never evidence that the damaged record belongs to another cut.
        if not isinstance(entry, dict) or entry.get('name') != existing_name:
            raise ValueError(REPAIR)
        # Damage cannot authorize a second name for a readable saved operation.
        # Check identity before any validation failure can isolate this receipt.
        if all(entry.get(key) == value for key, value in dict(project=context.project,
                account_id=context.account_id, operation_id=operation_id).items()):
            raise ValueError(CONFLICT)
        try:
            schema._closed(entry, schema.RECEIPT_FIELDS, 'receipt')
            if content_hash(entry) != entry['sha256']:
                raise ValueError(REPAIR)
        except (ValueError, TypeError, KeyError):
            continue
    if stage_name in stages:
        stage = _directory(base / stage_name)
        snapshot = schema.decode_snapshot(_read(stage / 'snapshot.json', schema.MAX_SNAPSHOT_BYTES))
        if any(snapshot[key] != value for key, value in dict(project=context.project, account_id=context.account_id,
                                                            name=name, operation_id=operation_id).items()):
            raise ValueError(CONFLICT)
    else:
        if len(names) + len(stages) >= schema.MAX_BASELINES:
            raise ValueError(FULL)
        snapshot = schema.build(context.project, name, context.account_id, operation_id,
                                schema.capture(project_path, context.project, run, operators), at, previous)
        # Verify both bounded artifacts BEFORE creating any sidecar path.
        schema.receipt(snapshot)
        _mark_write(run)
        base = _root(project_path, create=True)
        stage = _directory(base / stage_name, create=True)
        if hasattr(run, 'wrote'):
            run.wrote = True
        _write_once(stage / 'snapshot.json', schema.json_bytes(snapshot))
    markdown = _read(stage / 'BRD.md', schema.MAX_MARKDOWN_BYTES, missing=True)
    if markdown is None:
        markdown = render(snapshot['view'])
    receipt = schema.receipt(snapshot, markdown=markdown)
    _mark_write(run)
    _write_once(stage / 'BRD.md', markdown)
    _write_once(stage / 'receipt.json', schema.json_bytes(receipt))
    # Read back every exact file before publishing its directory or pointer.
    staged = dict(snapshot=schema.decode_snapshot(_read(stage / 'snapshot.json', schema.MAX_SNAPSHOT_BYTES)),
                  receipt=_json(_read(stage / 'receipt.json', MAX_RECEIPT_BYTES)))
    schema.validate_receipt(staged['receipt'], staged['snapshot'],
                            markdown=_read(stage / 'BRD.md', schema.MAX_MARKDOWN_BYTES))
    try:
        os.rename(stage, base / name)
    except OSError:
        raise ValueError(REPAIR) from None
    artifact = get(project_path, context.project, name)
    _publish_pointer(base, snapshot, artifact['receipt_sha256'])
    return dict(name=name, receipt_sha256=artifact['receipt_sha256'], source_sha256=snapshot['source_sha256'],
                snapshot_sha256=snapshot['sha256'], created=True)


def recoverable(project_path, context, name, operation_id):
    """Prove an unknown cut has a complete saved capture; never recapture it."""
    name = schema.baseline_name(name); identifier(operation_id)
    base = _root(project_path)
    if base is None:
        raise ValueError('No saved baseline capture was confirmed; keep the original operation unknown.')
    if _directory(base / name) is not None:
        snapshot = get(project_path, context.project, name)['snapshot']
    else:
        stage_name = '.pending-' + content_hash(dict(project=context.project,
            account_id=context.account_id, operation_id=operation_id))
        stage = _directory(base / stage_name)
        if stage is None:
            raise ValueError('No saved baseline capture was confirmed; keep the original operation unknown.')
        snapshot = schema.decode_snapshot(_read(stage / 'snapshot.json', schema.MAX_SNAPSHOT_BYTES))
    if any(snapshot[key] != value for key, value in dict(project=context.project,
            account_id=context.account_id, name=name, operation_id=operation_id).items()):
        raise ValueError(CONFLICT)
    return snapshot


def pending_backup_message(project_path, project):
    """Read-only bounded diagnostic; retry only the exact original owner request.

    Do not infer an HTTP idempotency key from an internal operation id, or read
    a possibly partial stage as if it were an authoritative new request.
    """
    governance.project_name(project)
    _, stages = _entries(_root(project_path))
    if not stages:
        return None
    command = ('python3 http_client.py --token="$ORCHESTRA_OWNER_TOKEN" call POST /v1/projects/' + project
               + '/requirements/baselines --body "$ORIGINAL_BODY"'
               + ' --idempotency-key "$ORIGINAL_KEY"')
    return ('Project ' + project + ': unfinished baseline stage ' + stages[0]
            + (' (and %d more)' % (len(stages) - 1) if len(stages) > 1 else '')
            + '. Retry as the original owner: ' + command)


def backup_files(project_path, project):
    base = _root(project_path)
    names, stages = _entries(base)
    if stages:
        raise ValueError(pending_backup_message(project_path, project))
    if not names:
        if base is not None and (_pointer(base) is not None
                or _read(base / 'source.json', MAX_RECEIPT_BYTES, missing=True) is not None):
            raise ValueError(REPAIR)
        return {}
    pointer = current(project_path, project)
    if pointer is None:
        raise ValueError('Finish the baseline current-pointer write before backing up this project.')
    result = {PREFIX + 'current.json': pointer}
    binding = _source(base, project)
    if binding:
        result[PREFIX + 'source.json'] = binding
    for name in names:
        artifact = get(project_path, project, name)
        for filename, value in (('snapshot.json', artifact['snapshot']), ('receipt.json', artifact['receipt']),
                                ('BRD.md', {'text': artifact['markdown'].decode('utf-8')})):
            result[PREFIX + name + '/' + filename] = value
    validate_files(result, project)
    return result


@schema._schema
def validate_files(files, project):
    if not isinstance(files, dict):
        raise ValueError(REPAIR)
    governance.project_name(project)
    family = {key: value for key, value in files.items() if isinstance(key, str) and key.startswith(PREFIX)}
    if not family:
        return
    names = set()
    for path in family:
        relative = path[len(PREFIX):]
        if relative in ('current.json', 'source.json'):
            continue
        parts = relative.split('/')
        if len(parts) != 2 or parts[1] not in FILES or parts[0] != schema.baseline_name(parts[0]):
            raise ValueError(REPAIR)
        names.add(parts[0])
    if not names or len(names) > schema.MAX_BASELINES:
        raise ValueError(REPAIR)
    binding = family.get(PREFIX + 'source.json')
    if PREFIX + 'source.json' in family:
        _source_record(binding, project)
        if set(binding['baselines']) - names:
            raise ValueError(REPAIR)
    receipts = {}
    for name in names:
        prefix = PREFIX + name + '/'
        snapshot = schema.validate_snapshot(family[prefix + 'snapshot.json'])
        markdown = family[prefix + 'BRD.md']
        schema._closed(markdown, {'text'}, 'Markdown file')
        if not isinstance(markdown['text'], str):
            raise ValueError(REPAIR)
        receipt = schema.validate_receipt(family[prefix + 'receipt.json'], snapshot,
                                          markdown=markdown['text'].encode('utf-8'))
        if snapshot['name'] != name:
            raise ValueError(REPAIR)
        digest = schema.byte_hash(schema.json_bytes(receipt)); receipts[name] = digest
        _belongs(dict(snapshot=snapshot, receipt_sha256=digest), project, binding)
    pointer = schema.validate_pointer(family[PREFIX + 'current.json'])
    if pointer['name'] not in receipts or receipts[pointer['name']] != pointer['receipt_sha256']:
        raise ValueError(REPAIR)


def restored_files(files, source, destination):
    """A new source binding; named snapshot/Markdown/receipt bytes never change."""
    validate_files(files, source)
    result = dict(files)
    names = {path[len(PREFIX):].split('/')[0] for path in files if path.startswith(PREFIX)
             and path.endswith('/snapshot.json')}
    if names and source != destination:
        binding = dict(schema_version=1, project=destination, restored_from=source, baselines={
            name: dict(project=files[PREFIX + name + '/snapshot.json']['project'],
                       receipt_sha256=schema.byte_hash(schema.json_bytes(files[PREFIX + name + '/receipt.json'])))
            for name in sorted(names)})
        binding['sha256'] = content_hash(binding)
        result[PREFIX + 'source.json'] = binding
    validate_files(result, destination)
    return result


def validate_backup(files, project=None):
    """Bind the frozen family to the same live project as governance sidecars."""
    if not any(isinstance(path, str) and path.startswith(PREFIX) for path in files):
        return
    governance.validate_files(files, project)
    if governance.FILE not in files:
        raise ValueError('Requirements baselines need their current governance history in the backup.')
    binding = files.get(governance.SOURCE_FILE)
    current_project = binding['project'] if binding else files[governance.FILE]['project']
    validate_files(files, current_project)


def restore_files(project_path, files, project):
    """Fresh/identical restore, with all reads blocked until exact retry finishes.

    Caller holds the canonical project lock. A different target or interrupted
    source cannot overwrite installed immutable history. No recursive cleanup.
    """
    project_path = Path(project_path)
    validate_files(files, project)
    family = {key: value for key, value in files.items() if key.startswith(PREFIX)}
    marker = project_path / RESTORE_MARKER
    if _directory(project_path) is None:
        raise ValueError(REPAIR)
    if not family:
        if os.path.lexists(project_path / DIRECTORY) or os.path.lexists(marker):
            raise ValueError('A baseline family already exists at this restore destination. Reconcile it before restoring a backup without baselines.')
        return False
    # Validate all existing target directories and paths before our marker or
    # file write. Partial restore files must be identical, never overwritten.
    base = _directory(project_path / DIRECTORY)
    names, stages = _entries(base)
    expected = {key[len(PREFIX):].split('/')[0] for key in family if key.endswith('/snapshot.json')}
    if stages or set(names) - expected:
        raise ValueError(REPAIR)
    encoded = {key: value['text'].encode('utf-8') if key.endswith('/BRD.md')
               else schema.json_bytes(value) for key, value in family.items()}
    for name in names:
        _directory(base / name)
    for key, raw in encoded.items():
        target = project_path / key
        if target.parent != project_path / DIRECTORY:
            if base is None or _directory(target.parent) is None:
                continue
        prior = _read(target, len(raw), missing=True)
        if prior is not None and prior != raw:
            raise ValueError('The restore destination holds different immutable baseline data. Use a fresh destination.')
    identity = dict(schema_version=1, project=project, files={
        key: schema.byte_hash(raw) for key, raw in sorted(encoded.items())})
    _write_once(marker, schema.json_bytes(identity))
    base = _directory(project_path / DIRECTORY, create=True)
    for name in sorted(expected):
        _directory(base / name, create=True)
    # Named files before source binding, binding before current; marker prevents
    # a member observing an old passing pointer during any interrupted step.
    for key in sorted(encoded, key=lambda key: (2 if key.endswith('/current.json') else 1 if key.endswith('/source.json') else 0, key)):
        _write_once(project_path / key, encoded[key])
    for key, raw in encoded.items():
        if _read(project_path / key, len(raw)) != raw:
            raise ValueError(REPAIR)
    # A path/hash proof above is complete before the marker is removed.
    try:
        marker.unlink()
    except OSError:
        raise ValueError(REPAIR) from None
    return True
