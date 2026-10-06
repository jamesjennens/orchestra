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
