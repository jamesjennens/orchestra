"""A disk-backed cache of the parsed tracker export, keyed by the tracker's own
generation (kittrial-5bb.339).

One bd `export --all` was two thirds of every read of a large project, and it was
paid again on every request because nothing survived between endpoint processes.
The kit's writes all commit to Dolt, so the tracker's newest `dolt_log` commit hash
IS the generation of the data: any write - a task, a label, a status, a comment, a
dependency, a lifecycle fact - creates a new commit and changes the key, and no read
commits. A read therefore serves the previously parsed rows straight from the cache
when the key still matches, and re-exports (then refreshes the cache) when it does
not; a write is visible to the next read by construction.

The cache holds the OUTPUT of `record_json.loads_rows` (the coordinator's addition:
the nesting walk is not repeated either), as one JSON file beside a key file, both
mode 0600, written to a temporary name and renamed so a concurrent reader never sees
a half-written cache. Anything unexpected - no Dolt server coordinates, an unreadable
key, a corrupt rows file, a cache write that fails - answers TODAY's behaviour: a
fresh export, nothing cached. The cache is derived state only; the tracker is never
touched.
"""
import json
import os
import re
from pathlib import Path

#: Where the cache lives, under the project directory (never inside the tracker).
CACHE_DIR = '.read-cache'
ROWS_NAME = 'export-rows.json'
KEY_NAME = 'export-key'

#: The database-name rule admin.sql itself applies.
_DATABASE = re.compile(r'[A-Za-z0-9_]{1,64}')


def project_generation(root, name):
    """The tracker's newest dolt commit hash, or ``None`` when it cannot be read.

    Reading it is one SQL query over the loopback server (~0.1 s on the fixture);
    a project with no server coordinates has no query to ask and no cache (a
    file-mode tracker pays the export as before).
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
        answer = admin.sql(root, 'USE `%s`; SELECT commit_hash FROM dolt_log '
                                 'ORDER BY date DESC LIMIT 1' % database, timeout=30)
    except Exception:
        return None
    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] != 'commit_hash':
        return None
    commit = lines[1]
    if not re.fullmatch(r'[0-9a-z]{32}', commit):
        return None
    return commit


def _write_private(path, payload):
    """Write bytes privately and atomically (temp file, 0600, rename)."""
    import tempfile
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.read-cache-')
    try:
        os.write(handle, payload)
        os.fsync(handle)
    finally:
        os.close(handle)
    os.chmod(temp, 0o600)
    os.replace(temp, str(path))


def _cached_rows(cache_dir, key):
    """The parsed rows for ``key``, or ``None`` when the cache cannot serve."""
    try:
        stored = (cache_dir / KEY_NAME).read_text(encoding='utf-8').strip()
    except (OSError, ValueError):
        return None
    if stored != key:
        return None
    try:
        import record_json
        with (cache_dir / ROWS_NAME).open(encoding='utf-8') as handle:
            rows = record_json.loads(handle.read())
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return rows


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
    cache_dir = path / CACHE_DIR
    rows = _cached_rows(cache_dir, key)
    if rows is not None:
        return rows
    import record_json
    rows = record_json.loads_rows(run(['export', '--all']))
    try:
        cache_dir.mkdir(mode=0o700, exist_ok=True)
        _write_private(cache_dir / ROWS_NAME,
                       json.dumps(rows).encode('utf-8'))
        _write_private(cache_dir / KEY_NAME, key.encode('utf-8'))
    except OSError:
        pass                      # the cache is an optimization, never a requirement
    return rows
