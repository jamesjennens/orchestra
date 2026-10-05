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


ROW_NESTING_MAX = 1000
ROW_MESSAGE = 'Tracker row nested too deeply (more than %d levels)' % ROW_NESTING_MAX


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


def loads_row(line):
    """Parse one issue row from tracker export (`bd export --all`).

    A row main reads (including 65 and 500 levels of nesting) must parse
    normally. Only rows that truly cannot be parsed by json.loads or that exceed
    ROW_NESTING_MAX (1000 levels) are returned as synthetic malformed records.
    If the row has a recoverable ID, that ID is preserved; if not, id is None.
    Neither is ever dropped. Status is always unknown and assignee is always None
    on unparseable rows so attacker-supplied metadata never grants ownership or
    visibility.
    """
    if not line or not line.strip():
        return None
    try:
        if nesting(line, ROW_NESTING_MAX) > ROW_NESTING_MAX:
            raise NestingError(ROW_MESSAGE)
        return json.loads(line)
    except (ValueError, RecursionError) as error:
        m = re.search(r'"id"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.-]{0,160})"', line)
        task_id = m.group(1) if m else None
        m_type = re.search(r'"issue_type"\s*:\s*"([A-Za-z0-9_.-]+)"', line)
        issue_type = m_type.group(1) if m_type else ('event' if (task_id and '.' in task_id) else 'task')
        m_title = re.search(r'"title"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', line)
        if m_title:
            title = re.sub(r'\\(["\\/bfnrt])', lambda m: {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}.get(m.group(1), m.group(1)), m_title.group(1))
        else:
            title = 'Malformed issue row (%s)' % error
        raw_bytes = line.encode('utf-8')
        m_labels = re.search(r'"labels"\s*:\s*\[(.*?)\]', line)
        labels = re.findall(r'"([^"\\]+)"', m_labels.group(1)) if m_labels else []
        return {'id': task_id,
                'title': title,
                'status': 'unknown',
                'assignee': None,
                'issue_type': issue_type,
                'labels': labels,
                'malformed': True,
                'error': str(error),
                'raw_length': len(raw_bytes),
                'raw_sha256': hashlib.sha256(raw_bytes).hexdigest()}


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
    If a row in the array raises RecursionError or ValueError due to deep nesting
    or corruption, each element is parsed iteratively without recursion using loads_row,
    recovering malformed items with their IDs preserved.
    """
    if not text or not text.strip() or text.strip() == 'null':
        return []
    if nesting(text, ROW_NESTING_MAX) <= ROW_NESTING_MAX:
        try:
            res = json.loads(text)
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                return [res]
            return []
        except (ValueError, RecursionError):
            pass

    stripped = text.strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        row = loads_row(stripped)
        return [row] if row is not None else []

    rows = []
    start = text.find('[')
    if start == -1:
        if '{' in text:
            row = loads_row(text)
            return [row] if row is not None else []
        return []

    i = start + 1
    n = len(text)
    in_string = False
    escape = False
    item_start = None
    depth = 0

    while i < n:
        c = text[i]
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
                item_text = text[item_start:i + 1]
                row = loads_row(item_text)
                if row is not None:
                    rows.append(row)
                item_start = None
        elif c == ']' and depth == 0:
            break
        i += 1

    return rows

