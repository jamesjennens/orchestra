"""Attestations of decision rows: how a decision counts (kittrial-5bb.454 r2; PROGRAM_DESIGN.md section 6).

Why a reserved record and not the row's author: a native row's ``created_by`` says who made the row, not who wrote the
text now in it, and a contributor's edit keeps it (delegations.py says the same of its own records). So a decision
counts only with a ``Kind: decision-attest-v1`` comment naming its author and a digest of the row's exact type, title
and description, a reserved prefix a raw comment cannot write, honoured only with its host journal entry
(``<project>/.decision-attests/<sha256>.json``). It is written only by the dedicated operation:

- the web route, for the author's own web session or agent, with standing in the project now; ``via``, ``author`` and
  the time are the server's, and the body names the digest the author read, so an edit that lands between the read and
  the attestation is refused and never attested by mistake;
- the operator's host path (``admin.py decision-attest``), for an actor on the installation's allowlist.

A row whose text carries ``Adopts:`` or ``Authorises:`` pins the program decision's attestation id. It is attested only by
someone who can read that decision now, and only with the id it has now: an area must not read ``adopted`` for text it
never saw. The same refusal answers an unreadable program, no such project and no such decision. The id is the sha256 of
the first record of the unbroken run of newest records over this text, a hash over a server-random ``nonce``, so a reader
of the area learns nothing about the program's text and cannot confirm a guess of it. New text starts a new run, so the
id changes exactly when the text attested does, and another attester of the same words does not change it.

At every read, in this order: a closed row is ``closed``; a row with no honoured record is ``not-attested``; a newest
record over any other text is ``attestation-stale`` (the newest, not any: going back to text the author has since
replaced stays stale); an author whose standing has gone is ``attester-not-authorised``. Only then does the row count,
as the highest live standing among the unbroken run of newest records over this text, so a coordinator attesting the
same words cannot lower an owner's attestation.

A status change, or a type or label change that starts or stops a row being a decision, needs standing too (the gate
below), on a row with an honoured attestation and on any decision row carrying these lines or an attestation comment,
honoured or not: a reopen would otherwise bring an owner's retired decision back, and a row whose journal entry is gone, or
that was restored under a new name, would stand open to a plain host actor. An owner's level is needed for a text that says ``Owner-only: yes``, that
carries ``Authorises:``, or an adoption a person owner or an operator attested. Where there is a record that comes from the
record (``owner_gated``), not from the row's text now, which any contributor can edit. A text edit is not gated: it only
makes the row stale, which removes a binding and never makes one.

This module has no store and no clock; callers pass what they read. Anyone with a shell on the host can write these
files and forge bd rows; this defends the routes the kit exposes (the as-built says so).
"""
import datetime
import hashlib
import re
import secrets

import record_json
from requirements import canonical_bytes, content_hash

PREFIX = 'Kind: decision-attest-v1\n'
JOURNAL_DIR = '.decision-attests'
JOURNAL_FILE_LIMIT = 16384
VIA = ('session', 'agent', 'operator')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
NONCE = re.compile(r'[0-9a-f]{32}\Z')
ISSUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
OPERATION = re.compile(r'[\x21-\x7e]{1,200}\Z')
OPERATOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}\Z')
PERSON = re.compile(r'usr_[0-9a-f]{16}\Z')
AGENT = re.compile(r'agent_[0-9a-f]{16}\Z')
REQUEST_FIELDS = frozenset({'schema_version', 'operation_id', 'decision', 'digest'})
RECORD_FIELDS = REQUEST_FIELDS | frozenset({'project', 'via', 'author', 'recorded_by', 'owner_gated', 'nonce', 'at',
                                            'sha256'})
#: Rows that are never decisions, whatever their labels: what the queue never lists as work.
NOT_WORK_TYPES = ('event', 'gate')
OWNERS = ('owner', 'operator')
COORDINATORS = ('owner', 'operator', 'coordinator', 'agent-coordinator')
#: Highest first: who a run of records over one text counts as.
RANK = ('operator', 'owner', 'coordinator', 'agent-coordinator')
#: What a row that does not count reads as, and why.
REASONS = {'closed': 'a closed row binds nothing',
           'not-attested': 'no attestation: an author has not attested this text',
           'attestation-stale': 'the text changed after the newest attestation',
           'attester-not-authorised': 'the attester no longer holds the standing'}


class AttestRefusal(ValueError):
    """A refused attestation or gated change: ``status`` is the answer, and nothing was written."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def is_decision(row):
    """A decision issue (the kit's one predicate) that is not an event or gate row."""
    from delegations import is_decision_issue
    return is_decision_issue(row) and row.get('issue_type') not in NOT_WORK_TYPES


def digest(row):
    """SHA-256 of the row's exact type, title and description, with no trimming; no description hashes as empty."""
    def text(name):
        value = row.get(name) if isinstance(row, dict) else None
        return value if isinstance(value, str) else ''
    return hashlib.sha256(canonical_bytes({'type': text('issue_type'), 'title': text('title'),
                                           'description': text('description')})).hexdigest()


def authority(author, project, state):
    """Who a web identity is in ``project`` now: 'owner', 'coordinator', 'agent-coordinator' or None.

    * a bare ``usr_`` id: its account enabled, and its role in the project owner or coordinator;
    * a bare ``agent_`` id: the agent enabled and holding the project, its account enabled and itself owner or
      coordinator there (a superuser who is only a viewer does not count: .447 r2's rule), and the coordinator grant
      in force (http_authority.coordinator_grant, grant_issuer_holds);
    * anything else, a worker credential (``usr_.../label``) or a bare name included: None. A name proves nothing over
      a host route; an operator counts only through an operator's record (``role_now``)."""
    if not isinstance(author, str) or not author:
        return None
    state = state if isinstance(state, dict) else {}
    users = state.get('users') or {}
    roles = ((state.get('memberships') or {}).get(project) or {})
    if PERSON.fullmatch(author):
        user = users.get(author)
        if not isinstance(user, dict) or user.get('disabled'):
            return None
        return {'owner': 'owner', 'coordinator': 'coordinator'}.get(roles.get(author))
    if AGENT.fullmatch(author):
        from http_authority import coordinator_grant, grant_issuer_holds
        agent = (state.get('agents') or {}).get(author)
        if not isinstance(agent, dict) or not agent.get('enabled') or project not in (agent.get('projects') or []):
            return None
        owner = users.get(agent.get('owner'))
        if not isinstance(owner, dict) or owner.get('disabled') or roles.get(agent.get('owner')) not in ('owner',
                                                                                                        'coordinator'):
            return None
        grant = coordinator_grant(state, project, author)
        if grant is None or not grant_issuer_holds(state, project, grant):
            return None
        return 'agent-coordinator'
    return None


def role_now(via, author, project, state, operators):
    """The standing of a record's author now. An operator's is the exact actor still on the allowlist."""
    if via == 'operator':
        try:
            listed = set(operators) if operators is not None and not isinstance(operators, str) else set()
        except TypeError:
            return None
        return 'operator' if author in listed else None
    if via == 'session' and not PERSON.fullmatch(str(author)):
        return None
    if via == 'agent' and not AGENT.fullmatch(str(author)):
        return None
    return authority(author, project, state)


def owner_gated(row, role):
    """Whether this text needs an owner's level to change: it says Owner-only: yes, it carries Authorises: (an owner's
    choice a coordinator acts on), it is an adoption a person owner or an operator attested, or its lines do not read."""
    from program_decisions import lines
    found = lines(row)
    return bool(found['owner_only'] or found['authorises'] is not None or found['problems']
                or (found['adopts'] is not None and role in OWNERS))


def validate_request(payload):
    if not isinstance(payload, dict) or set(payload) != REQUEST_FIELDS:
        raise ValueError('An attestation has its exact field set')
    if payload['schema_version'] != 1 or isinstance(payload['schema_version'], bool):
        raise ValueError('An attestation has schema_version 1')
    if not isinstance(payload['operation_id'], str) or not OPERATION.fullmatch(payload['operation_id']):
        raise ValueError('An attestation needs a stable operation identity')
    if not isinstance(payload['decision'], str) or not ISSUE_ID.fullmatch(payload['decision']):
        raise ValueError('An attestation names one decision')
    if not isinstance(payload['digest'], str) or not DIGEST.fullmatch(payload['digest']):
        raise ValueError('An attestation names the digest the author read')


def _validate_record(record):
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
        raise ValueError('An attestation record has its exact field set')
    validate_request({key: record[key] for key in REQUEST_FIELDS})
    if not isinstance(record['project'], str) or not PROJECT.fullmatch(record['project']):
        raise ValueError('An attestation record names its project')
    if record['via'] not in VIA:
        raise ValueError('An attestation record was written by a session, an agent or an operator')
    author = record['author']
    shape = {'session': PERSON, 'agent': AGENT, 'operator': OPERATOR}[record['via']]
    if not isinstance(author, str) or not shape.fullmatch(author) or record['recorded_by'] != author \
            or (record['via'] == 'operator' and (PERSON.fullmatch(author) or AGENT.fullmatch(author))):
        raise ValueError('An attestation record names the author that wrote it')
    if not isinstance(record['nonce'], str) or not NONCE.fullmatch(record['nonce']):
        raise ValueError('An attestation record carries its random nonce')
    if not isinstance(record['owner_gated'], bool):
        raise ValueError('An attestation record says whether its text needs an owner to change')
    if not isinstance(record['at'], str):
        raise ValueError('An attestation record needs the server time')
    try:
        at = datetime.datetime.fromisoformat(record['at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('An attestation record needs the server time') from exc
    if at.tzinfo is None or at.utcoffset() != datetime.timedelta(0):
        raise ValueError('An attestation record needs the server UTC time')
    if record['sha256'] != content_hash(record):
        raise ValueError('An attestation record hash differs')


def make_record(payload, *, project, author, via, gated, at):
    """Build a record from a validated request and trusted server attribution only."""
    validate_request(payload)
    record = dict(payload, project=project, via=via, author=author, recorded_by=author, owner_gated=gated, at=at,
                  nonce=secrets.token_hex(16))
    record['sha256'] = content_hash(record)
    _validate_record(record)
    return record


def body(record):
    _validate_record(record)
    return PREFIX + canonical_bytes(record).decode('utf-8')


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('Duplicate attestation record field')
        out[key] = value
    return out


def parse_comment(comment, *, project, decision):
    """An exact record bound to its native comment author, its project and its issue, or None."""
    if not isinstance(comment, dict):
        return None
    text = comment.get('text')
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    try:
        record = record_json.loads(text[len(PREFIX):], object_pairs_hook=_unique)
        _validate_record(record)
        if body(record) != text or record['project'] != project or record['decision'] != decision:
            return None
        if comment.get('author') != record['recorded_by']:
            return None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
    return record


def open_journal(project):
    """The host journal directory for a writer, or a refusal BEFORE any native write."""
    from pathlib import Path
    if project is None:
        raise AttestRefusal(503, 'An attestation needs the project directory that holds its host journal')
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink():
        raise AttestRefusal(503, 'Refusing %s: the path is a symlink' % JOURNAL_DIR)
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise AttestRefusal(503, 'Cannot create %s: %s' % (JOURNAL_DIR, exc)) from None
    if not directory.is_dir():
        raise AttestRefusal(503, '%s is not a directory' % JOURNAL_DIR)
    from uuid import uuid4
    probe = directory / ('.writable-' + uuid4().hex + '.tmp')
    try:
        probe.write_text('probe', encoding='utf-8')
    except OSError as exc:
        raise AttestRefusal(503, 'Cannot use %s: the directory is not writable (%s)' % (JOURNAL_DIR, exc)) from None
    try:
        probe.unlink()
    except OSError:
        pass
    return directory


def publish(project, record):
    """Persist the journal entry of one validated record, named by its hash."""
    from coordination import atomic
    _validate_record(record)
    directory = open_journal(project)
    path = directory / (record['sha256'] + '.json')
    if path.is_symlink():
        raise AttestRefusal(503, 'Refusing journal entry %s: it is a symlink' % path.name)
    atomic(path, record)
    return path


def validate_entry(record, name=None):
    """One journal entry (the record itself) and its ``<sha256>.json`` name, for a backup."""
    _validate_record(record)
    if name is not None and name != record['sha256'] + '.json':
        raise ValueError('Attestation journal path mismatch')
    return record


def journaled(project, record):
    """Whether the host journal holds exactly this record (fail-closed on any doubt)."""
    from pathlib import Path
    if project is None:
        return False
    directory = Path(project) / JOURNAL_DIR
    path = directory / (record['sha256'] + '.json')
    from admin import read_regular_text
    try:
        if directory.is_symlink() or not directory.is_dir() or path.is_symlink():
            return False
        stored = record_json.loads(read_regular_text(path, limit=JOURNAL_FILE_LIMIT), object_pairs_hook=_unique)
        _validate_record(stored)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return False
    return stored == record


def history(row, project, journal):
    """The honoured records on one row, oldest first, ordered by the record's own server time, then the native comment
    id, so the order never depends on how a native read happens to list comments."""
    decision = row.get('id') if isinstance(row, dict) else None
    out = []
    for comment in (row.get('comments') or []) if isinstance(row, dict) else []:
        record = parse_comment(comment, project=project, decision=decision)
        if record is not None and journaled(journal, record):
            out.append((record['at'], str(comment.get('id') or ''), record))
    return [record for _, _, record in sorted(out, key=lambda item: (item[0], item[1]))]


def _newest_run(records):
    """The unbroken run of newest records that share the newest record's digest, newest first."""
    run = []
    for record in reversed(records):
        if record['digest'] != records[-1]['digest']:
            break
        run.append(record)
    return run


def standing(row, project, state, operators, journal):
    """How a row counts now: ``{'state', 'role', 'author', 'id', 'owner_level', 'reason'}``. ``state`` is 'counts', or what
    it reads as; ``id`` is what an adoption pins: the sha256 of the first record of the newest run over this text;
    ``owner_level`` is what its records ask of anyone changing it, however it reads now."""
    records = history(row, project, journal)
    level = needs_owner(records)

    def reads(name):
        return {'state': name, 'role': None, 'author': None, 'id': None, 'owner_level': level, 'reason': REASONS[name]}
    if isinstance(row, dict) and row.get('status') == 'closed':
        return reads('closed')
    if not records:
        return reads('not-attested')
    if records[-1]['digest'] != digest(row):
        return reads('attestation-stale')
    run = _newest_run(records)
    found = [(role_now(record['via'], record['author'], project, state, operators), record) for record in run]
    found = sorted(((role, record) for role, record in found if role is not None), key=lambda item: RANK.index(item[0]))
    if not found:
        return reads('attester-not-authorised')
    return {'state': 'counts', 'role': found[0][0], 'author': found[0][1]['author'], 'id': run[-1]['sha256'],
            'owner_level': level, 'reason': None}


def read_issue(run, decision):
    """One native issue with its comments, or None when this project has no such issue."""
    from delegations import read_issue as read
    return read(run, decision)


def pins(row):
    """The ``(program, decision, attestation id)`` of a row's Adopts: and Authorises: lines."""
    from program_decisions import lines
    found = lines(row)
    return [found[name] for name in ('adopts', 'authorises') if found[name] is not None]


def pin_fact(row, program, state, operators, journal):
    """What the write checks of a pinned decision: its attestation id and how it reads, or None for no decision."""
    if not isinstance(row, dict) or not is_decision(row):
        return None
    found = standing(row, program, state, operators, journal)
    return {'pin': found['id'], 'state': found['state']}


def read_pins(row, read_decision):
    """{(program, decision): its fact, or None where this caller cannot read it or it does not exist}, read BEFORE the
    area's lock is taken, so that no lock is held across projects (the .449 rule)."""
    return {pin[:2]: read_decision(*pin[:2]) for pin in pins(row)}


def check_pins(row, pinned):
    """Refuse a row whose pins this caller did not read, or that are not the program decision's attestation id now."""
    for program, decision, pin in pins(row):
        if (program, decision) not in (pinned or {}):
            raise AttestRefusal(409, 'The text changed since you read it. Read it again before you attest it. Nothing was '
                                     'written.')
        fact = pinned[(program, decision)]
        if fact is None:
            raise AttestRefusal(403, 'This row names decision:%s/%s, and it cannot be read: it does not exist, or you may '
                                     'not read it. An area adopts only text it has read: ask for read of %s. Nothing was '
                                     'written.' % (program, decision, program))
        if fact['state'] != 'counts':
            raise AttestRefusal(409, 'decision:%s/%s does not count now (%s). Nothing was written.'
                                     % (program, decision, fact['state']))
        if fact['pin'] != pin:
            raise AttestRefusal(409, 'decision:%s/%s has other text now: copy the line again from its attestation read. '
                                     'Nothing was written.' % (program, decision))


def read_answer(row, project, state, operators, journal):
    """What the attest route's read shows: the exact text a person is asked to attest, its digest, and how it reads."""
    from reserved_comments import is_record_anchor
    if not isinstance(row, dict) or not is_decision(row) or is_record_anchor(row):
        raise AttestRefusal(404, 'No such decision in this project')
    found = standing(row, project, state, operators, journal)
    return {'schema_version': 1, 'project': project, 'decision': row.get('id'), 'type': row.get('issue_type'),
            'title': row.get('title'), 'description': row.get('description') or '', 'status': row.get('status'),
            'digest': digest(row), 'attestation': found['state'], 'attested_as': found['role'],
            'attested_by': found['author'], 'reason': found['reason'],
            'attestation_id': found['id'],
            'adopt_line': 'Adopts: decision:%s/%s@%s' % (project, row.get('id'), found['id']) if found['id'] else None,
            'authorise_line': 'Authorises: decision:%s/%s@%s' % (project, row.get('id'), found['id']) if found['id'] else None}


def apply(payload, *, project, journal, run, author, via, at, state, operators, pinned=None):
    """Append one attestation: check, journal readiness, one native comment, the entry.

    The caller holds the project's coordination lock and runs this inside its guarded write, and has proved who
    ``author`` is (a person's own web session, the agent's own credential, or a listed operator on the host path).
    ``pinned`` is what ``read_pins`` read for this caller before the lock.
    The journal is proved writable BEFORE the native comment and the entry is written after it: a failure in between
    leaves a comment no reader honours."""
    validate_request(payload)
    from reserved_comments import is_record_anchor
    row = read_issue(run, payload['decision'])
    if not isinstance(row, dict) or not is_decision(row) or is_record_anchor(row):
        raise AttestRefusal(404, 'No such decision in this project. Nothing was written.')
    role = role_now(via, author, project, state, operators)
    if role not in COORDINATORS:
        raise AttestRefusal(403, 'Only an owner or a coordinator of %s, or an operator, attests a decision. Nothing '
                                 'was written.' % project)
    records = history(row, project, journal)
    if role not in OWNERS and any(record['owner_gated'] for record in _newest_run(records)):
        raise AttestRefusal(403, 'This decision was attested at an owner\'s level: only a person owner of %s, or an '
                                 'operator, attests it again. Nothing was written.' % project)
    current = digest(row)
    if payload['digest'] != current:
        raise AttestRefusal(409, 'The text changed since you read it. Read it again before you attest it. Nothing was '
                                 'written.')
    check_pins(row, pinned)
    record = make_record(payload, project=project, author=author, via=via, gated=owner_gated(row, role), at=at)
    open_journal(journal)
    comment = record_json.loads(run(['comments', 'add', payload['decision'], body(record), '--json']))
    if not isinstance(comment, dict) or not comment.get('id'):
        raise ValueError('The attestation cannot be reconciled')
    publish(journal, record)
    return {'schema_version': 1, 'operation_id': payload['operation_id'], 'decision': payload['decision'],
            'record': record, 'comment_id': str(comment['id'])}


# -- the gate on status, type and label changes -------------------------------------------------------------------
def gated_change(args):
    """What a bd invocation would change that the gate covers, or None: ``(command, targets, change)``.

    ``change`` is {'status': bool, 'type': value or None, 'add': [...], 'remove': [...], 'set': [...] or None}.
    ``targets`` is None when the scan is ambiguous or names no issue, which the guard refuses. Flags are read by the
    same structural scan as the record-anchor guard, so no spelling of a flag is missed (reserved_comments)."""
    from reserved_comments import _bd_scan, _moves_status_or_assignee, status_change_targets
    if not isinstance(args, list) or not args or args[0] not in ('close', 'reopen', 'update'):
        return None
    if args[0] != 'update':
        command, targets = status_change_targets(args)
        return command, targets, {'status': True, 'type': None, 'add': [], 'remove': [], 'set': None}
    flags, operands, unknown = _bd_scan(args, 'update')
    change = {'status': _moves_status_or_assignee([flag for flag in flags if flag[0] not in ('--assignee', '-a')]),
              'type': None, 'add': [], 'remove': [], 'set': None}
    touched = change['status']
    for name, value in flags:
        words = [word for word in str(value or '').split(',') if word]
        if name in ('--type', '-t'):
            change['type'] = value
            touched = True
        elif name == '--add-label':
            change['add'] += words
            touched = touched or 'decision' in words        # no other label starts or stops a decision
        elif name == '--remove-label':
            change['remove'] += words
            touched = touched or 'decision' in words
        elif name == '--set-labels':
            change['set'] = (change['set'] or []) + words
            touched = True
    if not touched and not unknown:
        return None
    targets = [token for token in operands if not token.startswith('@attachment:')]
    return 'update', (targets or None) if not unknown else None, change


def changes_decision(row, change):
    """Whether the change would start or stop this row being a decision."""
    labels = list(row.get('labels') or [])
    if change['set'] is not None:
        labels = list(change['set'])
    labels = [label for label in labels + change['add'] if label not in change['remove']]
    after = dict(row, labels=labels, issue_type=change['type'] if change['type'] is not None else row.get('issue_type'))
    return is_decision(row) != is_decision(after)


def needs_owner(records):
    """Whether a change to a row with these honoured records needs an owner's level. Read from the records: a text
    somebody edited since cannot lower it."""
    return any(record['owner_gated'] for record in _newest_run(records))


#: What makes a line of section 6 for the gate, well formed or not. The three that point at a decision name one, so prose
#: such as ``Per: the meeting of Tuesday`` on an ordinary decision is not one of them, and closes as it always did.
GATED_LINES = ('Binds:', 'Owner-only:', 'Adopts: decision:', 'Per: decision:', 'Authorises: decision:')


def carries_lines(row):
    """Whether a row's text has any line of section 6."""
    description = row.get('description') if isinstance(row, dict) else None
    if not isinstance(description, str):
        return False
    return any(line.startswith(GATED_LINES) for line in description.split('\n'))


def carries_attestation_comment(row):
    """Whether a row has a comment with the reserved prefix, honoured or not (review of r3, b1): with its journal entry gone
    a row's lines can be edited out, so the lines alone cannot say that an owner once attested it."""
    comments = row.get('comments') if isinstance(row, dict) else None
    return any(isinstance(comment, dict) and str(comment.get('text') or '').startswith(PREFIX) for comment in comments or [])


def gate(row, *, project, journal, actor, state, by_service, command, change):
    """Refuse a gated change, or None: to a row with an honoured attestation, or to a decision row carrying the lines of
    section 6 or an attestation comment (reviews of r2 and r3: a row with no honoured record must not stand open, and a task
    these would turn into a decision is the same). On the web, the standing the row needs; every host route refuses everyone,
    an operator included: an operator goes through admin.py decision-change."""
    records = history(row, project, journal)
    flips = changes_decision(row, change)
    marked = carries_attestation_comment(row)
    if not records and not ((marked or carries_lines(row)) and (is_decision(row) or flips)):
        return None
    if not change['status'] and not flips:
        return None
    what = 'change the status of' if change['status'] else 'change whether this row is a decision:'
    if not by_service:
        return AttestRefusal(403, 'Refusing to %s %s: it is a decision that binds. On the host, an operator does this '
                                  'with admin.py decision-change. Nothing was written.' % (what, row.get('id')))
    # With a record, the record says. A comment no journal honours says nothing to rely on and the text can be edited, so it
    # needs an owner. With neither, the text is all there is, and nobody attested it.
    needed = OWNERS if (needs_owner(records) if records else (marked or owner_gated(row, None))) else COORDINATORS
    if authority(actor, project, state) in needed:
        return None
    who = 'a person owner' if needed is OWNERS else 'an owner or a coordinator'
    return AttestRefusal(403, 'Refusing to %s %s: it is a decision that binds, and only %s of %s, or an operator, may do '
                              'that. Nothing was written.' % (what, row.get('id'), who, project))
