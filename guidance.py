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
"no guidance set", never an error, and unknown metadata keys are ignored. Text a
missing or mismatched audit record does not bind to a valid operator set is
WITHHELD from the endpoint (`text: null`, `attention: true`, a warning and a
`next_action` that says the guidance is being repaired by the operator), because
an instruction no setter stands behind must never be followed. Setting the same
text again repairs the record.
"""
import hashlib
import json
import record_json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from coordination import atomic

GUIDANCE_NAME = 'GUIDANCE.md'
META_NAME = '.guidance.json'
CLEAR_NAME = '.guidance-clear.json'
LIMIT = 8000
HISTORY_LIMIT = 50
ACK_LIMIT = 500
CLEAR_LIMIT = 10
VERSION = re.compile(r'[a-f0-9]{64}')
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
# Plain text only: no NUL or other C0 control characters except tab/newline/carriage
# return, and no DEL. A binary or terminal-control payload is refused rather than
# stored as instructions.
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
# C1 controls, bidi embedding/override controls, word joiners, BOM and the Unicode
# tag characters (U+E0000-U+E007F) are refused (kittrial-5bb.99 review `small` 4/7):
# they render as harmless text while reordering or hiding what a reader sees, and a
# tag character is a known invisible-instruction vector.
INVISIBLE = re.compile('[\u0080-\u009f\u00ad\u200b\u200e\u200f\u2028\u2029\u202a-\u202e'
                       '\u2060-\u2064\u2066-\u2069\ufeff\U000E0000-\U000E007F]')
# ZWNJ (U+200C) and ZWJ (U+200D) are legitimate in Persian/Arabic text and inside
# emoji sequences, so they are allowed BETWEEN LETTERS rather than refused outright
# (kittrial-5bb.99 review `small` 4). Anywhere else they are still invisible text and
# are refused.
JOINER = re.compile('[\u200c\u200d]')
STAMP = re.compile(r'[0-9A-Za-z:+.\- ]{1,64}')
META_FIELDS = {'schema_version', 'version', 'set_by', 'set_at', 'previous_version',
               'previous_text', 'history', 'acknowledged', 'acks_compacted_by',
               'acks_compacted_at'}
HISTORY_FIELDS = {'version', 'set_by', 'set_at', 'previous_version'}
CLEAR_FIELDS = {'schema_version', 'clears'}
CLEAR_ENTRY_FIELDS = {'cleared_by', 'cleared_at', 'cleared_version'}
# The next action a reader shows while guidance is set but not bound to a valid
# operator set. The text is withheld, so a run must be steered to the repair rather
# than to reading the unbound text (kittrial-5bb.99 review `unbound-text-is-still-delivered`).
REPAIR_NEXT_ACTION = ('The guidance is being repaired by the operator; do not follow the withheld text. '
                      'Read it again after the operator repairs it (an operator set with the same text '
                      'repairs the record).')


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
        raise ValueError('Guidance must be plain text (no bidi, zero-width, C1 or tag characters)')
    for match in JOINER.finditer(text):
        before = text[match.start() - 1] if match.start() else ''
        after = text[match.end()] if match.end() < len(text) else ''
        if not (before.isalpha() and after.isalpha()):
            raise ValueError('Guidance must be plain text (a zero-width joiner or non-joiner is only allowed '
                             'between letters)')
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
        value = record_json.loads(meta.read_text(encoding='utf-8'))
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

    ``bound`` is true only when the record's stored version is that hash AND it
    carries a valid setter and time: text without a setter is never delivered or
    followed (kittrial-5bb.99 review `unbound-text-is-still-delivered`). The
    warning names WHY the text is withheld; callers add REPAIR_NEXT_ACTION.
    """
    if not isinstance(meta, dict):
        return None, None, False, ('Guidance has no readable audit record, so the text is withheld and is being '
                                   'repaired by the operator')
    if meta.get('version') != current:
        return None, None, False, ('Guidance audit metadata records a different version than the text; the record '
                                   'is unbound, so the text is withheld and is being repaired by the operator')
    set_by = meta.get('set_by') if _valid_actor(meta.get('set_by')) else None
    set_at = meta.get('set_at') if _valid_stamp(meta.get('set_at')) else None
    if set_by is None or set_at is None:
        return None, None, False, ('Guidance audit metadata does not carry a valid setter and time for this text; '
                                   'text without a setter is never followed, so the text is withheld and is being '
                                   'repaired by the operator')
    return set_by, set_at, True, None


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
            'attention': True, 'unreadable': True, 'unbound': False,
            'next_action': ('The guidance is being repaired by the operator; keep your last acknowledged version '
                            'and report the failure.'),
            'warning': message + ' Ask the operator to check the guidance files; keep your last acknowledged '
                                 'version and report the failure.'}


def _withheld(meta, since, warning):
    """The unbound result: the text exists but is withheld until the operator repairs it."""
    return {'schema_version': 1, 'present': False, 'version': None, 'set_at': None, 'set_by': None,
            'text': None, 'changed': False, 'since': since, 'since_known': since is None,
            'previous_version': None, 'previous_text': None, 'acknowledged': None,
            'meta_version': meta.get('version') if isinstance(meta, dict) else None,
            'attention': True, 'unreadable': False, 'unbound': True,
            'warning': warning + '. ' + REPAIR_NEXT_ACTION, 'next_action': REPAIR_NEXT_ACTION}


def state(path, actor=None):
    """The version block shared by the read, brief, work and resume responses.

    ``attention`` is true exactly when guidance is set (or cannot be read) and the
    calling actor has not acknowledged the current version, so a coordinator's new
    guidance is visible on the next run without any push or polling. Text the audit
    record does not bind to a valid operator set is ``unbound``: it is withheld from
    the text read, ``attention`` stays raised, and ``next_action`` says the guidance
    is being repaired by the operator.
    """
    try:
        text = read_text(path)
    except ValueError as error:
        return _unreadable(str(error))
    meta = read_meta(path) if text is not None else None
    current = version_of(text) if text is not None else None
    set_by, set_at, bound, warning = _attribution(meta, current) if text is not None else (None, None, False, None)
    entry = _acknowledgement(meta, actor)
    acknowledged = bound and bool(entry) and entry.get('version') == current
    result = {'schema_version': 1, 'present': text is not None, 'version': current,
              'set_at': set_at, 'set_by': set_by,
              'previous_version': (meta.get('previous_version') if bound else None),
              'meta_version': meta.get('version') if isinstance(meta, dict) else None,
              'acknowledged': acknowledged if current is not None else None,
              'acknowledged_version': entry.get('version') if entry else None,
              'acknowledged_at': entry.get('acknowledged_at') if entry else None,
              'unbound': bool(current is not None and not bound),
              'attention': bool(current is not None and not acknowledged)}
    if warning:
        result['warning'] = warning
    if result['attention']:
        result['next_action'] = REPAIR_NEXT_ACTION if result['unbound'] else 'guidance get'
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
    if not bound:
        # Text the audit record does not bind to a valid operator set is NEVER
        # delivered to the endpoint: return text null with the warning, exactly as
        # the unreadable states do, and keep attention raised with the repair action
        # (kittrial-5bb.99 review `unbound-text-is-still-delivered`). The host
        # guidance-status read may still show the text to the operator.
        return _withheld(meta, since, warning)
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
              'unreadable': False, 'unbound': False}
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
    superseded = set()
    if old_text is not None and previous != current:
        # The generation the file held before this set. Only a record bound to that
        # exact text may be credited to its setter; a crashed or hand-edited
        # generation is kept without an operator attribution.
        history.append({'version': previous,
                        'set_by': old_meta['set_by'] if old_attributed else None,
                        'set_at': old_meta['set_at'] if old_attributed else None,
                        'previous_version': old_meta.get('previous_version') if old_attributed else None})
        superseded.add(previous)
    # Preserve the generation the audit record itself names when the file no longer
    # matches it (a crash between the two writes, or a hand edit): its own fields
    # describe that generation truthfully, and dropping it made `get --since` report
    # since_known false for a version the record had already published
    # (kittrial-5bb.99 review `audit-gaps` 1).
    record_version = old_meta.get('version') if isinstance(old_meta, dict) else None
    if (isinstance(record_version, str) and VERSION.fullmatch(record_version)
            and record_version != current and record_version not in superseded):
        history.append({'version': record_version,
                        'set_by': old_meta.get('set_by') if _valid_actor(old_meta.get('set_by')) else None,
                        'set_at': old_meta.get('set_at') if _valid_stamp(old_meta.get('set_at')) else None,
                        'previous_version': (old_meta.get('previous_version')
                                             if isinstance(old_meta.get('previous_version'), str)
                                             and VERSION.fullmatch(old_meta['previous_version']) else None)})
        superseded.add(record_version)
    if previous == current:
        # A same-text repair replaced no distinct text on disk, so "the previous
        # generation" is the one the audit record names - never the current version
        # and never the current text as the previous text.
        previous_version = record_version if record_version in superseded else None
        previous_text = None
    else:
        previous_version = previous
        previous_text = old_text
    meta = {'schema_version': 1, 'version': current, 'set_by': actor, 'set_at': stamp,
            'previous_version': previous_version,
            'previous_text': previous_text,
            'history': history[-HISTORY_LIMIT:],
            'acknowledged': _prune_acks(old_meta, {current, previous_version})}
    # Keep the last compaction audit across a set: dropping it lost who compacted
    # the table and when (kittrial-5bb.99 review `audit-gaps` 3).
    if isinstance(old_meta, dict) and 'acks_compacted_by' in old_meta and 'acks_compacted_at' in old_meta:
        meta['acks_compacted_by'] = old_meta['acks_compacted_by']
        meta['acks_compacted_at'] = old_meta['acks_compacted_at']
    validate_meta(meta)
    # The text is written first: a crash before the metadata replace leaves the new
    # version already authoritative (readers derive it from the text) with stale
    # audit fields, which a set with the same text repairs (see above).
    write_text(target, text)
    atomic(meta_path, meta)
    return {'version': current, 'set_by': actor, 'set_at': stamp,
            'previous_version': previous_version,
            'changed': previous != current, 'repaired': needs_repair}


def acknowledge(path, actor, version=None):
    """Record that one actor has read the current guidance version.

    The caller must name the version it read; a version that is no longer current
    is refused WITHOUT naming the current one, so a caller cannot ack a version it
    never read by copying it out of the refusal (kittrial-5bb.99 review `small` 2);
    it must call ``guidance get`` first. The actor is whatever the endpoint
    accepts - an ack is unauthenticated, so any actor may ack for itself
    (kittrial-5bb.99 review `registration-gate-locks-out-unregistered-lanes`). The
    table is bounded: when it is full the oldest entry is evicted rather than
    refusing a new lane.
    """
    validate_actor(actor)
    text = read_text(path)
    if text is None:
        raise ValueError('No guidance is set for this project yet; nothing to acknowledge')
    current = version_of(text)
    if version is None:
        raise ValueError('Name the guidance version you read: use `guidance ack --version VERSION`. Run '
                         '`guidance get` first and name the version it reports')
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('A guidance version is a sha256 hex digest; run `guidance get` and name the version it '
                         'reports')
    if version != current:
        raise ValueError('The guidance version you named is not the current version. Run `guidance get` first and '
                         'name the version it reports')
    meta = read_meta(path)
    if meta is None:
        raise ValueError('Guidance audit metadata is missing or unreadable; ask the operator to set the '
                         'guidance again before acknowledging it (a set with the same text repairs it)')
    if meta.get('version') != current:
        raise ValueError('Guidance audit metadata does not match the text; ask the operator to set the guidance '
                         'again (a set with the same text repairs it) before acknowledging it')
    if not _valid_actor(meta.get('set_by')) or not _valid_stamp(meta.get('set_at')):
        raise ValueError('Guidance audit metadata does not bind the text to an operator set; ask the operator to '
                         'set the guidance again (a set with the same text repairs it) before acknowledging it')
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


def validate_clear_record(record):
    """Strict validation of the small local audit record ``clear`` writes.

    It keeps who cleared the guidance, when and the version (the SHA-256 of the
    cleared text, or null when the text could not be read), so clearing is no
    longer recorded only on stdout (kittrial-5bb.99 review `audit-gaps` 2). Only the
    most recent CLEAR_LIMIT clears are kept.
    """
    if not isinstance(record, dict):
        raise ValueError('Invalid guidance clear record')
    if set(record) != CLEAR_FIELDS:
        raise ValueError('Invalid guidance clear record: unexpected fields')
    if type(record['schema_version']) is not int or record['schema_version'] != 1:
        raise ValueError('Invalid guidance clear record: schema_version must be integer 1')
    clears = record['clears']
    if not isinstance(clears, list) or not clears or len(clears) > CLEAR_LIMIT:
        raise ValueError('Invalid guidance clear record: clears must be a list of 1..%d entries' % CLEAR_LIMIT)
    for entry in clears:
        if not isinstance(entry, dict) or set(entry) != CLEAR_ENTRY_FIELDS:
            raise ValueError('Invalid guidance clear record: malformed clear entry')
        if not _valid_actor(entry['cleared_by']):
            raise ValueError('Invalid guidance clear record: cleared_by must be an actor identity')
        if not _valid_stamp(entry['cleared_at']):
            raise ValueError('Invalid guidance clear record: cleared_at must be a bounded single-line timestamp')
        if entry['cleared_version'] is not None and (not isinstance(entry['cleared_version'], str)
                                                     or not VERSION.fullmatch(entry['cleared_version'])):
            raise ValueError('Invalid guidance clear record: cleared_version must be a sha256 hex digest or null')
    return record


def read_clear_record(path):
    """The local clear audit record, or None when it is absent or unreadable."""
    target = Path(path) / CLEAR_NAME
    if target.is_symlink() or not target.is_file():
        return None
    try:
        value = record_json.loads(target.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    try:
        return validate_clear_record(value)
    except ValueError:
        return None


def clear(path, actor):
    """Operator host route: remove the guidance text and its audit record entirely.

    A small local record (``.guidance-clear.json``) keeps who cleared it, when and
    the cleared version. A ``GUIDANCE.md`` that is a symlink is refused by
    ``_paths`` (that state needs a manual delete; see docs/OPERATIONS.md), so the
    clear never follows a link out of the project.
    """
    validate_actor(actor)
    text, meta = _paths(path)
    try:
        cleared_version = version_of(read_text(path))
    except ValueError:
        cleared_version = None
    removed = []
    for target in (text, meta, meta.with_suffix('.tmp')):
        if target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                raise ValueError('The guidance file could not be removed; ask the operator to check permissions')
            removed.append(target.name)
    stamp = datetime.now(timezone.utc).isoformat()
    previous = read_clear_record(path)
    clears = ([{'cleared_by': actor, 'cleared_at': stamp, 'cleared_version': cleared_version}]
              + (previous['clears'] if previous else []))[:CLEAR_LIMIT]
    record = {'schema_version': 1, 'clears': clears}
    validate_clear_record(record)
    atomic(Path(path) / CLEAR_NAME, record)
    return {'removed': sorted(set(removed)), 'cleared_by': actor, 'cleared_at': stamp,
            'cleared_version': cleared_version, 'clear_record': CLEAR_NAME}


def status(path, actor, operators=None, host=False):
    """Which lanes have acknowledged which version.

    The endpoint form (``host=False``, the default) reports only versions, actor
    names and the up_to_date/behind/stale classification: a self-declared operator
    name is not authentication, so guidance CONTENT (the current or previous text
    and the history) is not shown there. The authoritative host read
    (``admin.py guidance-status``, ``host=True``) adds the current text, the
    previous text and the history, and is the only place an operator sees the text
    of an unbound record (kittrial-5bb.99 review `small` 1).
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
              'acknowledged': rows, 'acknowledged_total': len(rows),
              'up_to_date': up_to_date, 'behind': behind, 'stale': stale,
              'text_unbound': bool(text is not None and not bound),
              'compact_hint': ('Acks for versions other than the current and previous one are stale; the '
                               'operator can drop them with `admin.py compact-guidance-acks PROJECT --actor OPERATOR`.'
                               if stale else None)}
    if host:
        # The host route (operator allowlist plus shell access): the operator may see
        # the text even when it is unbound, to diagnose the hand edit or crashed set.
        result['text'] = text
        result['previous_text'] = meta.get('previous_text') if bound else None
        result['history'] = meta.get('history') if bound else []
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
