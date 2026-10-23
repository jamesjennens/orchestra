"""Project rooms, slice 1 and 2: rooms for people (kittrial-5bb.385, .388).

Slice 1 of ``docs/PROJECT_ROOMS_DESIGN.md`` (section 10): append-only room files beside
the project, list/read/post/create/archive/moderate, the web page, a separate backup
file and export.  Slice 2 (kittrial-5bb.388) adds *items* (a candidate requirement, decision
or open question), their proposed/accepted/rejected/amended/superseded states, the
accept/reject/amend/answer item events, and the derived ``items`` cache in ``index.json``.
The rooms scope, the scribe and the summary are later slices.

Where the bytes live.  ``<project>/.rooms/`` beside the kit's other project-owned state,
never in the tracker or in source control::

    .rooms/index.json                         per-room last sequence and segments
    .rooms/<room-id>/room.json                name, state, creator, archive facts
    .rooms/<room-id>/messages-0001.jsonl      one JSON line per message
    .rooms/<room-id>/messages-0002.jsonl      ... after a rollover
    .rooms/<room-id>/messages-0001.jsonl.<sha>.incomplete   a torn last line, set aside

Every message line carries a consecutive ``sequence`` and an ``operation_id``.  A
retried post with the same operation id answers the original line, never a second one:
the segment lines are the truth, and the index is only a cache that is rebuilt from
them (coordinator requirement on the plan, 2026-10-23).  Each append is flushed and
fsynced; ``room.json`` and ``index.json`` are written by atomic rename.

A moderated message is replaced by a tombstone line (``kind: tombstone``) whose text is
``removed by NAME at TIME: REASON``.  Reads and both exports hide the original text;
the original stays only in the backup (and its quarantine), until an operator purges it
(design R10 and the coordinator's third point on the plan).

This module is import-safe on every platform (no ``fcntl`` at import): the caller holds
the project's ``.coordination.lock``.  On Linux that is an exclusive ``flock``; the
functions here never take it themselves.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import record_json
from datetime import datetime, timezone
from pathlib import Path

FOLDER = '.rooms'
INDEX = 'index.json'
ROOM = 'room.json'
SEGMENT_PREFIX = 'messages-'
SEGMENT_SUFFIX = '.jsonl'
SCHEMA_VERSION = 1
MIB = 1024 * 1024

#: The design's defaults.  Each is one setting under ``rooms`` in deployment.private.json.
LIMIT_DEFAULTS = {
    'max_message_chars': 4000,
    'segment_messages': 5000,
    'segment_bytes': 4 * MIB,
    'max_open_rooms': 50,
}

ROOM_ID = re.compile(r'r-[0-9a-f]{12}\Z')
SEGMENT_NAME = re.compile(r'messages-[0-9]{4,}\.jsonl\Z')
SEGMENT_ANY = re.compile(r'messages-([0-9]{4,})\.jsonl\Z')
QUARANTINE_NAME = re.compile(r'messages-[0-9]{4,}\.jsonl\.[a-f0-9]{16,64}\.incomplete\Z')
OPERATION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,199}\Z')
ROLES = ('viewer', 'contributor', 'coordinator', 'owner')

MESSAGE_FIELDS = {'schema_version', 'sequence', 'operation_id', 'kind', 'author',
                  'created_at', 'text'}
TOMBSTONE_FIELDS = {'schema_version', 'sequence', 'operation_id', 'kind', 'target_sequence',
                    'reason', 'author', 'created_at', 'text'}
AUTHOR_FIELDS = {'id', 'name', 'role'}

# -- items (slice 2, kittrial-5bb.388) -------------------------------------------------------------
ITEM_KINDS = ('requirement', 'decision', 'question')
ITEM_STATES = ('proposed', 'accepted', 'rejected', 'amended', 'superseded')
ITEM_EVENTS = {'accept': 'accepted', 'reject': 'rejected', 'amend': 'amended',
               'answer': 'accepted'}
ITEM_FIELDS = {'schema_version', 'sequence', 'operation_id', 'kind', 'item_kind', 'title',
               'author', 'created_at', 'text'}
ITEM_FIELDS_SUPERSEDES = ITEM_FIELDS | {'supersedes'}
#: A requirement must name the existing task it hangs under; a room item may carry it.
ITEM_FIELDS_PARENT = ITEM_FIELDS | {'parent'}
ITEM_FIELDS_PARENT_SUPERSEDES = ITEM_FIELDS | {'parent', 'supersedes'}
ITEM_EVENT_FIELDS = {'schema_version', 'sequence', 'operation_id', 'kind', 'target_sequence',
                     'event', 'state', 'author', 'created_at', 'text', 'record'}
#: An amended item-event also carries the new title; the body is its ``text``.
ITEM_EVENT_FIELDS_AMEND = ITEM_EVENT_FIELDS | {'title'}
RECORD_FIELDS = {'kind', 'id', 'revision', 'sha256', 'decision_id', 'evidence'}
RECORD_KINDS = ('requirement', 'proposal', 'decision')
EVIDENCE = re.compile(r'orchestra:room/(r-[0-9a-f]{12})#([0-9]{1,12})\Z')
TITLE_LIMIT = 200


class Refusal(ValueError):
    """One refused room request: an HTTP status, a code and the one sentence a person is told."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _timestamp(value, field='timestamp'):
    if not isinstance(value, str):
        raise ValueError('Invalid %s' % field)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Invalid %s' % field) from None
    if parsed.tzinfo is None:
        raise ValueError('Invalid %s' % field)
    return value


# -- the limits -----------------------------------------------------------------------------------
def limits(root, warnings=None):
    """The installation's ``rooms`` bounds: defaults for an absent or bad value.

    A value that is not a whole number (or is below its floor) keeps the DEFAULT with a
    warning naming the setting, as ``task_files.limits`` reads one: a typo must never lift
    a bound.  ``root`` is the runtime root (the folder holding ``deployment.private.json``).
    """
    values = dict(LIMIT_DEFAULTS)
    if root is None:
        return values
    marker = Path(root) / 'deployment.private.json'
    if not marker.is_file():
        return values
    from admin import audit_field, deployment_document
    section = deployment_document(marker).get('rooms')
    if section is None:
        return values

    def warn(message):
        if warnings is not None:
            warnings.append(message)
        else:
            import sys
            print(message, file=sys.stderr)

    if not isinstance(section, dict):
        warn('WARNING: deployment rooms is %s, not an object; every rooms bound keeps its default'
             % audit_field(section))
        return values
    for key, default in LIMIT_DEFAULTS.items():
        if key not in section:
            continue
        value = section[key]
        # segment_bytes must hold at least one maximal message; 64 KiB is a safe floor.
        floor = 64 * 1024 if key == 'segment_bytes' else 1
        if type(value) is not int or value < floor:
            warn('WARNING: deployment rooms.%s is %s, not a whole number of at least %d; it keeps its '
                 'default %d' % (key, audit_field(value), floor, default))
            continue
        values[key] = value
    return values


# -- checks ---------------------------------------------------------------------------------------
def check_room_id(value):
    if not isinstance(value, str) or not ROOM_ID.fullmatch(value):
        raise Refusal(404, 'not_found', 'Room not found')
    return value


def _plain_text(value, name):
    """Apply the kit's shared plain-text rule, as a room refusal (review s6).

    ``isprintable`` was both too strict (CR, NBSP and a Persian ZWNJ between letters were
    refused) and too lax (U+E0100-E01EF passed). ``review_workflow.plain_text`` is the rule
    every other human-authored record already uses, so rooms use it too rather than keep a
    second, drifting one.
    """
    from review_workflow import plain_text
    try:
        plain_text(value, name)
    except ValueError as error:
        raise Refusal(422, 'invalid', str(error)) from None


def check_name(value):
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', 'A room name must be text')
    text = value.strip()
    if not text or len(text) > 200:
        raise Refusal(422, 'invalid', 'A room name must be 1-200 characters')
    _plain_text(text, 'A room name')
    return text


def check_text(value, limit):
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', 'A room post must be text')
    if not value.strip():
        raise Refusal(422, 'invalid', 'A room post must not be empty')
    if len(value) > limit:
        raise Refusal(422, 'too_long', 'A room post is at most %d characters; this one is %d.'
                      % (limit, len(value)))
    _plain_text(value, 'A room post')
    from reserved_comments import reserved_match
    if reserved_match(value) is not None:
        raise Refusal(422, 'reserved_text',
                      'A room post cannot start with a reserved machine-record prefix; '
                      'write ordinary discussion, not a Kind: record.')
    return value


def check_reason(value, limit=500):
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', 'A moderation reason must be text')
    text = value.strip()
    if not text or len(text) > limit:
        raise Refusal(422, 'invalid', 'A moderation reason must be 1-%d characters' % limit)
    _plain_text(text, 'A moderation reason')
    return text


def check_title(value):
    """An item title: 1-200 characters, the kit's plain-text rule and no machine prefix."""
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', 'An item title must be text')
    text = value.strip()
    if not text or len(text) > TITLE_LIMIT:
        raise Refusal(422, 'invalid', 'An item title must be 1-%d characters' % TITLE_LIMIT)
    _plain_text(text, 'An item title')
    from reserved_comments import reserved_match
    if reserved_match(text) is not None:
        raise Refusal(422, 'reserved_text',
                      'An item title cannot start with a reserved machine-record prefix.')
    return text


def check_item_text(value, limit, what='An item text'):
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', what + ' must be text')
    if not value.strip():
        raise Refusal(422, 'invalid', what + ' must not be empty')
    if len(value) > limit:
        raise Refusal(422, 'too_long', '%s is at most %d characters; this one is %d.'
                       % (what, limit, len(value)))
    _plain_text(value, what)
    from reserved_comments import reserved_match
    if reserved_match(value) is not None:
        raise Refusal(422, 'reserved_text',
                      'An item text cannot start with a reserved machine-record prefix; '
                      'write ordinary discussion, not a Kind: record.')
    return value


def check_item_kind(value):
    if value not in ITEM_KINDS:
        raise Refusal(422, 'invalid', 'item_kind must be requirement, decision or question')
    return value


def check_evidence(value):
    if not isinstance(value, str) or not EVIDENCE.fullmatch(value):
        raise Refusal(422, 'invalid', 'Invalid room evidence pointer')
    return value


def check_record(record):
    """The pointer an accepted item stores: an existing record's identity, never a new shape."""
    if record is None:
        return None
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
        raise Refusal(422, 'invalid', 'Invalid accepted record pointer')
    if record['kind'] not in RECORD_KINDS:
        raise Refusal(422, 'invalid', 'Invalid accepted record pointer')
    for name in ('id', 'decision_id'):
        value = record[name]
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise Refusal(422, 'invalid', 'Invalid accepted record pointer')
    if record['revision'] is not None and (type(record['revision']) is not int or record['revision'] < 1):
        raise Refusal(422, 'invalid', 'Invalid accepted record pointer')
    if record['sha256'] is not None and (not isinstance(record['sha256'], str)
                                          or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
        raise Refusal(422, 'invalid', 'Invalid accepted record pointer')
    check_evidence(record['evidence'])
    return dict(record)


def check_operation_id(value):
    if not isinstance(value, str) or not OPERATION_ID.fullmatch(value):
        raise Refusal(422, 'invalid', 'Invalid operation id')
    return value


def item_operation(room_id, sequence, revision, action):
    """The deterministic operation id of one item decision (never the client's key)."""
    return 'room-%s-%d-%d-%s' % (room_id, sequence, revision, action)


def item_decision_operation(room_id, sequence, action):
    """The stable operation id of one item decision (review b1/b2 of kittrial-5bb.388).

    It names the item and the action, never the current event count: a retried native write
    reconciles on the same identity instead of creating a second record, and the route can
    refuse an already-decided item before it calls the native write at all.
    """
    return 'room-%s-%d-%s' % (room_id, sequence, action)


# -- paths and atomic writes ----------------------------------------------------------------------
def rooms_root(project):
    path = Path(project) / FOLDER
    if path.is_symlink():
        raise Refusal(500, 'damaged', 'The project rooms folder is a symlink; refusing to use it.')
    if path.exists() and not path.is_dir():
        raise Refusal(500, 'damaged', 'The project rooms path is not a folder.')
    return path


def room_dir(project, room_id):
    check_room_id(room_id)
    path = rooms_root(project) / room_id
    if path.is_symlink():
        raise Refusal(500, 'damaged', 'A room folder is a symlink; refusing to use it.')
    if path.exists() and not path.is_dir():
        raise Refusal(500, 'damaged', 'A room path is not a folder.')
    return path


def _refuse_symlink(path):
    if path.is_symlink():
        raise Refusal(500, 'damaged', 'A rooms file is a symlink; refusing to use it.')


def _fsync_directory(path):
    try:
        directory_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write(path, content):
    path = Path(path)
    _refuse_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path, value):
    _atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode('utf-8') + b'\n')


# -- validation of a stored line ------------------------------------------------------------------
def _validate_author(value):
    if not isinstance(value, dict) or set(value) != AUTHOR_FIELDS:
        raise ValueError('Invalid room message author')
    if not isinstance(value['id'], str) or not value['id'].strip() or len(value['id']) > 200:
        raise ValueError('Invalid room message author')
    if not isinstance(value['name'], str) or not value['name'].strip() or len(value['name']) > 200:
        raise ValueError('Invalid room message author')
    if value['role'] not in ROLES:
        raise ValueError('Invalid room message author role')
    return value


def _validate_line(line, expected=None):
    if not isinstance(line, dict):
        raise ValueError('Invalid room line')
    kind = line.get('kind')
    if kind == 'tombstone':
        required = TOMBSTONE_FIELDS
    elif kind == 'message':
        required = MESSAGE_FIELDS
    elif kind == 'item':
        if 'parent' in line and 'supersedes' in line:
            required = ITEM_FIELDS_PARENT_SUPERSEDES
        elif 'parent' in line:
            required = ITEM_FIELDS_PARENT
        else:
            required = ITEM_FIELDS_SUPERSEDES if 'supersedes' in line else ITEM_FIELDS
    elif kind == 'item-event':
        required = ITEM_EVENT_FIELDS_AMEND if line.get('event') == 'amend' else ITEM_EVENT_FIELDS
    else:
        raise ValueError('Invalid room line kind')
    if set(line) != required:
        raise ValueError('Invalid room line')
    if line['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Invalid room line schema')
    if type(line['sequence']) is not int or line['sequence'] < 1:
        raise ValueError('Invalid room line sequence')
    if expected is not None and line['sequence'] != expected:
        raise ValueError('Invalid room line sequence')
    if not isinstance(line['operation_id'], str) or not OPERATION_ID.fullmatch(line['operation_id']):
        raise ValueError('Invalid room line operation id')
    _timestamp(line['created_at'], 'room line time')
    _validate_author(line['author'])
    if kind == 'tombstone':
        if type(line['target_sequence']) is not int or line['target_sequence'] < 1:
            raise ValueError('Invalid room tombstone target')
        if not isinstance(line['reason'], str) or not line['reason'].strip() or len(line['reason']) > 500:
            raise ValueError('Invalid room tombstone reason')
    if not isinstance(line['text'], str) or len(line['text']) > 100000:
        raise ValueError('Invalid room line text')
    if kind == 'item':
        if line['item_kind'] not in ITEM_KINDS:
            raise ValueError('Invalid room item kind')
        if not isinstance(line['title'], str) or not line['title'].strip() or len(line['title']) > TITLE_LIMIT:
            raise ValueError('Invalid room item title')
        if not line['text'].strip():
            raise ValueError('Invalid room item text')
        if 'supersedes' in line and (type(line['supersedes']) is not int or line['supersedes'] < 1):
            raise ValueError('Invalid room item supersedes')
        if 'parent' in line:
            if not isinstance(line['parent'], str) or not OPERATION_ID.fullmatch(line['parent']):
                raise ValueError('Invalid room item parent')
    elif kind == 'item-event':
        if type(line['target_sequence']) is not int or line['target_sequence'] < 1:
            raise ValueError('Invalid room item-event target')
        event = line['event']
        if event not in ITEM_EVENTS or line['state'] != ITEM_EVENTS[event]:
            raise ValueError('Invalid room item-event event')
        if event in ('reject', 'amend', 'answer') and not line['text'].strip():
            raise ValueError('Invalid room item-event text')
        if event == 'amend' and (not isinstance(line.get('title'), str) or not line['title'].strip()
                                 or len(line['title']) > TITLE_LIMIT):
            raise ValueError('Invalid room item-event title')
        check_record(line['record'])
    elif not line['text'].strip():
        raise ValueError('Invalid room message text')
    return line


def _parse_lines(raw):
    """The lines of one segment, refusing anything but a torn LAST line.

    ``raw`` must be the whole file.  A trailing line without a newline is returned
    separately as ``tail`` so the caller can quarantine it exactly as ``.feedback.jsonl``
    does.  Any earlier invalid line is corruption: refusing is safer than dropping text.
    """
    if raw == b'':
        return [], b''
    complete = raw
    tail = b''
    if not raw.endswith(b'\n'):
        split_at = raw.rfind(b'\n')
        complete = raw[:split_at + 1] if split_at >= 0 else b''
        tail = raw[split_at + 1:]
    lines = []
    for number, encoded in enumerate(complete.split(b'\n')[:-1], 1):
        if not encoded.strip():
            raise ValueError('Invalid room segment: blank line')
        try:
            text = encoded.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('Invalid room segment: not UTF-8') from None
        lines.append(record_json.loads(text))
    return lines, tail


def _quarantine(path, tail):
    digest = hashlib.sha256(tail).hexdigest()
    for value in (digest[:16], digest):
        quarantine = path.with_name(path.name + '.' + value + '.incomplete')
        _refuse_symlink(quarantine)
        if quarantine.exists():
            if quarantine.read_bytes() == tail:
                return
            continue
        _atomic_write(quarantine, tail)
        return
    raise Refusal(500, 'damaged', 'Room recovery quarantine paths already hold different bytes.')


def _read_segment(path, first_sequence):
    """Validated lines of one segment, quarantining a torn last line."""
    _refuse_symlink(path)
    raw = path.read_bytes()
    try:
        lines, tail = _parse_lines(raw)
        parsed = []
        expected = first_sequence
        for line in lines:
            _validate_line(line, expected)
            parsed.append(line)
            expected += 1
        if tail:
            # A complete JSON line without its newline is accepted after adding the newline;
            # anything else is a torn write and is set aside (as feedback.py does).
            try:
                line = record_json.loads(tail.decode('utf-8'))
                _validate_line(line, expected)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                _quarantine(path, tail)
                _atomic_write(path, raw[:len(raw) - len(tail)])
                return parsed
            _atomic_write(path, raw + b'\n')
            parsed.append(line)
        return parsed
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, Refusal):
            raise
        raise Refusal(500, 'damaged', 'Room %s is damaged: %s' % (path.name, error)) from None


def _segments(project, room_id):
    folder = room_dir(project, room_id)
    if not folder.exists():
        raise Refusal(404, 'not_found', 'Room not found')
    found = []
    for item in folder.iterdir():
        if item.name == ROOM:
            continue
        if item.is_symlink():
            raise Refusal(500, 'damaged', 'A room file is a symlink; refusing to use it.')
        match = SEGMENT_ANY.fullmatch(item.name)
        if match:
            found.append((int(match.group(1)), item))
            continue
        if QUARANTINE_NAME.fullmatch(item.name):
            continue
        raise Refusal(500, 'damaged', 'Unknown file in room %s: %s' % (room_id, item.name))
    return sorted(found)


def load_lines(project, room_id, start=1):
    """Stored lines of a room from ``start`` on, validated and in sequence order.

    ``start=1`` is the whole room. For a page, the index says which segment holds the
    sequence, so the read opens that segment and the ones after it, not every older one;
    tombstones for an already-read message can only be later, so the page still learns
    about them.
    """
    pairs = _segments(project, room_id)
    first_index = 0
    expected = 1
    if start > 1:
        index = read_index(project)
        entry = index['rooms'].get(room_id) or {}
        if entry.get('damaged'):
            raise Refusal(500, 'damaged', entry['damaged'])
        segments = entry.get('segments') or []
        chosen = next((item for item in segments if (item.get('last') or 0) >= start), None)
        if chosen is None:
            return []
        names = [path.name for _, path in pairs]
        if chosen['name'] not in names:
            return []
        first_index = names.index(chosen['name'])
        expected = chosen.get('first') or start
    lines = []
    for number, path in pairs[first_index:]:
        segment = _read_segment(path, expected)
        for line in segment:
            _validate_line(line, expected)
            lines.append(line)
            expected += 1
    return lines


# -- the index ------------------------------------------------------------------------------------
def _index_path(project):
    return rooms_root(project) / INDEX


def _room_ids(project):
    root = rooms_root(project)
    if not root.exists():
        return []
    ids = []
    for item in root.iterdir():
        if item.is_symlink():
            raise Refusal(500, 'damaged', 'A rooms entry is a symlink; refusing to use it.')
        if item.name == INDEX:
            continue
        if item.is_dir() and ROOM_ID.fullmatch(item.name):
            # A create that died between mkdir and room.json leaves an orphan folder.
            # It is not a room; leave it alone rather than failing every other room.
            if (item / ROOM).is_file():
                ids.append(item.name)
            continue
        raise Refusal(500, 'damaged', 'Unknown file in the rooms folder: %s' % item.name)
    return sorted(ids)


def rebuild_index(project, tolerant=False):
    """The index rebuilt from the segment files, which are the truth.

    A damaged room must not take the whole project down (review s2): ``tolerant=True``
    (what the read path uses) records the room as damaged instead of raising, so a list
    can name it as damaged. The default still raises a ``Refusal``, never a bare
    ``ValueError``/``KeyError``, for a caller that asked for a strict rebuild.
    """
    rooms = {}
    for room_id in _room_ids(project):
        entry = {'last_sequence': 0, 'segments': [], 'items': []}
        try:
            all_lines = []
            for number, path in _segments(project, room_id):
                raw = path.read_bytes()
                parsed, _tail = _parse_lines(raw)
                for line in parsed:
                    _validate_line(line)
                all_lines.extend(parsed)
                if parsed:
                    entry['segments'].append({'name': path.name, 'bytes': len(raw),
                                              'count': len(parsed),
                                              'first': parsed[0]['sequence'],
                                              'last': parsed[-1]['sequence']})
                    entry['last_sequence'] = parsed[-1]['sequence']
            # A torn tail is handled (and quarantined) when the room is read; the index
            # only records the complete lines, so a rebuild never invents a sequence.
            entry['items'] = items_from_lines(all_lines, room_id)
        except Refusal:
            raise
        except (ValueError, KeyError, UnicodeDecodeError, OSError) as error:
            message = 'Room %s is damaged: %s' % (room_id, error)
            if not tolerant:
                raise Refusal(500, 'damaged', message) from None
            entry['damaged'] = message
        rooms[room_id] = entry
    return {'schema_version': SCHEMA_VERSION, 'rooms': rooms}


def read_index(project):
    path = _index_path(project)
    _refuse_symlink(path)
    rebuilt = None
    if path.exists():
        try:
            value = record_json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION
                    or not isinstance(value.get('rooms'), dict)):
                raise ValueError('invalid index')
            rebuilt = value
        except (OSError, ValueError, UnicodeDecodeError):
            rebuilt = None
    if rebuilt is None:
        rebuilt = rebuild_index(project, tolerant=True)
    # The index is a cache: compare its segment list with the files, and rebuild when stale.
    # A cached damage mark is re-checked too, so a repaired room becomes healthy again.
    stale = any(entry.get('damaged') for entry in rebuilt['rooms'].values())
    for room_id in _room_ids(project):
        if stale:
            break
        entry = rebuilt['rooms'].get(room_id)
        try:
            pairs = _segments(project, room_id)
        except Refusal:
            stale = True
            break
        names = [path.name for _, path in pairs]
        have = [item.get('name') for item in (entry or {}).get('segments', [])]
        if entry is None or names != have:
            stale = True
            break
        # The items cache is part of the index too: a cache that is absent (for example an
        # index written by slice 1) is rebuilt rather than served as "no items".
        if not isinstance(entry.get('items'), list):
            stale = True
            break
        # The index is only a cache, but a crash after an append and before its index
        # write leaves the same names with one line fewer. The file's size moves on every
        # append, so it catches that too (the segment lines remain the truth).
        if any(item.get('bytes') != path.stat().st_size
               for item, (_, path) in zip(entry.get('segments', []), pairs)):
            stale = True
            break
    if stale:
        rebuilt = rebuild_index(project, tolerant=True)
    return rebuilt


def write_index(project, index):
    _atomic_json(_index_path(project), index)


def _index_entry(index, room_id):
    return index['rooms'].setdefault(room_id, {'last_sequence': 0, 'segments': [], 'items': []})


# -- room records ---------------------------------------------------------------------------------
def _room_view(room):
    return {key: room.get(key) for key in
            ('id', 'name', 'state', 'created_by', 'created_by_name', 'created_at',
             'archived_by', 'archived_by_name', 'archived_at', 'archive_reason')}


def read_room(project, room_id):
    path = room_dir(project, room_id) / ROOM
    _refuse_symlink(path)
    if not path.is_file():
        raise Refusal(404, 'not_found', 'Room not found')
    try:
        room = record_json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        raise Refusal(500, 'damaged', 'The room record is unreadable.') from None
    required = {'schema_version', 'id', 'name', 'state', 'created_by', 'created_by_name',
                'created_at', 'archived_by', 'archived_by_name', 'archived_at',
                'archive_reason', 'archive_operation_id'}
    if (not isinstance(room, dict) or set(room) != required
            or room.get('schema_version') != SCHEMA_VERSION or room.get('id') != room_id
            or room.get('state') not in ('open', 'archived')):
        raise Refusal(500, 'damaged', 'The room record is invalid.')
    return room


def write_room(project, room):
    _atomic_json(room_dir(project, room['id']) / ROOM, room)


def list_rooms(project, include_archived=False):
    index = read_index(project)
    items = []
    for room_id in sorted(index['rooms']):
        entry = index['rooms'][room_id]
        try:
            room = read_room(project, room_id)
        except Refusal as error:
            # One damaged room is named as damaged here, not turned into a 500 for every
            # other room in the project (review s2).
            items.append({'id': room_id, 'name': None, 'state': 'damaged',
                          'created_by': None, 'created_by_name': None, 'created_at': None,
                          'archived_by': None, 'archived_by_name': None, 'archived_at': None,
                          'archive_reason': None, 'last_sequence': entry.get('last_sequence', 0),
                          'damaged': str(error)})
            continue
        if room['state'] != 'open' and not include_archived and not entry.get('damaged'):
            continue
        view = dict(_room_view(room),
                    last_sequence=entry.get('last_sequence', 0))
        if entry.get('damaged'):
            # The room's record is readable, but its message files are not: name it
            # damaged so the page cannot present it as a healthy open room.
            view['state'] = 'damaged'
            view['record_state'] = room['state']
            view['damaged'] = entry['damaged']
        items.append(view)
    items.sort(key=lambda item: (item['created_at'] or '', item['id']))
    return items


def create(project, name, actor, limits_values):
    name = check_name(name)
    root = rooms_root(project)
    root.mkdir(parents=True, exist_ok=True)
    index = read_index(project)
    opened = 0
    for room_id in index['rooms']:
        try:
            room = read_room(project, room_id)
        except Refusal:
            # A damaged room cannot be counted or compared by name; leave it for repair
            # rather than refusing every new room in the project (review s2).
            continue
        if room['state'] == 'open':
            opened += 1
            if room['name'].casefold() == name.casefold():
                raise Refusal(409, 'name_taken', 'An open room already has that name.')
    if opened >= limits_values['max_open_rooms']:
        raise Refusal(409, 'rooms_full', 'This project already has its %d open rooms.'
                      % limits_values['max_open_rooms'])
    while True:
        room_id = 'r-' + secrets.token_hex(6)
        folder = root / room_id
        if not folder.exists():
            break
    room = {
        'schema_version': SCHEMA_VERSION,
        'id': room_id,
        'name': name,
        'state': 'open',
        'created_by': actor['id'],
        'created_by_name': actor['name'],
        'created_at': now_iso(),
        'archived_by': None,
        'archived_by_name': None,
        'archived_at': None,
        'archive_reason': None,
        'archive_operation_id': None,
    }
    folder.mkdir()
    try:
        write_room(project, room)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    index['rooms'][room_id] = {'last_sequence': 0, 'segments': [], 'items': []}
    write_index(project, index)
    return _room_view(room)


def archive(project, room_id, actor, operation_id, reason=None):
    operation_id = check_operation_id(operation_id)
    if reason is not None:
        reason = check_reason(reason)
    room = read_room(project, room_id)
    if room['state'] == 'archived':
        return dict(_room_view(room), replayed=room.get('archive_operation_id') == operation_id)
    room['state'] = 'archived'
    room['archived_by'] = actor['id']
    room['archived_by_name'] = actor['name']
    room['archived_at'] = now_iso()
    room['archive_reason'] = reason
    room['archive_operation_id'] = operation_id
    write_room(project, room)
    return dict(_room_view(room), replayed=False)


# -- messages -------------------------------------------------------------------------------------
def _message_view(line):
    return {'sequence': line['sequence'], 'kind': line['kind'], 'author': dict(line['author']),
            'created_at': line['created_at'], 'text': line['text']}


def _tombstone_view(line):
    return {'sequence': line['sequence'], 'target_sequence': line['target_sequence'],
            'author': dict(line['author']), 'created_at': line['created_at'],
            'text': line['text'], 'reason': line['reason']}


def _tombstones(lines):
    by_target = {}
    events = []
    for line in lines:
        if line['kind'] == 'tombstone':
            by_target[line['target_sequence']] = line
            events.append(line)
    return by_target, events


def _redacted_view(line, tombstone):
    return {'sequence': line['sequence'], 'kind': 'tombstone', 'moderated': True,
            'author': dict(tombstone['author']), 'created_at': tombstone['created_at'],
            'text': tombstone['text'], 'reason': tombstone['reason'],
            'target_sequence': line['sequence']}


def _item_evidence(room_id, sequence):
    return 'orchestra:room/%s#%d' % (room_id, sequence)


def _item_line_view(line, room_id, tombstone=None):
    """One item line in the paged log: its own current text, or its tombstone."""
    if tombstone is not None:
        return {'sequence': line['sequence'], 'kind': 'tombstone', 'moderated': True,
                'item_kind': line['item_kind'], 'title': None,
                'author': dict(tombstone['author']), 'created_at': tombstone['created_at'],
                'text': tombstone['text'], 'reason': tombstone['reason'],
                'target_sequence': line['sequence'],
                'evidence': _item_evidence(room_id, line['sequence'])}
    view = {'sequence': line['sequence'], 'kind': 'item', 'item_kind': line['item_kind'],
            'title': line['title'], 'author': dict(line['author']),
            'created_at': line['created_at'], 'text': line['text'],
            'evidence': _item_evidence(room_id, line['sequence']), 'moderated': False}
    if 'supersedes' in line:
        view['supersedes'] = line['supersedes']
    if 'parent' in line:
        view['parent'] = line['parent']
    return view


def _item_event_view(line, redacted=None):
    view = {'sequence': line['sequence'], 'kind': 'item-event',
            'target_sequence': line['target_sequence'], 'event': line['event'],
            'state': line['state'], 'author': dict(line['author']),
            'created_at': line['created_at'], 'record': line['record']}
    if line['event'] == 'amend':
        view['title'] = line.get('title')
    if redacted is not None:
        # Review b4 of kittrial-5bb.388: every field a person wrote on a moderated item's
        # event is replaced, including an amendment's title (only text was replaced before).
        view['text'] = redacted['text']
        view['record'] = None
        if 'title' in view:
            view['title'] = None
        view['redacted'] = True
    else:
        view['text'] = line['text']
    return view


def items_from_lines(lines, room_id):
    """Current item views, derived only from the segment lines (the truth).

    The same function builds the derived ``items`` cache and a rebuild, so a cache and a
    rebuild from the lines are identical by construction (the coordinator's condition 2 on
    the plan).  A moderated item keeps its last state but its title and text are the
    tombstone's, its record is dropped and its decision history is not served.
    """
    tombstones, _ = _tombstones(lines)
    items = {}
    for line in lines:
        if line['kind'] == 'item':
            sequence = line['sequence']
            supersedes = line.get('supersedes')
            if supersedes is not None and supersedes in items and \
                    items[supersedes]['state'] in ('proposed', 'amended'):
                items[supersedes]['state'] = 'superseded'
                items[supersedes]['superseded_by'] = sequence
            items[sequence] = {
                'sequence': sequence, 'kind': 'item', 'item_kind': line['item_kind'],
                'title': line['title'], 'state': 'proposed', 'text': line['text'],
                'author': dict(line['author']), 'created_at': line['created_at'],
                'supersedes': supersedes, 'parent': line.get('parent'), 'history': [],
                'record': None,
                'evidence': _item_evidence(room_id, sequence), 'moderated': False,
            }
            for name in ('accepted_by', 'rejected_by', 'amended_by', 'answered_by'):
                items[sequence][name] = None
            for name in ('accepted_at', 'rejected_at', 'amended_at', 'answered_at'):
                items[sequence][name] = None
        elif line['kind'] == 'item-event':
            target = items.get(line['target_sequence'])
            if target is None or target['moderated']:
                continue
            if target['state'] not in ('proposed', 'amended'):
                continue
            event = line['event']
            target['history'].append(_item_event_view(line))
            target['state'] = line['state']
            if event == 'accept':
                target['record'] = check_record(line['record'])
                target['accepted_by'] = dict(line['author'])
                target['accepted_at'] = line['created_at']
            elif event == 'reject':
                target['rejected_by'] = dict(line['author'])
                target['rejected_at'] = line['created_at']
            elif event == 'amend':
                target['title'] = line['title']
                target['text'] = line['text']
                target['amended_by'] = dict(line['author'])
                target['amended_at'] = line['created_at']
            elif event == 'answer':
                target['text'] = line['text']
                target['answered_by'] = dict(line['author'])
                target['answered_at'] = line['created_at']
    for sequence, tombstone in tombstones.items():
        target = items.get(sequence)
        if target is None:
            continue
        target.update({'moderated': True, 'title': None, 'text': tombstone['text'],
                       'author': dict(tombstone['author']),
                       'created_at': tombstone['created_at'], 'record': None, 'history': []})
    return [items[key] for key in sorted(items)]


def read_item(project, room_id, sequence):
    """One current item view, or the refusal the route answers."""
    if type(sequence) is not int or sequence < 1:
        raise Refusal(422, 'invalid', 'The item number must be a positive whole number')
    read_room(project, room_id)
    views = items_from_lines(load_lines(project, room_id), room_id)
    for view in views:
        if view['sequence'] == sequence:
            return view
    raise Refusal(404, 'not_found', 'No such item in this room.')


def read_messages(project, room_id, since=0, limit=100):
    if type(since) is not int or since < 0:
        raise Refusal(422, 'invalid', 'since must be a whole number of at least 0')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise Refusal(422, 'invalid', 'limit must be 1..500')
    room = read_room(project, room_id)
    lines = load_lines(project, room_id, since + 1)
    tombstones, events = _tombstones(lines)
    messages = []
    last_visible = since
    more = False
    for line in lines:
        if line['kind'] == 'tombstone':
            continue
        if line['sequence'] <= since:
            continue
        if len(messages) >= limit:
            more = True
            break
        tombstone = tombstones.get(line['sequence'])
        if line['kind'] == 'message':
            messages.append(_redacted_view(line, tombstone) if tombstone else _message_view(line))
        elif line['kind'] == 'item':
            messages.append(_item_line_view(line, room_id, tombstone))
        else:
            target_tombstone = tombstones.get(line['target_sequence'])
            messages.append(_item_event_view(line, target_tombstone))
        last_visible = line['sequence']
    if not more:
        # Is there a visible line after the page even when no tombstone followed it?
        more = any(line['kind'] != 'tombstone' and line['sequence'] > last_visible for line in lines)
    moderations = [_tombstone_view(line) for line in events if line['sequence'] > since]
    index = read_index(project)
    entry = index['rooms'].get(room_id) or {}
    last_sequence = entry.get('last_sequence', 0)
    if lines:
        last_sequence = max(last_sequence, lines[-1]['sequence'])
    # The items cache is derived from the segment lines. read_index rebuilds a missing or
    # stale cache; a room line just appended is seen by the byte check there.
    items = list(entry.get('items') or [])
    return {'room': _room_view(room), 'messages': messages, 'moderations': moderations,
            'items': items, 'since': since, 'last_sequence': last_sequence,
            'next_since': last_sequence if not more else last_visible, 'has_more': more}


def _find_operation(lines, operation_id):
    for line in reversed(lines):
        if line['operation_id'] == operation_id:
            return line
    return None


def find_operation(project, room_id, operation_id):
    """The existing room line written under one operation id, or None (review b1/b2)."""
    check_operation_id(operation_id)
    read_room(project, room_id)
    return _find_operation(load_lines(project, room_id), operation_id)


def decidable_item(project, room_id, sequence, event):
    """The item an accept/reject/amend/answer may act on, before any native record write.

    Reviews b1 and b2 of kittrial-5bb.388: the route checks the item's state, moderation,
    room and event kind BEFORE it creates the record an acceptance becomes, under the
    project's item-decision lock, so a refused or concurrent decision writes nothing.
    """
    if type(sequence) is not int or sequence < 1:
        raise Refusal(422, 'invalid', 'The item number must be a positive whole number')
    if event not in ITEM_EVENTS:
        raise Refusal(422, 'invalid', 'Unknown item decision')
    _room_open(project, room_id)
    lines = load_lines(project, room_id)
    current = next((item for item in items_from_lines(lines, room_id)
                    if item['sequence'] == sequence), None)
    if current is None or current['kind'] != 'item':
        raise Refusal(404, 'not_found', 'No such item in this room.')
    if current['moderated']:
        raise Refusal(409, 'moderated', 'A moderated item cannot be decided.')
    if current['state'] not in ('proposed', 'amended'):
        raise Refusal(409, 'final', 'That item is already decided.')
    if event == 'accept' and current['item_kind'] == 'question':
        raise Refusal(400, 'invalid', 'A question is answered, not accepted.')
    if event == 'answer' and current['item_kind'] != 'question':
        raise Refusal(400, 'invalid', 'Only a question is answered.')
    return current


def _append(project, room_id, line, limits_values):
    index = read_index(project)
    entry = _index_entry(index, room_id)
    encoded = json.dumps(line, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8') + b'\n'
    segment = entry['segments'][-1] if entry['segments'] else None
    if segment is None or segment['count'] >= limits_values['segment_messages'] or \
            segment['bytes'] + len(encoded) > limits_values['segment_bytes']:
        number = int(SEGMENT_ANY.fullmatch(segment['name']).group(1)) + 1 if segment else 1
        name = SEGMENT_PREFIX + ('%04d' % number) + SEGMENT_SUFFIX
        segment = {'name': name, 'bytes': 0, 'count': 0, 'first': line['sequence'],
                   'last': line['sequence']}
        entry['segments'].append(segment)
    path = room_dir(project, room_id) / segment['name']
    _refuse_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    segment['bytes'] += len(encoded)
    segment['count'] += 1
    segment['last'] = line['sequence']
    if segment['first'] is None:
        segment['first'] = line['sequence']
    entry['last_sequence'] = line['sequence']
    if line['kind'] in ('item', 'item-event'):
        entry['items'] = items_from_lines(load_lines(project, room_id), room_id)
    elif line['kind'] == 'tombstone':
        target = next((stored for stored in load_lines(project, room_id)
                       if stored['sequence'] == line['target_sequence']), None)
        if target is not None and target['kind'] == 'item':
            entry['items'] = items_from_lines(load_lines(project, room_id), room_id)
    write_index(project, index)


def post(project, room_id, text, operation_id, author, limits_values):
    operation_id = check_operation_id(operation_id)
    text = check_text(text, limits_values['max_message_chars'])
    room = read_room(project, room_id)
    if room['state'] != 'open':
        raise Refusal(409, 'archived', 'This room is archived; it can be read and exported, not posted to.')
    lines = load_lines(project, room_id)
    existing = _find_operation(lines, operation_id)
    if existing is not None:
        if existing['kind'] == 'message' and existing['text'] == text and \
                existing['author']['id'] == author['id']:
            return dict(_message_view(existing), replayed=True)
        raise Refusal(409, 'operation_reused',
                      'That operation id already wrote a different room line.')
    line = {'schema_version': SCHEMA_VERSION, 'sequence': (lines[-1]['sequence'] + 1) if lines else 1,
            'operation_id': operation_id, 'kind': 'message', 'author': dict(author),
            'created_at': now_iso(), 'text': text}
    _append(project, room_id, line, limits_values)
    return dict(_message_view(line), replayed=False)


def _room_open(project, room_id):
    room = read_room(project, room_id)
    if room['state'] != 'open':
        raise Refusal(409, 'archived', 'This room is archived; it can be read and exported, not posted to.')
    return room


def post_item(project, room_id, item_kind, title, text, operation_id, author, limits_values,
              supersedes=None, parent=None):
    """Post one item line: a proposed requirement, decision or question."""
    operation_id = check_operation_id(operation_id)
    item_kind = check_item_kind(item_kind)
    title = check_title(title)
    text = check_item_text(text, limits_values['max_message_chars'])
    if supersedes is not None and (type(supersedes) is not int or supersedes < 1):
        raise Refusal(422, 'invalid', 'supersedes must be the number of an earlier item')
    if parent is not None:
        if item_kind != 'requirement':
            raise Refusal(422, 'invalid', 'Only a requirement item names a parent job')
        if not isinstance(parent, str) or not OPERATION_ID.fullmatch(parent):
            raise Refusal(422, 'invalid', 'Invalid parent job id')
    _room_open(project, room_id)
    lines = load_lines(project, room_id)
    existing = _find_operation(lines, operation_id)
    if existing is not None:
        if (existing['kind'] == 'item' and existing['item_kind'] == item_kind
                and existing['title'] == title and existing['text'] == text
                and existing.get('supersedes') == supersedes and existing.get('parent') == parent
                and existing['author']['id'] == author['id']):
            view = next(item for item in items_from_lines(lines, room_id)
                        if item['sequence'] == existing['sequence'])
            return dict(view, replayed=True)
        raise Refusal(409, 'operation_reused',
                      'That operation id already wrote a different room line.')
    if supersedes is not None:
        current = next((item for item in items_from_lines(lines, room_id)
                        if item['sequence'] == supersedes), None)
        if current is None or current['kind'] != 'item':
            raise Refusal(404, 'not_found', 'No such item to supersede in this room.')
        if current['moderated']:
            raise Refusal(409, 'moderated', 'A moderated item cannot be superseded.')
        if current['state'] not in ('proposed', 'amended'):
            raise Refusal(409, 'final', 'That item is already decided; a decided item cannot be superseded.')
    line = {'schema_version': SCHEMA_VERSION,
            'sequence': (lines[-1]['sequence'] + 1) if lines else 1,
            'operation_id': operation_id, 'kind': 'item', 'item_kind': item_kind,
            'title': title, 'author': dict(author), 'created_at': now_iso(), 'text': text}
    if supersedes is not None:
        line['supersedes'] = supersedes
    if parent is not None:
        line['parent'] = parent
    _append(project, room_id, line, limits_values)
    view = next(item for item in items_from_lines(load_lines(project, room_id), room_id)
                if item['sequence'] == line['sequence'])
    return dict(view, replayed=False)


def _decide_item(project, room_id, sequence, event, operation_id, author, limits_values,
                 text='', title=None, record=None):
    """Append one item-event after checking the item's current state (the truth)."""
    operation_id = check_operation_id(operation_id)
    if type(sequence) is not int or sequence < 1:
        raise Refusal(422, 'invalid', 'The item number must be a positive whole number')
    if event not in ITEM_EVENTS:
        raise Refusal(422, 'invalid', 'Unknown item decision')
    _room_open(project, room_id)
    lines = load_lines(project, room_id)
    existing = _find_operation(lines, operation_id)
    if existing is not None:
        matches = (existing['kind'] == 'item-event' and existing['target_sequence'] == sequence
                   and existing['event'] == event and existing['text'] == text
                   and existing.get('title') == title and existing['record'] == record)
        if matches:
            view = next(item for item in items_from_lines(lines, room_id)
                        if item['sequence'] == sequence)
            return dict(view, replayed=True)
        raise Refusal(409, 'operation_reused',
                      'That operation id already wrote a different room line.')
    current = next((item for item in items_from_lines(lines, room_id)
                    if item['sequence'] == sequence), None)
    if current is None or current['kind'] != 'item':
        raise Refusal(404, 'not_found', 'No such item in this room.')
    if event == 'accept' and current['item_kind'] == 'question':
        raise Refusal(400, 'invalid', 'A question is answered, not accepted.')
    if current['moderated']:
        raise Refusal(409, 'moderated', 'A moderated item cannot be decided.')
    if current['state'] not in ('proposed', 'amended'):
        raise Refusal(409, 'final', 'That item is already decided.')
    if event == 'accept':
        if current['item_kind'] == 'question':
            raise Refusal(400, 'invalid', 'A question is answered, not accepted.')
        if record is None:
            raise Refusal(422, 'invalid', 'An accepted item needs the record it became.')
        record = check_record(record)
        check_evidence(record['evidence'])
        if record['evidence'] != _item_evidence(room_id, sequence):
            raise Refusal(422, 'invalid', 'The record pointer does not name this room item.')
    else:
        if record is not None:
            raise Refusal(422, 'invalid', 'Only an acceptance carries a record pointer.')
    if event == 'answer':
        if current['item_kind'] != 'question':
            raise Refusal(400, 'invalid', 'Only a question is answered.')
        text = check_item_text(text, limits_values['max_message_chars'], 'An answer')
    elif event == 'reject':
        text = check_reason(text)
    elif event == 'amend':
        title = check_title(title)
        text = check_item_text(text, limits_values['max_message_chars'], 'An amendment')
    else:
        if not isinstance(text, str) or len(text) > limits_values['max_message_chars']:
            raise Refusal(422, 'invalid', 'An acceptance note is at most %d characters'
                          % limits_values['max_message_chars'])
        if text:
            _plain_text(text, 'An acceptance note')
    line = {'schema_version': SCHEMA_VERSION, 'sequence': lines[-1]['sequence'] + 1,
            'operation_id': operation_id, 'kind': 'item-event',
            'target_sequence': sequence, 'event': event, 'state': ITEM_EVENTS[event],
            'author': dict(author), 'created_at': now_iso(), 'text': text, 'record': record}
    if event == 'amend':
        line['title'] = title
    _append(project, room_id, line, limits_values)
    view = next(item for item in items_from_lines(load_lines(project, room_id), room_id)
                if item['sequence'] == sequence)
    return dict(view, replayed=False)


def accept_item(project, room_id, sequence, operation_id, author, limits_values, record, text=''):
    return _decide_item(project, room_id, sequence, 'accept', operation_id, author,
                        limits_values, text=text, record=record)


def reject_item(project, room_id, sequence, operation_id, author, limits_values, reason):
    return _decide_item(project, room_id, sequence, 'reject', operation_id, author,
                        limits_values, text=reason)


def amend_item(project, room_id, sequence, operation_id, author, limits_values, title, text):
    return _decide_item(project, room_id, sequence, 'amend', operation_id, author,
                        limits_values, text=text, title=title)


def answer_item(project, room_id, sequence, operation_id, author, limits_values, text):
    return _decide_item(project, room_id, sequence, 'answer', operation_id, author,
                        limits_values, text=text)


def moderate(project, room_id, target_sequence, reason, operation_id, author, limits_values):
    operation_id = check_operation_id(operation_id)
    reason = check_reason(reason)
    if type(target_sequence) is not int or target_sequence < 1:
        raise Refusal(422, 'invalid', 'target_sequence must be a positive whole number')
    room = read_room(project, room_id)
    lines = load_lines(project, room_id)
    existing = _find_operation(lines, operation_id)
    if existing is not None:
        if existing['kind'] == 'tombstone' and existing['target_sequence'] == target_sequence and \
                existing['reason'] == reason:
            return dict(_tombstone_view(existing), replayed=True)
        raise Refusal(409, 'operation_reused',
                      'That operation id already wrote a different room line.')
    target = next((line for line in lines if line['sequence'] == target_sequence), None)
    if target is None or target['kind'] not in ('message', 'item'):
        raise Refusal(404, 'not_found', 'No such message or item in this room.')
    if any(line['kind'] == 'tombstone' and line['target_sequence'] == target_sequence for line in lines):
        raise Refusal(409, 'already_moderated', 'That line is already a tombstone.')
    at = now_iso()
    text = 'removed by %s at %s: %s' % (author['name'], at, reason)
    line = {'schema_version': SCHEMA_VERSION, 'sequence': lines[-1]['sequence'] + 1,
            'operation_id': operation_id, 'kind': 'tombstone',
            'target_sequence': target_sequence, 'reason': reason, 'author': dict(author),
            'created_at': at, 'text': text}
    _append(project, room_id, line, limits_values)
    return dict(_tombstone_view(line), replayed=False)


# -- export ---------------------------------------------------------------------------------------
def _fenced(text):
    """A fenced code block around one message body (review s4).

    A body is literal text: without a fence a message starting ``### #2 Carol (owner)``
    forges a message heading in the Markdown export. The fence is longer than any run of
    backticks in the body, so a body cannot close it early.
    """
    longest = 0
    for match in re.finditer('`+', text):
        longest = max(longest, len(match.group(0)))
    fence = '`' * max(3, longest + 1)
    return fence + '\n' + text + '\n' + fence


def export_markdown(project, room_id):
    room = read_room(project, room_id)
    lines = load_lines(project, room_id)
    tombstones, _ = _tombstones(lines)
    items = {view['sequence']: view for view in items_from_lines(lines, room_id)}
    out = ['# %s' % room['name'], '',
           'State: %s' % room['state'],
           'Created by %s at %s' % (room['created_by_name'], room['created_at'])]
    if room['state'] == 'archived':
        out.append('Archived by %s at %s%s' % (
            room['archived_by_name'], room['archived_at'],
            (': ' + room['archive_reason']) if room.get('archive_reason') else ''))
    out.append('')
    for line in lines:
        if line['kind'] == 'tombstone':
            continue
        tombstone = tombstones.get(line['sequence'])
        if line['kind'] == 'message':
            body = tombstone if tombstone else line
            out.append('### #%d %s (%s), %s' % (line['sequence'], body['author']['name'],
                                                body['author']['role'], body['created_at']))
            out.append('')
            out.append(_fenced(body['text']))
            out.append('')
        elif line['kind'] == 'item':
            view = items.get(line['sequence']) or {}
            author = view.get('author') or line['author']
            out.append('### #%d Item: %s (%s) - %s (%s), %s' % (
                line['sequence'], view.get('title') or '(removed)', line['item_kind'],
                author['name'], author['role'], view.get('created_at') or line['created_at']))
            out.append('')
            out.append('State: %s  Evidence: %s' % (view.get('state'),
                                                    view.get('evidence') or ''))
            if view.get('record'):
                out.append('Record: %s' % json.dumps(view['record'], ensure_ascii=False,
                                                     sort_keys=True))
            out.append('')
            out.append(_fenced(view.get('text') or ''))
            out.append('')
        elif line['kind'] == 'item-event':
            target = tombstones.get(line['target_sequence'])
            text = target['text'] if target is not None else line['text']
            record = None if target is not None else line['record']
            out.append('### #%d Item %s on #%d - %s (%s), %s' % (
                line['sequence'], line['event'], line['target_sequence'],
                line['author']['name'], line['author']['role'], line['created_at']))
            out.append('')
            if record:
                out.append('Record: %s' % json.dumps(record, ensure_ascii=False, sort_keys=True))
                out.append('')
            if text:
                out.append(_fenced(text))
                out.append('')
    return '\n'.join(out).rstrip() + '\n'


def export_jsonl(project, room_id):
    """The raw JSON lines, with every moderated line's text replaced by its tombstone.

    The original text is not in this export in any form; it remains only in the stored
    segment and the backup, until an operator purges it (design R10).
    """
    lines = load_lines(project, room_id)
    tombstones, _ = _tombstones(lines)
    out = []
    for line in lines:
        tombstone = None
        if line['kind'] in ('message', 'item'):
            tombstone = tombstones.get(line['sequence'])
        if tombstone is not None:
            safe = {'schema_version': SCHEMA_VERSION, 'sequence': line['sequence'],
                    'operation_id': tombstone['operation_id'], 'kind': 'tombstone',
                    'target_sequence': line['sequence'], 'reason': tombstone['reason'],
                    'author': dict(tombstone['author']), 'created_at': tombstone['created_at'],
                    'text': tombstone['text']}
        elif line['kind'] == 'item-event' and line['target_sequence'] in tombstones:
            safe = dict(line, text=tombstones[line['target_sequence']]['text'], record=None)
            safe.pop('title', None)
        else:
            safe = dict(line)
        out.append(json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return '\n'.join(out) + ('\n' if out else '')


# -- its own backup file (backups/<name>.rooms.json) ----------------------------------------------
def _backup_files(project):
    root = rooms_root(project)
    if not root.exists():
        return None
    files = {}
    if not root.is_dir():
        raise Refusal(500, 'damaged', 'The project rooms path is not a folder.')
    index = root / INDEX
    _refuse_symlink(index)
    if index.is_file():
        files[INDEX] = index.read_bytes()
    for room_id in _room_ids(project):
        folder = room_dir(project, room_id)
        room = folder / ROOM
        _refuse_symlink(room)
        if not room.is_file():
            raise Refusal(500, 'damaged', 'Room %s has no room record.' % room_id)
        files[room_id + '/' + ROOM] = room.read_bytes()
        for number, path in _segments(project, room_id):
            files[room_id + '/' + path.name] = path.read_bytes()
        for item in folder.iterdir():
            if QUARANTINE_NAME.fullmatch(item.name):
                _refuse_symlink(item)
                files[room_id + '/' + item.name] = item.read_bytes()
    return files


def backup_record(project):
    """The self-contained record for ``backups/<name>.rooms.json``, or None with no rooms.

    Every file is carried with its sha256 and byte count, so a restore can verify all of
    it before writing one byte.  The caller holds the project's coordination lock; this
    function takes none.
    """
    files = _backup_files(project)
    if not files:
        return None
    encoded = {}
    for name, content in sorted(files.items()):
        encoded[name] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
                         'base64': base64.b64encode(content).decode('ascii')}
    return {'schema_version': SCHEMA_VERSION, 'status': 'complete',
            'project': Path(project).name, 'generated_at': now_iso(), 'files': encoded}


def _safe_relative(name):
    if not isinstance(name, str) or not name or name.startswith('/') or '\\' in name:
        raise ValueError('Invalid rooms backup path')
    parts = name.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('Invalid rooms backup path')
    if name == INDEX:
        return
    if len(parts) == 2 and ROOM_ID.fullmatch(parts[0]) and (
            parts[1] == ROOM or SEGMENT_NAME.fullmatch(parts[1]) or
            QUARANTINE_NAME.fullmatch(parts[1])):
        return
    raise ValueError('Invalid rooms backup path')


def validate_backup(record):
    """Check a ``.rooms.json`` record completely, before any restore writes."""
    if not isinstance(record, dict) or set(record) != {'schema_version', 'status', 'project',
                                                       'generated_at', 'files'}:
        raise ValueError('Invalid rooms backup')
    if record['schema_version'] != SCHEMA_VERSION or record['status'] != 'complete':
        raise ValueError('Invalid rooms backup: not a complete rooms backup')
    if not isinstance(record['project'], str) or not record['project'].strip():
        raise ValueError('Invalid rooms backup project')
    _timestamp(record['generated_at'], 'rooms backup time')
    if not isinstance(record['files'], dict) or not record['files']:
        raise ValueError('Invalid rooms backup files')
    for name, item in record['files'].items():
        _safe_relative(name)
        if not isinstance(item, dict) or set(item) != {'sha256', 'bytes', 'base64'}:
            raise ValueError('Invalid rooms backup file entry')
        if not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('Invalid rooms backup digest')
        if type(item['bytes']) is not int or item['bytes'] < 0:
            raise ValueError('Invalid rooms backup size')
        try:
            content = base64.b64decode(item['base64'], validate=True)
        except Exception:
            raise ValueError('Invalid rooms backup content') from None
        if len(content) != item['bytes'] or hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ValueError('Rooms backup content does not match its digest (%s)' % name)
    return record


def restore_files(project, record):
    """Write a validated rooms backup into a fresh destination's ``.rooms/``.

    All files are verified by :func:`validate_backup` first; this function only writes.
    The destination's ``.rooms/`` must be absent or empty: a restore never overwrites a
    live rooms folder.
    """
    validate_backup(record)
    root = rooms_root(project)
    if root.exists():
        if not root.is_dir():
            raise ValueError('Restore destination has a non-folder rooms path')
        if any(root.iterdir()):
            raise ValueError('Restore destination already has room files; refusing to overwrite them')
    root.mkdir(parents=True, exist_ok=True)
    items = sorted(record['files'].items(), key=lambda pair: (pair[0] == INDEX, pair[0]))
    for name, item in items:
        content = base64.b64decode(item['base64'], validate=True)
        _atomic_write(root / name, content)
    return len(items)
