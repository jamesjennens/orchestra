"""Reserve native lifecycle facts on the contributor raw-bd write path.

This is a write boundary, independent of configurable policy rules and of the
legacy reader trust decision. Dedicated lifecycle and operator paths bypass it.
"""
import csv
import re

from lifecycle import DIMENSIONS, ENABLED, LIVE
from reserved_comments import _bd_scan, _parse_go_bool, write_targets

NAMES = frozenset((*DIMENSIONS, 'lifecycle-scope', ENABLED, LIVE))
ADVICE = 'Use the dedicated lifecycle action (lifecycle.py record); no native write was attempted.'
_DESCRIPTION = re.compile(r'(?:Set |Changed )([A-Za-z0-9-]+)(?: from [^\n]+)? to ([^\n]+)(?:\n\nReason: ([\s\S]*))?')
_TITLE = re.compile(r'State change: ([A-Za-z0-9-]+) → (.*)')
_ALIASES = {'-t': '--type', '-d': '--description', '-l': '--labels', '--label': '--labels'}
_LABEL_FLAGS = {'--labels', '--add-label', '--set-labels', '--remove-label'}
_STRUCTURAL = {'--type', '--title', '--description', '--body-file', '--parent'}


def refuse(reason):
    raise ValueError('Reserved lifecycle namespace: '+reason+'. '+ADVICE)


def reserved_label(label):
    return isinstance(label, str) and label.partition(':')[0] in NAMES and ':' in label


def labels(value):
    # pflag StringSlice uses CSV, including quoted fields and repeated flags.
    if not isinstance(value, str):
        refuse('an incomplete label value cannot be checked')
    try:
        parsed = list(csv.reader([value], strict=True))
    except (csv.Error, TypeError):
        refuse('an ambiguous label value cannot be checked')
    if len(parsed) != 1:
        refuse('an ambiguous label value cannot be checked')
    return [item.strip() for item in parsed[0]]


def event_shape(row):
    """Reserve incomplete and legacy events too, before a later parent write.

    Full-string matches preserve prose that merely discusses state changes.
    Both native spellings are reserved even when the other field disagrees;
    changing the description later must not activate a planted fallback title.
    """
    if row.get('issue_type', '').strip().lower() != 'event':
        return False
    for field, pattern in (('description', _DESCRIPTION), ('title', _TITLE)):
        match = pattern.fullmatch(row.get(field, ''))
        if match and match.group(1) in NAMES:
            return True
    return False


def checked_rows(read, tokens):
    rows = read(list(dict.fromkeys(tokens)))
    if not isinstance(rows, list) or len(rows) != len(set(tokens)):
        refuse('the affected tasks could not be read completely')
    ids = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get('malformed')
                or any(not isinstance(row.get(k), str) or not row[k]
                       for k in ('id', 'issue_type', 'title'))
                or not isinstance(row.get('description', ''), str)
                or not isinstance(row.get('labels', []) or [], list)
                or any(not isinstance(v, str) for v in (row.get('labels') or []))
                or (row['id'] in ids and row != ids[row['id']])):
            refuse('the affected tasks could not be read completely')
        ids[row['id']] = row
    return list(ids.values())


def guard(args, attachments, read):
    """Called under the project lock before the raw native write or journal.

    ``read`` returns the unfiltered JSON rows for the exact native target list.
    Existing read/identity/anchor guards still apply. No structured route uses
    this helper; configured operators keep the original raw route.
    """
    command = args[0]
    if command not in ('create', 'update', 'dep', 'close', 'reopen'):
        return
    if command in ('dep', 'close', 'reopen'):
        request = write_targets(args, attachments)
        if request is None:
            return
        if request['refusal']:
            refuse('the affected rows cannot be resolved')
        if any(event_shape(row) for row in checked_rows(read, request['targets'])):
            refuse('editing a lifecycle event through raw bd is not allowed')
        return

    # Resolve transported description bytes before scanning, without consulting
    # request-created paths. Other attachment flags are checked by the endpoint.
    expanded = []
    for token in args:
        if token.startswith('@attachment:'):
            item = attachments.get(token.partition(':')[2])
            if not isinstance(item, dict) or not isinstance(item.get('text'), str):
                refuse('an attachment is incomplete')
            flag = item.get('flag')
            expanded.extend([flag, item['text']])
        else:
            expanded.append(token)
    flags, operands, unknown = _bd_scan(expanded, command)
    flags = [(_ALIASES.get(name, name), value) for name, value in flags]
    if unknown or any(value is None for _, value in flags):
        refuse('the write effect cannot be resolved')
    names = {name for name, _ in flags}
    if any(name == '--stdin' and _parse_go_bool(value) is not False for name, value in flags):
        refuse('a stdin description is not available to the guard')
    for name, value in flags:
        if name in _LABEL_FLAGS and any(reserved_label(v) for v in labels(value)):
            refuse('lifecycle dimension, scope, enabled and live labels require a lifecycle write')
    bodies = [value for name, value in flags if name in ('--description', '--body-file')]
    if '--body-file' in names and len(bodies) != 1:
        refuse('multiple description sources make the final event ambiguous')
    final = {}
    for name, value in flags:
        if name in _STRUCTURAL:
            final[{'--type': 'issue_type', '--body-file': 'description'}.get(name, name[2:])] = value
    if command == 'create':
        if len(operands) > 1 or (operands and '--title' in names):
            refuse('multiple title sources make the final event ambiguous')
        row = dict(issue_type='task', title=operands[0] if operands else '', description='')
        row.update(final)
        if event_shape(row):
            refuse('creating a lifecycle event through raw bd is not allowed')
        parents = [v for name, v in flags if name == '--parent']
        inherit = [v for name, v in flags if name == '--no-inherit-labels']
        if len(parents) > 1 or any(_parse_go_bool(v) is None for v in inherit):
            refuse('the inherited labels cannot be resolved')
        if parents and not (inherit and _parse_go_bool(inherit[-1]) is True):
            for parent in checked_rows(read, parents):
                if any(reserved_label(v) for v in (parent.get('labels') or [])):
                    refuse('inheriting lifecycle labels requires --no-inherit-labels')
        return

    if not operands:
        refuse('the task to update is not named')
    for row in checked_rows(read, operands):
        if event_shape(row):
            refuse('editing a lifecycle event through raw bd is not allowed')
        if '--set-labels' in names and any(reserved_label(v) for v in (row.get('labels') or [])):
            refuse('replacing labels would change existing lifecycle facts')
        candidate = dict(row, **final)
        if names & _STRUCTURAL and event_shape(candidate):
            refuse('creating, changing or removing a lifecycle event through raw bd is not allowed')
