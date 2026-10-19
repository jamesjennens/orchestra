"""The coordinator's recorded hand-back of an integrated task to its assignee.

A task whose contribution is integrated but which is still open used to be the
coordinator's move only (kittrial-5bb.336): close it, or record what remains. That
left the worker unable to see the remaining scope as its own move. This module adds
ONE reserved native record kind, ``assignee-followon-v1``, by which a coordinator
records that remaining scope and hands the move back to the assignee.

Authority is a host journal, exactly as for integration reverts. On the SSH/endpoint
path the stored native comment author is the caller's self-declared actor, so a
native comment alone is not proof the coordination host issued it. A record is
honoured ONLY when its matching entry is present in
``<project>/.assignee-followons/<sha256-of-canonical-payload>.json``. A comment
posted by any other route (the worker transport, a hand-written comment, a kit that
predates this one) is inert: it changes no move and reads only as an ordinary
coordinator comment when its author is not the assignee.

The record is deliberately NOT a contribution-review operation. It has its own
reserved ``Kind:`` prefix, so a kit that predates it ignores it completely (the
same rollback-safe shape as ``integration-revert-v1``) and no contribution, review
or lifecycle fact is touched. It creates no review request, adds no pending item
and cannot be answered with a ``respond`` record.

It IS one kind of direction: the coordinator's note the assignee must disposition
in a checkpoint's ``directions`` field. The ONLY extra is the move consequence, and
that consequence comes only from the journalled, gated record. A withdrawal record
is the coordinator's own ``superseded`` disposition of that direction, so the
direction reader must not list the withdrawal itself as another direction.

The journal lives OUTSIDE the tracker. Every read of it is a fresh read of the
project directory: it is never served from, keyed on, or cached with the export
rows. A journal write does not move the tracker export, so a read cache keyed on
the export (the in-memory HTTP read cache, or the ``.read-cache/`` directory a
later slice adds) must never answer a journal read. In particular the work queue
reads the journal ONCE per page, separate from the rows, and each read surface
uses that fresh result. The bounded consequence is the same one the revert journal
lives with: a coordinator's own write drops that principal's cache, while another
principal can keep a cached queue row for the cache TTL, so the move flip may take
that long to appear for them. That is immaterial to a blocking wait and is not
worked around here.

Ceiling values (text 600 characters, reason 1000) are bounds on the recorded text,
not on the escaped display form; the display form is escaped exactly once, as
checkpoint wait text is.
"""
import re
from pathlib import Path

import record_json
from requirements import canonical_bytes, content_hash

#: The reserved native comment kind. Its own prefix keeps every older reader away
#: from it: no existing chain validates it, so a rollback stays safe.
PREFIX = 'Kind: assignee-followon-v1\n'
KIND = 'assignee-followon-v1'
JOURNAL_KIND = 'assignee-followon'
#: Host-issued journal directory inside one project directory.
JOURNAL_DIR = '.assignee-followons'
#: Bound on the recorded hand-back text. A longer text is refused before any write.
TEXT_LIMIT = 600
#: Bound on a withdrawal reason (the same limit the review chain uses for reasons).
REASON_LIMIT = 1000
#: Bound on one journal file, so a planted giant file cannot be read into memory.
JOURNAL_FILE_LIMIT = 256000
#: Bound on the ``why`` text a read carries.
WHY_LIMIT = 300

OPERATION_RECORD = 'record'
OPERATION_WITHDRAW = 'withdraw'
OPERATIONS = (OPERATION_RECORD, OPERATION_WITHDRAW)

RECORD_FIELDS = frozenset({'schema_version', 'operation', 'task', 'contribution',
                           'commit', 'integration', 'integration_order', 'text', 'actor'})
WITHDRAW_FIELDS = frozenset({'schema_version', 'operation', 'task', 'target', 'reason', 'actor'})
#: The caller supplies only these; the server derives every other record fact under
#: the project lock (the current contribution, its integration evidence and order).
RECORD_REQUEST_FIELDS = frozenset({'schema_version', 'operation', 'task', 'text'})
WITHDRAW_REQUEST_FIELDS = frozenset({'schema_version', 'operation', 'task', 'target', 'reason'})
JOURNAL_FIELDS = frozenset({'schema_version', 'kind', 'operation', 'task', 'comment_id',
                            'actor', 'payload', 'sha256', 'native_order'})
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}\Z')
COMMIT = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})\Z')
SHA256 = re.compile(r'[a-f0-9]{64}\Z')


def _refuse(message):
    """Every refusal ends with the same sentence, so a caller always knows nothing was written."""
    return ValueError(message.rstrip() + ' Nothing was written.')


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip():
        raise _refuse('A %s must be nonempty text.' % label)
    if len(value) > limit:
        raise _refuse('A %s is limited to %d characters.' % (label, limit))
    return value


def _actor(value):
    if not isinstance(value, str) or not ACTOR.fullmatch(value):
        raise _refuse('The record actor is invalid.')
    return value


def _identity(value, label):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise _refuse('The %s is invalid.' % label)
    return value


def _payload(value, where='record'):
    if not isinstance(value, dict):
        raise _refuse('The %s payload must be a JSON object.' % where)
    operation = value.get('operation')
    if operation not in OPERATIONS:
        raise _refuse('The %s payload has no valid operation (record or withdraw).' % where)
    allowed = RECORD_FIELDS if operation == OPERATION_RECORD else WITHDRAW_FIELDS
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _refuse('The %s payload carries a field it does not take: %s.'
                      % (where, ', '.join(str(name) for name in unknown[:5])))
    if value.get('schema_version') != 1:
        raise _refuse('The %s payload needs schema_version 1.' % where)
    _identity(value.get('task'), 'task id')
    _actor(value.get('actor'))
    if operation == OPERATION_RECORD:
        _identity(value.get('contribution'), 'contribution id')
        if not isinstance(value.get('commit'), str) or not COMMIT.fullmatch(value['commit'].lower()):
            raise _refuse('The integration commit is not a 40 or 64 character hexadecimal revision.')
        _identity(value.get('integration'), 'integration record id')
        if type(value.get('integration_order')) is not int or value['integration_order'] < 0:
            raise _refuse('The integration record order is invalid.')
        _text(value.get('text'), 'hand-back text', TEXT_LIMIT)
    else:
        _identity(value.get('target'), 'follow-on record id')
        _text(value.get('reason'), 'withdrawal reason', REASON_LIMIT)
    return value


def request_payload(value):
    """Validate the caller's request (task+text or followon+reason) before facts are derived.

    The caller never supplies a contribution, commit, integration record or actor:
    the server derives those under the lock. The request may carry an optional
    ``task`` for a withdrawal (the web path names it), which is checked against the
    target's own task before anything is written.
    """
    if not isinstance(value, dict):
        raise _refuse('The follow-on request must be a JSON object.')
    operation = value.get('operation')
    if operation not in OPERATIONS:
        raise _refuse('Use operation record (task and text) or withdraw (followon and reason).')
    allowed = RECORD_REQUEST_FIELDS if operation == OPERATION_RECORD else WITHDRAW_REQUEST_FIELDS
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _refuse('The follow-on request carries a field it does not take: %s.'
                      % ', '.join(str(name) for name in unknown[:5]))
    if value.get('schema_version') != 1:
        raise _refuse('The follow-on request needs schema_version 1.')
    if operation == OPERATION_RECORD:
        _identity(value.get('task'), 'task id')
        _text(value.get('text'), 'hand-back text', TEXT_LIMIT)
    else:
        _identity(value.get('target'), 'follow-on record id')
        _text(value.get('reason'), 'withdrawal reason', REASON_LIMIT)
        if 'task' in value:
            _identity(value['task'], 'task id')
    return value


def body(payload):
    """The exact native comment text for one validated payload."""
    return PREFIX + canonical_bytes(payload).decode('utf-8')


def parse_text(text):
    """The payload one native comment carries, or ``ValueError``.

    The body must be the prefix followed by the canonical JSON of the payload:
    a byte edit is not accepted as the same record.
    """
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise ValueError('not a follow-on record')
    raw = text[len(PREFIX):]
    try:
        value = record_json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('not canonical JSON') from None
    if not isinstance(value, dict):
        raise ValueError('not a JSON object')
    if canonical_bytes(value).decode('utf-8') != raw:
        raise ValueError('not canonical JSON')
    return value


def withdrawal_text(text):
    """True when a native comment body is a follow-on WITHDRAWAL record.

    The withdrawal is the coordinator's own ``superseded`` disposition of the
    direction. The direction reader must not list the withdrawal itself as another
    outstanding direction; the record it withdraws is the direction. A malformed
    body is not claimed as a withdrawal (it reads as an ordinary comment).
    """
    try:
        return parse_text(text).get('operation') == OPERATION_WITHDRAW
    except (ValueError, TypeError, KeyError):
        return False


def _journal_directory(project):
    """The host journal directory inside one project directory, or None (fail-closed).

    An absent project directory, an absent path, a symlink or a path that is not a
    directory means no journal entry can be proved, so no record is honoured.
    """
    if project is None:
        return None
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink() or not directory.is_dir():
        return None
    return directory


def _journal_writable(directory):
    """Prove ``directory`` accepts a write, or refuse before a native write.

    ``mkdir(exist_ok=True)`` only proves the path exists; the probe is the real
    check (``os.access`` lies for a privileged process, while create-and-remove
    fails exactly when the journal write would). The probe name cannot collide with
    an entry and is removed again.
    """
    from uuid import uuid4
    probe = directory / ('.writable-' + uuid4().hex + '.tmp')
    try:
        probe.write_text('probe', encoding='utf-8')
    except OSError as exc:
        raise _refuse('Cannot use ' + JOURNAL_DIR + ': the directory is not writable (%s).' % (exc,)) from None
    try:
        probe.unlink()
    except OSError:
        pass
    return directory


def open_journal(project):
    """The journal directory for a WRITER; refuses when it cannot be used.

    Called BEFORE the native comment, so a host that cannot persist the entry
    writes nothing at all instead of leaving a record no reader would honour.
    """
    if project is None:
        raise _refuse('Recording a hand-back requires the project directory that holds the '
                      'host-issued ' + JOURNAL_DIR + ' journal; none was supplied.')
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink():
        raise _refuse('Refusing ' + JOURNAL_DIR + ': the path is a symlink.')
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise _refuse('Cannot create ' + JOURNAL_DIR + ': %s.' % (exc,))
    if not directory.is_dir():
        raise _refuse(JOURNAL_DIR + ' is not a directory; refusing to record a hand-back.')
    return _journal_writable(directory)


def journal_entry(payload, comment_id, actor, native_order):
    """One validated host journal entry, bound to the native comment it was written for."""
    entry = {'schema_version': 1, 'kind': JOURNAL_KIND, 'operation': payload['operation'],
             'task': payload['task'], 'comment_id': str(comment_id), 'actor': actor,
             'payload': payload, 'native_order': int(native_order),
             'sha256': content_hash(payload)}
    validate_entry(entry)
    return entry


def validate_entry(entry, name=None):
    """Validate one journal entry, its payload and its ``<sha256>.json`` path/hash binding."""
    if not isinstance(entry, dict) or set(entry) != set(JOURNAL_FIELDS):
        raise ValueError('Invalid follow-on journal entry')
    if entry.get('schema_version') != 1 or entry.get('kind') != JOURNAL_KIND:
        raise ValueError('Invalid follow-on journal entry')
    payload = _payload(entry.get('payload'), 'journal')
    if entry.get('operation') != payload['operation'] or entry.get('task') != payload['task']:
        raise ValueError('Follow-on journal entry does not match its payload')
    _identity(entry.get('comment_id'), 'comment id')
    _actor(entry.get('actor'))
    order = entry.get('native_order')
    if type(order) is not int or order < 0:
        raise ValueError('Invalid follow-on journal native order')
    digest = content_hash(payload)
    if entry.get('sha256') != digest:
        raise ValueError('Follow-on journal entry hash does not match its payload')
    if name is not None and name != digest + '.json':
        raise ValueError('Follow-on journal path mismatch')
    return entry


def publish(project, entry):
    """Persist one validated journal entry atomically; returns its path."""
    from coordination import atomic
    directory = open_journal(project)
    path = directory / (entry['sha256'] + '.json')
    if path.is_symlink():
        raise _refuse('Refusing follow-on journal entry ' + path.name + ': it is a symlink.')
    atomic(path, entry)
    return path


def host_journal(project):
    """Read the host follow-on journal fresh, as ``(entries, invalid)``.

    Fail-closed: an absent, unreadable or symlinked directory yields two empty
    results, so no record is trusted. Entries are keyed by the native comment id
    they were written for. A malformed, misnamed or conflicting entry is ignored
    and reported as ``.assignee-followons/<name>`` in ``invalid``.

    This is its own read of the project directory on EVERY call. It is never
    served from, keyed on, or cached with the export rows: the journal lives
    outside the tracker, so a journal write does not move the export, and no read
    cache keyed on a dolt commit or an export snapshot may answer it.
    """
    entries = {}
    invalid = []
    directory = _journal_directory(project)
    if directory is None:
        return entries, invalid
    try:
        names = sorted(path.name for path in directory.glob('*.json'))
    except OSError:
        return entries, invalid
    for name in names:
        path = directory / name
        try:
            if path.is_symlink():
                raise ValueError('symlink')
            if path.stat().st_size > JOURNAL_FILE_LIMIT:
                raise ValueError('oversized')
            entry = validate_entry(record_json.loads(path.read_text(encoding='utf-8')), name)
        except (OSError, ValueError, TypeError, KeyError):
            invalid.append(JOURNAL_DIR + '/' + name)
            continue
        if entry['comment_id'] in entries:
            invalid.append(JOURNAL_DIR + '/' + name)
            continue
        entries[entry['comment_id']] = entry
    return entries, invalid


def native(issue):
    """Every reserved-prefix comment on one issue, in native order (UNTRUSTED).

    ``valid`` on each entry is decided only later, against the host journal. The
    native order of a comment is its position among the issue's comments, which is
    the order the kit already uses for every other comment record.
    """
    found = []
    for position, comment in enumerate(issue.get('comments') or []):
        if not isinstance(comment, dict):
            continue
        text = comment.get('text')
        if not isinstance(text, str) or not text.startswith(PREFIX):
            continue
        item = {'comment_id': str(comment.get('id')), 'actor': comment.get('author'),
                'recorded_at': comment.get('created_at'), 'native_order': position,
                'text': text, 'payload': None, 'parse_error': None}
        try:
            item['payload'] = parse_text(text)
        except ValueError as exc:
            item['parse_error'] = str(exc)
        found.append(item)
    return found


def _why(value):
    return value[:WHY_LIMIT]


def _entry_for(issue, record):
    """The digest basis of one comment entry, exactly as the checkpoint snapshot hashes it.

    Checkpoint direction dispositions bind to ``entry_identity`` provenance: the
    snapshot drops ``issue_title`` before hashing, so this builds the comment entry
    the same way and hashes it with the same function.
    """
    from activity import build_entries
    from requirements import content_hash as digest
    entries = build_entries([issue])
    entry_id = str(issue.get('id')) + '-c' + str(record['comment_id'])
    for entry in entries:
        if entry.get('entry_id') == entry_id:
            entry.pop('issue_title', None)
            return entry_id, digest(entry)
    return entry_id, None


def _disposition(issue, record):
    """The checkpoint disposition of this record, or None.

    The follow-on IS a direction: a checkpoint resolves or supersedes it by its
    entry id with note and evidence. Only a RESOLUTION state with the recorded
    digest ends it -- acknowledgement alone, or an edited body whose digest no
    longer matches, does not.
    """
    try:
        from briefing import checkpoint_history, chain_dispositions
        entry_id, digest = _entry_for(issue, record)
        if digest is None:
            return None
        found = chain_dispositions(checkpoint_history(issue)).get(entry_id)
        if not isinstance(found, dict):
            return None
        if found.get('state') not in ('resolved', 'superseded'):
            return None
        if found.get('digest') != digest:
            return None
        return found
    except (ValueError, KeyError, TypeError):
        return None


def _cited_record(scopes, payload):
    """The named integration record, re-verified from the trusted per-scope evidence.

    Returns the matching ``integrated`` evidence entry when the record it names is
    still present for the record's contribution commit, is a trusted pass, and its
    native order is the order the record carries (the order the lifecycle evidence
    has always carried). A LATER attestation for the same commit adds a newer event;
    it does not change this named one, which is exactly why it must not end the
    hand-back. A missing, foreign or re-ordered citation is never guessed.
    """
    commit = str(payload.get('commit') or '').lower()
    if not commit:
        return None
    for entry in scopes or []:
        if not isinstance(entry, dict):
            continue
        scope = entry.get('scope') or {}
        if str(scope.get('source_commit') or '').lower() != commit:
            continue
        integrated = entry.get('integrated') or {}
        if integrated.get('value') != 'passed':
            continue
        if str(integrated.get('event_id') or '') != str(payload.get('integration') or ''):
            continue
        if integrated.get('order') != payload.get('integration_order'):
            continue
        return integrated
    return None


def evaluate(issue, review, scopes, host=None, project=None, dispositions=True):
    """THE applicability decision for one task's newest hand-back.

    Returns ``{'applies','block','newest','withdrawn','invalid','ignored'}``:

    * ``block`` is the additive row/brief block
      ``{id, actor, recorded_at, text, state, applies, why}``, or None when the
      task carries no follow-on comment at all. An unjournalled comment still
      yields a block, in state ``ignored``, so the brief can say why it was not
      honoured; ``applies`` is False and the move is unchanged;
    * ``applies`` is True only for an open, journaled, cited, undispositioned
      record that is the newest valid one;
    * ``invalid`` names the journal files that failed validation.

    ``host`` is the already-read ``(entries, invalid)`` journal pair for a caller
    that evaluates many tasks on one page; without it the journal is read fresh
    here, from the project directory only.
    """
    task = issue.get('id')
    if host is None:
        entries, invalid = host_journal(project)
    else:
        entries, invalid = host
    found = native(issue)
    for item in found:
        entry = entries.get(item['comment_id'])
        payload = item.get('payload')
        item['valid'] = bool(entry is not None and payload is not None
                             and entry.get('payload') == payload
                             and entry.get('operation') == payload.get('operation')
                             and entry.get('actor') == item.get('actor')
                             and entry.get('task') == task)
    valid = [item for item in found if item['valid']]
    records = [item for item in valid if item['payload']['operation'] == OPERATION_RECORD]
    withdrawals = [item for item in valid if item['payload']['operation'] == OPERATION_WITHDRAW]
    newest = max(records, key=lambda item: item['native_order']) if records else None
    withdraw = None
    if newest is not None:
        withdraw = next((item for item in withdrawals
                         if item['payload'].get('target') == newest['comment_id']), None)
    result = {'applies': False, 'block': None, 'newest': newest, 'withdrawn': withdraw,
              'invalid': list(invalid), 'ignored': None}
    if newest is None:
        # An inert prefix comment (forged, hand-written, or written by a kit with no
        # journal) is not a hand-back, but the brief still says why it was ignored.
        injected = max(found, key=lambda item: item['native_order']) if found else None
        if injected is not None:
            state = 'ignored'
            why = ('This comment is not a host-journaled hand-back, so it is ignored and the '
                   'move rule is unchanged.')
            result['ignored'] = injected
            result['block'] = {'id': injected['comment_id'], 'actor': injected.get('actor'),
                               'recorded_at': injected.get('recorded_at'),
                               'text': str(((injected.get('payload') or {}).get('text')) or '')[:TEXT_LIMIT],
                               'state': state, 'applies': False, 'why': _why(why)}
        return result
    payload = newest['payload']
    block = {'id': newest['comment_id'], 'actor': newest.get('actor'),
             'recorded_at': newest.get('recorded_at'),
             'text': payload['text'], 'state': 'open', 'applies': False, 'why': ''}

    def ended(state, why):
        block['state'] = state
        block['applies'] = False
        block['why'] = _why(why)
        result['block'] = block
        return result

    if issue.get('status') == 'closed':
        return ended('ended', 'The task is closed, so the hand-back no longer moves it.')
    if withdraw is not None:
        reason = str(withdraw['payload'].get('reason') or '')
        return ended('withdrawn', 'The coordinator withdrew this hand-back'
                     + (': ' + reason[:200] if reason else '') + '. The move has returned to the coordinator.')
    if dispositions:
        found = _disposition(issue, newest)
        if found is not None:
            return ended(found['state'], 'A checkpoint recorded this hand-back as '
                         + str(found.get('state')) + ' with evidence, so it no longer moves the task.')
    contribution = review.get('contribution') if isinstance(review, dict) else None
    current = (contribution or {}).get('comment_id')
    state = review.get('review_state') if isinstance(review, dict) else None
    if current != payload['contribution']:
        return ended('ended', 'A newer contribution is current, so the record for the contribution it '
                    'followed no longer moves the task.')
    if state != 'integrated':
        return ended('ended', 'The contribution reads ' + str(state)
                     + ', not integrated; the hand-back applies only to an integrated contribution.')
    evidence = review.get('integration') if isinstance(review, dict) else None
    if not isinstance(evidence, dict) or evidence.get('fact') != 'passed':
        return ended('ended', 'The contribution is not integrated (its effective integration fact is '
                     + str((evidence or {}).get('fact', 'unknown')) + '); the hand-back does not move it.')
    if str(payload.get('commit') or '').lower() != str((contribution or {}).get('commit') or '').lower():
        return ended('ended', 'The cited integration commit is not the current contribution commit.')
    if _cited_record(scopes, payload) is None:
        return ended('ended', 'The named integration record is no longer present for this contribution '
                    'at the order the record carries, so its citation cannot be re-verified.')
    block['state'] = 'open'
    block['applies'] = True
    block['why'] = 'An open, host-journaled hand-back: the move is the assignee\'s until a checkpoint ' \
                   'disposition, a withdrawal, or a new contribution ends it.'
    result['applies'] = True
    result['block'] = block
    return result


def view_text(value):
    """The recorded text, escaped exactly once and bounded for display.

    The stored text is bounded at :data:`TEXT_LIMIT`; this clips an older or
    hand-written longer value to the same bound before escaping. Escaping is the
    checkpoint-wait escaping (``briefing.plain_text`` then the quote/newline/tab
    escapes), so the text cannot close the sentence's quote or start a second line.
    """
    from briefing import plain_text
    text = plain_text(str(value or '')[:TEXT_LIMIT].replace('\\', '\\\\'))
    return text.replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')


def next_text(block, checkpoint_available=True):
    """The ``next`` sentence for an applying hand-back, quoting its recorded text once."""
    quoted = view_text(block.get('text'))
    if checkpoint_available:
        tail = ('Clear it in your next checkpoint: record its id and digest from `checkpoint TASK '
                '--directions` in `directions`, with a note and evidence. Acknowledgement alone does '
                'not clear it; the coordinator can also withdraw it, and a new contribution ends it.')
    else:
        tail = ('The checkpoint direction route is unavailable on this installation '
                '(checkpoint_provenance_writes is off), so the coordinator withdraws it or a new '
                'contribution ends it.')
    return ('This is your work now: the coordinator recorded a hand-back on this integrated '
            'contribution: "%s". %s' % (quoted, tail))


# ---------------------------------------------------------------------------
# Writes. Both routes (the confined coordinator command and the web route) call
# THIS one pair of functions, so the two authority gates cannot drift apart.

def _rows(run, project):
    from lifecycle import read_event_rows
    return read_event_rows(run)


def _issue(rows, task):
    matches = [row for row in rows if isinstance(row, dict) and row.get('id') == task]
    if len(matches) != 1 or matches[0].get('issue_type') == 'event':
        raise _refuse('Task ' + str(task) + ' is missing, duplicated or is an event.')
    return matches[0]


def _projection(rows, issue, operators, project):
    from review_state import project as reviewed, scopes_for
    scopes = scopes_for(rows, issue['id'])
    return reviewed(issue, scopes, operators=operators, journal=project), scopes


def _adopt(issue, entries, payload, actor, project):
    """An exact retry of a write already on the tracker: complete its journal entry, or adopt it.

    Returns the answer for a reconciled write, or None when no identical comment
    exists. This is what makes a crash between the native comment and its journal
    entry reconcilable: re-running the SAME record persists the missing entry
    instead of writing a second comment.
    """
    for item in native(issue):
        if item.get('payload') != payload or item.get('actor') != actor:
            continue
        entry = entries.get(item['comment_id'])
        if entry is None:
            publish(project, journal_entry(payload, item['comment_id'], actor, item['native_order']))
        elif entry.get('payload') != payload:
            raise _refuse('The host journal for comment ' + item['comment_id'] + ' records a different '
                          'payload; refusing to adopt it.')
        return {'comment_id': item['comment_id'], 'task': payload['task'],
                'operation': payload['operation'], 'reconciled': True}
    return None


def _record(rows, issue, task, text, actor, run, project, operators):
    if issue.get('status') == 'closed':
        raise _refuse('The task is closed; a hand-back is recorded only on an open task.')
    review, scopes = _projection(rows, issue, operators, project)
    contribution = review.get('contribution') or {}
    evidence = review.get('integration') or {}
    if review.get('review_state') != 'integrated' or evidence.get('fact') != 'passed':
        raise _refuse('The current contribution is not integrated (effective review state %s), so there '
                      'is no remaining scope after integration to hand back.'
                      % (review.get('review_state'),))
    record_id = evidence.get('record_id')
    order = evidence.get('order')
    if not isinstance(record_id, str) or type(order) is not int:
        raise _refuse('The integration evidence does not name the native integration record and its '
                      'order, so a hand-back cannot be cited.')
    payload = {'schema_version': 1, 'operation': OPERATION_RECORD, 'task': task,
               'contribution': str(contribution.get('comment_id')),
               'commit': str(contribution.get('commit') or '').lower(),
               'integration': record_id, 'integration_order': order,
               'text': text, 'actor': actor}
    _payload(payload)
    entries, invalid = host_journal(project)
    adopted = _adopt(issue, entries, payload, actor, project)
    if adopted is not None:
        return adopted
    # One hand-back at a time: a second OPEN record for the same task is refused.
    current = evaluate(issue, review, scopes, host=(entries, invalid))
    if current['applies']:
        raise _refuse('Task ' + task + ' already has an open hand-back (record '
                      + current['block']['id'] + '); withdraw it before recording another.')
    open_journal(project)
    body_text = body(payload)
    result = record_json.loads(run(['comments', 'add', task, body_text, '--json']))
    comment_id = str(result['id'])
    try:
        publish(project, journal_entry(payload, comment_id, actor, len(issue.get('comments') or [])))
    except ValueError as exc:
        # Not a refusal: the native comment WAS written. This sentence does not claim
        # otherwise; the guarded write reports the uncertain outcome and re-running the
        # same hand-back persists the missing entry instead of writing a second comment.
        raise ValueError('Follow-on comment ' + comment_id + ' was written but its host journal entry '
                         'could not be persisted (%s). The record is inert until it is: re-run the '
                         'same hand-back to persist the entry.' % (exc,)) from None
    return {'comment_id': comment_id, 'task': task, 'operation': OPERATION_RECORD,
            'contribution': payload['contribution'], 'commit': payload['commit'],
            'integration': payload['integration'], 'integration_order': payload['integration_order'],
            'reconciled': False}


def _withdraw(rows, issue, target, reason, actor, run, project, operators, expected_task=None):
    entries, invalid = host_journal(project)
    record = next((item for item in native(issue)
                   if item['comment_id'] == target and item.get('payload') is not None
                   and item['payload'].get('operation') == OPERATION_RECORD), None)
    entry = entries.get(target) if target else None
    if record is None or entry is None or entry.get('payload') != record['payload']:
        raise _refuse('No host-journaled hand-back ' + str(target) + ' exists on this task; nothing '
                      'can be withdrawn.')
    task = record['payload']['task']
    if task != issue.get('id'):
        raise _refuse('That hand-back belongs to another task.')
    if expected_task is not None and expected_task != task:
        raise _refuse('That hand-back belongs to another task.')
    payload = {'schema_version': 1, 'operation': OPERATION_WITHDRAW, 'task': task,
               'target': target, 'reason': reason, 'actor': actor}
    _payload(payload)
    adopted = _adopt(issue, entries, payload, actor, project)
    if adopted is not None:
        return adopted
    review, scopes = _projection(rows, issue, operators, project)
    current = evaluate(issue, review, scopes, host=(entries, invalid))
    if current['block'] is None or current['block']['id'] != target:
        raise _refuse('That hand-back is not the current one on this task.')
    if current['block']['state'] != 'open' or not current['applies']:
        raise _refuse('That hand-back is not open (' + str(current['block']['state'])
                      + '), so there is nothing to withdraw.')
    open_journal(project)
    result = record_json.loads(run(['comments', 'add', task, body(payload), '--json']))
    comment_id = str(result['id'])
    try:
        publish(project, journal_entry(payload, comment_id, actor, len(issue.get('comments') or [])))
    except ValueError as exc:
        # Not a refusal: the withdrawal comment WAS written (see `_record`).
        raise ValueError('Withdrawal comment ' + comment_id + ' was written but its host journal entry '
                         'could not be persisted (%s). The withdrawal is inert until it is: re-run the '
                         'same withdrawal to persist the entry.' % (exc,)) from None
    return {'comment_id': comment_id, 'task': task, 'operation': OPERATION_WITHDRAW,
            'target': target, 'reconciled': False}


def apply(payload, actor, run, project, operators=None):
    """Validate one request, derive its facts under the lock, and carry it out.

    ``run`` is the native command runner bound to this project (the same runner the
    endpoint passes its canonical writes). ``project`` is the project directory
    holding the host journal. The caller supplies only task+text or followon+reason;
    every other record fact is derived here from the tracker the runner reads.
    """
    _actor(actor)
    request = request_payload(payload)
    rows = _rows(run, project)
    if request['operation'] == OPERATION_RECORD:
        issue = _issue(rows, request['task'])
        return _record(rows, issue, request['task'], request['text'], actor, run, project, operators)
    expected = request.get('task')
    target = request['target']
    # The withdrawal names only the follow-on; the task is derived from the record
    # itself. When the caller also named a task (the web path's {tid}), it must be
    # the record's own task, so a route can never withdraw across tasks.
    matches = []
    for row in rows:
        if not isinstance(row, dict) or row.get('issue_type') == 'event':
            continue
        for item in native(row):
            if item['comment_id'] == target:
                matches.append((row, item))
    if len(matches) != 1:
        raise _refuse('No hand-back ' + target + ' exists in this project; nothing can be withdrawn.')
    row, item = matches[0]
    return _withdraw(rows, row, target, request['reason'], actor, run, project, operators,
                     expected_task=expected)
