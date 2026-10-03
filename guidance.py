"""Versioned, operator-set, per-project guidance for every actor run.

Guidance is an INSTRUCTION channel to agents, so its only writer is the operator
host command (``admin.py set-guidance``), which checks the deployment operator
allowlist before it writes. The endpoint can only read it and record that an
actor has acknowledged a version; contributor-written text (task titles,
proposal text, capability summaries) can never reach this file. The text is
attributed to a setter only when the stored version is the hash of the exact text
read, so a hand edit or a crash between the two writes is reported as a mismatch
instead of being credited to the previous setter.

Storage is two files in the project directory, deliberately separate from the
onboarding entry point (``ONBOARDING.md``):

* ``GUIDANCE.md`` - the bounded plain text. The version is the SHA-256 of its
  exact UTF-8 bytes, computed by every reader.
* ``.guidance.json`` - the audit record: version, set_by, set_at, the previous
  version and text, a bounded history of earlier versions, and one
  acknowledgement per actor.

Every reader is tolerant of an older kit's project: no ``GUIDANCE.md`` means
"no guidance set", never an error, and unknown metadata keys are ignored. A
missing or mismatched audit record still leaves the text readable, with no setter
attribution and a warning, and an operator set with the same text repairs it.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from coordination import atomic

GUIDANCE_NAME = 'GUIDANCE.md'
META_NAME = '.guidance.json'
LIMIT = 8000
HISTORY_LIMIT = 50
ACK_LIMIT = 500
VERSION = re.compile(r'[a-f0-9]{64}')
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
# Plain text only: no NUL or other C0 control characters except tab/newline/carriage
# return, and no DEL. A binary or terminal-control payload is refused rather than
# stored as instructions.
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
# C1 controls, bidi embedding/override controls and zero-width/invisible characters
# are refused too (kittrial-5bb.99 review `small` 7): they render as harmless text
# while reordering or hiding what a reader sees.
INVISIBLE = re.compile('[\u0080-\u009f\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e'
                       '\u2060-\u2064\u2066-\u2069\ufeff]')
STAMP = re.compile(r'[0-9A-Za-z:+.\- ]{1,64}')
META_FIELDS = {'schema_version', 'version', 'set_by', 'set_at', 'previous_version',
               'previous_text', 'history', 'acknowledged', 'acks_compacted_by',
               'acks_compacted_at'}
HISTORY_FIELDS = {'version', 'set_by', 'set_at', 'previous_version'}


def version_of(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def validate_actor(actor):
    if not isinstance(actor, str) or not ACTOR.fullmatch(actor):
        raise ValueError('Guidance needs a valid actor identity')
    return actor


def _valid_actor(actor):
    return isinstance(actor, str) and bool(ACTOR.fullmatch(actor))


def _valid_stamp(value):
    """A bounded single-line timestamp (no newline, no control or invisible text)."""
    return isinstance(value, str) and bool(STAMP.fullmatch(value))


def validate_text(text):
    """A bounded, nonempty, plain-text guidance payload."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Guidance must be nonempty UTF-8 text')
    if CONTROL.search(text):
        raise ValueError('Guidance must be plain text (no control characters)')
    if INVISIBLE.search(text):
        raise ValueError('Guidance must be plain text (no bidi, zero-width or C1 characters)')
    if len(text.encode('utf-8')) > LIMIT:
        raise ValueError('Guidance exceeds %d bytes; shorten it or keep the long material in the '
                         'project onboarding entry point' % LIMIT)
    return text


def _paths(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Guidance path must not be a symlink')
    text = path / GUIDANCE_NAME
    meta = path / META_NAME
    for candidate in (text, meta, meta.with_suffix('.tmp')):
        if candidate.is_symlink():
            raise ValueError('Guidance paths must not be symlinks')
    return text, meta


def read_text(path):
    """The current guidance text, or None when the project has none.

    Over-limit or undecodable text is refused, never truncated: a worker must not
    follow a partial instruction. Errors never carry a server path.
    """
    text, _ = _paths(path)
    if not text.exists():
        return None
    try:
        data = text.read_bytes()
    except OSError:
        raise ValueError('The guidance file could not be read (a directory in its place, or '
                         'permissions); ask the operator to check the guidance files') from None
    if len(data) > LIMIT * 4:
        raise ValueError('Guidance is far over its %d-byte limit; ask the operator to shorten it' % LIMIT)
    try:
        value = data.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Guidance is not valid UTF-8 text; ask the operator to reinstall it') from None
    validate_text(value)
    return value


def _valid_meta(value):
    """Tolerant shape check: the fields every reader needs, unknown keys ignored."""
    if not isinstance(value, dict):
        return False
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        return False
    if not isinstance(value.get('version'), str) or not VERSION.fullmatch(value['version']):
        return False
    return True


def read_meta(path):
    """The audit metadata, or None when it is absent or unreadable.

    A reader never fails on a missing, malformed or deeply nested metadata file:
    it still has the text and its computed version. ``validate_meta`` is the strict
    form used by the writer and by backup validation.
    """
    _, meta = _paths(path)
    if not meta.exists():
        return None
    try:
        value = json.loads(meta.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return value if _valid_meta(value) else None


def validate_meta(meta):
    """Strict validation of a record this kit wrote (writer and backup path)."""
    if not isinstance(meta, dict):
        raise ValueError('Invalid guidance record')
    missing = sorted({'schema_version', 'version', 'set_by', 'set_at', 'previous_version', 'history',
                      'acknowledged'} - set(meta))
    if missing:
        raise ValueError('Invalid guidance record: missing fields ' + ', '.join(missing))
    unknown = sorted(set(meta) - META_FIELDS)
    if unknown:
        raise ValueError('Invalid guidance record: unknown fields ' + ', '.join(unknown))
    if type(meta['schema_version']) is not int or meta['schema_version'] != 1:
        raise ValueError('Invalid guidance record: schema_version must be integer 1')
    if not isinstance(meta['version'], str) or not VERSION.fullmatch(meta['version']):
        raise ValueError('Invalid guidance record: version must be a sha256 hex digest')
    if not _valid_actor(meta['set_by']):
        raise ValueError('Invalid guidance record: set_by must be an actor identity')
    if not _valid_stamp(meta['set_at']):
        raise ValueError('Invalid guidance record: set_at must be a bounded single-line timestamp')
    previous = meta['previous_version']
    if previous is not None and (not isinstance(previous, str) or not VERSION.fullmatch(previous)):
        raise ValueError('Invalid guidance record: previous_version')
    if meta.get('previous_text') is not None:
        validate_text(meta['previous_text'])
    history = meta['history']
    if not isinstance(history, list) or len(history) > HISTORY_LIMIT:
        raise ValueError('Invalid guidance record: history must be a list of at most %d entries' % HISTORY_LIMIT)
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != HISTORY_FIELDS:
            raise ValueError('Invalid guidance record: malformed history entry')
        if not isinstance(entry['version'], str) or not VERSION.fullmatch(entry['version']):
            raise ValueError('Invalid guidance record: malformed history version')
        if entry['set_by'] is not None and not _valid_actor(entry['set_by']):
            raise ValueError('Invalid guidance record: malformed history set_by')
        if entry['set_at'] is not None and not _valid_stamp(entry['set_at']):
            raise ValueError('Invalid guidance record: malformed history set_at')
        if entry['previous_version'] is not None and (not isinstance(entry['previous_version'], str)
                                                      or not VERSION.fullmatch(entry['previous_version'])):
            raise ValueError('Invalid guidance record: malformed history previous_version')
    acknowledged = meta['acknowledged']
    if not isinstance(acknowledged, dict) or len(acknowledged) > ACK_LIMIT:
        raise ValueError('Invalid guidance record: acknowledged must be a map of at most %d actors' % ACK_LIMIT)
    for actor, entry in acknowledged.items():
        if not isinstance(actor, str) or not ACTOR.fullmatch(actor) or not isinstance(entry, dict):
            raise ValueError('Invalid guidance record: malformed acknowledgement')
        if set(entry) != {'version', 'acknowledged_at'}:
            raise ValueError('Invalid guidance record: malformed acknowledgement fields')
        if not isinstance(entry['version'], str) or not VERSION.fullmatch(entry['version']):
            raise ValueError('Invalid guidance record: malformed acknowledgement version')
        if not _valid_stamp(entry['acknowledged_at']):
            raise ValueError('Invalid guidance record: malformed acknowledgement time')
    if ('acks_compacted_by' in meta) != ('acks_compacted_at' in meta):
        raise ValueError('Invalid guidance record: compaction audit needs both fields')
    if 'acks_compacted_by' in meta:
        if not _valid_actor(meta['acks_compacted_by']) or not _valid_stamp(meta['acks_compacted_at']):
            raise ValueError('Invalid guidance record: compaction audit')
    return meta


def write_text(target, text):
    """Atomic sibling-temporary write of the guidance text (also used by restore)."""
    target = Path(target)
    if target.is_symlink():
        raise ValueError('Guidance path must not be a symlink')
    fd, tmp = tempfile.mkstemp(prefix='.' + target.name + '.', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _acknowledgement(meta, actor):
    if not isinstance(meta, dict) or not isinstance(actor, str):
        return None
    table = meta.get('acknowledged')
    if not isinstance(table, dict):
        return None
    entry = table.get(actor)
    return entry if isinstance(entry, dict) else None


def _attribution(meta, current):
    """(set_by, set_at, bound, warning) for text whose exact hash is ``current``.

    The record is bound only when its stored version is that hash; otherwise the
    text is not attributed to a setter, because the metadata demonstrably describes
    a different generation (a hand edit or a crash between the two writes).
    """
    if not isinstance(meta, dict):
        return None, None, False, 'Guidance has no readable audit record; ask the operator to set it again'
    if meta.get('version') != current:
        return None, None, False, ('Guidance audit metadata records a different version than the text; the record '
                                   'is unbound. Ask the operator to set the guidance again (a set with the same '
                                   'text repairs it)')
    set_by = meta.get('set_by') if _valid_actor(meta.get('set_by')) else None
    set_at = meta.get('set_at') if _valid_stamp(meta.get('set_at')) else None
    warning = None
    if set_by is None or set_at is None:
        warning = ('Guidance audit metadata does not carry a valid setter and time for this text; treat it as '
                   'unattributed and ask the operator to set it again')
    return set_by, set_at, True, warning


def _history_entries(meta):
    """Structurally valid history entries from a metadata record (best effort)."""
    out = []
    if isinstance(meta, dict) and isinstance(meta.get('history'), list):
        for entry in meta['history']:
            if not isinstance(entry, dict) or set(entry) != HISTORY_FIELDS:
                continue
            if not isinstance(entry.get('version'), str) or not VERSION.fullmatch(entry['version']):
                continue
            if entry['set_by'] is not None and not _valid_actor(entry['set_by']):
                continue
            if entry['set_at'] is not None and not _valid_stamp(entry['set_at']):
                continue
            if entry['previous_version'] is not None and (not isinstance(entry['previous_version'], str)
                                                          or not VERSION.fullmatch(entry['previous_version'])):
                continue
            out.append(dict(entry))
    return out


def _prune_acks(meta, keep):
    """Ack entries whose version is still current or previous, and that validate."""
    table = meta.get('acknowledged') if isinstance(meta, dict) else None
    out = {}
    if isinstance(table, dict):
        for name, entry in table.items():
            if not _valid_actor(name) or not isinstance(entry, dict):
                continue
            if set(entry) != {'version', 'acknowledged_at'}:
                continue
            if (not isinstance(entry.get('version'), str) or not VERSION.fullmatch(entry['version'])
                    or entry['version'] not in keep or not _valid_stamp(entry.get('acknowledged_at'))):
                continue
            out[name] = {'version': entry['version'], 'acknowledged_at': entry['acknowledged_at']}
    return out


def _unreadable(message):
    return {'schema_version': 1, 'present': None, 'version': None, 'set_at': None, 'set_by': None,
            'text': None, 'changed': False, 'since': None, 'since_known': False,
            'previous_version': None, 'previous_text': None, 'acknowledged': None,
            'attention': True, 'unreadable': True,
            'warning': message + ' Ask the operator to check the guidance files; keep your last acknowledged '
                                 'version and report the failure.'}


def state(path, actor=None):
    """The version block shared by the read, brief, work and resume responses.

    ``attention`` is true exactly when guidance is set (or cannot be read) and the
    calling actor has not acknowledged the current version, so a coordinator's new
    guidance is visible on the next run without any push or polling. A mismatched
    audit record is reported in ``warning`` and never attributes the text to a
    setter the metadata does not bind to it.
    """
    try:
        text = read_text(path)
    except ValueError as error:
        return _unreadable(str(error))
    meta = read_meta(path) if text is not None else None
    current = version_of(text) if text is not None else None
    meta_version = meta.get('version') if isinstance(meta, dict) else None
    set_by, set_at, bound, warning = _attribution(meta, current) if text is not None else (None, None, False, None)
    entry = _acknowledgement(meta, actor)
    acknowledged = bool(entry) and entry.get('version') == current
    result = {'schema_version': 1, 'present': text is not None, 'version': current,
              'set_at': set_at, 'set_by': set_by,
              'previous_version': (meta.get('previous_version') if bound else None),
              'meta_version': meta_version,
              'acknowledged': acknowledged if current is not None else None,
              'acknowledged_version': entry.get('version') if entry else None,
              'acknowledged_at': entry.get('acknowledged_at') if entry else None,
              'attention': bool(current is not None and not acknowledged)}
    if warning:
        result['warning'] = warning
    if result['attention']:
        result['next_action'] = 'guidance get'
    return result


def read(path, args, actor=None):
    """``guidance get [--since VERSION]``: the text plus what changed since a version."""
    since = None
    index = 0
    if args[:1] == ['get']:
        args = args[1:]
    if args[:1] == ['version']:
        if len(args) != 1:
            raise ValueError('Use guidance version without arguments')
        return state(path, actor)
    while index < len(args):
        token = args[index]
        if token == '--since' or token.startswith('--since='):
            if since is not None:
                raise ValueError('Give --since at most once')
            if token == '--since':
                index += 1
                if index >= len(args):
                    raise ValueError('--since needs a version')
                since = args[index]
            else:
                since = token.split('=', 1)[1]
        else:
            raise ValueError('Use guidance get [--since VERSION], guidance version, guidance ack or guidance status')
        index += 1
    if since is not None and not VERSION.fullmatch(since):
        raise ValueError('--since must be a guidance version (sha256 hex digest)')
    try:
        text = read_text(path)
    except ValueError as error:
        result = _unreadable(str(error))
        result['since'] = since
        return result
    if text is None:
        return {'schema_version': 1, 'present': False, 'version': None, 'set_at': None, 'set_by': None,
                'text': None, 'changed': False, 'since': since, 'since_known': since is None,
                'previous_version': None, 'previous_text': None, 'acknowledged': None,
                'attention': False, 'unreadable': False}
    current = version_of(text)
    meta = read_meta(path)
    set_by, set_at, bound, warning = _attribution(meta, current)
    previous = meta.get('previous_version') if bound else None
    known = {current}
    if bound:
        if isinstance(previous, str):
            known.add(previous)
        known.update(entry['version'] for entry in _history_entries(meta))
    since_known = since is None or since in known
    changed = since is not None and since != current
    entry = _acknowledgement(meta, actor)
    result = {'schema_version': 1, 'present': True, 'version': current,
              'set_at': set_at, 'set_by': set_by, 'text': text, 'changed': changed,
              'since': since, 'since_known': since_known,
              'meta_version': meta.get('version') if isinstance(meta, dict) else None,
              'previous_version': previous,
              # "What changed since a version": the immediately previous text is kept
              # beside its version, so a caller that names it gets the exact prior text.
              'previous_text': (meta.get('previous_text') if bound and since == previous else None),
              'acknowledged': bool(entry) and entry.get('version') == current,
              'attention': not bool(entry) or entry.get('version') != current,
              'unreadable': False}
    if warning:
        result['warning'] = warning
    elif since is not None and not since_known:
        result['warning'] = ('The version given to --since is not the current version, the previous version or a '
                             'recorded history version; the full current guidance is shown instead')
    return result


def write_guidance(path, text, actor, now=None):
    """The operator write. Returns the new audit summary.

    Idempotent on unchanged text, but a missing or mismatched audit record is
    repaired even when the text is unchanged (``repaired: true``), so a crash
    between the text write and the metadata write recovers by setting the same
    text again. Acknowledgement entries for versions other than the current and
    previous one are dropped at every set.
    """
    validate_text(text)
    validate_actor(actor)
    target, meta_path = _paths(path)
    old_text = read_text(path)
    old_meta = read_meta(path)
    current = version_of(text)
    previous = version_of(old_text) if old_text is not None else None
    old_bound = old_text is not None and isinstance(old_meta, dict) and old_meta.get('version') == previous
    old_attributed = old_bound and _valid_actor(old_meta.get('set_by')) and _valid_stamp(old_meta.get('set_at'))
    needs_repair = old_text is not None and not old_attributed
    if previous == current and old_attributed:
        return {'version': current, 'set_by': old_meta['set_by'], 'set_at': old_meta['set_at'],
                'previous_version': old_meta.get('previous_version'), 'changed': False, 'repaired': False}
    stamp = now or datetime.now(timezone.utc).isoformat()
    history = _history_entries(old_meta)
    if old_text is not None and previous != current:
        # Only a record bound to the old text may be credited to its setter; a
        # crashed or hand-edited generation is kept without an operator attribution.
        # A same-text repair records no history entry: the repaired text IS the new
        # version, so there is no superseded version to keep.
        history.append({'version': previous,
                        'set_by': old_meta['set_by'] if old_attributed else None,
                        'set_at': old_meta['set_at'] if old_attributed else None,
                        'previous_version': old_meta.get('previous_version') if old_attributed else None})
    meta = {'schema_version': 1, 'version': current, 'set_by': actor, 'set_at': stamp,
            'previous_version': previous,
            'previous_text': old_text,
            'history': history[-HISTORY_LIMIT:],
            'acknowledged': _prune_acks(old_meta, {current, previous})}
    validate_meta(meta)
    # The text is written first: a crash before the metadata replace leaves the new
    # version already authoritative (readers derive it from the text) with stale
    # audit fields, which a set with the same text repairs (see above).
    write_text(target, text)
    atomic(meta_path, meta)
    return {'version': current, 'set_by': actor, 'set_at': stamp,
            'previous_version': previous if old_attributed else None,
            'changed': previous != current, 'repaired': needs_repair}


def acknowledge(path, actor, version=None):
    """Record that one actor has read the current guidance version.

    The caller must name the version it read; a version that is no longer current
    is refused, naming the current one, so an actor cannot mark a version it never
    read as picked up. The table is bounded: when it is full the oldest entry is
    evicted rather than refusing a new lane.
    """
    validate_actor(actor)
    text = read_text(path)
    if text is None:
        raise ValueError('No guidance is set for this project yet; nothing to acknowledge')
    current = version_of(text)
    if version is None:
        raise ValueError('Name the guidance version you read: use `guidance ack --version VERSION`. '
                         'The current version is %s' % current)
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('A guidance version is a sha256 hex digest; the current version is %s' % current)
    if version != current:
        raise ValueError('Guidance version %s is not current; the current version is %s. Read the current '
                         'guidance first.' % (version, current))
    meta = read_meta(path)
    if meta is None:
        raise ValueError('Guidance audit metadata is missing or unreadable; ask the operator to set the '
                         'guidance again before acknowledging it (a set with the same text repairs it)')
    if meta.get('version') != current:
        raise ValueError('Guidance audit metadata does not match the text; ask the operator to set the guidance '
                         'again (a set with the same text repairs it) before acknowledging it')
    table = meta.get('acknowledged')
    if not isinstance(table, dict):
        raise ValueError('Guidance audit metadata has no acknowledgement table; ask the operator to set the '
                         'guidance again')
    table = _prune_acks(meta, {current, meta.get('previous_version')})
    stamp = datetime.now(timezone.utc).isoformat()
    prior = table.get(actor)
    if isinstance(prior, dict) and prior.get('version') == current:
        return {'acknowledged': True, 'version': current, 'acknowledged_at': prior.get('acknowledged_at'),
                'reconciled': True}
    if actor not in table and len(table) >= ACK_LIMIT:
        # Evict the oldest acknowledgement instead of refusing a new lane; the
        # operator can also compact the table (admin.py compact-guidance-acks).
        oldest = sorted(table, key=lambda name: (table[name].get('acknowledged_at') or '', name))[0]
        table.pop(oldest, None)
    table[actor] = {'version': current, 'acknowledged_at': stamp}
    meta['acknowledged'] = table
    validate_meta(meta)
    atomic(path / META_NAME, meta)
    return {'acknowledged': True, 'version': current, 'acknowledged_at': stamp, 'reconciled': False}


def compact(path, actor):
    """Operator host route: drop acknowledgements for versions no longer current or
    previous, and record who compacted the table and when (the audit trail)."""
    validate_actor(actor)
    text = read_text(path)
    meta = read_meta(path)
    if text is None or meta is None:
        raise ValueError('No readable guidance record to compact')
    current = version_of(text)
    if meta.get('version') != current:
        raise ValueError('Guidance audit metadata does not match the text; ask the operator to set the guidance '
                         'again before compacting acknowledgements')
    previous = meta.get('previous_version')
    table = meta.get('acknowledged')
    kept = _prune_acks(meta, {current, previous})
    removed = sorted(set(table) - set(kept)) if isinstance(table, dict) else []
    meta['acknowledged'] = kept
    meta['acks_compacted_by'] = actor
    meta['acks_compacted_at'] = datetime.now(timezone.utc).isoformat()
    validate_meta(meta)
    atomic(path / META_NAME, meta)
    return {'removed': removed, 'removed_total': len(removed), 'kept': sorted(kept),
            'compacted_by': actor}


def clear(path, actor):
    """Operator host route: remove the guidance text and its audit record entirely."""
    validate_actor(actor)
    text, meta = _paths(path)
    removed = []
    for target in (text, meta, meta.with_suffix('.tmp')):
        if target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                raise ValueError('The guidance file could not be removed; ask the operator to check permissions')
            removed.append(target.name)
    return {'removed': sorted(set(removed)), 'cleared_by': actor}


def status(path, actor, operators=None):
    """Operator read: which lanes have acknowledged which version.

    Gated by the configured-operator allowlist, but the calling actor is
    self-declared (see docs/CLI_CONTRACT.md): the authoritative operator read is
    the host command ``admin.py guidance-status``.
    """
    from keyed_records import require_configured_operator
    require_configured_operator(actor, operators, 'read the guidance acknowledgement status')
    text = read_text(path)
    meta = read_meta(path) if text is not None else None
    current = version_of(text) if text is not None else None
    set_by, set_at, bound, warning = _attribution(meta, current) if text is not None else (None, None, False, None)
    previous = meta.get('previous_version') if bound else None
    table = meta.get('acknowledged') if isinstance(meta, dict) else None
    rows = []
    if isinstance(table, dict):
        for name, entry in sorted(table.items()):
            if not _valid_actor(name) or not isinstance(entry, dict) or set(entry) != {'version', 'acknowledged_at'}:
                continue
            if not isinstance(entry.get('version'), str) or not VERSION.fullmatch(entry['version']):
                continue
            rows.append({'actor': name, 'version': entry.get('version'),
                         'acknowledged_at': entry.get('acknowledged_at'),
                         'current': entry.get('version') == current})
    up_to_date = sorted(row['actor'] for row in rows if row['current'])
    behind = sorted(row['actor'] for row in rows if not row['current'] and row['version'] == previous)
    stale = sorted(row['actor'] for row in rows if not row['current'] and row['version'] != previous)
    result = {'schema_version': 1, 'present': text is not None, 'version': current,
              'set_by': set_by, 'set_at': set_at,
              'meta_version': meta.get('version') if isinstance(meta, dict) else None,
              'previous_version': previous,
              'previous_text': meta.get('previous_text') if bound else None,
              'history': meta.get('history') if bound else [],
              'acknowledged': rows, 'acknowledged_total': len(rows),
              'up_to_date': up_to_date, 'behind': behind, 'stale': stale,
              'compact_hint': ('Acks for versions other than the current and previous one are stale; the '
                               'operator can drop them with `admin.py compact-guidance-acks PROJECT --actor OPERATOR`.'
                               if stale else None)}
    if 'acks_compacted_by' in (meta or {}):
        result['acks_compacted_by'] = meta.get('acks_compacted_by')
        result['acks_compacted_at'] = meta.get('acks_compacted_at')
    if warning:
        result['warning'] = warning
    return result


def brief_block(path, actor=None):
    """The compact version block for brief/work/resume; never raises on old projects."""
    if path is None:
        return None
    try:
        return state(path, actor)
    except (OSError, ValueError, RecursionError):
        # Guidance is an instruction channel: if it cannot be read reliably, say so
        # (and keep attention true) instead of silently reporting "no guidance".
        return _unreadable('Guidance could not be read.')
