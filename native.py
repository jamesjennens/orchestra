"""Native (bd) process results: keep success JSON separate from warnings.

The endpoint protocol carries stdout and stderr separately. A native command can
still emit warnings on stderr with exit status 0; they must reach the caller's
stderr instead of being dropped or concatenated onto the success JSON.

The native boundary is deliberately narrow. Native *stderr* on success is
operator-facing native output and is forwarded verbatim. Everything else that
would otherwise reach an error message or a log is bounded and redacted:

* a nonzero exit becomes ``Native command failed (<rc>)`` with at most one short
  diagnostic line, absolute paths replaced, and any longer output withheld behind
  a line/character count and a content digest;
* stdout that is not the expected JSON is treated as contamination: short
  non-JSON lines are re-labelled as ``native stdout note:`` warnings, and a
  stdout with no JSON at all becomes a labelled error instead of a bare
  ``JSONDecodeError``.

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
TIMEOUT = 120
NOISE_PREFIX = 'native stdout note: '
PATH_TOKEN = re.compile(r'(?:[A-Za-z]:)?[\\/][^\s\'";,]+')
_JSON = json.JSONDecoder()


def argv(root, path, actor, command, scoped=True):
    """Build the fixed native argv. Only trusted kit values enter it."""
    prefix = [str(root / 'bin' / 'bd'), '--directory', str(path), '--sandbox']
    if scoped:
        prefix += ['--actor', actor]
    return [*prefix, *command]


def run(command, env, timeout=TIMEOUT):
    return subprocess.run(command, env=env, capture_output=True, text=True,
                          encoding='utf-8', timeout=timeout)


def redact(text):
    """Replace absolute paths and cap the length of one echoed diagnostic line."""
    value = PATH_TOKEN.sub('<path>', str(text)).strip()
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


def _parses(text):
    try:
        _JSON.decode(text)
        return True
    except ValueError:
        return False


def json_stdout(stdout):
    """Return (json_text, noise_lines) for native stdout.

    ``json_text`` is empty when stdout has no JSON at all. JSON Lines and a
    single JSON document survive surrounding noise lines; noise is returned so
    the caller can label it on stderr instead of failing with a decode error.
    """
    text = stdout or ''
    if not text.strip():
        return '', []
    if _parses(text):
        return text, []
    clean, noise = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        (clean if _parses(line) else noise).append(line)
    if clean:
        return '\n'.join(clean) + '\n', noise
    # A pretty-printed document can still carry one warning line around it.
    starts = [index for index in (text.find('{'), text.find('[')) if index != -1]
    if starts:
        start = min(starts)
        try:
            _, end = _JSON.raw_decode(text, start)
        except ValueError:
            end = None
        if end is not None:
            outside = (text[:start] + text[end:]).splitlines()
            outside = [line for line in outside if line.strip()]
            if outside:
                return text[start:end], outside
    return '', [line for line in text.splitlines() if line.strip()]


def split(completed):
    """Return (stdout, warnings); raise a bounded, labelled error on failure."""
    if completed.returncode:
        raise ValueError(failure(completed))
    stdout, noise = json_stdout(completed.stdout or '')
    if not stdout.strip() and noise:
        raise ValueError(stdout_failure(completed.stdout or ''))
    warnings = completed.stderr or ''
    for line in noise:
        warnings += NOISE_PREFIX + (redact(line) if len(line) <= DETAIL_LINE_LIMIT
                                    else withheld([line], completed.stdout or '')) + '\n'
    return stdout, warnings
