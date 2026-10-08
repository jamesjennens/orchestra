"""One guard for every JSON text the kit parses that somebody else wrote (kittrial-5bb.108).

A record comment, a `--file` attachment, a payload argument, a request body: each is
text from a caller or from the tracker, and each used to go straight to `json.loads`.
JSON nested about a thousand levels deep raises `RecursionError` there. That is a
`RuntimeError`, not the `ValueError` every parser catches, so one such comment failed the
whole read (`ref get`, `work` for every actor of the project, and `void-record`, the
command meant to repair it), and one such request was answered "outcome unknown".

`loads` refuses nesting deeper than NESTING_MAX before it parses, by counting brackets
outside string literals in ONE pass over the text, so the result does not depend on the
interpreter's recursion limit, on how deep the call stack already is, or on the Python
version, and the cost is linear in the text whatever it holds. Deeper nesting
raises `NestingError`, a `ValueError`: each reader's existing "this comment is malformed"
path takes it, the entry reads malformed and is voidable, and a request is refused with
one sentence. A `RecursionError` from the parse itself is turned into the same error, as
a second line of defence.

The deepest JSON the kit writes nests 4 levels. NESTING_MAX leaves room for a later
record and is far below any interpreter limit.
"""
import hashlib
import json
import re

NESTING_MAX = 64
MESSAGE = 'JSON nested too deeply (more than %d levels)' % NESTING_MAX
# The only characters that matter to nesting: a backslash with the character it escapes
# (so an escaped quote never closes a string), a quote, and the four brackets. Each match
# consumes its characters and the scan never goes back, so it is one pass and linear
# however malformed the text is. (The first version removed whole string literals with a
# pattern that searched again from every quote of a literal that was never closed, which
# was quadratic: review of 7c14f6a.)
_TOKEN = re.compile(r'\\.|["\[\]{}]', re.DOTALL)


class NestingError(ValueError):
    """JSON text nested deeper than NESTING_MAX."""


ROW_NESTING_MAX = 750
ROW_MESSAGE = 'Tracker row nested too deeply (more than %d levels)' % ROW_NESTING_MAX


def native_ids(run, *filters):
    """IDs selected by bd itself, even when a selected row cannot be decoded.

    Selection is evidence; the unreadable row's title, type and embedded labels
    are not. An incomplete answer cannot prove that an ID is absent.
    """
    rows = loads_array_rows(run(['list', *filters, '--all', '--limit', '0', '--json']) or '[]')
    return set(ids_from_native(rows))


def ids_from_native(rows):
    """Keep native selection order, refusing an answer that loses a row's ID."""
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError('Native membership read returned a row without an ID; cannot classify unreadable rows')
        ids.append(row['id'])
    return ids


def mark_selected(rows, ids, labels, issue_type=None):
    """Attach ONLY labels proven by a native filter to unreadable selected rows.

    Healthy rows are untouched. The marker is a Python attribute, never a JSON
    field that a contributor could supply or that a view would serialize.
    """
    result = []
    for row in rows:
        if isinstance(row, dict) and row.get('malformed') and row.get('id') in ids:
            if not isinstance(row, SelectedRow):
                row = SelectedRow(row)
                row['labels'] = []
                row['issue_type'] = 'unknown'
            row.selected_labels.update(labels)
            row['labels'] = sorted(row.selected_labels)
            if issue_type is not None:
                row.selected_types.add(issue_type)
                row['issue_type'] = issue_type
        result.append(row)
    return result


class SelectedRow(dict):
    def __init__(self, row):
        super().__init__(row)
        self.selected_labels = set()
        self.selected_types = set()


def selected(row, labels=(), types=()):
    return isinstance(row, SelectedRow) and bool(row.selected_labels.intersection(labels)
                                               or row.selected_types.intersection(types))


def classify(rows, run, labels=(), types=()):
    """One native membership read per label, only if this read has unreadable rows."""
    if not any(isinstance(r, dict) and r.get('malformed') for r in rows):
        return rows
    for label in labels:
        rows = mark_selected(rows, native_ids(run, '--label', label), [label])
    for issue_type in types:
        rows = mark_selected(rows, native_ids(run, '--type', issue_type), [], issue_type=issue_type)
    return rows


def classify_key_rows(rows, listed, run, type_label, labels):
    """The type AND (key OR request) selection proves type, not which OR arm.

    A get has one arm and needs no extra read. A write with unreadable rows pays
    separate filters so a request collision cannot be mistaken for another key.
    """
    ids = set(ids_from_native(listed))
    rows = mark_selected(rows, ids, [type_label])
    if any(isinstance(r, dict) and r.get('malformed') for r in rows):
        for label in labels:
            chosen = ids if len(labels) == 1 else native_ids(run, '--label', type_label, '--label', label)
            rows = mark_selected(rows, chosen, [label])
    return rows


def nesting(text, max_depth=NESTING_MAX):
    """The deepest bracket nesting of a JSON text, counted outside string literals, or
    max_depth + 1 as soon as it is exceeded.

    One pass, no recursion, linear in the text. A text with at most max_depth opening
    brackets cannot nest deeper than that, so it is answered without a scan. Inside a
    string literal brackets are text; a literal that is never closed makes the rest of
    the text a string, which the parser then refuses as malformed. The scan stops at the
    first bracket past the bound, so deep nesting is refused without reading the rest.
    """
    if text.count('[') + text.count('{') <= max_depth:
        return 0
    depth = deepest = 0
    in_string = False
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token == '"':
            in_string = not in_string
        elif in_string or token[0] == '\\':
            continue
        elif token in '[{':
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > max_depth:
                    return deepest
        elif depth:
            depth -= 1
    return deepest


def check(text):
    """Raise NestingError when `text` nests deeper than NESTING_MAX."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode('utf-8', 'replace')
    if isinstance(text, str) and nesting(text) > NESTING_MAX:
        raise NestingError(MESSAGE)


def loads(text, **options):
    """`json.loads` for text somebody else wrote: bounded nesting, and never a RecursionError."""
    check(text)
    try:
        return json.loads(text, **options)
    except RecursionError:
        raise NestingError(MESSAGE) from None


def _make_malformed_row(line, error):
    m = re.search(r'"id"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.-]{0,160})"', line)
    task_id = m.group(1) if m else None
    m_title = re.search(r'"title"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', line)
    if m_title:
        title = re.sub(r'\\(["\\/bfnrt])', lambda m: {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}.get(m.group(1), m.group(1)), m_title.group(1))
    else:
        title = 'Malformed issue row (%s)' % error
    raw_bytes = line.encode('utf-8')
    return {'id': task_id,
            'title': title,
            'status': 'unknown',
            'assignee': None,
            'issue_type': 'unknown',
            'labels': [],
            'malformed': True,
            'error': str(error),
            'raw_length': len(raw_bytes),
            'raw_sha256': hashlib.sha256(raw_bytes).hexdigest()}


def loads_row(line):
    """Parse one issue row from tracker export (`bd export --all`).

    A row main reads (including 65 and 500 levels of nesting) must parse
    normally. Only rows that truly cannot be parsed by json.loads or that exceed
    ROW_NESTING_MAX (750 levels) are returned as synthetic malformed records.
    If the row has a recoverable ID, that ID is preserved; if not, id is None.
    Neither is ever dropped. Status is always unknown and assignee is always None
    on unparseable rows so attacker-supplied metadata never grants ownership or
    visibility. Labels are always empty on unparseable rows to prevent attacker-supplied
    metadata from spoofing labels.
    """
    if not line or not line.strip():
        return None
    if nesting(line, ROW_NESTING_MAX) > ROW_NESTING_MAX:
        return _make_malformed_row(line, NestingError(ROW_MESSAGE))
    try:
        return json.loads(line)
    except (ValueError, RecursionError) as error:
        return _make_malformed_row(line, error)


def loads_rows(lines):
    """Parse lines from tracker export (`bd export --all`) through `loads_row`.

    Accepts a string (which it splits into lines) or an iterable of line strings.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    rows = []
    for line in lines:
        if not line or not line.strip():
            continue
        row = loads_row(line)
        if row is not None:
            rows.append(row)
    return rows


def loads_array_rows(text):
    """Parse JSON array output from bd (`bd list --json` or `bd show --json`).

    If json.loads succeeds, returns the decoded list (or single object in a list).
    If the text raises RecursionError or exceeds ROW_NESTING_MAX, each element is parsed
    iteratively using loads_row, recovering malformed items with their IDs preserved.
    Truncated inputs, non-JSON text, or syntax errors outside depth-limit failures
    raise ValueError as json.loads does.
    """
    if isinstance(text, list):
        return text
    if isinstance(text, dict):
        return [text]
    if not text or not isinstance(text, str) or not text.strip() or text.strip() == 'null':
        return []
    if nesting(text, ROW_NESTING_MAX) <= ROW_NESTING_MAX:
        try:
            res = json.loads(text)
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                return [res]
            raise ValueError('Expected JSON array or object, got %s' % type(res).__name__)
        except RecursionError:
            pass
        except (ValueError, json.JSONDecodeError):
            raise

    stripped = text.strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        row = loads_row(stripped)
        return [row] if row is not None else []
    if not stripped.startswith('['):
        raise ValueError('Truncated or invalid JSON array')

    rows = []
    start = stripped.find('[')
    i = start + 1
    n = len(stripped)
    in_string = False
    escape = False
    item_start = None
    depth = 0

    found_end = False
    while i < n:
        c = stripped[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == '\\' and in_string:
            escape = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue

        if c == '{':
            if depth == 0:
                item_start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and item_start is not None:
                item_text = stripped[item_start:i + 1]
                row = loads_row(item_text)
                if row is not None:
                    rows.append(row)
                item_start = None
            elif depth < 0:
                raise ValueError('Truncated or invalid JSON array')
        elif c == ']' and depth == 0:
            found_end = True
            if stripped[i + 1:].strip():
                raise ValueError('Trailing data after JSON array')
            break
        i += 1

    if not found_end or depth != 0 or item_start is not None:
        raise ValueError('Truncated or invalid JSON array')

    return rows
