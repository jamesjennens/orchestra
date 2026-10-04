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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from coordination import atomic

GUIDANCE_NAME = 'GUIDANCE.md'
META_NAME = '.guidance.json'
CLEAR_NAME = '.guidance-clear.json'
# The audit of a same-text repair (kittrial-5bb.121 review): who repaired the record and
# when, beside the record rather than in it. A kit before this one validates the record
# strictly and refused ack, compaction, backup and restore on unknown fields there; it
# never parses this file, and the backup does not carry it (the sidecar names its files).
REPAIR_NAME = '.guidance-repair.json'
REPAIR_FIELDS = {'schema_version', 'version', 'set_by', 'set_at', 'repaired_by', 'repaired_at'}
# The file is local and unauthenticated (anyone who can write the project directory can
# plant one), so it is read with a bound and shown only when it names the current
# generation AND a configured operator as the repairer (kittrial-5bb.124).
REPAIR_LIMIT = 4096
CLEAR_INVALID_NAME = '.guidance-clear.json.invalid'
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
# More characters that render as nothing, or as a blank, while changing the bytes
# (kittrial-5bb.105): they let two texts look the same with different versions, defeat a
# keyword match, or carry data nobody sees. Variation selectors (U+FE00-FE0F and the
# supplement U+E0100-E01EF, a known smuggling vector) and the Mongolian free variation
# selectors; the Hangul fillers; the Arabic letter mark; the combining grapheme joiner;
# the Mongolian vowel separator; the Khmer invisible vowels; the braille blank; the
# interlinear annotation and object replacement characters. Refused when read as well
# as when set, like the ones above, so a hand-edited file cannot carry them either.
HIDDEN = re.compile('[\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2800\u3164\ufe00-\ufe0f'
                    '\uffa0\ufff9-\ufffc\U00016FE4\U000E0100-\U000E01EF]')
# Beyond the two lists: EVERY format character (Unicode category Cf) is refused, so the
# rule does not depend on a list that keeps growing (review of f2d6050: the musical and
# shorthand format controls U+1D173 and U+1BCA0 and the Egyptian ones at U+13430 were
# still accepted). Two exceptions. ZWNJ and ZWJ have the joiner rule below. And the
# prepended concatenation marks are Cf but VISIBLE (the Arabic number signs and end of
# ayah, the Syriac abbreviation mark, the Kaithi number signs), so they are allowed -
# but only directly BEFORE a letter or digit of their own script (kittrial-5bb.121): all
# thirteen are prepended marks (Unicode Prepended_Concatenation_Mark; the Unicode Standard,
# chapters 9.2 Arabic, 9.3 Syriac and 15.2 Kaithi), written in front of the number or word
# they span. A terminal often draws them with zero width, so one inside a Latin word split
# a keyword without showing, and so did one followed by anything that is not itself a
# visible base of that script - a combining mark, tatweel, a presentation form or an
# unassigned code point ("ig" + U+0600 + U+064E + "nore" no longer passes).
# The lists above remain for what is not Cf: variation selectors and the Khitan filler
# (marks), the Hangul fillers (letters), the braille blank and U+FFFC (symbols).
#
# Python 3.10 to 3.13 carry different Unicode tables (13.0 to 15.1), so neither rule may
# depend on them where they differ. The only characters whose category differs between
# those versions are U+0890, U+0891 (Arabic pound and piastre marks above, Cf from 14.0)
# and U+13439-U+1343F (Egyptian hieroglyph format controls, Cf from 15.0); in 3.10 they
# are unassigned. So the allowed marks are this explicit table, and the late Egyptian
# controls are refused by name in LATE_FORMAT. The character after a mark must be L* or N*
# and in one of the ranges listed for its script; those ranges leave out every code point
# whose L*/N* status changed between Unicode 13.0 and 18.0 (U+0870-U+089F and U+08B5,
# U+08C8-U+08FF, all assigned or reassigned from 14.0), so every supported Python gives
# the same answer. Presentation forms (U+FB50-U+FDFF, U+FE70-U+FEFF) are compatibility
# characters, not text a number sign is written before, and tatweel (U+0640) only
# stretches a joining letter, so neither counts.
_ARABIC_FOLLOWING = ((0x0600, 0x063F), (0x0641, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08B4), (0x08B6, 0x08C7),
                     (0x10E60, 0x10E7F), (0x1EC70, 0x1ECBF), (0x1ED00, 0x1ED4F), (0x1EE00, 0x1EEFF))
_SYRIAC_FOLLOWING = ((0x0700, 0x074F), (0x0860, 0x086F))
# Kaithi has no digits of its own: Unicode's ScriptExtensions.txt (UCD 14.0 to 18.0) lists
# the Devanagari digits U+0966-U+096F with scx {Deva Dogr Kthi Mahj}, and the Unicode
# Standard's Kaithi section (15.2) says Kaithi uses them, so the Kaithi number signs
# U+110BD and U+110CD are written before Devanagari digits in real text.
_KAITHI_FOLLOWING = ((0x11080, 0x110CF), (0x0966, 0x096F))
VISIBLE_FORMAT = {character: _ARABIC_FOLLOWING for character in '\u0600\u0601\u0602\u0603\u0604\u0605\u06dd'
                  '\u0890\u0891\u08e2'}
VISIBLE_FORMAT.update({'\u070f': _SYRIAC_FOLLOWING, '\U000110BD': _KAITHI_FOLLOWING,
                       '\U000110CD': _KAITHI_FOLLOWING})
LATE_FORMAT = frozenset(chr(code) for code in range(0x13439, 0x13440))


def _starts_script(character, ranges):
    """A visible base that a prepended mark of this script may stand before: a letter or
    digit (category L* or N*) in one of the script's ranges."""
    if not character:
        return False
    code = ord(character)
    return (any(low <= code <= high for low, high in ranges)
            and unicodedata.category(character)[0] in 'LN')


def _format_character(text):
    """The first refused format character (category Cf) in ``text``, or None."""
    if text.isascii():
        return None
    for index, character in enumerate(text):
        if character in '\u200c\u200d':
            continue
        ranges = VISIBLE_FORMAT.get(character)
        if ranges is not None:
            if _starts_script(text[index + 1:index + 2], ranges):
                continue
            return character
        if character in LATE_FORMAT or unicodedata.category(character) == 'Cf':
            return character
    return None
# ZWNJ (U+200C) and ZWJ (U+200D) are part of ordinary spelling in some scripts and
# invisible text everywhere else. They are allowed only between two characters that are
# letters or combining marks of the SAME joiner-using script (kittrial-5bb.105). That
# admits Persian and Urdu, and the commonest Indic use, after a virama (a combining
# mark, which the earlier "between letters" rule refused); it refuses a joiner inside
# an English word, which defeated a keyword match and gave two texts that look the same
# with different versions. An emoji ZWJ sequence is refused: guidance is instructions,
# and such a sequence needs a variation selector, which is refused above.
JOINER = re.compile('[\u200c\u200d]')
JOINER_SCRIPTS = frozenset(('ARABIC', 'SYRIAC', 'MONGOLIAN', 'NKO', 'DEVANAGARI', 'BENGALI', 'GURMUKHI', 'GUJARATI',
                            'ORIYA', 'TAMIL', 'TELUGU', 'KANNADA', 'MALAYALAM', 'SINHALA'))
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


def _joiner_script(character):
    """The joiner-using script a letter or combining mark belongs to, else None."""
    if not character or unicodedata.category(character)[0] not in 'LM':
        return None
    name = unicodedata.name(character, '')
    script = name.split(' ', 1)[0] if name else ''
    return script if script in JOINER_SCRIPTS else None


def validate_text(text):
    """A bounded, nonempty, plain-text guidance payload."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Guidance must be nonempty UTF-8 text')
    if CONTROL.search(text):
        raise ValueError('Guidance must be plain text (no control characters)')
    if INVISIBLE.search(text):
        raise ValueError('Guidance must be plain text (no bidi, zero-width, C1 or tag characters)')
    hidden = HIDDEN.search(text)
    if hidden:
        raise ValueError('Guidance must be plain text (no invisible or blank-looking character: U+%04X, a '
                         'variation selector, filler, letter mark or similar)' % ord(hidden.group()))
    formatting = _format_character(text)
    if formatting in VISIBLE_FORMAT:
        raise ValueError('Guidance must be plain text (the format character U+%04X is only allowed directly before a '
                         'letter or digit of its own script, such as an Arabic number sign before an Arabic digit)'
                         % ord(formatting))
    if formatting:
        raise ValueError('Guidance must be plain text (no invisible or blank-looking character: U+%04X, a '
                         'format character)' % ord(formatting))
    for match in JOINER.finditer(text):
        before = _joiner_script(text[match.start() - 1] if match.start() else '')
        after = _joiner_script(text[match.end()] if match.end() < len(text) else '')
        if before is None or before != after:
            raise ValueError('Guidance must be plain text (a zero-width joiner or non-joiner is only allowed '
                             'between letters of one script that uses it, such as Arabic or Devanagari)')
    if len(text.encode('utf-8')) > LIMIT:
        raise ValueError('Guidance exceeds %d bytes; shorten it or keep the long material in the '
                         'project onboarding entry point' % LIMIT)
    return text


def _readable_file(target):
    """True for a regular file whose bytes can be read (so only its CONTENT is at fault)."""
    try:
        if not target.is_file():
            return False
        with target.open('rb') as handle:
            handle.read(1)
        return True
    except OSError:
        return False


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


def _storable_text(text):
    """Shape of a stored previous text: nonempty bounded text with no control character."""
    return (isinstance(text, str) and bool(text.strip()) and not CONTROL.search(text)
            and len(text.encode('utf-8', 'replace')) <= LIMIT)


def _deliverable_text(text):
    """A stored text only if it passes TODAY's plain-text rule, else None (withheld)."""
    try:
        return validate_text(text) if text is not None else None
    except ValueError:
        return None


def _strictly_valid(meta):
    try:
        validate_meta(meta)
        return True
    except ValueError:
        return False


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
    # The previous text is HISTORY: it is never delivered as guidance, and it was valid
    # under the rules of the kit that stored it. Checking it against today's character
    # rules made a healthy record fail after an upgrade that refuses a character it
    # holds, so nobody could acknowledge, compact or back up (review of f2d6050). It is
    # checked for shape only; a reader withholds it if it fails today's rules.
    if meta.get('previous_text') is not None and not _storable_text(meta['previous_text']):
        raise ValueError('Invalid guidance record: previous_text must be bounded text without control characters')
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


def read_repair(path):
    """The same-text repair audit file, or None when absent, a symlink, larger than
    ``REPAIR_LIMIT`` bytes or not one this kit wrote."""
    target = Path(path) / REPAIR_NAME
    if target.is_symlink() or not target.is_file():
        return None
    try:
        with target.open('rb') as handle:
            raw = handle.read(REPAIR_LIMIT + 1)
        if len(raw) > REPAIR_LIMIT:
            return None
        value = record_json.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if (not isinstance(value, dict) or set(value) != REPAIR_FIELDS or value.get('schema_version') != 1
            or not isinstance(value.get('version'), str) or not VERSION.fullmatch(value['version'])
            or not _valid_actor(value.get('set_by')) or not _valid_stamp(value.get('set_at'))
            or not _valid_actor(value.get('repaired_by')) or not _valid_stamp(value.get('repaired_at'))):
        return None
    return value


def deployment_operators(path):
    """The configured operators of the deployment a project directory belongs to
    (``ROOT/projects/NAME``), or an empty set when it cannot be told: the repair audit
    is then not shown, never shown unchecked."""
    project = Path(path)
    if project.parent.name != 'projects':
        return frozenset()
    try:
        from admin import operators
        return frozenset(operators(project.parent.parent))
    except (OSError, ValueError, RecursionError):
        return frozenset()


def _repair_audit(path, meta, bound, operators=None):
    """``repaired_by``/``repaired_at`` when a same-text repair kept the original setter of
    this exact, bound version and recorded itself beside the record (kittrial-5bb.121). The
    file names the generation it repaired (version, setter, time); any later set, including
    a same-text set by an older kit that never reads this file, changes one of them. The
    file is local and unauthenticated, so it is shown only when the repairer it names is a
    configured operator (``operators``, or the deployment's when not given)."""
    repair = read_repair(path) if bound and isinstance(meta, dict) else None
    if repair is None or any(repair[key] != meta.get(key) for key in ('version', 'set_by', 'set_at')):
        return {}
    if operators is None:
        operators = deployment_operators(path)
    if repair['repaired_by'] not in operators:
        return {}
    return {'repaired_by': repair['repaired_by'], 'repaired_at': repair['repaired_at']}


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
    """The unbound result: the text exists but is withheld until the operator repairs it.

    `present` is true (a text file is there) and `version` is null (nobody may follow or
    acknowledge it), in this read and in `state`, so `get`, `version`, `brief`, `work`
    and resume say the same thing (kittrial-5bb.105)."""
    return {'schema_version': 1, 'present': True, 'version': None, 'set_at': None, 'set_by': None,
            'text': None, 'changed': False, 'since': since, 'since_known': since is None,
            'previous_version': None, 'previous_text': None, 'acknowledged': None,
            'meta_version': meta.get('version') if isinstance(meta, dict) else None,
            'attention': True, 'unreadable': False, 'unbound': True,
            'warning': warning + '. ' + REPAIR_NEXT_ACTION, 'next_action': REPAIR_NEXT_ACTION}


def state(path, actor=None, operators=None):
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
    # The hash of unbound text is not a version: nobody may follow or acknowledge it, so
    # no reader hands it out (`meta_version` still says what the record names).
    result = {'schema_version': 1, 'present': text is not None, 'version': current if bound else None,
              'set_at': set_at, 'set_by': set_by,
              'previous_version': (meta.get('previous_version') if bound else None),
              'meta_version': meta.get('version') if isinstance(meta, dict) else None,
              'acknowledged': acknowledged if current is not None else None,
              'acknowledged_version': entry.get('version') if entry else None,
              'acknowledged_at': entry.get('acknowledged_at') if entry else None,
              'unbound': bool(current is not None and not bound),
              'attention': bool(current is not None and not acknowledged)}
    result.update(_repair_audit(path, meta, bound, operators))
    if warning:
        result['warning'] = warning
    if result['attention']:
        result['next_action'] = REPAIR_NEXT_ACTION if result['unbound'] else 'guidance get'
    return result


def read(path, args, actor=None, operators=None):
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
              'set_at': set_at, 'set_by': set_by, **_repair_audit(path, meta, True, operators), 'text': text, 'changed': changed,
              'since': since, 'since_known': since_known,
              'meta_version': meta.get('version') if isinstance(meta, dict) else None,
              'previous_version': previous,
              # "What changed since a version": the immediately previous text is kept
              # beside its version, so a caller that names it gets the exact prior text.
              'previous_text': (_deliverable_text(meta.get('previous_text'))
                                if bound and since == previous else None),
              'acknowledged': bool(entry) and entry.get('version') == current,
              'attention': not bool(entry) or entry.get('version') != current,
              'unreadable': False, 'unbound': False}
    if bound and since == previous and meta.get('previous_text') is not None and result['previous_text'] is None:
        # The stored previous text holds a character this kit refuses: withheld, as an
        # unreadable current text is (review of f2d6050).
        result['previous_text_withheld'] = True
        result['warning'] = ('The previous guidance text holds a character this kit refuses, so it is withheld; '
                             'the current text above is complete.')
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

    A set also replaces a file on disk that cannot be read as guidance (a refused
    character, over the limit, not UTF-8): ``replaced_unreadable: true``. That file is
    a generation nobody set, so nothing of it is copied into the record: no history
    entry, no previous version, no previous text. Before kittrial-5bb.105 the set was
    refused with the OLD file's error, which read as if the new text were wrong, and
    the only repair was a clear, which drops the acknowledgements and the history.
    """
    validate_text(text)
    validate_actor(actor)
    target, meta_path = _paths(path)
    replaced_unreadable = False
    try:
        old_text = read_text(path)
    except ValueError:
        # Only a regular file whose CONTENT is refused is replaced; a directory in its
        # place or a file that cannot be opened (permissions) is still the operator's
        # to look at.
        if not _readable_file(target):
            raise
        old_text, replaced_unreadable = None, True
    old_meta = read_meta(path)
    current = version_of(text)
    previous = version_of(old_text) if old_text is not None else None
    old_bound = old_text is not None and isinstance(old_meta, dict) and old_meta.get('version') == previous
    old_attributed = old_bound and _valid_actor(old_meta.get('set_by')) and _valid_stamp(old_meta.get('set_at'))
    needs_repair = replaced_unreadable or (old_text is not None and not old_attributed)
    # A record that binds the text but fails the strict check (the check acknowledge,
    # compact and backup apply) is rewritten by a same-text set, instead of "unchanged".
    rewrite = previous == current and old_attributed and not _strictly_valid(old_meta)
    needs_repair = needs_repair or rewrite
    if previous == current and old_attributed and not rewrite:
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
        if rewrite:
            # The record already names this text: keep its own previous generation where
            # those two fields are well formed.
            kept = old_meta.get('previous_version')
            previous_version = kept if isinstance(kept, str) and VERSION.fullmatch(kept) else None
            previous_text = (old_meta.get('previous_text')
                             if previous_version and _storable_text(old_meta.get('previous_text')) else None)
    else:
        previous_version = previous
        previous_text = old_text
    set_by, set_at = actor, stamp
    if rewrite:
        # The record already credited this exact text to an operator: the repair keeps
        # that setter and records itself beside it (kittrial-5bb.121). Before, the
        # repairing operator replaced the setter and the original was kept nowhere.
        set_by, set_at = old_meta['set_by'], old_meta['set_at']
    meta = {'schema_version': 1, 'version': current, 'set_by': set_by, 'set_at': set_at,
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
    repair_path = Path(path) / REPAIR_NAME
    if rewrite and (repair_path.is_symlink() or (repair_path.exists() and not repair_path.is_file())):
        # Checked before anything is written (kittrial-5bb.124): the repair and its audit
        # are one operation, so a path the audit cannot be written to refuses both.
        raise ValueError('Nothing was changed: %s in the project directory is a symlink or not a regular file, so '
                         'the repair audit could not be written; remove it and set the guidance again' % REPAIR_NAME)
    # The text is written first: a crash before the metadata replace leaves the new
    # version already authoritative (readers derive it from the text) with stale
    # audit fields, which a set with the same text repairs (see above).
    write_text(target, text)
    atomic(meta_path, meta)
    audit_warning = None
    if rewrite:
        try:
            atomic(repair_path, {'schema_version': 1, 'version': current, 'set_by': set_by, 'set_at': set_at,
                                 'repaired_by': actor, 'repaired_at': stamp})
        except OSError:
            # The record is already repaired; say so in one sentence rather than fail.
            audit_warning = ('The guidance record was repaired, but the repair audit %s could not be written, so '
                             'readers show the original setter without the repair' % REPAIR_NAME)
    elif repair_path.exists() and not repair_path.is_symlink():
        # A new generation, or a repair of an unbound record that names its own setter:
        # an earlier repair audit no longer describes the record.
        try:
            repair_path.unlink()
        except OSError:
            pass   # readers ignore an audit whose version is not the record's
    result = {'version': current, 'set_by': set_by, 'set_at': set_at,
              'previous_version': previous_version,
              'changed': previous != current, 'repaired': needs_repair}
    if rewrite:
        result['repaired_by'], result['repaired_at'] = actor, stamp
    if audit_warning:
        result['warning'] = audit_warning
    if replaced_unreadable:
        result['replaced_unreadable'] = True
    return result


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


def clear_record_state(path):
    """(state, record): 'absent', 'ok' with the validated record, or 'invalid'.

    'invalid' is a file at the record's path that is a symlink, is not a regular file,
    cannot be read or does not validate. It is reported, never silently treated as "no
    clears" (kittrial-5bb.105).
    """
    target = Path(path) / CLEAR_NAME
    if not (target.exists() or target.is_symlink()):
        return 'absent', None
    if target.is_symlink() or not target.is_file():
        return 'invalid', None
    try:
        value = record_json.loads(target.read_text(encoding='utf-8'))
        return 'ok', validate_clear_record(value)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return 'invalid', None


def read_clear_record(path):
    """The local clear audit record, or None when it is absent or invalid."""
    return clear_record_state(path)[1]


def clears_view(path):
    """What `guidance-status` shows about clears: who, when and which version; never text."""
    state, record = clear_record_state(path)
    try:
        aside = sorted(entry.name for entry in Path(path).iterdir()
                       if entry.name.startswith(CLEAR_INVALID_NAME + '.'))
    except OSError:
        aside = []
    view = {'clear_record': state, 'clears': list(record['clears']) if record else [],
            'clear_record_kept_aside': bool(aside),
            # The invalid records kept so far, newest last; at most the last ten are named.
            'clear_records_kept_aside': aside[-CLEAR_LIMIT:], 'clear_records_kept_aside_total': len(aside)}
    if state == 'invalid':
        view['clear_warning'] = ('The clear record %s is not a valid record this kit wrote, so who cleared the '
                                 'guidance cannot be read from it. It is kept as it is; the next clear moves it to '
                                 '%s.<UTC time> and starts a new record.' % (CLEAR_NAME, CLEAR_INVALID_NAME))
    return view


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
    for target in (text, meta, meta.with_suffix('.tmp'), Path(path) / REPAIR_NAME):
        if target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                raise ValueError('The guidance file could not be removed; ask the operator to check permissions')
            removed.append(target.name)
    stamp = datetime.now(timezone.utc).isoformat()
    state, previous = clear_record_state(path)
    kept_aside = None
    if state == 'invalid':
        # A record this kit did not write (a hand edit, a planted file, a symlink) used
        # to be replaced without a word. Keep it beside the new record and say so.
        # Dated, and numbered within a second, so a second invalid record never
        # overwrites the first one kept (review of f2d6050).
        moment = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        aside = Path(path) / ('%s.%s' % (CLEAR_INVALID_NAME, moment))
        number = 1
        while aside.exists() or aside.is_symlink():
            number += 1
            aside = Path(path) / ('%s.%s-%d' % (CLEAR_INVALID_NAME, moment, number))
        try:
            os.replace(Path(path) / CLEAR_NAME, aside)
            kept_aside = aside.name
        except OSError:
            raise ValueError('The existing guidance clear record is not valid and could not be moved aside; ask '
                             'the operator to check %s' % CLEAR_NAME) from None
    clears = ([{'cleared_by': actor, 'cleared_at': stamp, 'cleared_version': cleared_version}]
              + (previous['clears'] if previous else []))[:CLEAR_LIMIT]
    record = {'schema_version': 1, 'clears': clears}
    validate_clear_record(record)
    atomic(Path(path) / CLEAR_NAME, record)
    result = {'removed': sorted(set(removed)), 'cleared_by': actor, 'cleared_at': stamp,
              'cleared_version': cleared_version, 'clear_record': CLEAR_NAME}
    if kept_aside:
        result['invalid_record_kept_as'] = kept_aside
    return result


UNREADABLE_REPAIR = ('One `admin.py set-guidance PROJECT --actor OPERATOR --file FILE` with clean text replaces the '
                     'file and keeps the audit record, the history and the acknowledgement rules; nothing of the '
                     'unreadable file is copied.')


def _unreadable_status(path, reason, host):
    """The status of a project whose guidance file cannot be read as guidance.

    What can be seen without the text: why it cannot be read (the reason names the
    character or the limit), the audit record's own version, setter and time (the last
    generation an operator set), and the acknowledgement table. Never the unreadable
    text, on either form.
    """
    meta = read_meta(path)
    recorded = meta.get('version') if isinstance(meta, dict) else None
    rows = []
    table = meta.get('acknowledged') if isinstance(meta, dict) else None
    if isinstance(table, dict):
        for name, entry in sorted(table.items()):
            if not _valid_actor(name) or not isinstance(entry, dict) or set(entry) != {'version', 'acknowledged_at'}:
                continue
            if not isinstance(entry.get('version'), str) or not VERSION.fullmatch(entry['version']):
                continue
            rows.append({'actor': name, 'version': entry['version'],
                         'acknowledged_at': entry.get('acknowledged_at'), 'current': False})
    record = None
    if isinstance(meta, dict):
        record = {'version': recorded,
                  'set_by': meta.get('set_by') if _valid_actor(meta.get('set_by')) else None,
                  'set_at': meta.get('set_at') if _valid_stamp(meta.get('set_at')) else None}
    result = {'schema_version': 1, 'present': None, 'unreadable': True, 'unreadable_reason': reason,
              'repair': UNREADABLE_REPAIR, 'version': None, 'set_by': None, 'set_at': None,
              'meta_version': recorded, 'audit_record': record, 'previous_version': None,
              'acknowledged': rows, 'acknowledged_total': len(rows),
              # Nobody is up to date with a file nobody can read. `behind` are the
              # actors whose acknowledgement is the version the audit record names.
              'up_to_date': [],
              'behind': sorted(row['actor'] for row in rows if row['version'] == recorded),
              'stale': sorted(row['actor'] for row in rows if row['version'] != recorded),
              'text_unbound': False, 'compact_hint': None,
              'warning': 'The guidance file cannot be read as guidance: %s. %s' % (reason.rstrip('.'), UNREADABLE_REPAIR)}
    if host:
        result['text'] = None
        result['previous_text'] = None
        result['history'] = _history_entries(meta)
    result.update(clears_view(path))
    return result


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
    target, _ = _paths(path)
    try:
        text = read_text(path)
    except ValueError as error:
        # The operator runs this to find out what is wrong, so it answers with what it
        # can see instead of failing with the file's error (kittrial-5bb.105). A
        # directory in the file's place or a permission fault still raises.
        if not _readable_file(target):
            raise
        return _unreadable_status(path, str(error), host)
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
              'set_by': set_by, 'set_at': set_at, **_repair_audit(path, meta, bound, operators),
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
        result['previous_text'] = _deliverable_text(meta.get('previous_text')) if bound else None
        if bound and meta.get('previous_text') is not None and result['previous_text'] is None:
            result['previous_text_withheld'] = True
        result['history'] = meta.get('history') if bound else []
    if 'acks_compacted_by' in (meta or {}):
        result['acks_compacted_by'] = meta.get('acks_compacted_by')
        result['acks_compacted_at'] = meta.get('acks_compacted_at')
    # Who cleared the guidance, when and which version: it was written to a local file
    # and shown nowhere (kittrial-5bb.105). Names, times and hashes only, on both forms.
    result.update(clears_view(path))
    if warning:
        result['warning'] = warning
    return result


def brief_block(path, actor=None, operators=None):
    """The compact version block for brief/work/resume; never raises on old projects."""
    if path is None:
        return None
    try:
        return state(path, actor, operators)
    except (OSError, ValueError, RecursionError):
        # Guidance is an instruction channel: if it cannot be read reliably, say so
        # (and keep attention true) instead of silently reporting "no guidance".
        return _unreadable('Guidance could not be read.')
