"""The per-project claiming setting (kittrial-5bb.352).

By default a project is **open**: any member who may write tasks can claim an open,
unassigned task.  A project may instead be set to **assigned**: only its owners and
coordinators (``CAP_COORDINATE``, which includes an agent holding the coordinator
grant) hand work out; unassigned tasks are never offered as claimable and a claim of
one is refused, while a task already assigned to the caller may still be started by
that assignee.

The setting lives in ``<project>/.claiming.json`` rather than in the tracker.  It is
therefore read FRESH on every ``work``/``wait``/``ready``/``next`` read: the tracker
read cache is keyed on the dolt commit, which a setting change does not move, so a
cached read would otherwise hide the change (kittrial-5bb.339/.346).

An absent file means **open**, today's behaviour.  A file that cannot be read as a
valid record means **assigned**: a damaged setting must fail closed towards "only the
coordinator assigns", never silently open claiming again.  The read reports the
damage in a warning rather than raising, so a listing still answers.

The record keeps who set the value, when, and by which route (``web`` or ``host``),
plus a bounded history, so an operator can see how the project got here.  The write
is atomic and is carried by the coordination backup like the other per-project files.
"""
import datetime
import json
import os
from pathlib import Path

FILENAME = '.claiming.json'
SCHEMA_VERSION = 1
VALUES = ('open', 'assigned')
DEFAULT = 'open'
#: The value a damaged record reads as: restrictive, never permissive.
DAMAGED = 'assigned'
HISTORY_LIMIT = 20
MAX_SET_BY = 200

#: The one sentence a refused claim carries, on every route.  It names the setting so
#: a reader knows why the ordinary claim was refused and who can change the answer.
CLAIM_REFUSAL = ("This project's coordinators assign work here (the claiming setting is 'assigned'): "
                 "only a coordinator assigns a task before it is claimed.")
#: The sentence work/next/wait answer with when the listing is empty under `assigned`.
EMPTY_SENTENCE = "Only the project's coordinators assign work here; nothing is assigned to you yet."


def path(project_dir):
    """The setting file's path inside a project directory."""
    return Path(project_dir) / FILENAME


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _clean(value, limit=MAX_SET_BY):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _entry(record):
    """The bounded public shape of one setting record (never an arbitrary payload)."""
    return {
        'claiming': record.get('claiming'),
        'set_by': _clean(record.get('set_by')),
        'set_at': _clean(record.get('set_at')),
        'set_via': _clean(record.get('set_via')),
    }


def _history(value):
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        entry = _entry(item)
        if entry['claiming'] in VALUES:
            out.append(entry)
    return out[-HISTORY_LIMIT:]


def validate(record):
    """Refuse a record this kit could not have written.

    The whole record is checked before a write, and a backup carrying one is refused
    rather than restored as a setting nobody can trust.
    """
    if not isinstance(record, dict):
        raise ValueError('Invalid claiming record')
    if record.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Invalid claiming record schema')
    if record.get('claiming') not in VALUES:
        raise ValueError('Invalid claiming value')
    if not _clean(record.get('set_by')):
        raise ValueError('Invalid claiming set_by')
    if not _clean(record.get('set_at')):
        raise ValueError('Invalid claiming set_at')
    if not _clean(record.get('set_via')):
        raise ValueError('Invalid claiming set_via')
    if not isinstance(record.get('history', []), list):
        raise ValueError('Invalid claiming history')
    for item in record.get('history') or []:
        if not isinstance(item, dict) or item.get('claiming') not in VALUES:
            raise ValueError('Invalid claiming history entry')
    return record


def read(project_dir):
    """The setting as a public block; an absent file is open, a damaged one assigned.

    ``warning`` is present only when the file exists and could not be read as this
    kit's record.  ``source`` says whether the answer came from the file (``record``),
    its absence (``default``) or a damaged file (``damaged``).
    """
    target = path(project_dir)
    from admin import read_regular_text
    try:
        text = read_regular_text(target)
    except FileNotFoundError:
        return {'claiming': DEFAULT, 'set_by': None, 'set_at': None, 'set_via': None,
                'previous': None, 'history': [], 'source': 'default'}
    except (OSError, UnicodeError, ValueError):
        return {'claiming': DAMAGED, 'set_by': None, 'set_at': None, 'set_via': None,
                'previous': None, 'history': [], 'source': 'damaged',
                'warning': ("The claiming setting in %s could not be read, so the restrictive value "
                            "'assigned' is used." % FILENAME)}
    try:
        record = json.loads(text)
        validate(record)
    except (ValueError, TypeError):
        return {'claiming': DAMAGED, 'set_by': None, 'set_at': None, 'set_via': None,
                'previous': None, 'history': [], 'source': 'damaged',
                'warning': ("The claiming setting in %s is not a valid record, so the restrictive value "
                            "'assigned' is used; set it again to repair it." % FILENAME)}
    result = dict(_entry(record))
    result['previous'] = record.get('previous') if isinstance(record.get('previous'), dict) else None
    result['history'] = _history(record.get('history'))
    result['source'] = 'record'
    return result


def mode(project_dir):
    """The one value the listings and the claim gates compare: ``open`` or ``assigned``."""
    return read(project_dir)['claiming']


def is_restrictive(project_dir):
    return mode(project_dir) == 'assigned'


def write(project_dir, value, actor, via='host', at=None):
    """Record ``value``, recording who set it, when, and by which route.

    The caller holds the project's coordination lock for the write; the file itself is
    written atomically.  A previous valid record moves to ``previous`` and joins a
    bounded history, so a setting change is not an untraceable edit.
    """
    if value not in VALUES:
        raise ValueError("The claiming setting must be 'open' or 'assigned'")
    set_by = _clean(actor)
    if not set_by:
        raise ValueError('Setting the claiming value needs the actor who sets it')
    set_via = _clean(via) or 'host'
    set_at = _clean(at) or _now()
    current = read(project_dir)
    history = list(current.get('history') or [])
    previous = None
    if current.get('source') == 'record':
        previous = {key: current.get(key) for key in ('claiming', 'set_by', 'set_at', 'set_via')}
        history.append(previous)
    record = {'schema_version': SCHEMA_VERSION, 'claiming': value, 'set_by': set_by,
              'set_at': set_at, 'set_via': set_via, 'previous': previous,
              'history': _history(history)}
    from coordination import atomic
    atomic(path(project_dir), record)
    result = dict(_entry(record))
    result['previous'] = previous
    result['history'] = _history(record['history'])
    result['source'] = 'record'
    return result


def backup_record(project_dir):
    """The record a coordination backup carries, or None when there is no file.

    A damaged file is still carried, byte for byte, so a restore cannot silently turn
    a damaged restrictive setting into the permissive default.
    """
    target = path(project_dir)
    from admin import read_regular_text
    try:
        raw = read_regular_text(target, raw=True)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError('The claiming setting file cannot be read: %s' % error) from None
    try:
        text = raw.decode('utf-8')
        record = json.loads(text)
        validate(record)
    except (ValueError, TypeError, UnicodeError):
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            import base64
            return {'base64': base64.b64encode(raw).decode('ascii')}
        return {'text': text}
    return record


#: Keys a backed-up damaged file may carry instead of a validated record: its text, or
#: its bytes base64-encoded when the text was not UTF-8. Either restores byte for byte.
DAMAGED_KEYS = ({'text'}, {'base64'})


def restore_record(value):
    """Validate one backup entry before it is written; refuse anything this kit cannot read."""
    if isinstance(value, dict) and set(value) in DAMAGED_KEYS:
        if set(value) == {'text'} and not isinstance(value['text'], str):
            raise ValueError('Invalid claiming backup text')
        if set(value) == {'base64'}:
            import base64
            if not isinstance(value['base64'], str):
                raise ValueError('Invalid claiming backup bytes')
            try:
                base64.b64decode(value['base64'], validate=True)
            except (ValueError, TypeError):
                raise ValueError('Invalid claiming backup bytes') from None
        return value
    return validate(value)
