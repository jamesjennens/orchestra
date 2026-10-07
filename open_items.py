"""Open items, owner questions and coordinator decisions: slice 0, names only.

kittrial-5bb.126, slice 0 of docs/OPEN_ITEMS_DECISIONS_DESIGN.md. Nothing here reads
or writes a record or a journal. The module names what the later slices build on
(the four record prefixes, which reserved_comments reserves, the family and state
label, and the two journals) and carries the one piece of behaviour slice 0 needs:
`validate_owner_entry`, the frozen shape of a host-issued `.owner-answers/` entry.

`.owner-answers/` is validated like `.integration-reverts/`
(review_workflow.validate_revert_journal_entry), not as a receipt: the entry binds a
record's identity to the native comment id and to the exact canonical payload that
comment carries, and its file name is `<sha256 of that payload>.json` (design 11.1,
11.2). `admin.validate_coordination_files` and `admin.restore_coordination` call it, so
a backup can never carry an entry a later reader would have to guess about. The field
set is fixed here, in slice 0, so a kit with writes off already refuses any other
shape (design 11.2.1).
"""
import re

from recovery import identity
from requirements import content_hash
from reserved_comments import (COORDINATOR_DECISION_PREFIX, ITEM_RESOLUTION_PREFIX, OPEN_ITEM_PREFIX,
                               OWNER_ANSWER_PREFIX)

PREFIXES = (OPEN_ITEM_PREFIX, ITEM_RESOLUTION_PREFIX, OWNER_ANSWER_PREFIX, COORDINATOR_DECISION_PREFIX)
FAMILY_LABEL = 'open-item'
STATE_LABEL_PREFIX = 'open-item:'
REQUESTS_JOURNAL = '.open-item-requests'
OWNER_ANSWERS_JOURNAL = '.owner-answers'

# Record limits the entry validator checks (design 4.3, 4.4).
ANSWER_ID = re.compile(r'a-[0-9a-f]{12}')
DECISION_ID = re.compile(r'd-[0-9a-f]{12}')
OPTION_ID = re.compile(r'[a-z][a-z0-9-]{0,31}')
SHA256 = re.compile(r'[0-9a-f]{64}')
STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
ANSWER_WORDS_MAX = 4000
DECISION_TITLE_MAX = 200
DECIDES_MAX = 16
OPTIONS_MAX = 8
OPTION_TEXT_MAX = 300
ROUTES = ('endpoint', 'host', 'web')
ATTRIBUTION_FIELDS = frozenset({'actor', 'route', 'identity', 'person'})

ANSWER_FIELDS = frozenset({'schema_version', 'answer', 'item', 'question_revision', 'question_sha256', 'owner',
                           'option', 'options_offered', 'words', 'authority', 'relayed_by', 'by', 'at',
                           'sha256'})
DECISION_FIELDS = frozenset({'schema_version', 'decision', 'revision', 'issue', 'title', 'decides', 'authority',
                             'supersedes', 'decided_by', 'at', 'sha256'})

# The two entry kinds and their exact field sets. Each binds what design 11.2 lists -
# the kind, the item or issue, the revision, the owner, the option or `decides` list,
# the words or title, and the comment id - plus the version, the payload and its hash.
ENTRY_ANSWER = 'owner-answer'
ENTRY_DECISION = 'coordinator-decision'
ENTRY_FIELDS = {
    ENTRY_ANSWER: frozenset({'schema_version', 'kind', 'item', 'revision', 'owner', 'option', 'words',
                             'comment_id', 'payload', 'sha256'}),
    ENTRY_DECISION: frozenset({'schema_version', 'kind', 'issue', 'revision', 'decides', 'title',
                               'comment_id', 'payload', 'sha256'}),
}
# Entry field -> payload field it must equal.
ENTRY_BINDINGS = {
    ENTRY_ANSWER: {'item': 'item', 'revision': 'question_revision', 'owner': 'owner', 'option': 'option',
                   'words': 'words'},
    ENTRY_DECISION: {'issue': 'issue', 'revision': 'revision', 'decides': 'decides', 'title': 'title'},
}


def _bad(what):
    raise ValueError('Invalid owner answers journal entry: ' + what)


def _positive(value, what):
    if type(value) is not int or value < 1:
        _bad(what + ' must be a positive integer')


def _text(value, limit, what):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _bad('%s must be nonempty text of at most %d characters' % (what, limit))


def _native(value, what):
    identity(value, 'Invalid owner answers journal entry: %s must be a native id' % what)


def _durable(value, what):
    from capability_records import valid_owner
    try:
        valid_owner(value, True)
    except ValueError:
        _bad(what + ' must be a durable identity, account:<uid> or person:<name>')


def _stamp(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        _bad('at must be a %Y-%m-%dT%H:%M:%SZ server stamp')


def _attribution(block, what):
    """A host-issued entry is written by the host CLI or the web service: the route is
    one of those two and its identity is verified (design 9.1, 11.2)."""
    if not isinstance(block, dict) or set(block) != ATTRIBUTION_FIELDS:
        _bad(what + ' must be exactly {actor, route, identity, person}')
    if not isinstance(block['actor'], str) or not block['actor'].strip() or len(block['actor']) > 200:
        _bad(what + '.actor must be nonempty text')
    if block['route'] not in ('host', 'web') or block['identity'] != 'verified':
        _bad(what + ' must come from the host or web route, verified')
    # Design 9.1: on the host route an operator with no actor-map entry is recorded as
    # `operator:<actor>` (kittrial-5bb.127 corrects slice 0, which refused that form).
    if block['route'] == 'host' and block['person'] == 'operator:' + block['actor']:
        return
    _durable(block['person'], what + '.person')


def _answer(payload):
    if not isinstance(payload, dict) or set(payload) != ANSWER_FIELDS:
        _bad('an owner-answer payload must have exactly the fourteen owner-answer-v1 fields')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        _bad('payload schema_version must be 1')
    if not isinstance(payload['answer'], str) or not ANSWER_ID.fullmatch(payload['answer']):
        _bad('answer must match a-<12 hex>')
    _native(payload['item'], 'item')
    _positive(payload['question_revision'], 'question_revision')
    if not isinstance(payload['question_sha256'], str) or not SHA256.fullmatch(payload['question_sha256']):
        _bad('question_sha256 must be 64 lowercase hex')
    _durable(payload['owner'], 'owner')
    offered = payload['options_offered']
    if not isinstance(offered, list) or not 1 <= len(offered) <= OPTIONS_MAX:
        _bad('options_offered must list 1..%d options' % OPTIONS_MAX)
    ids = []
    for option in offered:
        if not isinstance(option, dict) or set(option) != {'id', 'text'} \
                or not isinstance(option['id'], str) or not OPTION_ID.fullmatch(option['id']):
            _bad('each offered option must be exactly {id, text} with an option id')
        _text(option['text'], OPTION_TEXT_MAX, 'option text')
        ids.append(option['id'])
    if len(set(ids)) != len(ids):
        _bad('offered option ids must be unique')
    if payload['option'] is not None and payload['option'] not in ids:
        _bad('option must be null or one of the offered option ids')
    _text(payload['words'], ANSWER_WORDS_MAX, 'words')
    if payload['authority'] not in ('owner', 'relayed'):
        _bad('authority must be owner or relayed')
    if payload['authority'] == 'owner':
        if payload['relayed_by'] is not None:
            _bad('relayed_by must be null when authority is owner')
    else:
        _attribution(payload['relayed_by'], 'relayed_by')
    _attribution(payload['by'], 'by')
    _stamp(payload['at'])


def _decision(payload):
    if not isinstance(payload, dict) or set(payload) != DECISION_FIELDS:
        _bad('a decision payload must have exactly the twelve coordinator-decision-v1 fields')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        _bad('payload schema_version must be 1')
    if not isinstance(payload['decision'], str) or not DECISION_ID.fullmatch(payload['decision']):
        _bad('decision must match d-<12 hex>')
    _positive(payload['revision'], 'revision')
    _native(payload['issue'], 'issue')
    _text(payload['title'], DECISION_TITLE_MAX, 'title')
    decides = payload['decides']
    if not isinstance(decides, list) or not 1 <= len(decides) <= DECIDES_MAX:
        _bad('decides must list 1..%d native ids' % DECIDES_MAX)
    for task in decides:
        _native(task, 'decides')
    if len(set(decides)) != len(decides):
        _bad('decides must not repeat an id')
    # Only an owner decision is journaled: a coordinator decision needs no proof (design 11.2).
    if payload['authority'] != 'owner':
        _bad('only an authority: owner decision has a journal entry')
    if payload['supersedes'] is not None:
        _native(payload['supersedes'], 'supersedes')
    _attribution(payload['decided_by'], 'decided_by')
    _stamp(payload['at'])


def validate_owner_entry(entry, name=None):
    """Validate one host-issued `.owner-answers/` entry, and optionally its file name.

    Raises ValueError for anything that is not exactly the shape the host writer of a
    later slice will emit: the field set of its kind, version 1, a payload that is a
    well-formed record of that kind, bound fields equal to the payload's, a `sha256`
    equal to `content_hash(payload)` and to the payload's own `sha256`, and, when
    `name` is given, a file name of `<that sha256>.json`.
    """
    if not isinstance(entry, dict):
        _bad('not an object')
    kind = entry.get('kind')
    if kind not in ENTRY_FIELDS or set(entry) != ENTRY_FIELDS[kind]:
        _bad('fields')
    if type(entry['schema_version']) is not int or entry['schema_version'] != 1:
        _bad('schema_version must be 1')
    _native(entry['comment_id'], 'comment_id')
    payload = entry['payload']
    (_answer if kind == ENTRY_ANSWER else _decision)(payload)
    for field, bound in ENTRY_BINDINGS[kind].items():
        if entry[field] != payload[bound]:
            _bad('%s does not match its payload' % field)
    digest = content_hash(payload)
    if payload['sha256'] != digest or entry['sha256'] != digest:
        _bad('hash does not match its payload')
    if name is not None and name != digest + '.json':
        raise ValueError('Owner answers journal path mismatch')
    return entry


# ---------------------------------------------------------------------------------------
# Slice 1 (kittrial-5bb.127): the item and question reader, read-only.
#
# Parses `open-item-v1`, `item-resolution-v1` and `owner-answer-v1` records on item
# anchors (rows labelled `open-item`), derives each item as design 4.5 defines it, and
# answers `items list|get` and `questions --for|get`, plus the `open-item` and
# `owner-question` brief kinds. No writer, no decision reader (slice 3), no void (no kit
# can void these kinds yet: recovery keeps them out of KIND_PREFIXES until the writer
# slice). Reading changes nothing.
# ---------------------------------------------------------------------------------------
import datetime

import json

import record_json
from requirements import canonical_bytes
from reserved_comments import record_comment_kind, _reserved_prefix_view

ITEM_KINDS = ('blocker', 'correction', 'decision', 'dependency', 'question')
ITEM_STATES = ('open', 'blocked', 'resolved', 'superseded')
CLOSED_STATES = ('resolved', 'superseded')
DISPOSITIONS = ('resolved', 'superseded', 'reopened')
ITEM_TEXT_MAX = 4000
SOURCE_MAX = 240
STATE_NOTE_MAX = 1000
REASON_MAX = 2000
EVIDENCE_MAX = 1000
LIST_LIMIT_MAX = 100
LIST_LIMIT_DEFAULT = 20
RECORDS_PER_ANCHOR_MAX = 2000
ITEM_ANCHORS_MAX = 2000
BRIEF_MAX = 3
DUE_SOON_DAYS = 7
JOURNAL_ENTRY_MAX_BYTES = 256000
LIST_TEXT_MAX = 400
RESOLUTION_ID = re.compile(r'r-[0-9a-f]{12}')
EVIDENCE = re.compile(r'(?:commit|comment|answer|decision|task|item):\S+')
DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
HTTP_AUTHOR = re.compile(r'(?:usr|agent)_[0-9a-f]{16}')
ITEM_FIELDS = frozenset({'schema_version', 'id', 'revision', 'kind', 'text', 'source', 'owner', 'task', 'for',
                         'options', 'recommended', 'due_by', 'state', 'state_note', 'resolved_by', 'provenance',
                         'submitted_by', 'at', 'sha256'})
RESOLUTION_FIELDS = frozenset({'schema_version', 'resolution', 'item', 'revision', 'disposition', 'reason',
                               'evidence', 'answer', 'by', 'at', 'sha256'})
# The four family markers (the prefix up to its version), derived from the reserved prefixes.
FAMILY_MARKERS = tuple(prefix[:-len('v1\n')] for prefix in PREFIXES)
RELAYED_WARNING = ("Relayed answer: the owner's words as reported by %s; not proof the owner said them. "
                   "The item is closed by this answer; the owner's own later answer or a reopen replaces it.")
TRUST_WORDS = ('attested', 'unattested')


class Malformed(ValueError):
    """One record comment that is not a well-formed record of its kind."""


def _need(condition, kind, what):
    if not condition:
        raise Malformed('%s: %s' % (kind, what))


def _text_ok(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _durable_ok(value):
    from capability_records import valid_owner
    try:
        valid_owner(value, True)
    except ValueError:
        return False
    return True


def _block_ok(block):
    """The one attribution shape of design 9.1, as any route writes it."""
    if not isinstance(block, dict) or set(block) != ATTRIBUTION_FIELDS:
        return False
    if not _text_ok(block['actor'], 200) or block['route'] not in ROUTES:
        return False
    if block['route'] == 'endpoint':
        return block['identity'] == 'unverified' and block['person'] is None
    if block['identity'] != 'verified':
        return False
    if block['route'] == 'host' and block['person'] == 'operator:' + block['actor']:
        return True
    return _durable_ok(block['person'])


def _native_ok(value):
    try:
        identity(value)
    except ValueError:
        return False
    return True


def _stamp_ok(value):
    return isinstance(value, str) and bool(STAMP.fullmatch(value))


def _day_ok(value):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _options_ok(options):
    if not isinstance(options, list) or not 1 <= len(options) <= OPTIONS_MAX:
        return False
    ids = []
    for option in options:
        if not isinstance(option, dict) or set(option) != {'id', 'text'} or not isinstance(option['id'], str) \
                or not OPTION_ID.fullmatch(option['id']) or not _text_ok(option['text'], OPTION_TEXT_MAX):
            return False
        ids.append(option['id'])
    return len(set(ids)) == len(ids)


def _payload(body, prefix, kind):
    """The JSON object after `prefix`, through the nesting guard; Malformed otherwise."""
    try:
        record = record_json.loads(body[len(prefix):])
    except ValueError as error:
        raise Malformed('%s: not JSON (%s)' % (kind, error)) from None
    _need(isinstance(record, dict), kind, 'not a JSON object')
    # Records are written as canonical JSON (requirements.canonical_bytes); requiring it
    # makes the brief pre-filter below exact.
    try:
        canonical = canonical_bytes(record).decode('utf-8')
    except ValueError as error:
        raise Malformed('%s: %s' % (kind, error)) from None
    _need(body[len(prefix):] == canonical, kind, 'not canonical JSON')
    return record


def _hash_ok(record):
    return isinstance(record.get('sha256'), str) and record['sha256'] == content_hash(record)


def parse_item(body, anchor):
    """One `open-item-v1` record on anchor `anchor` (design 4.1), or Malformed."""
    kind = 'open-item-v1'
    r = _payload(body, OPEN_ITEM_PREFIX, kind)
    _need(set(r) == ITEM_FIELDS, kind, 'fields must be exactly the nineteen of design 4.1')
    _need(type(r['schema_version']) is int and r['schema_version'] == 1, kind, 'schema_version must be 1')
    _need(r['id'] == anchor, kind, 'id must be the native anchor id')
    _need(type(r['revision']) is int and r['revision'] >= 1, kind, 'revision must be a positive integer')
    _need(r['kind'] in ITEM_KINDS, kind, 'kind must be one of ' + ', '.join(ITEM_KINDS))
    _need(_text_ok(r['text'], ITEM_TEXT_MAX), kind, 'text must be 1..%d characters' % ITEM_TEXT_MAX)
    _need(_text_ok(r['source'], SOURCE_MAX), kind, 'source must be 1..%d characters' % SOURCE_MAX)
    _need(_durable_ok(r['owner']), kind, 'owner must be a durable identity')
    _need(r['task'] is None or _native_ok(r['task']), kind, 'task must be a native id or null')
    question = r['kind'] == 'question'
    _need((r['for'] is not None) == question and (r['options'] is not None) == question, kind,
          'for and options are set exactly on a question')
    if question:
        _need(_durable_ok(r['for']), kind, 'for must be a durable identity')
        _need(_options_ok(r['options']), kind, 'options must be 1..%d unique {id, text}' % OPTIONS_MAX)
    offered = [option['id'] for option in r['options'] or []]
    _need(r['recommended'] is None or r['recommended'] in offered, kind, 'recommended must be an offered option')
    _need(r['due_by'] is None or _day_ok(r['due_by']), kind, 'due_by must be YYYY-MM-DD or null')
    _need(r['state'] in ITEM_STATES, kind, 'state must be one of ' + ', '.join(ITEM_STATES))
    if r['state'] == 'blocked':
        _need(_text_ok(r['state_note'], STATE_NOTE_MAX), kind, 'a blocked item needs a state_note')
    else:
        _need(r['state_note'] is None, kind, 'state_note is set exactly when blocked')
    _need(r['resolved_by'] is None or _native_ok(r['resolved_by']), kind, 'resolved_by must be a comment id')
    p = r['provenance']
    _need(p == {'kind': 'new'} or (isinstance(p, dict) and set(p) == {'kind', 'checkpoint', 'item'}
                                   and p['kind'] == 'imported' and _native_ok(p['checkpoint'])
                                   and _text_ok(p['item'], SOURCE_MAX)), kind, 'provenance is malformed')
    _need(_block_ok(r['submitted_by']), kind, 'submitted_by must be {actor, route, identity, person}')
    _need(_stamp_ok(r['at']), kind, 'at must be a server stamp')
    _need(_hash_ok(r), kind, 'sha256 does not match the record')
    return r


def parse_resolution(body, anchor):
    """One `item-resolution-v1` record on anchor `anchor` (design 4.2), or Malformed."""
    kind = 'item-resolution-v1'
    r = _payload(body, ITEM_RESOLUTION_PREFIX, kind)
    _need(set(r) == RESOLUTION_FIELDS, kind, 'fields must be exactly the eleven of design 4.2')
    _need(type(r['schema_version']) is int and r['schema_version'] == 1, kind, 'schema_version must be 1')
    _need(isinstance(r['resolution'], str) and bool(RESOLUTION_ID.fullmatch(r['resolution'])), kind,
          'resolution must match r-<12 hex>')
    _need(r['item'] == anchor, kind, 'item must be the native anchor id')
    _need(type(r['revision']) is int and r['revision'] >= 1, kind, 'revision must be a positive integer')
    _need(r['disposition'] in DISPOSITIONS, kind, 'disposition must be one of ' + ', '.join(DISPOSITIONS))
    _need(_text_ok(r['reason'], REASON_MAX), kind, 'reason must be 1..%d characters' % REASON_MAX)
    _need(_text_ok(r['evidence'], EVIDENCE_MAX) and bool(EVIDENCE.fullmatch(r['evidence'])), kind,
          'evidence must be one pointer, commit:|comment:|answer:|decision:|task:|item:<id>')
    _need(r['answer'] is None or _native_ok(r['answer']), kind, 'answer must be a comment id or null')
    _need(_block_ok(r['by']), kind, 'by must be {actor, route, identity, person}')
    _need(_stamp_ok(r['at']), kind, 'at must be a server stamp')
    _need(_hash_ok(r), kind, 'sha256 does not match the record')
    return r


def parse_answer(body, anchor):
    """One `owner-answer-v1` record on anchor `anchor` (design 4.3), or Malformed. The
    shape is the one the `.owner-answers` entry validator fixes (slice 0)."""
    kind = 'owner-answer-v1'
    r = _payload(body, OWNER_ANSWER_PREFIX, kind)
    try:
        _answer(r)
    except ValueError as error:
        raise Malformed('%s: %s' % (kind, str(error).replace('Invalid owner answers journal entry: ', ''))) from None
    _need(r['item'] == anchor, kind, 'item must be the native anchor id')
    _need(_hash_ok(r), kind, 'sha256 does not match the record')
    return r


PARSERS = {'open-item': parse_item, 'item-resolution': parse_resolution, 'owner-answer': parse_answer}


def trust_of(block, author, operators):
    """`attested` or `unattested` (design 9.2): where a record came from. Attested only
    when the identity is verified, the native comment author is the record's actor, and
    either the route is host and that author is on the operator allowlist, or the route
    is web and the author is a web account or agent the service bound."""
    if not isinstance(block, dict) or block.get('identity') != 'verified' or author != block.get('actor'):
        return 'unattested'
    if block.get('route') == 'host' and author in (operators or ()):
        return 'attested'
    if block.get('route') == 'web' and isinstance(author, str) and HTTP_AUTHOR.fullmatch(author):
        return 'attested'
    return 'unattested'


def journal_entry_matches(journal, record, comment_id):
    """True only when `.owner-answers/<sha256>.json` exists in project directory
    `journal`, is a regular file within bounds, validates, and binds exactly this
    record to this comment. Fail closed: anything else is False (design 11.2)."""
    if journal is None:
        return False
    from pathlib import Path
    folder = Path(journal) / OWNER_ANSWERS_JOURNAL
    try:
        path = folder / (record['sha256'] + '.json')
        if folder.is_symlink() or path.is_symlink() or not path.is_file() \
                or path.stat().st_size > JOURNAL_ENTRY_MAX_BYTES:
            return False
        entry = record_json.loads(path.read_text(encoding='utf-8'))
        validate_owner_entry(entry, path.name)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return entry['kind'] == ENTRY_ANSWER and entry['comment_id'] == str(comment_id) and entry['payload'] == record


def due_of(due_by, current):
    if due_by is None:
        return 'unset'
    day = datetime.date.fromisoformat(due_by)
    if day < current:
        return 'expired'
    if day <= current + datetime.timedelta(days=DUE_SOON_DAYS):
        return 'due-soon'
    return 'ok'


def _comment_key(comment):
    raw = comment.get('id')
    number = raw if type(raw) is int else int(raw) if isinstance(raw, str) and raw.isdigit() else None
    return (str(comment.get('created_at') or ''), number if number is not None else -1, str(raw))


def _answer_view(answer, clip_words=LIST_TEXT_MAX):
    record = answer['record']
    view = {'comment_id': answer['comment_id'], 'answer': record['answer'], 'authority': record['authority'],
            'option': record['option'], 'owner': record['owner'],
            'words': {'text': record['words'][:clip_words],
                      'omitted_chars': max(0, len(record['words']) - clip_words)},
            'by': record['by']['actor'], 'relayed_by': (record['relayed_by'] or {}).get('actor'),
            'question_revision': record['question_revision'], 'trust': answer['trust'],
            'journal': answer['journal'], 'closes': answer['closes'], 'at': record['at']}
    if record['authority'] == 'relayed':
        view['warning'] = RELAYED_WARNING % record['relayed_by']['actor']
    return view


CONTENT_FIELDS = ('kind', 'text', 'source', 'owner', 'task', 'for', 'options', 'recommended', 'due_by')
QUESTION_FIELDS = ('kind', 'text', 'for', 'options')
# Depth rule for an export row (kittrial-5bb.127 review B3): a row nested deeper than this
# is reported unreadable WITHOUT being parsed, so the result never depends on the
# interpreter's recursion limit. 750 is the value kittrial-5bb.169 proposes for
# record_json.ROW_NESTING_MAX; this module counts depth itself until that lands.
ROW_NESTING_MAX = 750
_DEPTH_TOKEN = re.compile(r'\\.|["\[\]{}]', re.DOTALL)
_ROW_ID = re.compile(r'"id"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.-]{0,160})"')
_ANCHOR_LABEL = '"%s"' % FAMILY_LABEL


def nesting_exceeds(text, limit=ROW_NESTING_MAX):
    """True when `text` nests brackets deeper than `limit` outside string literals.
    One pass, no recursion: the same scan as record_json.nesting, with a bound."""
    if text.count('[') + text.count('{') <= limit:
        return False
    depth = 0
    in_string = False
    for match in _DEPTH_TOKEN.finditer(text):
        token = match.group()
        if token == '"':
            in_string = not in_string
        elif in_string or token[0] == '\\':
            continue
        elif token in '[{':
            depth += 1
            if depth > limit:
                return True
        else:
            depth -= 1
    return False


def _classify(comments):
    """Split an anchor's comments into record comments of the three item kinds (in
    comment order) and warnings for everything that only looks like one."""
    records, warnings = [], []
    for order, comment in enumerate(comments):
        text = comment.get('text')
        if not isinstance(text, str):
            continue
        view = _reserved_prefix_view(text)
        if not view.startswith(FAMILY_MARKERS):
            continue
        cid = str(comment.get('id'))
        kind = record_comment_kind(text)
        if kind is None or kind[0] == 'unknown' or kind[1] is None:
            # Starts like a record but is not one (no newline after the version, `-vx`):
            # hidden from every surface by slice 0, never a record.
            warnings.append({'code': 'not-a-record', 'comment_id': cid,
                             'detail': 'starts like an open-item record but is not one; it is ignored'})
        elif kind[0] == 'coordinator-decision':
            warnings.append({'code': 'misplaced-record', 'comment_id': cid,
                             'detail': 'a coordinator decision belongs on a decision issue, not an item anchor'})
        elif kind[0] not in PARSERS:
            warnings.append({'code': 'not-a-record', 'comment_id': cid, 'detail': 'unknown kind %s' % kind[0]})
        elif kind[2] == 'unsupported':
            warnings.append({'code': 'unsupported-record', 'comment_id': cid,
                             'detail': '%s-v%s is newer than this kit' % (kind[0], kind[1])})
        elif text != view:
            warnings.append({'code': 'malformed-record', 'comment_id': cid,
                             'detail': '%s: the prefix is not exact (byte order mark or CRLF)' % kind[0]})
        else:
            records.append((order, kind[0], comment))
    return records, warnings


def _adopt(revisions, operators, warnings, conflicted):
    """The revisions the item is made of, in revision order (design 4.5, review B1).

    The first readable revision fixes the kind: a later revision of another kind is
    refused, never adopted. Once an attested revision exists, an unattested revision
    that changes any content field is refused, so it never changes what an attested one
    said; an unattested state move (block, unblock) with the same content is adopted.
    Two different records claiming one revision number keep the first and are reported.
    """
    adopted = []
    attested = False
    for revision in sorted(revisions, key=lambda r: (r['record']['revision'], r['order'])):
        record = revision['record']
        revision['trust'] = trust_of(record['submitted_by'], revision['author'], operators)
        if adopted:
            previous = adopted[-1]['record']
            if record['revision'] == previous['revision']:
                if record['sha256'] != previous['sha256']:
                    conflicted.append('two different records claim revision %d' % record['revision'])
                continue
            if record['kind'] != adopted[0]['record']['kind']:
                warnings.append({'code': 'kind-change', 'comment_id': revision['comment_id'],
                                 'detail': 'revision %d changes the kind from %s to %s; refused'
                                           % (record['revision'], adopted[0]['record']['kind'], record['kind'])})
                continue
            if attested and revision['trust'] != 'attested' \
                    and any(record[f] != previous[f] for f in CONTENT_FIELDS):
                warnings.append({'code': 'unattested-change', 'comment_id': revision['comment_id'],
                                 'detail': 'revision %d is unattested and changes what an attested revision '
                                           'said; refused' % record['revision']})
                continue
        adopted.append(revision)
        attested = attested or revision['trust'] == 'attested'
    if adopted and adopted[0]['record']['revision'] != 1:
        warnings.append({'code': 'missing-revision', 'detail': 'revision 1 is not readable'})
    return adopted


def _question_closure(newest, adopted, resolutions, answers):
    """(answer, problem) for the closure of a question whose newest revision says
    `resolved` (design 9.3, review B1). A closure counts only when every link is sound:
    the resolution the revision names is an attested `resolved` resolution; it names an
    answer that `closes`; that answer answered revision N of the question, whose sha256
    and options it repeats; the closing revision is attested, is revision N+1 and repeats
    revision N's kind, text, options and addressee; no later `reopened` resolution
    withdraws it."""
    record = newest['record']
    resolution = resolutions.get(record['resolved_by'])
    if resolution is None or resolution['record']['disposition'] != 'resolved':
        return None, 'the closing revision names no resolved item-resolution-v1 (resolved_by %s)' % record['resolved_by']
    if resolution['trust'] != 'attested':
        return None, 'the closing resolution is unattested'
    if newest['trust'] != 'attested':
        return None, 'the closing revision is unattested'
    answer = answers.get(resolution['record']['answer'])
    if answer is None:
        return None, 'the closing resolution names no readable answer'
    if not answer['closes']:
        return None, 'the answer the closure rests on is not trusted'
    number = answer['record']['question_revision']
    asked = next((r for r in adopted if r['record']['revision'] == number), None)
    if asked is None or asked['record']['sha256'] != answer['record']['question_sha256'] \
            or asked['record']['options'] != answer['record']['options_offered']:
        return None, 'the answer does not match revision %d of the question as asked' % number
    if resolution['record']['revision'] != number or record['revision'] != number + 1 \
            or any(record[f] != asked['record'][f] for f in QUESTION_FIELDS):
        return None, 'the closing revision is not the unchanged revision after the one answered'
    if any(r['record']['disposition'] == 'reopened' and r['order'] > resolution['order']
           for r in resolutions.values()):
        return None, 'a later reopen withdrew this closure'
    return answer, None


def item_view(row, operators=(), journal=None, current=None):
    """One item anchor as every read sees it (design 4.5). Never raises.

    Returns a dict with `state` (the effective state) and the derived words, or, when
    the anchor holds no readable revision, `state: 'unreadable'` with the reason. Every
    record comment that is not used is named in `warnings`, so nothing is dropped
    silently. Above RECORDS_PER_ANCHOR_MAX record comments the NEWEST are read and the
    view says it was cut.
    """
    current = current or datetime.date.today()
    anchor = row.get('id')
    comments = sorted((c for c in row.get('comments') or [] if isinstance(c, dict)), key=_comment_key)
    candidates, warnings = _classify(comments)
    total = len(candidates)
    cut = total > RECORDS_PER_ANCHOR_MAX
    if cut:
        warnings.append({'code': 'records-cap', 'detail': 'this anchor holds %d record comments; only the newest '
                                                          '%d were read' % (len(candidates), RECORDS_PER_ANCHOR_MAX)})
        candidates = candidates[-RECORDS_PER_ANCHOR_MAX:]
    coverage = {'records': total, 'records_read': len(candidates), 'cut': cut}
    parsed = {'open-item': [], 'item-resolution': [], 'owner-answer': []}
    for order, kind, comment in candidates:
        cid = str(comment.get('id'))
        try:
            record = PARSERS[kind](comment['text'], anchor)
        except Malformed as error:
            warnings.append({'code': 'malformed-record', 'comment_id': cid, 'detail': str(error)})
            continue
        parsed[kind].append({'record': record, 'comment_id': cid, 'author': comment.get('author'), 'order': order})
    conflicted = []
    adopted = _adopt(parsed['open-item'], operators, warnings, conflicted)
    if not adopted:
        return {'id': anchor, 'state': 'unreadable', 'warnings': warnings, 'coverage': coverage,
                'reason': 'the anchor holds no readable open-item-v1 revision'}
    newest = adopted[-1]
    record = newest['record']
    question = record['kind'] == 'question'
    resolutions = {}
    for resolution in parsed['item-resolution']:
        resolution['trust'] = trust_of(resolution['record']['by'], resolution['author'], operators)
        resolutions.setdefault(resolution['comment_id'], resolution)
    answers = {}
    for answer in parsed['owner-answer']:
        answer['trust'] = trust_of(answer['record']['by'], answer['author'], operators)
        answer['journal'] = journal_entry_matches(journal, answer['record'], answer['comment_id'])
        wrong = []
        if answer['trust'] != 'attested':
            wrong.append('not attested on the host or web route')
        if not answer['journal']:
            wrong.append('no matching .owner-answers entry')
        # Rules 2 and 3 of design 9.3, re-applied on read.
        if answer['record']['owner'] != record['for']:
            wrong.append('the answer names %s, but the question is for %s' % (answer['record']['owner'], record['for']))
        if answer['record']['authority'] == 'owner' and answer['record']['by']['person'] != record['for']:
            wrong.append('authority owner, but the writer is mapped to %s, not %s'
                         % (answer['record']['by']['person'], record['for']))
        answer['closes'] = not wrong
        if wrong:
            warnings.append({'code': 'untrusted-answer', 'comment_id': answer['comment_id'],
                             'detail': 'shown, but it closes nothing: ' + '; '.join(wrong)})
        answers.setdefault(answer['comment_id'], answer)
    state = record['state']
    closed_by = None
    closing = None
    resolution = None
    if state in CLOSED_STATES and question:
        closing, problem = _question_closure(newest, adopted, resolutions, answers) if state == 'resolved' \
            else (None, 'a question is closed only by an answer, never superseded')
        if problem:
            conflicted.append(problem)
        else:
            closed_by = closing['record']['authority']
            resolution = resolutions[record['resolved_by']]
    elif state in CLOSED_STATES:
        resolution = resolutions.get(record['resolved_by']) if record['resolved_by'] else None
        if resolution is None or resolution['record']['disposition'] != state:
            conflicted.append('state %s has no matching item-resolution-v1 (resolved_by %s)'
                              % (state, record['resolved_by']))
            resolution = None
    # A question whose closure is not sound closes nothing (design 9.3 rule 7).
    effective = 'open' if question and state in CLOSED_STATES and closed_by is None else state
    reopened = [r for r in resolutions.values() if r['record']['disposition'] == 'reopened']
    reopened.sort(key=lambda r: r['order'])
    reopened_by = reopened[-1]['comment_id'] if reopened and effective in ('open', 'blocked') else None
    for problem in conflicted:
        warnings.append({'code': 'conflicted', 'detail': problem})
    answers_newest = sorted(answers.values(), key=lambda a: a['order'], reverse=True)
    return {'id': anchor, 'revision': record['revision'], 'kind': record['kind'], 'state': effective,
            'stored_state': state, 'state_note': record['state_note'], 'owner': record['owner'],
            'task': record['task'], 'due_by': record['due_by'], 'due': due_of(record['due_by'], current),
            'for': record['for'], 'options': record['options'], 'recommended': record['recommended'],
            'text': record['text'], 'source': record['source'], 'provenance': record['provenance'],
            'submitted_by': record['submitted_by'], 'trust': newest['trust'],
            'record': record, 'record_comment_id': newest['comment_id'],
            'revisions': [{'revision': r['record']['revision'], 'comment_id': r['comment_id'],
                           'sha256': r['record']['sha256'], 'trust': r['trust']} for r in adopted],
            'resolved_by': record['resolved_by'], 'resolution': resolution, 'closing_answer': closing,
            'answers': answers_newest, 'closed_by': closed_by, 'reopened_by': reopened_by,
            'conflicted': bool(conflicted), 'warnings': warnings, 'coverage': coverage}


def is_anchor(row):
    """An item anchor: the exact `open-item` label AND an exact v1 record comment of the
    family (reserved_comments.is_record_anchor, the rule that hides it). A row a
    contributor labelled `open-item` with no record is an ordinary row: it is not read,
    not reported and not counted against ITEM_ANCHORS_MAX (review P2 a)."""
    from reserved_comments import is_record_anchor
    return isinstance(row.get('id'), str) and FAMILY_LABEL in (row.get('labels') or []) and is_record_anchor(row)


def ledger(rows, operators=(), journal=None, current=None):
    """Every item anchor among `rows` as `item_view` sees it, in id order, cut at
    ITEM_ANCHORS_MAX with the cut reported, and whether any anchor's records were cut."""
    anchors = sorted((row for row in rows if isinstance(row, dict) and is_anchor(row)), key=lambda row: row['id'])
    views = [item_view(row, operators, journal, current) for row in anchors[:ITEM_ANCHORS_MAX]]
    records_cut = [view['id'] for view in views if view['coverage']['cut']]
    return views, {'anchors': len(anchors), 'anchors_read': len(views), 'records_cut': records_cut,
                   'cut': len(anchors) > ITEM_ANCHORS_MAX or bool(records_cut)}


def parse_export(text):
    """The rows of one `bd export --all`, the rows that could not be read, and the
    count of other lines.

    Parsed here, line by line, not by the endpoint's JSON policy: a row nested deeper
    than ROW_NESTING_MAX is reported unreadable without being parsed, so no interpreter
    limit decides the answer (review B3). A row that cannot be parsed is reported only
    when its text names the `open-item` label, the only rows that can be anchors."""
    rows, unreadable, other = [], [], 0
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith('{'):
            other += 1
            continue
        found = _ROW_ID.search(line)
        reason = None
        if nesting_exceeds(line):
            reason = 'nested deeper than %d levels' % ROW_NESTING_MAX
        else:
            try:
                row = json.loads(line)
            except (ValueError, RecursionError) as error:
                reason = 'not JSON (%s)' % type(error).__name__
            else:
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    other += 1
                continue
        if _ANCHOR_LABEL in line:
            unreadable.append({'id': found.group(1) if found else None,
                               'reason': 'this row cannot be read (%s); it may be an item anchor' % reason})
    return rows, unreadable, other


def read_anchor_rows(run):
    """Every row of the project in one `bd export --all`, plus the unreadable ones.

    One bd call: the export brief already reads returns the rows with their labels and
    comments, closed anchors included (review B2: `bd show` of every anchor took 15 s at
    400 anchors and over 70 s at 2,000 on real bd; the export took 0.55 s and 1.8 s).
    `run` must return the raw stdout, not the endpoint's decoded document."""
    rows, unreadable, _ = parse_export(run(['export', '--all']))
    return rows, unreadable


# -- the read commands -------------------------------------------------------------------

def _options(args, flags, usage):
    """Parse `--flag VALUE` pairs (repeatable where `flags[name]` is True)."""
    values = {name: ([] if many else None) for name, many in flags.items()}
    positional = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in flags:
            if index + 1 >= len(args):
                raise ValueError('%s needs a value; usage: %s' % (token, usage))
            if flags[token]:
                values[token].append(args[index + 1])
            elif values[token] is not None:
                raise ValueError('%s given twice; usage: %s' % (token, usage))
            else:
                values[token] = args[index + 1]
            index += 2
        elif token.startswith('--'):
            raise ValueError('unknown option %s; usage: %s' % (token, usage))
        else:
            positional.append(token)
            index += 1
    return values, positional


def _page(values, command):
    limit, offset = values.get('--limit'), values.get('--offset')
    try:
        limit = LIST_LIMIT_DEFAULT if limit is None else int(limit)
        if not 1 <= limit <= LIST_LIMIT_MAX:
            raise ValueError
    except ValueError:
        raise ValueError('%s: --limit must be 1..%d' % (command, LIST_LIMIT_MAX)) from None
    try:
        offset = 0 if offset is None else int(offset)
        if offset < 0:
            raise ValueError
    except ValueError:
        raise ValueError('%s: --offset must be >= 0' % command) from None
    return limit, offset


def _list_entry(view):
    entry = {key: view[key] for key in ('id', 'revision', 'kind', 'state', 'stored_state', 'state_note', 'owner',
                                        'task', 'due_by', 'due', 'for', 'options', 'recommended', 'closed_by',
                                        'reopened_by', 'resolved_by', 'trust', 'record_comment_id', 'provenance',
                                        'conflicted', 'warnings')}
    entry['text'] = {'text': view['text'][:LIST_TEXT_MAX], 'omitted_chars': max(0, len(view['text']) - LIST_TEXT_MAX)}
    entry['source'] = {'text': view['source'], 'omitted_chars': 0}
    entry.update(_answer_fields(view, LIST_TEXT_MAX))
    entry['answers'] = len(view['answers'])
    entry['coverage'] = view['coverage']
    return entry


def _answer_fields(view, clip):
    """`answer` is only the answer a sound closure rests on (review P2 c). The newest
    answer that closes nothing, if any, is shown only as `answer_that_closes_nothing`."""
    closing = view['closing_answer']
    other = next((a for a in view['answers'] if a is not closing and not a['closes']), None)
    return {'answer': None if closing is None else _answer_view(closing, clip),
            'answer_that_closes_nothing': None if other is None else _answer_view(other, clip)}


def _get_entry(view):
    entry = _list_entry(view)
    entry['text'] = {'text': view['text'], 'omitted_chars': 0}
    entry['answers'] = [_answer_view(answer, ANSWER_WORDS_MAX) for answer in view['answers']]
    entry['record'] = view['record']
    entry['revisions'] = view['revisions']
    entry['submitted_by'] = view['submitted_by']
    entry['resolution'] = None if view['resolution'] is None else {
        'comment_id': view['resolution']['comment_id'], 'trust': view['resolution']['trust'],
        **{key: view['resolution']['record'][key] for key in ('resolution', 'revision', 'disposition', 'reason',
                                                              'evidence', 'answer', 'at')}}
    entry['coverage'] = view['coverage']
    entry['schema_version'] = 1
    return entry


COVERAGE = ('Every item anchor (rows labelled open-item) up to %d, and up to %d record comments on each; '
            '`cut` says when either cap was reached. Reading changes nothing.' % (ITEM_ANCHORS_MAX,
                                                                                  RECORDS_PER_ANCHOR_MAX))
ITEMS_USAGE = ('items list [--owner IDENTITY] [--task TASK] [--kind KIND]... '
               '[--state open|blocked|resolved|superseded|all] [--closed-by owner|relayed] '
               '[--due expired|due-soon|unset] [--limit N] [--offset N] | items get ITEM')
QUESTIONS_USAGE = ('questions --for OWNER [--state open|resolved|all] [--closed-by owner|relayed] '
                   '[--limit N] [--offset N] | questions get QUESTION')


def _unreadable_views(views, unreadable):
    out = list(unreadable)
    out += [{'id': view['id'], 'reason': view['reason'], 'warnings': view['warnings']}
            for view in views if view['state'] == 'unreadable']
    return out


def read_items(args, rows, unreadable=(), operators=(), journal=None, current=None):
    """`items list` / `items get` over already-read anchor rows."""
    views, cut = ledger(rows, operators, journal, current)
    if args[:1] == ['get']:
        if len(args) != 2:
            raise ValueError('items get takes exactly one ITEM; usage: ' + ITEMS_USAGE)
        view = next((v for v in views if v['id'] == args[1]), None)
        if view is None:
            listed = next((u for u in unreadable if u.get('id') == args[1]), None)
            if listed is not None:
                return {'schema_version': 1, 'id': args[1], 'state': 'unreadable', 'reason': listed['reason']}
            raise ValueError('items get takes exactly one ITEM that exists; %s is unknown' % args[1])
        if view['state'] == 'unreadable':
            return {'schema_version': 1, 'id': view['id'], 'state': 'unreadable', 'reason': view['reason'],
                    'warnings': view['warnings'], 'coverage': view['coverage']}
        return _get_entry(view)
    if args[:1] != ['list']:
        raise ValueError('usage: ' + ITEMS_USAGE)
    values, positional = _options(args[1:], {'--owner': False, '--task': False, '--kind': True, '--state': False,
                                             '--closed-by': False, '--due': False, '--limit': False,
                                             '--offset': False}, ITEMS_USAGE)
    if positional:
        raise ValueError('items list takes no positional argument; usage: ' + ITEMS_USAGE)
    state = values['--state'] or 'all'
    if state not in ITEM_STATES + ('all',):
        raise ValueError('items list: --state must be open, blocked, resolved, superseded or all')
    for kind in values['--kind']:
        if kind not in ITEM_KINDS:
            raise ValueError('items list: --kind must be one of ' + ', '.join(ITEM_KINDS))
    if values['--closed-by'] not in (None, 'owner', 'relayed'):
        raise ValueError('items list: --closed-by must be owner or relayed')
    if values['--due'] not in (None, 'expired', 'due-soon', 'unset'):
        raise ValueError('items list: --due must be expired, due-soon or unset')
    limit, offset = _page(values, 'items list')
    chosen = [v for v in views if v['state'] != 'unreadable'
              and (state == 'all' or v['state'] == state)
              and (values['--owner'] is None or v['owner'] == values['--owner'].strip())
              and (values['--task'] is None or v['task'] == values['--task'])
              and (not values['--kind'] or v['kind'] in values['--kind'])
              and (values['--closed-by'] is None or v['closed_by'] == values['--closed-by'])
              and (values['--due'] is None or v['due'] == values['--due'])]
    page = chosen[offset:offset + limit]
    return {'schema_version': 1, 'total': len(chosen), 'items': [_list_entry(v) for v in page],
            'next_offset': offset + limit if offset + limit < len(chosen) else None,
            'unreadable': _unreadable_views(views, unreadable), 'coverage': dict(cut, note=COVERAGE)}


def _question_entry(view, full=False):
    entry = {'id': view['id'], 'revision': view['revision'], 'state': view['state'],
             'stored_state': view['stored_state'], 'for': view['for'], 'owner': view['owner'],
             'task': view['task'],
             'text': {'text': view['text'] if full else view['text'][:LIST_TEXT_MAX],
                      'omitted_chars': 0 if full else max(0, len(view['text']) - LIST_TEXT_MAX)},
             'options': view['options'], 'recommended': view['recommended'], 'due_by': view['due_by'],
             'due': view['due'], 'closed_by': view['closed_by'], 'reopened_by': view['reopened_by'],
             'asked_by': view['submitted_by']['actor'], 'trust': view['trust'], 'conflicted': view['conflicted'],
             'warnings': view['warnings']}
    entry.update(_answer_fields(view, ANSWER_WORDS_MAX if full else LIST_TEXT_MAX))
    if full:
        entry['answers'] = [_answer_view(answer, ANSWER_WORDS_MAX) for answer in view['answers']]
        entry['resolution'] = _get_entry(view)['resolution']
        entry['revisions'] = view['revisions']
        entry['coverage'] = view['coverage']
        entry['schema_version'] = 1
    return entry


def read_questions(args, rows, unreadable=(), operators=(), journal=None, current=None):
    """`questions --for OWNER` / `questions get QUESTION` over already-read anchor rows."""
    views, cut = ledger(rows, operators, journal, current)
    questions = [v for v in views if v['state'] != 'unreadable' and v['kind'] == 'question']
    if args[:1] == ['get']:
        if len(args) != 2:
            raise ValueError('questions get takes exactly one QUESTION; usage: ' + QUESTIONS_USAGE)
        view = next((v for v in questions if v['id'] == args[1]), None)
        if view is None:
            raise ValueError('questions get takes exactly one QUESTION that exists; %s is unknown or not a question'
                             % args[1])
        return _question_entry(view, full=True)
    values, positional = _options(args, {'--for': False, '--state': False, '--closed-by': False, '--limit': False,
                                         '--offset': False}, QUESTIONS_USAGE)
    if positional:
        raise ValueError('usage: ' + QUESTIONS_USAGE)
    if values['--for'] is None:
        raise ValueError('questions needs --for OWNER; list every question with items list --kind question')
    owner = values['--for'].strip()
    if not _durable_ok(owner):
        raise ValueError('questions --for takes a durable identity, account:<uid> or person:<name>')
    state = values['--state'] or 'all'
    if state not in ('open', 'resolved', 'all'):
        raise ValueError('questions: --state must be open, resolved or all')
    if values['--closed-by'] not in (None, 'owner', 'relayed'):
        raise ValueError('questions: --closed-by must be owner or relayed')
    limit, offset = _page(values, 'questions')
    mine = [v for v in questions if v['for'] == owner]
    # Disjoint counts (design 7.2): open (open or blocked) + closed by owner + closed relayed
    # = total. A question whose closure is not trusted reads open (item_view).
    counts = {'open': sum(1 for v in mine if v['closed_by'] is None),
              'closed_by_owner': sum(1 for v in mine if v['closed_by'] == 'owner'),
              'closed_relayed': sum(1 for v in mine if v['closed_by'] == 'relayed')}
    chosen = [v for v in mine
              if (state == 'all' or (state == 'open') == (v['closed_by'] is None))
              and (values['--closed-by'] is None or v['closed_by'] == values['--closed-by'])]
    page = chosen[offset:offset + limit]
    return {'schema_version': 1, 'for': owner, 'for_kind': owner.partition(':')[0], 'total': len(mine), **counts,
            'matching': len(chosen), 'items': [_question_entry(v) for v in page],
            'next_offset': offset + limit if offset + limit < len(chosen) else None,
            'unreadable': _unreadable_views(views, unreadable), 'coverage': dict(cut, note=COVERAGE)}


def read(action, args, run, operators=(), journal=None):
    """The endpoint read of `items` or `questions`: one `bd export --all` (raw stdout)."""
    if any(token in ('--help', '-h') for token in args) or args[:1] == ['help']:
        return help_payload(action)
    rows, unreadable = read_anchor_rows(run)
    reader = read_items if action == 'items' else read_questions
    return reader(args, rows, unreadable, operators, journal)


def help_payload(action):
    return {'schema_version': 1, 'action': action, 'contract': 'cli-contract-v1',
            'usage': [ITEMS_USAGE if action == 'items' else QUESTIONS_USAGE],
            'read_only': True,
            'trust': 'attested or unattested (design 9.2); an answer closes a question only when it is attested '
                     'and has its .owner-answers entry',
            'limits': {'limit': '1..%d' % LIST_LIMIT_MAX, 'item_anchors': ITEM_ANCHORS_MAX,
                       'records_per_anchor': RECORDS_PER_ANCHOR_MAX, 'due_soon_days': DUE_SOON_DAYS},
            'coverage': COVERAGE}


# -- brief attention (design 7.4) ----------------------------------------------------------

def brief_attention(rows, task_row, operators=(), journal=None, current=None, limit=BRIEF_MAX):
    """The `open-item` and `owner-question` brief kinds for one task: items whose `task`
    is the briefed task and whose state is open or blocked (questions included), and
    the questions among them. No bd read: `rows` is the export brief already holds. Each
    kind contributes at most `limit` items; the totals count every one."""
    task = (task_row or {}).get('id')
    if not isinstance(task, str):
        return {'attention': [], 'attention_total': 0, 'attention_more': None}
    # An item is about TASK only if one of its canonical records holds `"task":"TASK"`;
    # anchors that hold no such text cannot be about it and are not parsed at all. This
    # keeps brief's cost on an unrelated task to one substring scan per record comment.
    needle = '"task":' + json.dumps(task, ensure_ascii=False)
    rows = [row for row in rows if isinstance(row, dict) and FAMILY_LABEL in (row.get('labels') or [])
            and any(isinstance(c, dict) and isinstance(c.get('text'), str) and needle in c['text']
                    for c in row.get('comments') or [])]
    views, cut = ledger(rows, operators, journal, current)
    live = [v for v in views if v['state'] in ('open', 'blocked') and v['task'] == task]
    order = {'expired': 0, 'due-soon': 1, 'ok': 2, 'unset': 3}
    live.sort(key=lambda v: (order[v['due']], v['id']))
    questions = [v for v in live if v['kind'] == 'question']
    items = [{'kind': 'open-item', 'id': v['id'], 'state': v['state'], 'trust': v['trust'],
              'text': v['text'][:200], 'source': 'items get ' + v['id']} for v in live[:limit]]
    items += [{'kind': 'owner-question', 'id': v['id'], 'due': v['due'], 'for': v['for'],
               'text': v['text'][:200], 'source': 'questions get ' + v['id']} for v in questions[:limit]]
    total = len(live) + len(questions)
    result = {'attention': items, 'attention_total': total, 'attention_more': (total - len(items)) or None}
    if cut['cut']:
        # A cap stopped the read (review P2 b): say so, so the brief is never silently partial.
        result['open_items_cut'] = {'anchors': cut['anchors'], 'anchors_read': cut['anchors_read'],
                                    'records_cut': cut['records_cut']}
    return result
