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
are never served after an upgrade (review B2). Since kittrial-5bb.346 r2 the file
is a HEADER LINE then the export's own bytes verbatim: nothing is re-serialised
on a refresh, and the key compare reads the header alone, so a miss with a stale
file present pays neither the container parse nor the re-dump that made the
first read after a write slower than the kit before the cache. The header carries
the payload's length and its sha256, and a reader refuses a file whose payload
matches neither - a torn file is never served as fewer rows, and a power loss
that leaves a right-length block of zeros is never served at all. A ``.read-cache`` that is a
symlink or not a directory is refused, and the file is opened with ``O_NOFOLLOW``
(review S1). Everything unexpected - no Dolt server coordinates, an unreadable
key, a file this kit cannot read - answers TODAY's behaviour: a fresh export,
nothing cached. The cache is derived state only; the tracker is never touched.

Refreshing is ONCE per generation, across processes (kittrial-5bb.346): a miss
holds the cache directory's flock for the whole refresh, so the five reads a
web page fires right after a write share one export instead of queueing five
on the server - the others poll briefly and serve what the refresher wrote,
and a wait that runs out answers from its own export and writes nothing. The
directory itself is the lock, so the cache stays one file, and the write
carries no fsync: the rename is atomic, and a file a power loss leaves short
fails the reader's compare and falls back to a fresh export.

One limit, stated rather than hidden (review S3): the key is the committed HEAD,
while ``bd export`` reads the working set. A change made OUTSIDE the kit by direct
SQL - an operator's ``UPDATE`` without a commit - does not move the key and is not
seen until the next kit write commits it. Every write the kit itself makes
commits, including with bd's auto-commit off and through the endpoint; the kit's
only working-set-over-SQL writer, ``DOLT_BACKUP('restore')``, replaces the
database and follows its HEAD.
"""
import errno
import json
import os
import re
from pathlib import Path

#: Where the cache lives, under the project directory (never inside the tracker).
CACHE_DIR = '.read-cache'
FILE_NAME = 'export.json'

#: Raised whenever the stored shape or the parse behind it changes (review B2):
#: a file written with another value is never served, so rows parsed by an older
#: kit fall back to a fresh export after an upgrade. FORMAT 3 is the RAW form of
#: kittrial-5bb.346 r2: a header line then the export's own bytes (FORMAT 2 was
#: the container of parsed rows; its re-serialisation was a third of what the
#: cache added to the first read after a write). The parse the digest below
#: guards is unchanged - only the shape moved, but a 2-kit would misread a 3-file,
#: so the number rises with the shape.
FORMAT = 3

#: What FORMAT guards, in bytes (kittrial-5bb.345 item 3, review item: the
#: computed digest was circular - after a real change to the parse it would be
#: recomputed and still match): a LITERAL hex constant, the digest of the source
#: of record_json.loads_rows, loads_row and _make_malformed_row at the time FORMAT
#: was last set. The TEST computes the live digest the same way and compares it
#: with this literal; a parse change without FORMAT rising fails it, telling the
#: editor to raise FORMAT and update the constant. No source is read at import
#: (compiled-only installs pay nothing and never fail here).
FORMAT_DIGEST = 'd98ed175e4d422068d2989218d8a2241f73d5cf8d1e839027f217f64ec5b200a'

#: The database-name rule admin.sql itself applies.
_DATABASE = re.compile(r'[A-Za-z0-9_]{1,64}')

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

    The one file is a header line then the export's own bytes (see the module
    docstring). The key compare happens on the HEADER ONLY - a miss with a
    stale file present pays reading it, never parsing it, which is where the
    container form of .339 spent 1.3 s per check on a 66 MB payload - and the
    payload is parsed only after the format, the key AND the recorded length
    all match, so a file a power loss left short is refused, not served
    truncated (the torn-file test pins both halves).
    """
    raw = _read_no_follow(cache / FILE_NAME)
    if raw is None:
        return None
    header, separator, payload = raw.partition(b'\n')
    if not separator:
        return None
    import record_json
    try:
        head = record_json.loads(header.decode('utf-8', 'replace'))
    except ValueError:
        return None
    if not isinstance(head, dict):
        return None
    if head.get('format') != FORMAT or head.get('key') != key:
        return None
    if not isinstance(head.get('bytes'), int) or head['bytes'] != len(payload):
        return None                       # torn or foreign: fall back, never serve
    digest = head.get('sha256')
    if not isinstance(digest, str):
        return None
    import hashlib
    if digest != hashlib.sha256(payload).hexdigest():
        return None                       # zero-filled or flipped: never serve it
    return record_json.loads_rows(payload.decode('utf-8', 'replace'))


def _raw_payload(text, key):
    """The one file's bytes: a header line, then the export's own bytes verbatim.

    Nothing is re-serialised - the bytes bd printed are the payload, so a 58-66 MB
    refresh writes a byte copy instead of a json.dumps of the parsed rows (the
    serialize stage the .339 container form paid on every miss). The header
    carries the length and the sha256 of the payload that follows: the reader
    refuses a file whose payload is shorter (a torn file a dropped fsync can
    leave) or whose digest differs (the same power loss can leave a right-length
    block of zeros), so truncation and zero-fill can never be served as rows.
    Rows the export prints one per line need no special form any more - the
    payload IS one row per line."""
    payload = text.encode('utf-8')
    import hashlib
    header = json.dumps({'format': FORMAT, 'key': key, 'bytes': len(payload),
                         'sha256': hashlib.sha256(payload).hexdigest()})
    return header.encode('utf-8') + b'\n' + payload


def _write_one_file(cache, payload):
    """Write the whole cache as ONE privately-modeled file, one rename (review B1).

    A write that fails leaves NO temp file behind (kittrial-5bb.345 item 4): the
    name is removed in a finally, so a project that can never cache does not grow
    a new `.read-cache-XXXX` file per read. There is deliberately NO fsync
    (kittrial-5bb.346): the rename is atomic against every reader, and a file a
    power loss leaves short is DETECTED - the reader compares the key stored
    inside and a torn or short read falls back to a fresh export - so waiting
    for the disk serves no correctness the format does not already have."""
    import tempfile
    handle, temp = tempfile.mkstemp(dir=str(cache), prefix='.read-cache-')
    try:
        try:
            os.write(handle, payload)
        finally:
            os.close(handle)
        os.chmod(temp, 0o600)
        os.replace(temp, str(cache / FILE_NAME))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


#: How long a miss that finds another refresh already running waits for it to
#: land before answering from its own export (kittrial-5bb.346). One miss of the
#: largest live project is ~20 s end to end, so waiting half a minute longer
#: than that still beats a queue of five exports the server runs one at a time;
#: a refresher that is merely slow is waited out, and one that DIED releases
#: its lock - the kernel closes the directory when the process goes - so the
#: waiter stops waiting and refreshes itself.
_REFRESH_WAIT_SECONDS = 45.0
_REFRESH_POLL_SECONDS = 0.05


def _refresh_lock(cache):
    """Take the cache DIRECTORY's exclusive flock, or find it held.

    Locking the directory - not a lock FILE - keeps the cache at one file
    (review B1's shape is unchanged: there is nothing to clean up, and the open
    carries the same O_NOFOLLOW refusal as every other cache path), and it
    coordinates PROCESSES, not just threads: a web page's concurrent reads are
    concurrent endpoint processes, and every caller opens the directory itself,
    so two calls in one process conflict exactly like two endpoints. Returns
    (fd, 'held'); (None, 'busy') ONLY when flock says the lock is held
    (EWOULDBLOCK/EAGAIN - nothing else: review B2 of .346 r3, where every error
    read as busy made a filesystem without flock wait the whole 45 s per miss,
    past the endpoint's timeout); (None, 'posix') when locking is unavailable -
    no fcntl, ENOLCK - and the read refreshes without waiting, exactly as a
    Windows kit does; or (None, 'refuse') when the directory cannot even be
    opened (a symlink won the race, permissions): no cache for this read at all.
    """
    try:
        import fcntl
    except ImportError:
        return None, 'posix'
    try:
        fd = os.open(str(cache), os.O_RDONLY | os.O_CLOEXEC
                     | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return None, 'refuse'
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None, 'busy'           # ONLY the held-lock answer means wait
        return None, 'posix'              # ENOLCK, EINVAL: no locking here
    return fd, 'held'


def _header_key(cache):
    """The key named in the file's header line, or ``None`` - the HEADER ONLY.

    A waiter that has just acquired the lock answers with a compare that must
    not read (let alone parse) the whole payload: on a 66 MB payload that is the
    difference between five waiters parsing one at a time under the lock and
    five parsing in parallel after it (review S of .346 r3, where the queue made
    five concurrent reads SLOWER than the kit before the cache). The header is
    one short line by construction; a first chunk without a newline in it is not
    this format's file and answers ``None``."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        descriptor = os.open(str(cache / FILE_NAME), flags)
    except OSError:
        return None
    try:
        chunk = os.read(descriptor, 4096)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if b'\n' not in chunk:
        return None
    import record_json
    try:
        head = record_json.loads(chunk.partition(b'\n')[0].decode('utf-8', 'replace'))
    except ValueError:
        return None
    if not isinstance(head, dict):
        return None
    if head.get('format') != FORMAT:           # a foreign format never serves,
        return None                            # not even as a waiter's fast path
    key = head.get('key')
    return key if isinstance(key, str) else None


def _full_refresh(cache, key, run):
    """Export, parse and write the raw payload - the CALLER holds the lock (or
    there is none to hold). A failed write still answers the fresh rows."""
    import record_json
    text = run(['export', '--all'])
    rows = record_json.loads_rows(text)
    try:
        _write_one_file(cache, _raw_payload(text, key))
    except OSError:
        pass        # the cache is an optimization, never a requirement
    return rows


def _refresh(cache, key, run):
    """Refresh under the directory lock, or wait briefly for the one in flight.

    This is the whole of kittrial-5bb.346's second half. The lock is held
    across the ENTIRE refresh - export, parse, payload build, write - because
    the export is the part worth sharing: five concurrent misses for one tracker
    are five endpoint processes the export server would run one at a time, so
    the first miss to take the lock exports once and the others poll until it
    lands, then serve what it wrote. A poll that acquires re-checks only the
    file's HEADER before exporting (the refresh it waited out may have been for
    another key, or may have failed) and releases the lock BEFORE the payload is
    parsed, so waiters parse in parallel instead of one at a time under the lock
    (review S of .346 r3, where that queue made five concurrent reads slower
    than the kit before the cache). A header that names our key but does not
    serve it - torn, zero-filled, a foreign format - forces the refresh, which
    is also what repairs such a file. A wait that runs out answers from the
    caller's own export and writes nothing. A kit without POSIX locks (no
    fcntl, ENOLCK) keeps today's behaviour: every miss refreshes, each write
    still one file, one rename.
    """
    import time
    deadline = time.monotonic() + _REFRESH_WAIT_SECONDS
    force = False
    while True:
        fd, state = _refresh_lock(cache)
        if fd is None:
            if state == 'refuse':
                return None                     # the directory cannot be opened
            if state == 'busy':
                if time.monotonic() >= deadline:
                    return None                 # waited out: caller exports, no write
                time.sleep(_REFRESH_POLL_SECONDS)
                continue
            # 'posix': no locking here - refresh without waiting (Windows shape)
        try:
            if force or _header_key(cache) != key:
                return _full_refresh(cache, key, run)   # under the lock
        finally:
            if fd is not None:
                os.close(fd)
        served = _cached_rows(cache, key)       # OUTSIDE the lock: waiters parse
        if served is not None:                  # in parallel, not one at a time
            return served
        force = True    # names our key but does not serve it: repair it now


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
    return _miss(path, key, run)


def _miss(path, key, run):
    """A read whose generation the cache does not hold (kittrial-5bb.346).

    The generation was read BEFORE the export (the .339 ordering: a write that
    lands mid-read is missed, never half-seen); everything past that point -
    the one-refresher lock held across the whole refresh, the brief wait for a
    refresh already in flight, the fallback that answers from the caller's own
    export when the wait runs out - is _refresh's."""
    import record_json
    try:
        (path / CACHE_DIR).mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return record_json.loads_rows(run(['export', '--all']))
    cache = _cache_directory(path)          # a symlink won the race: skip caching
    if cache is None:
        return record_json.loads_rows(run(['export', '--all']))
    served = _refresh(cache, key, run)
    if served is not None:
        return served
    return record_json.loads_rows(run(['export', '--all']))
