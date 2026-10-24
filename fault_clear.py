#!/usr/bin/env python3
"""The coordinator's clear of a lane's ``needs-investigation`` flag (kittrial-5bb.398).

A lane raises the flag with a ``lane-state-v1`` record whose state is ``needs-investigation``
(lane_state.py): a one-line untrusted reason and an optional task. It stays shown on the lane
board and in the coordinator wait until a COORDINATOR clears it, so a fault cannot quietly
disappear. Neither the lane nor another member can clear it, and a later state record of the
lane (working, waiting...) never does. A lane may raise it again with a new reason; the newest
raise is the one that is shown and the one a clear must name.

A clear is ONE reserved native record kind, ``lane-fault-clear-v1``, modelled on
``assignee-followon-v1`` (followon.py): it is honoured ONLY when its matching host journal
entry exists in ``<project>/.lane-fault-clears/<sha256-of-canonical-payload>.json``. On the
SSH/endpoint path the native comment author is only the caller's declared actor, so a comment
alone proves nothing; the journal is written by the coordination host under the project lock,
by the coordinator command or the host command, both of which require a configured operator.
Anything else (a hand-written comment, a kit that predates this one) is inert: the flag
stays shown. The record carries who cleared it and when (the journal entry and the comment
both do), which is the audit.

The journal lives outside the tracker and is read fresh on every call. A read with no
journal (``None``) honours no clear, so the flag stays shown (fail closed).
"""
import re
from pathlib import Path

import lane_state
import record_json
from requirements import canonical_bytes, content_hash
from reserved_comments import LANE_FAULT_CLEAR_PREFIX as PREFIX

KIND = 'lane-fault-clear-v1'
JOURNAL_KIND = 'lane-fault-clear'
JOURNAL_DIR = '.lane-fault-clears'
#: Bound on the clearing reason and on one journal file (a planted giant file is never read whole).
REASON_LIMIT = 300
JOURNAL_FILE_LIMIT = 64000

FIELDS = frozenset({'schema_version', 'task', 'target', 'lane', 'reason', 'actor'})
#: The caller supplies only these; the lane and the actor are derived by the server.
REQUEST_FIELDS = frozenset({'schema_version', 'task', 'target', 'reason'})
JOURNAL_FIELDS = frozenset({'schema_version', 'kind', 'task', 'comment_id', 'actor', 'payload',
                            'sha256', 'native_order'})
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}\Z')


def _refuse(message):
    return ValueError(message.rstrip() + ' Nothing was written.')


def _identity(value, label):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise _refuse('The %s is invalid.' % label)
    return value


def _actor(value, label='record actor'):
    if not isinstance(value, str) or not ACTOR.fullmatch(value):
        raise _refuse('The %s is invalid.' % label)
    return value


def _reason(value):
    if not isinstance(value, str) or not value.strip():
        raise _refuse('A clearing reason must be nonempty text.')
    if len(value) > REASON_LIMIT:
        raise _refuse('A clearing reason is limited to %d characters.' % REASON_LIMIT)
    if not value.isprintable():
        raise _refuse('A clearing reason must be one line without control or direction characters.')
    return value


def _check(value, allowed, where):
    if not isinstance(value, dict):
        raise _refuse('The %s must be a JSON object.' % where)
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _refuse('The %s carries a field it does not take: %s.'
                      % (where, ', '.join(str(name) for name in unknown[:5])))
    if value.get('schema_version') != 1 or type(value.get('schema_version')) is not int:
        raise _refuse('The %s needs schema_version 1.' % where)
    _identity(value.get('task'), 'task id')
    _identity(value.get('target'), 'raise record id')
    _reason(value.get('reason'))


def request_payload(value):
    """Validate the caller's request (task, target, reason) before any fact is derived."""
    _check(value, REQUEST_FIELDS, 'fault-clear request')
    return value


def payload_check(value, where='record'):
    _check(value, FIELDS, where + ' payload')
    _actor(value.get('lane'), 'lane actor')
    _actor(value.get('actor'))
    return value


def body(payload):
    """The exact native comment text for one validated payload."""
    return PREFIX + canonical_bytes(payload).decode('utf-8')


def parse_text(text):
    """The payload a clear comment carries, or ``ValueError``; a byte edit is not the same record."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise ValueError('not a lane fault clear')
    raw = text[len(PREFIX):]
    try:
        value = record_json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('not canonical JSON') from None
    if not isinstance(value, dict) or canonical_bytes(value).decode('utf-8') != raw:
        raise ValueError('not canonical JSON')
    return value


def _directory(project):
    """The journal directory, or None (fail closed): absent, a symlink or not a directory."""
    if project is None:
        return None
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink() or not directory.is_dir():
        return None
    return directory


def open_journal(project):
    """The journal directory for a WRITER, created if needed; refuses before any native write."""
    if project is None:
        raise _refuse('Clearing a fault requires the project directory that holds the host-issued '
                      + JOURNAL_DIR + ' journal; none was supplied.')
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink():
        raise _refuse('Refusing ' + JOURNAL_DIR + ': the path is a symlink.')
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise _refuse('Cannot create ' + JOURNAL_DIR + ': %s.' % (exc,))
    if not directory.is_dir():
        raise _refuse(JOURNAL_DIR + ' is not a directory; refusing to clear a fault.')
    probe = directory / ('.writable-%d.tmp' % id(directory))
    try:
        probe.write_text('probe', encoding='utf-8')
        probe.unlink()
    except OSError as exc:
        raise _refuse('Cannot use ' + JOURNAL_DIR + ': the directory is not writable (%s).' % (exc,)) from None
    return directory


def journal_entry(payload, comment_id, actor, native_order):
    entry = {'schema_version': 1, 'kind': JOURNAL_KIND, 'task': payload['task'],
             'comment_id': str(comment_id), 'actor': actor, 'payload': payload,
             'native_order': int(native_order), 'sha256': content_hash(payload)}
    validate_entry(entry)
    return entry


def validate_entry(entry, name=None):
    """One journal entry, its payload and its ``<sha256>.json`` path/hash binding."""
    if not isinstance(entry, dict) or set(entry) != set(JOURNAL_FIELDS):
        raise ValueError('Invalid lane fault clear journal entry')
    if entry.get('schema_version') != 1 or entry.get('kind') != JOURNAL_KIND:
        raise ValueError('Invalid lane fault clear journal entry')
    payload = payload_check(entry.get('payload'), 'journal')
    if entry.get('task') != payload['task']:
        raise ValueError('Lane fault clear journal entry does not match its payload')
    _identity(entry.get('comment_id'), 'comment id')
    _actor(entry.get('actor'))
    order = entry.get('native_order')
    if type(order) is not int or order < 0:
        raise ValueError('Invalid lane fault clear journal native order')
    digest = content_hash(payload)
    if entry.get('sha256') != digest or (name is not None and name != digest + '.json'):
        raise ValueError('Lane fault clear journal entry hash or path mismatch')
    return entry


def publish(project, entry):
    """Persist one validated journal entry atomically."""
    from coordination import atomic
    directory = open_journal(project)
    path = directory / (entry['sha256'] + '.json')
    if path.is_symlink():
        raise _refuse('Refusing journal entry ' + path.name + ': it is a symlink.')
    atomic(path, entry)
    return path


def host_journal(project):
    """The host journal read fresh, as ``(entries, invalid)`` keyed by native comment id.

    Fail closed: an absent, unreadable or symlinked directory yields nothing, so no clear is
    trusted. A malformed, misnamed, oversized, symlinked or conflicting entry is ignored and
    named in ``invalid``.
    """
    entries, invalid = {}, []
    directory = _directory(project)
    if directory is None:
        return entries, invalid
    try:
        names = sorted(path.name for path in directory.glob('*.json'))
    except OSError:
        return entries, invalid
    for name in names:
        path = directory / name
        try:
            if path.is_symlink() or path.stat().st_size > JOURNAL_FILE_LIMIT:
                raise ValueError('unusable')
            entry = validate_entry(record_json.loads(path.read_text(encoding='utf-8')), name)
        except (OSError, ValueError, TypeError, KeyError):
            invalid.append(JOURNAL_DIR + '/' + name)
            continue
        if entry['comment_id'] in entries:
            invalid.append(JOURNAL_DIR + '/' + name)
            continue
        entries[entry['comment_id']] = entry
    return entries, invalid


def _stamp(comment):
    return str(comment.get('created_at') or '')


def raises(issue):
    """Every usable needs-investigation record on one task: ``(author, stamp, index, comment, payload)``."""
    task = str(issue.get('id'))
    comments = [c for c in (issue.get('comments') or []) if isinstance(c, dict)]
    ids = {str(c.get('id')) for c in comments if c.get('id') is not None}
    found = []
    for index, comment in enumerate(comments):
        payload = lane_state.parse(comment.get('text'))
        if payload is None or payload.get('state') != lane_state.FAULT_STATE:
            continue
        named = payload.get('checkpoint')
        if not isinstance(named, str) or named not in ids or lane_state.problems(payload, task, named):
            continue
        author = comment.get('author')
        if isinstance(author, str) and author:
            found.append((author, _stamp(comment), index, comment, payload))
    return found


def honoured_clears(rows, entries):
    """``{raise_comment_id: clear info}`` for every clear whose host journal entry matches."""
    cleared = {}
    for issue in rows:
        if not isinstance(issue, dict) or not issue.get('id'):
            continue
        for position, comment in enumerate(issue.get('comments') or []):
            if not isinstance(comment, dict) or not isinstance(comment.get('text'), str) \
                    or not comment['text'].startswith(PREFIX):
                continue
            try:
                payload = payload_check(parse_text(comment['text']))
            except ValueError:
                continue
            entry = entries.get(str(comment.get('id')))
            if entry is None or entry.get('payload') != payload or payload['task'] != issue.get('id'):
                continue
            cleared[payload['target']] = {'by': payload['actor'], 'at': comment.get('created_at'),
                                          'reason': payload['reason']}
    return cleared


def open_faults(rows, entries=None):
    """``{lane actor: fault}`` for each lane whose NEWEST needs-investigation raise is not cleared.

    A raise anywhere in the project counts, closed task or not: a fault must not disappear
    because the task it was raised on was closed. ``entries`` is the host journal; ``None``
    honours no clear. The fault carries the raise's own task, its optional ``about_task``,
    the display-safe reason and the raise time; nothing here is derived from another record.
    """
    newest = {}
    for issue in rows:
        if not isinstance(issue, dict) or not issue.get('id') or issue.get('malformed'):
            continue
        for author, stamp, index, comment, payload in raises(issue):
            key = (stamp, str(issue['id']), index)
            if author not in newest or key > newest[author][0]:
                newest[author] = (key, issue, comment, payload)
    cleared = honoured_clears(rows, entries or {}) if entries else {}
    faults = {}
    for author, (_key, issue, comment, payload) in newest.items():
        if str(comment.get('id')) in cleared:
            continue
        faults[author] = {'record': str(comment.get('id')), 'task': str(issue['id']),
                          'about_task': payload.get('about_task'),
                          'reason': lane_state.display_reason(payload.get('reason')),
                          'since': comment.get('created_at')}
    return faults


def _rows(run):
    from lifecycle import read_event_rows
    return read_event_rows(run)


def apply(payload, actor, run, project, operators=None):
    """Validate one request, derive its facts under the project lock, and clear the fault.

    The caller names the task and the raise record it saw (``target``) and gives a reason.
    The lane is the raise record's author, and the record must be that lane's CURRENT
    uncleared fault, so a clear can never hide a newer raise it did not read. An exact retry
    of a clear already on the tracker completes its journal entry instead of writing twice.
    """
    _actor(actor, 'actor')
    request = request_payload(payload)
    rows = _rows(run)
    task, target = request['task'], request['target']
    matches = [row for row in rows if isinstance(row, dict) and row.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise _refuse('Task ' + str(task) + ' is missing, duplicated or is an event.')
    issue = matches[0]
    record = next((item for item in raises(issue) if str(item[3].get('id')) == target), None)
    if record is None:
        raise _refuse('No needs-investigation record ' + target + ' exists on task ' + task + '.')
    lane = record[0]
    entries, _invalid = host_journal(project)
    full = payload_check({'schema_version': 1, 'task': task, 'target': target, 'lane': lane,
                          'reason': request['reason'], 'actor': actor})
    for position, comment in enumerate(issue.get('comments') or []):
        if not isinstance(comment, dict) or not isinstance(comment.get('text'), str):
            continue
        try:
            existing = parse_text(comment['text'])
        except ValueError:
            continue
        if existing == full and comment.get('author') == actor:
            cid = str(comment.get('id'))
            entry = entries.get(cid)
            if entry is None:
                publish(project, journal_entry(full, cid, actor, position))
            elif entry.get('payload') != full:
                raise _refuse('The host journal for comment ' + cid + ' records a different payload.')
            return {'comment_id': cid, 'task': task, 'target': target, 'lane': lane, 'reconciled': True}
    faults = open_faults(rows, entries)
    current = faults.get(lane)
    if current is None:
        raise _refuse('That fault is already cleared.')
    if current['record'] != target:
        raise _refuse('Lane ' + lane + ' raised a newer needs-investigation record (' + current['record']
                      + ' on ' + current['task'] + '); clear that one after reading it.')
    open_journal(project)
    result = record_json.loads(run(['comments', 'add', task, body(full), '--json']))
    cid = str(result['id'])
    try:
        publish(project, journal_entry(full, cid, actor, len(issue.get('comments') or [])))
    except ValueError as exc:
        raise ValueError('Clear comment ' + cid + ' was written but its host journal entry could not '
                         'be persisted (%s). The clear is inert until it is: re-run the same clear to '
                         'persist the entry.' % (exc,)) from None
    return {'comment_id': cid, 'task': task, 'target': target, 'lane': lane, 'reconciled': False}
