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
* stdout is classified by one reviewed policy before it becomes a result: the
  whole stream is decoded first (so an indented document wrapped in warning
  lines stays one document and never leaks line by line), line mode is used only
  when every data line is a complete JSON object or array, and standalone
  scalars or log-record objects are noise;
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
_JSON = json.JSONDecoder()

# --- path redaction ---------------------------------------------------------
# One reviewed boundary, applied in order. Each pattern replaces a whole
# path-shaped token so no private fragment survives in an echoed line:
# quoted spans, URLs (file:/http:/...), absolute Windows/UNC paths (a quoted or
# unquoted path with spaces is taken whole up to its last space-free segment),
# absolute POSIX paths, and relative path tokens that look like a path (>= 2
# separators, or a dotted final segment). A two-segment token without a dot -
# an actor like ``alice/session`` - is deliberately left alone.
_QUOTED_PATH = re.compile(r'(["\'])([^"\']*[\\/][^"\']*)\1')
_URL = re.compile(r'\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\'";,]+')
_WIN_SEGMENT = r'[^\s\\/:*?"<>|]+'
_WIN_DIR = _WIN_SEGMENT + r'(?: ' + _WIN_SEGMENT + r')*'
_WINDOWS_PATH = re.compile(
    r'(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+[\\/])'
    r'(?:' + _WIN_DIR + r'[\\/])*' + _WIN_SEGMENT)
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


def redact(text):
    """Replace path-shaped tokens and cap one echoed diagnostic line."""
    value = str(text)
    value = _QUOTED_PATH.sub(r'\1<path>\1', value)
    value = _URL.sub('<path>', value)
    value = _WINDOWS_PATH.sub('<path>', value)
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


def _decode_whole(text):
    try:
        return _JSON.decode(text)
    except ValueError:
        return None


def _is_log_record(value):
    return (isinstance(value, dict) and bool(value)
            and set(value).issubset(LOG_RECORD_KEYS))


def _result_value(text):
    """The whole text as one JSON object/array result, or None."""
    value = _decode_whole(text)
    if isinstance(value, JSON_RESULT_TYPES) and not _is_log_record(value):
        return value
    return None


def _data_line(line):
    """Whether one line is a standalone JSON Lines data row."""
    return _result_value(line.strip()) is not None


def _line_starts(text):
    """Offsets of ``{``/``[`` that begin a line - where native output starts."""
    for match in re.finditer(r'(?m)^[ \t]*([{\[])', text):
        yield match.start(1)


def _embedded_document(text):
    """Longest line-anchored JSON object/array plus the noise lines around it."""
    best = None
    for start in _line_starts(text):
        if best is not None and start < best[1]:
            continue
        try:
            value, end = _JSON.raw_decode(text, start)
        except ValueError:
            continue
        if not isinstance(value, JSON_RESULT_TYPES) or _is_log_record(value):
            continue
        if best is None or (end - start) > (best[1] - best[0]):
            best = (start, end)
    if best is None:
        return None
    start, end = best
    outside = [line for line in (text[:start] + text[end:]).splitlines()
               if line.strip()]
    return text[start:end], outside


def json_stdout(stdout):
    """Return (json_text, noise_lines) for native stdout.

    ``json_text`` is empty when stdout carries no result document at all. The
    whole stream is decoded first, so an indented document wrapped in warning
    lines is one document rather than a per-line fragment. Line mode (JSON
    Lines) is used only when every data line is a complete JSON object/array;
    scalars and log-record objects are noise. Noise is returned so the caller
    can label it on stderr instead of failing with a decode error.
    """
    text = stdout or ''
    if not text.strip():
        return '', []
    if _result_value(text) is not None:
        return text, []
    data, noise = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        (data if _data_line(line) else noise).append(line)
    embedded = _embedded_document(text)
    if embedded is not None:
        document, outside = embedded
        # A multi-line document outranks single-line JSON Lines candidates.
        if not data or '\n' in document:
            return document, outside
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
