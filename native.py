"""Native (bd) process results: keep success JSON separate from warnings.

The endpoint protocol carries stdout and stderr separately. A native command can
still emit warnings on stderr with exit status 0; they must reach the caller's
stderr instead of being dropped or concatenated onto the success JSON.

The native boundary is deliberately narrow. Native *stderr* on success is
operator-facing native output and is forwarded verbatim. Everything else that
would otherwise reach an error message or a log is bounded and redacted:

* a nonzero exit becomes ``Native command failed (<rc>)`` with at most one short
  diagnostic line, path-shaped tokens replaced, and any longer output withheld
  behind a line/character count and a content digest;
* stdout is classified by one reviewed policy before it becomes a result:
  **exactly one data document per stream**. The whole stream is decoded first
  (so an indented document wrapped in warning lines stays one document and never
  leaks line by line); line mode (JSON Lines) is used only when every data line
  is a complete JSON object or array, and all of those rows are returned
  together, which is the native ``export --all`` shape; a single multi-line
  line-anchored document may be wrapped in notes; two data documents — two
  indented documents, or a document plus a one-line row — are refused with a
  labelled error instead of one being returned and the others noted as noise
  (that was silent data loss with rc 0);
* diagnostic objects are notes, never data: a log record (``{"level": "warn"}``)
  or a warning/notice shape (``{"warning": ...}``, keys drawn only from
  ``NOTICE_KEYS``) is re-labelled on stderr, so a warning object next to a
  one-line row yields one data line and one note, not two data lines;
* a bare scalar is never a data document, with one reviewed exception: a
  whole-stream ``null`` is returned verbatim. Base accepted it and callers
  consume it with ``json.loads(...) or []``, so keeping it preserves backward
  compatibility; every other bare scalar is noise. A stream whose only JSON is a
  diagnostic object (for example ``{"message": ...}``) has no data document and
  is refused — a documented tightening against base, which accepted it;
* forwarded stdout-noise lines are re-labelled as ``native stdout note:`` and
  capped (see ``NOISE_LINE_LIMIT``); leftovers are withheld with a digest.

Raw ``bd`` passthrough (the ``bd`` action and ``refresh``) is intentionally
outside this boundary and returns native output unchanged.
"""
import hashlib
import json
import re
import subprocess

DETAIL_LINE_LIMIT = 160
# Kept as the published name of the detail bound.
DIAGNOSTIC_LIMIT = DETAIL_LINE_LIMIT
# At most this many stdout-noise lines are re-labelled individually; the rest
# are withheld behind one count/digest line.
NOISE_LINE_LIMIT = 8
TIMEOUT = 120
NOISE_PREFIX = 'native stdout note: '
# A structured result is an object or an array; a bare scalar never is.
JSON_RESULT_TYPES = (dict, list)
# A standalone object whose keys are all log-record keys is a log line, not a
# result row (``{"level": "warn"}``).
LOG_RECORD_KEYS = frozenset((
    'level', 'severity', 'time', 'timestamp', 'ts', 'msg', 'message', 'logger',
    'caller', 'thread', 'module', 'service', 'component', 'pid',
))
# Log-record keys plus the warning/notice shapes native tools print next to a
# result. An object whose keys are all drawn from this set is a diagnostic note,
# never a data row (``{"warning": "beads.role not configured"}``).
NOTICE_KEYS = LOG_RECORD_KEYS | frozenset((
    'warning', 'warnings', 'warn', 'notice', 'notices', 'error', 'errors',
    'hint', 'hints', 'detail', 'details', 'reason', 'reasons', 'code',
))
_JSON = json.JSONDecoder()

# --- path redaction ---------------------------------------------------------
# One reviewed boundary, applied in order. Each pattern replaces a whole
# path-shaped token so no private fragment survives in an echoed line:
# quoted spans, URLs (file:/http:/...), home-relative ``~/``-``~\`` tokens,
# absolute Windows/UNC paths (a quoted or unquoted path with spaces is taken
# whole, including its last space-free segment), absolute POSIX paths, and
# relative path tokens that look like a path (>= 2 separators, or a dotted final
# segment). A two-segment token without a dot - an actor like ``alice/session`` -
# is deliberately left alone.
_QUOTED_PATH = re.compile(r'(["\'])([^"\']*[\\/][^"\']*)\1')
_URL = re.compile(r'\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\'";,]+')
_TILDE_PATH = re.compile(r'~[\\/][^\s\'";,]+')
_WIN_SEGMENT = r'[^\s\\/:*?"<>|]+'
_WIN_DIR = _WIN_SEGMENT + r'(?: ' + _WIN_SEGMENT + r')*'
_WIN_HEAD = r'(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+[\\/])'
# The path proper: up to its last space-free segment.
_WINDOWS_PATH = re.compile(_WIN_HEAD + r'(?:' + _WIN_DIR + r'[\\/])*' + _WIN_SEGMENT)
# The same path plus every following space-separated segment, so the whole run
# can be judged by ``_windows_path`` below.
_WINDOWS_PATH_RUN = re.compile(_WINDOWS_PATH.pattern + r'(?: ' + _WIN_SEGMENT + r')*')
_POSIX_PATH = re.compile(r'(?<![\w.-])/[^\s\'";,]+')
_RELATIVE_PATH = re.compile(r'[A-Za-z0-9._-]+(?:[\\/][A-Za-z0-9._-]+)+')


def argv(root, path, actor, command, scoped=True):
    """Build the fixed native argv. Only trusted kit values enter it."""
    prefix = [str(root / 'bin' / 'bd'), '--directory', str(path), '--sandbox']
    if scoped:
        prefix += ['--actor', actor]
    return [*prefix, *command]


def run(command, env, timeout=TIMEOUT):
    return subprocess.run(command, env=env, capture_output=True, text=True,
                          encoding='utf-8', timeout=timeout)


def _relative_path(match):
    token = match.group(0)
    last = token.replace('\\', '/').rsplit('/', 1)[-1]
    if token.count('/') + token.count('\\') >= 2 or '.' in last:
        return '<path>'
    return token


def _windows_path(match):
    """Replace a drive/UNC path whole, including a space-broken final segment.

    An unquoted Windows path may contain spaces (``C:\\Users\\James Smith\\private
    notes``), so the run is greedy over space-separated segments. When the path
    proper - up to its last space-free segment - already contains a space, every
    segment of the run belongs to the path and is removed. Otherwise the trailing
    space-separated text is ordinary prose, so ``cannot open C:\\db file is
    locked`` keeps ``file is locked`` readable while the path itself is removed.
    """
    token = match.group(0)
    base = _WINDOWS_PATH.match(token)
    base_text = base.group(0) if base else token
    if ' ' in base_text:
        return '<path>'
    return '<path>' + token[len(base_text):]


def redact(text):
    """Replace path-shaped tokens and cap one echoed diagnostic line."""
    value = str(text)
    value = _QUOTED_PATH.sub(r'\1<path>\1', value)
    value = _URL.sub('<path>', value)
    value = _TILDE_PATH.sub('<path>', value)
    value = _WINDOWS_PATH_RUN.sub(_windows_path, value)
    value = _POSIX_PATH.sub('<path>', value)
    value = _RELATIVE_PATH.sub(_relative_path, value).strip()
    if len(value) > DETAIL_LINE_LIMIT:
        value = value[:DETAIL_LINE_LIMIT] + '...'
    return value


def digest(text):
    return hashlib.sha256(str(text).encode('utf-8', 'replace')).hexdigest()[:12]


def withheld(lines, raw):
    return '[native output withheld: %d line(s), %d characters, sha256:%s]' % (
        len(lines), len(raw), digest(raw))


def detail(raw):
    """Bounded, redacted echo of native output; never a whole private payload."""
    lines = [line.strip() for line in str(raw or '').splitlines() if line.strip()]
    if not lines:
        return ''
    if len(lines[0]) > DETAIL_LINE_LIMIT:
        return withheld(lines, raw)
    if len(lines) == 1:
        return redact(lines[0])
    return '%s ... %s' % (redact(lines[0]), withheld(lines, raw))


def failure(completed):
    label = 'Native command failed (%d)' % completed.returncode
    text = detail(completed.stderr or completed.stdout or '')
    return '%s: %s' % (label, text) if text else label


def stdout_failure(stdout):
    return 'Native stdout is not JSON (exit 0): %s' % detail(stdout)


def stdout_ambiguous(stdout):
    """Labelled refusal for a stream that carries more than one data document.

    The whole stream is withheld behind a count and digest rather than echoed:
    the ambiguity itself is the finding, and the fragments on either side of it
    may be private.
    """
    lines = [line for line in str(stdout or '').splitlines() if line.strip()]
    return ('Native stdout carries more than one JSON document (exit 0): %s'
            % withheld(lines, stdout or ''))


def _decode_whole(text):
    try:
        return _JSON.decode(text)
    except ValueError:
        return None


def _is_null_result(text):
    """Whether the whole stream is the one accepted bare scalar: ``null``."""
    if text.strip() != 'null':
        return False
    try:
        return _JSON.decode(text) is None
    except ValueError:
        return False


def _is_note_object(value):
    """A diagnostic object: log record or warning/notice shape, never data."""
    return (isinstance(value, dict) and bool(value)
            and set(value).issubset(NOTICE_KEYS))


def _result_value(text):
    """The whole text as one JSON object/array result, or None."""
    value = _decode_whole(text)
    if isinstance(value, JSON_RESULT_TYPES) and not _is_note_object(value):
        return value
    return None


def _data_line(line):
    """Whether one line is a standalone JSON Lines data row."""
    return _result_value(line.strip()) is not None


def _line_starts(text):
    """Offsets of ``{``/``[`` that begin a line - where native output starts."""
    for match in re.finditer(r'(?m)^[ \t]*([{\[])', text):
        yield match.start(1)


def _documents(text):
    """Disjoint line-anchored data documents, as ``(start, end)`` in order.

    Starts nested inside a document that already began are skipped, so one
    indented document counts once however many inner objects it contains.
    Diagnostic objects are not documents. An undecodable start followed by a
    more-indented data object is refused rather than returning a nested fragment.
    """
    documents = []
    first_failed_start = None
    first_failed_indent = None
    for start in _line_starts(text):
        if documents and start < documents[-1][1]:
            continue
        try:
            value, end = _JSON.raw_decode(text, start)
        except ValueError:
            if first_failed_start is None:
                first_failed_start = start
                line_start = text.rfind('\n', 0, start) + 1
                first_failed_indent = start - line_start
            continue
        if not isinstance(value, JSON_RESULT_TYPES) or _is_note_object(value):
            continue
        line_start = text.rfind('\n', 0, start) + 1
        if (first_failed_start is not None and start > first_failed_start
                and start - line_start > first_failed_indent):
            raise ValueError(stdout_failure(text))
        documents.append((start, end))
    return documents


def json_stdout(stdout):
    """Return (json_text, noise_lines) for native stdout under one policy.

    Exactly one data document may reach a caller:

    * the whole stream is one JSON object/array (indented or not) - that is the
      document, and every warning line around it is a note;
    * a whole-stream ``null`` is kept verbatim for backward compatibility (base
      accepted it, and callers use ``json.loads(...) or []``); any other bare
      scalar is never data;
    * line mode (JSON Lines, one result row per line) is used when no multi-line
      document is present and every data line is a complete JSON object/array -
      the native ``export --all`` shape - and every one of those rows is
      returned, so nothing is dropped;
    * a single multi-line line-anchored document may be wrapped in notes;
    * two or more data documents (two indented documents, or a document plus a
      one-line row) are refused with ``Native stdout carries more than one JSON
      document`` instead of one being returned and the rest noted as noise -
      that silent pick was data loss with rc 0.

    Diagnostic objects (log records and warning/notice shapes) are notes, never
    data, so a warning object beside a one-line row yields one data line and one
    note. A stream whose only JSON is diagnostic has no data document at all and
    is refused with the labelled stdout error; that is a deliberate, documented
    tightening against base, which accepted a lone ``{"message": ...}``.
    """
    text = stdout or ''
    if not text.strip():
        return '', []
    if _result_value(text) is not None or _is_null_result(text):
        return text, []
    documents = _documents(text)
    if any('\n' in text[start:end] for start, end in documents):
        if len(documents) > 1:
            raise ValueError(stdout_ambiguous(text))
        start, end = documents[0]
        outside = [line for line in (text[:start] + text[end:]).splitlines()
                   if line.strip()]
        return text[start:end], outside
    data, noise = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        (data if _data_line(line) else noise).append(line)
    if data:
        return '\n'.join(data) + '\n', noise
    return '', [line for line in text.splitlines() if line.strip()]


def _noise_notes(noise, raw):
    notes = ''
    for line in noise[:NOISE_LINE_LIMIT]:
        notes += NOISE_PREFIX + (redact(line) if len(line) <= DETAIL_LINE_LIMIT
                                 else withheld([line], raw)) + '\n'
    if len(noise) > NOISE_LINE_LIMIT:
        notes += NOISE_PREFIX + withheld(noise[NOISE_LINE_LIMIT:], raw) + '\n'
    return notes


def split(completed):
    """Return (stdout, warnings); raise a bounded, labelled error on failure."""
    if completed.returncode:
        raise ValueError(failure(completed))
    stdout, noise = json_stdout(completed.stdout or '')
    if not stdout.strip() and noise:
        raise ValueError(stdout_failure(completed.stdout or ''))
    return stdout, (completed.stderr or '') + _noise_notes(noise,
                                                           completed.stdout or '')
