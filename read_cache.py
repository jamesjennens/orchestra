"""A disk-backed cache of the parsed tracker export, keyed by the tracker's own
generation (kittrial-5bb.339).

One bd `export --all` was two thirds of every read of a large project, and it was
paid again on every request because nothing survived between endpoint processes.
The kit's writes all commit to Dolt, so the tracker's HEAD hash IS the generation
of the data: any kit write - a task, a label, a status, a comment, a dependency, a
lifecycle fact - creates a new commit and changes the key, and no read commits. A
read therefore serves the previously parsed rows straight from the cache when the
key still matches, and re-exports (then refreshes the cache) when it does not; a
write is visible to the next read by construction.

The cache is ONE file holding the format version, the key and the rows together,
replaced by a single rename (kittrial-5bb.339 review B1: two files renamed apart
let interleaving refreshes leave older rows under the newer key), and the stored
format version is part of what the reader compares, so rows parsed by an older kit
are never served after an upgrade (review B2). Rows whose nesting exceeds the
whole-text bound (a row at 70 levels) are stored one per line and read back with
``loads_rows``, whose per-row bound is 750 - such a project still caches, and no
read pays a failed write every time (review S2). A ``.read-cache`` that is a
symlink or not a directory is refused, and the file is opened with ``O_NOFOLLOW``
(review S1). Everything unexpected - no Dolt server coordinates, an unreadable
key, a file this kit cannot read - answers TODAY's behaviour: a fresh export,
nothing cached. The cache is derived state only; the tracker is never touched.

One limit, stated rather than hidden (review S3): the key is the committed HEAD,
while ``bd export`` reads the working set. A change made OUTSIDE the kit by direct
SQL - an operator's ``UPDATE`` without a commit - does not move the key and is not
seen until the next kit write commits it. Every write the kit itself makes
commits, including with bd's auto-commit off and through the endpoint; the kit's
only working-set-over-SQL writer, ``DOLT_BACKUP('restore')``, replaces the
database and follows its HEAD.
"""
import json
import os
import re
from pathlib import Path

#: Where the cache lives, under the project directory (never inside the tracker).
CACHE_DIR = '.read-cache'
FILE_NAME = 'export.json'

#: Raised whenever the stored shape or the parse behind it changes (review B2):
#: a file written with another value is never served, so rows parsed by an older
#: kit fall back to a fresh export after an upgrade.
FORMAT = 2

#: The database-name rule admin.sql itself applies.
_DATABASE = re.compile(r'[A-Za-z0-9_]{1,64}')

#: The whole-text nesting bound of record_json.loads (its NESTING_MAX). A rows
#: array nested deeper than this cannot be read back as one text, so such a
#: project's rows are stored one per line instead (per-row bound 750).
_WHOLE_TEXT_NESTING = 64


def project_generation(root, name):
    """The tracker's HEAD hash, or ``None`` when it cannot be read.

    ``SELECT HASHOF('HEAD')`` asks the exact question - the working tree's current
    commit, not the newest entry in a dated log (review note: an ORDER BY date
    query answers a slightly different one). Reading it is one SQL query over the
    loopback server (~0.04 s measured); a project with no server coordinates has
    no query to ask and no cache (a file-mode tracker pays the export as before).
    """
    try:
        import admin
        metadata = admin.project_server_metadata(root, name)
    except (ValueError, OSError):
        return None
    if metadata is None:
        return None
    database = metadata[3]
    if not _DATABASE.fullmatch(database):
        return None
    try:
        answer = admin.sql(root, "USE `%s`; SELECT HASHOF('HEAD')" % database, timeout=30)
    except Exception:
        return None
    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[1]:
        return None
    commit = lines[1]
    if not re.fullmatch(r'[0-9a-z]{32}', commit):
        return None
    return commit


def _read_no_follow(path):
    """The file's bytes, or ``None`` - opened without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return None
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _cache_directory(path):
    """The cache directory when it is a real directory, else ``None``.

    A symlink planted at ``.read-cache`` is refused, not followed (review S1): the
    refresh then writes nothing outside the project, and a planted file beyond it
    is never served. A non-directory (a file, a device) is refused for the same
    reason.
    """
    cache = path / CACHE_DIR
    try:
        if cache.is_symlink():                    # lstat: does not follow
            return None
        if cache.exists() and not cache.is_dir():
            return None
    except OSError:
        return None
    return cache


def _cached_rows(cache, key):
    """The parsed rows for ``key``, or ``None`` when the cache cannot serve.

    The one file holds ``{"format": F, "key": K, "rows": [...]}`` or, for a
    project whose rows nest deeper than the whole-text bound,
    ``{"format": F, "key": K, "kind": "lines", "text": "one row per line"}``.
    """
    raw = _read_no_follow(cache / FILE_NAME)
    if raw is None:
        return None
    import record_json
    try:
        container = record_json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return None
    if not isinstance(container, dict):
        return None
    if container.get('format') != FORMAT or container.get('key') != key:
        return None
    if container.get('kind') == 'lines':
        rows = record_json.loads_rows(container.get('text') or '')
    else:
        rows = container.get('rows')
    if not isinstance(rows, list):
        return None
    return rows


def _serialize(rows, key):
    """The one file's bytes: the array form, or per-line when nesting is too deep."""
    container = {'format': FORMAT, 'key': key, 'rows': rows}
    text = json.dumps(container)
    import record_json
    if record_json.nesting(text) <= _WHOLE_TEXT_NESTING:
        return text.encode('utf-8')
    lines = ''.join(json.dumps(row) + '\n' for row in rows)
    return json.dumps({'format': FORMAT, 'key': key, 'kind': 'lines',
                       'text': lines}).encode('utf-8')


def _write_one_file(cache, payload):
    """Write the whole cache as ONE privately-modeled file, one rename (review B1)."""
    import tempfile
    handle, temp = tempfile.mkstemp(dir=str(cache), prefix='.read-cache-')
    try:
        os.write(handle, payload)
        os.fsync(handle)
    finally:
        os.close(handle)
    os.chmod(temp, 0o600)
    os.replace(temp, str(cache / FILE_NAME))


def cached_export_rows(root, path, run, name=None):
    """The project's export rows: from the cache when the generation matches.

    ``run(['export','--all'])`` is the fresh path, exactly the call every reader
    paid before; ``name`` is the project (derived from ``path`` when omitted).
    The returned list is a freshly parsed object each call - downstream readers
    may mark and replace rows - so no state is shared between requests.
    """
    if name is None:
        name = path.name
    key = project_generation(root, name)
    if key is None:
        import record_json
        return record_json.loads_rows(run(['export', '--all']))
    cache = _cache_directory(path)
    if cache is not None:
        rows = _cached_rows(cache, key)
        if rows is not None:
            return rows
    import record_json
    rows = record_json.loads_rows(run(['export', '--all']))
    try:
        (path / CACHE_DIR).mkdir(mode=0o700, exist_ok=True)
        cache = _cache_directory(path)              # a symlink won the race: skip
        if cache is not None:
            _write_one_file(cache, _serialize(rows, key))
    except OSError:
        pass                      # the cache is an optimization, never a requirement
    return rows
