"""Project rooms, slice 1: rooms for people (kittrial-5bb.385).

Slice 1 of ``docs/PROJECT_ROOMS_DESIGN.md`` (section 10): append-only room files beside
the project, list/read/post/create/archive/moderate, the web page, a separate backup
file and export. Items, the rooms scope, the scribe and the summary are later slices.

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


def check_name(value):
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', 'A room name must be text')
    text = value.strip()
    if not text or len(text) > 200:
        raise Refusal(422, 'invalid', 'A room name must be 1-200 characters')
    if any((not ch.isprintable()) for ch in text):
        raise Refusal(422, 'invalid', 'A room name must not contain control characters')
    return text


def check_text(value, limit):
    if not isinstance(value, str):
        raise Refusal(422, 'invalid', 'A room post must be text')
    if not value.strip():
        raise Refusal(422, 'invalid', 'A room post must not be empty')
    if len(value) > limit:
        raise Refusal(422, 'too_long', 'A room post is at most %d characters; this one is %d.'
                      % (limit, len(value)))
    if any(ch == '\x00' or (not ch.isprintable() and ch not in '\n\t') for ch in value):
        raise Refusal(422, 'invalid', 'A room post must not contain control characters')
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
    if any((not ch.isprintable()) and ch not in '\n\t' for ch in text):
        raise Refusal(422, 'invalid', 'A moderation reason must not contain control characters')
    return text


def check_operation_id(value):
    if not isinstance(value, str) or not OPERATION_ID.fullmatch(value):
        raise Refusal(422, 'invalid', 'Invalid operation id')
    return value


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
        raise ValueError('Invalid room message')
    kind = line.get('kind')
    required = TOMBSTONE_FIELDS if kind == 'tombstone' else MESSAGE_FIELDS
    if kind not in ('message', 'tombstone') or set(line) != required:
        raise ValueError('Invalid room message')
    if line['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Invalid room message schema')
    if type(line['sequence']) is not int or line['sequence'] < 1:
        raise ValueError('Invalid room message sequence')
    if expected is not None and line['sequence'] != expected:
        raise ValueError('Invalid room message sequence')
    if not isinstance(line['operation_id'], str) or not OPERATION_ID.fullmatch(line['operation_id']):
        raise ValueError('Invalid room message operation id')
    _timestamp(line['created_at'], 'room message time')
    _validate_author(line['author'])
    if not isinstance(line['text'], str) or not line['text'].strip():
        raise ValueError('Invalid room message text')
    if len(line['text']) > 100000:
        raise ValueError('Invalid room message text')
    if kind == 'tombstone':
        if type(line['target_sequence']) is not int or line['target_sequence'] < 1:
            raise ValueError('Invalid room tombstone target')
        if not isinstance(line['reason'], str) or not line['reason'].strip() or len(line['reason']) > 500:
            raise ValueError('Invalid room tombstone reason')
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


def rebuild_index(project):
    """The index rebuilt from the segment files, which are the truth."""
    rooms = {}
    for room_id in _room_ids(project):
        segments = []
        last_sequence = 0
        for number, path in _segments(project, room_id):
            raw = path.read_bytes()
            parsed, _tail = _parse_lines(raw)
            # A torn tail is handled (and quarantined) when the room is read; the index
            # only records the complete lines, so a rebuild never invents a sequence.
            if parsed:
                segments.append({'name': path.name, 'bytes': len(raw),
                                 'count': len(parsed),
                                 'first': parsed[0]['sequence'],
                                 'last': parsed[-1]['sequence']})
                last_sequence = parsed[-1]['sequence']
        rooms[room_id] = {'last_sequence': last_sequence, 'segments': segments}
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
        rebuilt = rebuild_index(project)
    # The index is a cache: compare its segment list with the files, and rebuild when stale.
    stale = False
    for room_id in _room_ids(project):
        entry = rebuilt['rooms'].get(room_id)
        pairs = _segments(project, room_id)
        names = [path.name for _, path in pairs]
        have = [item.get('name') for item in (entry or {}).get('segments', [])]
        if entry is None or names != have:
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
        rebuilt = rebuild_index(project)
    return rebuilt


def write_index(project, index):
    _atomic_json(_index_path(project), index)


def _index_entry(index, room_id):
    return index['rooms'].setdefault(room_id, {'last_sequence': 0, 'segments': []})


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
        room = read_room(project, room_id)
        if room['state'] != 'open' and not include_archived:
            continue
        items.append(dict(_room_view(room), last_sequence=index['rooms'][room_id].get('last_sequence', 0)))
    items.sort(key=lambda item: (item['created_at'], item['id']))
    return items


def create(project, name, actor, limits_values):
    name = check_name(name)
    root = rooms_root(project)
    root.mkdir(parents=True, exist_ok=True)
    index = read_index(project)
    opened = 0
    for room_id in index['rooms']:
        room = read_room(project, room_id)
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
    index['rooms'][room_id] = {'last_sequence': 0, 'segments': []}
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
        messages.append(_redacted_view(line, tombstone) if tombstone else _message_view(line))
        last_visible = line['sequence']
    if not more:
        # Is there a visible message after the page even when no tombstone followed it?
        more = any(line['kind'] != 'tombstone' and line['sequence'] > last_visible for line in lines)
    moderations = [_tombstone_view(line) for line in events if line['sequence'] > since]
    index = read_index(project)
    last_sequence = (index['rooms'].get(room_id) or {}).get('last_sequence', 0)
    if lines:
        last_sequence = max(last_sequence, lines[-1]['sequence'])
    return {'room': _room_view(room), 'messages': messages, 'moderations': moderations,
            'since': since, 'last_sequence': last_sequence,
            'next_since': last_sequence if not more else last_visible, 'has_more': more}


def _find_operation(lines, operation_id):
    for line in reversed(lines):
        if line['operation_id'] == operation_id:
            return line
    return None


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
    if target is None or target['kind'] != 'message':
        raise Refusal(404, 'not_found', 'No such message in this room.')
    if any(line['kind'] == 'tombstone' and line['target_sequence'] == target_sequence for line in lines):
        raise Refusal(409, 'already_moderated', 'That message is already a tombstone.')
    at = now_iso()
    text = 'removed by %s at %s: %s' % (author['name'], at, reason)
    line = {'schema_version': SCHEMA_VERSION, 'sequence': lines[-1]['sequence'] + 1,
            'operation_id': operation_id, 'kind': 'tombstone',
            'target_sequence': target_sequence, 'reason': reason, 'author': dict(author),
            'created_at': at, 'text': text}
    _append(project, room_id, line, limits_values)
    return dict(_tombstone_view(line), replayed=False)


# -- export ---------------------------------------------------------------------------------------
def export_markdown(project, room_id):
    room = read_room(project, room_id)
    lines = load_lines(project, room_id)
    tombstones, _ = _tombstones(lines)
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
        body = tombstone if tombstone else line
        out.append('### #%d %s (%s), %s' % (line['sequence'], body['author']['name'],
                                            body['author']['role'], body['created_at']))
        out.append('')
        out.append(body['text'])
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


def export_jsonl(project, room_id):
    """The raw JSON lines, with every moderated original's text replaced by its tombstone.

    The original text is not in this export in any form; it remains only in the stored
    segment and the backup, until an operator purges it (design R10).
    """
    lines = load_lines(project, room_id)
    tombstones, _ = _tombstones(lines)
    out = []
    for line in lines:
        tombstone = tombstones.get(line['sequence']) if line['kind'] == 'message' else None
        if tombstone is not None:
            safe = {'schema_version': SCHEMA_VERSION, 'sequence': line['sequence'],
                    'operation_id': tombstone['operation_id'], 'kind': 'tombstone',
                    'target_sequence': line['sequence'], 'reason': tombstone['reason'],
                    'author': dict(tombstone['author']), 'created_at': tombstone['created_at'],
                    'text': tombstone['text']}
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
