#!/usr/bin/env python3
"""The one live-authority and operation-identity boundary shared by the HTTP service
and the canonical endpoint.

Two decisions in ``docs/HTTP_TRANSPORT_DESIGN.md`` are implemented here once and used
from both sides of the process seam:

* **Live authority** (:func:`decide`). Given a durable authority document and a
  request descriptor (token type, principal, project, capability), it derives the
  capability set from *current* state: session/credential validity, the issuer's live
  membership and role, and the credential's scopes. A worker credential can never
  hold more authority than its issuer currently holds, so membership removal or
  demotion stops that credential immediately. ``http_auth.Service`` uses this
  function for every route; ``endpoint.py`` calls it again immediately before the
  canonical effect, under the same cross-process lock (:func:`file_lock`) the HTTP
  service takes to persist authority changes. The authority document and lock are
  :class:`AuthorityConfig` values passed by the trusted HTTP service as endpoint
  launch arguments; they are never read from the request body. A revocation that
  commits first is therefore observed before the effect; a mutation that starts first
  completes.
* **Operation identity** (:class:`OperationJournal` + :func:`run_guarded`). Every
  canonical mutation carries a deterministic ``operation_id``, and the identity binds
  the authenticated principal and route as well as the request body. The endpoint
  reserves the identity durably *before* the effect and records the response envelope
  *with* it, inside the canonical coordination lock and the authority lock. A retry
  after a lost response replays the recorded envelope instead of repeating the effect;
  an identity reused by a different principal is a clean conflict; a retry whose first
  attempt never committed can still proceed after the reservation is released.
  Uncertainty is preserved, never converted into a duplicate: an exception that is not
  proven pre-effect keeps the reservation and reports ``124``. A refusal (bad
  attachment, task mismatch, malformed payload) raised before the effect's first
  native write through the injected runner is proven pre-effect: the identity is
  released and the real error keeps the canonical ``rc=2`` message. Identity
  retention is a **time-only**, tombstoned receipt policy: a committed receipt is
  replayable for its whole window and is compacted to a durable tombstone only once
  that window has genuinely closed, an uncertain reservation stays live (and fails
  closed) for the long window, and a clock jump larger than the skew allowance is not
  accepted — reclaim, compaction and the read-path expiry decision all use the same
  trusted clock. Neither the byte budget nor the tombstone budget can evict a live
  identity: the size is *reported* by :meth:`OperationJournal.stats` (and
  ``admin.py journal``) instead. Reclaim never *drops* an identity: the store is a
  SQLite database with one indexed row per identity, and an oversized response is kept
  as a digest instead of a body. The only admission bound is the live-identity count:
  when the journal genuinely cannot hold one more live identity the mutation fails
  closed (``124``, :class:`JournalFull`) with the pre-existing store untouched, so an
  exact retry is refused as expired rather than re-executed.

Nothing here imports ``fcntl`` at module import time, so the same module imports on a
Windows workstation and a Linux office host.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# ------------------------------------------------------- operation journal bounds
#: Hard capacity of one project's *live* operation journal, in entries. Revision 8
#: makes this the *only* admission bound: retention is by time, so a live identity is
#: never evicted to make room (not for the byte budget and not for the tombstone
#: budget), and the journal refuses a mutation with ``124`` only when the live
#: identities genuinely cannot hold one more. Tombstones never count against it, so
#: accumulated tombstones can never block a write.
JOURNAL_LIMIT = 100000
#: How long an *uncertain* reservation (``in_progress``/``unknown``) stays live. This
#: is the long-lived window: such an identity may correspond to a committed native
#: effect whose outcome was never observed, so it is retained (and an exact retry
#: reports uncertainty) until an operator reconciles it or this window closes.
JOURNAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
#: How long a *committed* receipt keeps its replayable response envelope. This is a
#: time-only retention window: for the whole of it an exact retry REPLAYS the recorded
#: envelope, and only once it has genuinely closed (against the trusted clock) is the
#: record compacted to a tombstone and an exact retry refused as expired, never re-run.
JOURNAL_COMMITTED_RETENTION_SECONDS = 24 * 60 * 60
#: How long a compact tombstone (operation id + request hash + principal) keeps
#: refusing a retry of a reclaimed identity. A tombstone is the durable replacement
#: for "the record was dropped", so reclaim never turns a duplicate into a new effect.
JOURNAL_TOMBSTONE_SECONDS = 30 * 24 * 60 * 60
#: Count bound for the compact tombstone set. It is a *reported* backstop: a still-live
#: tombstone is NEVER dropped to honour it (dropping one is exactly what let an exact
#: retry re-execute), and reaching it never blocks a write — reclaim simply stops and
#: leaves the remaining closed-window records in place, where an exact retry is still
#: refused as expired. ``--prune-before`` remains the explicit operator override.
JOURNAL_TOMBSTONE_LIMIT = 100000
#: A wall-clock jump larger than this is treated as implausible: reclaim, compaction and
#: the read-path expiry decision refuse to act while ``now`` is this far ahead of the
#: persisted non-decreasing high-water mark, so a forward clock jump can neither reclaim
#: nor expire a live identity. The mark is never *ratcheted* toward such a clock: it
#: advances only when the clock is within this tolerance, and ``reset_high_water()`` is
#: the explicit operator recovery for a genuine correction.
JOURNAL_MAX_SKEW_SECONDS = 24 * 60 * 60
#: Largest serialized response envelope retained for replay. A larger envelope is
#: recorded by digest only, so a retry reports uncertainty instead of a truncated
#: result.
MAX_ENVELOPE_BYTES = 65536
#: Reported byte budget for one project's retained journal payload, summed over live
#: entries and tombstones (``OperationJournal.stats()['bytes']``). Revision 8 reports it
#: (``over_bytes``, ``admin.py journal``) instead of enforcing it by eviction: the byte
#: budget can never compact an in-window receipt or drop a live tombstone. The
#: admission bound is :data:`JOURNAL_LIMIT`; disk exhaustion is the platform's.
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
#: On-disk schema of the journal store. The live store is a SQLite database
#: (:data:`JOURNAL_FILENAME`); a legacy JSON document (:data:`LEGACY_JOURNAL_FILENAME`)
#: is read transparently and imported in the same transaction that creates the schema,
#: guarded by the persisted ``legacy_migrated`` marker.
JOURNAL_SCHEMA = 4
#: The live operation-journal store, one SQLite database per project directory.
JOURNAL_FILENAME = '.http-operations.sqlite3'
#: The pre-revision-7 JSON journal document, kept only as a one-time migration source.
LEGACY_JOURNAL_FILENAME = '.http-operations.json'


def journal_path(project_dir):
    """The live operation-journal store for one project directory."""
    return Path(project_dir) / JOURNAL_FILENAME

# --------------------------------------------------------------- capability model
CAP_READ = 'read'
CAP_TASKS = 'tasks.write'
CAP_CHECKPOINTS = 'checkpoints.write'
CAP_REVIEWS = 'reviews.write'
CAP_FEEDBACK = 'feedback.write'
CAP_APPROVE = 'reviews.approve'
CAP_PROJECT_ADMIN = 'project.admin'
CAP_PROJECT_CREATE = 'project.create'
CAP_ACCOUNTS_ADMIN = 'accounts.admin'

ROLES = ('viewer', 'contributor', 'owner')
RANK = {'viewer': 0, 'contributor': 1, 'owner': 2}

SCOPE_CAPABILITIES = {
    'read': frozenset({CAP_READ}),
    'tasks': frozenset({CAP_TASKS}),
    'checkpoints': frozenset({CAP_CHECKPOINTS}),
    'reviews': frozenset({CAP_REVIEWS}),
    'feedback': frozenset({CAP_FEEDBACK}),
}
CREDENTIAL_SCOPES = tuple(sorted(SCOPE_CAPABILITIES))

ROLE_CAPABILITIES = {
    'viewer': frozenset({CAP_READ}),
    'contributor': frozenset({CAP_READ, CAP_TASKS, CAP_CHECKPOINTS, CAP_REVIEWS, CAP_FEEDBACK}),
    'owner': frozenset({CAP_READ, CAP_TASKS, CAP_CHECKPOINTS, CAP_REVIEWS, CAP_FEEDBACK,
                        CAP_APPROVE, CAP_PROJECT_ADMIN}),
}
CREDENTIAL_FORBIDDEN_CAPABILITIES = frozenset({CAP_APPROVE, CAP_PROJECT_ADMIN,
                                               CAP_PROJECT_CREATE, CAP_ACCOUNTS_ADMIN})
ALL_CAPABILITIES = frozenset(ROLE_CAPABILITIES['owner'] | {CAP_PROJECT_CREATE,
                                                           CAP_ACCOUNTS_ADMIN})


class AuthorityDenied(Exception):
    """A clean, redacted authorization failure that carries its HTTP status."""

    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def deny(status, code, message, detail=None):
    return AuthorityDenied(status, code, message, detail)


def authority_request(principal, project_id, capability, *, now=None):
    """Build the durable-identity descriptor for one principal/capability decision."""
    return {'via': principal.via, 'user_id': principal.user_id,
            'credential_id': principal.credential_id,
            'session_hash': principal.session_hash,
            'project': project_id, 'capability': capability,
            'now': now}


def credential_capabilities(state, credential, issuer, project_id):
    """Scopes capped by the issuer's *current* membership role.

    A superuser issuer has global authority, so the cap is only the credential's own
    scope set; every other issuer must still be a current member, and a demotion
    immediately narrows the credential.
    """
    caps = {CAP_READ}
    for scope in credential.get('scopes') or []:
        caps |= SCOPE_CAPABILITIES.get(scope, frozenset())
    caps -= CREDENTIAL_FORBIDDEN_CAPABILITIES
    if issuer.get('superuser'):
        return frozenset(caps)
    role = state.get('memberships', {}).get(project_id, {}).get(issuer['id'])
    if role is None:
        raise deny(403, 'forbidden',
                   'The credential issuer is no longer a member of this project')
    return frozenset(cap for cap in caps if cap in ROLE_CAPABILITIES.get(role, frozenset()))


def decide(state, request, *, now=None, allow_self_user=None):
    """Authorize one capability from durable state, or raise :class:`AuthorityDenied`.

    ``state`` is the service state document; ``request`` is a descriptor from
    :func:`authority_request` (or the ``authority`` block the HTTP service sends the
    canonical endpoint). Every decision reads current membership and revocation, so
    no caller can act on a stale role or a live credential of a removed issuer.
    """
    if not isinstance(state, dict):
        raise deny(401, 'unauthenticated', 'Service authority state is unavailable')
    moment = request.get('now') if request.get('now') is not None else \
        (now if now is not None else time.time())
    via = request.get('via')
    user_id = request.get('user_id')
    project_id = request.get('project')
    capability = request.get('capability')
    if via not in ('session', 'credential') or not user_id:
        raise deny(401, 'unauthenticated', 'Authentication required')

    if via == 'credential':
        credential = state.get('credentials', {}).get(request.get('credential_id'))
        if not isinstance(credential, dict) or credential.get('revoked') or \
                credential.get('expires_at', 0) <= moment:
            raise deny(401, 'unauthenticated', 'Credential expired or revoked')
        issuer = state.get('users', {}).get(credential.get('user_id'))
        if not isinstance(issuer, dict) or issuer.get('disabled'):
            raise deny(401, 'unauthenticated', 'Authentication is no longer valid')
        if capability in (CAP_ACCOUNTS_ADMIN, CAP_PROJECT_CREATE):
            raise deny(403, 'forbidden',
                       'A worker credential cannot perform administrative operations')
        if credential.get('project_id') != project_id:
            raise deny(404, 'not_found', 'Project not found')
        if project_id not in state.get('projects', {}):
            raise deny(404, 'not_found', 'Project not found')
        if capability not in credential_capabilities(state, credential, issuer, project_id):
            raise deny(403, 'forbidden', 'Credential scope does not permit this operation')
        return {'role': 'credential'}

    if via != 'session':
        raise deny(401, 'unauthenticated', 'Authentication required')

    session = state.get('sessions', {}).get(request.get('session_hash'))
    if not isinstance(session, dict) or session.get('revoked') or \
            session.get('absolute_expires', 0) <= moment or \
            session.get('idle_expires', 0) <= moment:
        raise deny(401, 'unauthenticated', 'Session expired or revoked')
    user = state.get('users', {}).get(session.get('user_id'))
    if not isinstance(user, dict) or user.get('disabled'):
        raise deny(401, 'unauthenticated', 'Authentication is no longer valid')

    if allow_self_user is not None and user_id == allow_self_user:
        return {'role': 'self'}
    if capability == CAP_ACCOUNTS_ADMIN:
        if not user.get('superuser'):
            raise deny(403, 'forbidden', 'Superuser authority required')
        return {'role': 'superuser'}
    if capability == CAP_PROJECT_CREATE:
        return {'role': 'session'}
    if project_id not in state.get('projects', {}):
        raise deny(404, 'not_found', 'Project not found')
    if user.get('superuser'):
        return {'role': 'owner'}
    role = state.get('memberships', {}).get(project_id, {}).get(user_id)
    if role is None:
        raise deny(404, 'not_found', 'Project not found')
    if capability not in ROLE_CAPABILITIES.get(role, frozenset()):
        raise deny(403, 'forbidden', 'Project role does not permit this operation')
    return {'role': role}


# --------------------------------------------------------------- file lock
_LOCAL = threading.local()


@contextmanager
def file_lock(path, timeout=60.0, poll=0.02):
    """Cross-process exclusive lock on ``path`` (``fcntl`` or ``msvcrt``).

    Re-entrant *per thread* so nested persistence calls in one mutation cannot
    deadlock against themselves, while a second process (the canonical endpoint)
    is excluded for the whole guarded section.
    """
    held = getattr(_LOCAL, 'held', None)
    if held is None:
        held = _LOCAL.held = set()
    key = os.path.abspath(str(path))
    if key in held:
        yield
        return
    directory = os.path.dirname(key)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = open(key, 'a+b')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Timed out waiting for lock %s' % key)
                time.sleep(poll)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
    finally:
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def read_state(path):
    """Read the durable authority/state document, or raise :class:`AuthorityDenied`."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        raise deny(401, 'unauthenticated', 'Service authority state is unavailable')
    try:
        state = json.loads(text)
    except ValueError:
        raise deny(401, 'unauthenticated', 'Service authority state is unreadable')
    if not isinstance(state, dict) or state.get('schema_version') != SCHEMA_VERSION:
        raise deny(401, 'unauthenticated', 'Service authority state is unsupported')
    return state


# --------------------------------------------------------------- operation journal
class AuthorityConfig:
    """Server-side location of the live-authority document and its lock.

    The trusted HTTP service passes these to the canonical endpoint as *launch*
    arguments, exactly as it passes ``--root``. They are never read from the request
    body, so an SSH-shaped request cannot redirect the live-authority read or make
    ``file_lock`` create a lock at a caller-chosen path.
    """

    __slots__ = ('store', 'lock')

    def __init__(self, store, lock=None):
        if not isinstance(store, str) or not store:
            raise ValueError('AuthorityConfig.store must be a non-empty path')
        self.store = store
        self.lock = lock or (store + '.lock')


def principal_key(request, authority_configured=False):
    """The authenticated identity an operation identity is bound to.

    A trusted authority descriptor binds the stable principal (user, credential and
    session), not the mutable actor label. That descriptor is only trustworthy when
    the endpoint was launched with a live-authority store: on the unauthenticated SSH
    path (``authority_configured`` false) a request-supplied ``authority`` block is
    caller-controlled data, so it must not be able to assert another principal for
    the journal identity. In that case the actor label is the only available
    attribution and is used unchanged.
    """
    authority = request.get('authority') if authority_configured else None
    if isinstance(authority, dict):
        user_id = authority.get('user_id')
        if isinstance(user_id, str) and user_id:
            return 'user:%s|cred:%s|session:%s' % (
                user_id, authority.get('credential_id') or '-',
                authority.get('session_hash') or '-')
    actor = request.get('actor')
    return 'actor:%s' % (actor if isinstance(actor, str) else '')


def operation_hash(request, authority_configured=False):
    """Canonical identity of a mutation.

    Binds the project, route, actor *and* authenticated principal in addition to the
    action arguments and attachments, so the same client-supplied ``operation_id``
    from two principals is a conflict rather than a replay of another principal's
    result. The principal half is only taken from the request's authority descriptor
    when the endpoint actually has a configured authority store (see
    :func:`principal_key`).
    """
    payload = {'project': request.get('project'), 'action': request.get('action'),
               'route': request.get('route'), 'actor': request.get('actor') or '',
               'principal': principal_key(request, authority_configured),
               'args': request.get('args'),
               'attachments': request.get('attachments') or {}}
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class JournalFull(Exception):
    """The operation journal cannot accept an identity without dropping one."""


class PreEffectFailure(Exception):
    """An effect may raise this to assert the failure happened before any write.

    Only this marker (and a refusal classified as pre-effect by :class:`NativeRunner`)
    releases a reservation. Any other exception is reported as an uncertain outcome
    (``124``) and keeps the reservation, because a timeout or a mid-flow error can
    happen *after* a native write committed.
    """


#: bd invocations that cannot change canonical state. The runner injected into a
#: guarded effect is the effect's only route to the canonical store, so a failure
#: raised before the first *mutating* invocation is provably pre-effect. This is an
#: allowlist: an unrecognized verb is treated as a write, so an unknown future
#: mutation can never be mistaken for a read and released.
READ_ONLY_BD_VERBS = frozenset({'export', 'list', 'show', 'ready', 'search',
                                'count', 'dep', 'state', 'lint'})

#: Exceptions the canonical effect layer raises to refuse a request (bad attachment,
#: task mismatch, malformed payload, unknown flag). They release the identity only
#: when the runner has not attempted a write; a timeout, ``OSError`` or programming
#: error never does, because it may follow a committed native write.
REFUSAL_EXCEPTIONS = (ValueError, KeyError, TypeError, IndexError, UnicodeError)


def is_mutating_invocation(argv):
    """Whether one injected ``bin/bd`` argv can change canonical/native state."""
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return True
    verb = argv[0]
    if verb in READ_ONLY_BD_VERBS:
        return False
    if verb == 'comments':
        return len(argv) > 1 and argv[1] == 'add'
    if verb == 'merge-slot':
        return not (len(argv) > 1 and argv[1] == 'check')
    return True


class NativeRunner:
    """The endpoint's ``bin/bd`` runner, recording whether it attempted a write.

    ``endpoint.py`` builds one of these per guarded request and passes the *same*
    object to the effect and to :func:`run_guarded`. The guarded effect reaches
    canonical state only through this callable, so :attr:`attempted_write` is the
    honest boundary between a validation/refusal raised before any write and an
    exception that may follow a committed one. The wrapper is otherwise transparent:
    it returns whatever the dispatch returns and raises whatever it raises.
    """

    __slots__ = ('_dispatch', 'attempted_write', 'calls')

    def __init__(self, dispatch):
        self._dispatch = dispatch
        self.attempted_write = False
        self.calls = 0

    def __call__(self, argv):
        self.calls += 1
        if is_mutating_invocation(argv):
            self.attempted_write = True
        return self._dispatch(argv)


def read_legacy_journal(path):
    """Read a pre-revision-7 JSON journal document, or ``None`` if there is none.

    Two shapes are understood: the schema-2 ``{'high_water', 'entries',
    'tombstones'}`` document and the older flat ``operation_id -> record`` document.
    The result feeds :meth:`OperationJournal.import_document`, the one importer used
    by both the automatic migration and an explicit operator restore.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get('entries'), dict) or isinstance(data.get('tombstones'), dict):
        return {'high_water': data.get('high_water') or 0,
                'entries': {key: value for key, value in (data.get('entries') or {}).items()
                            if isinstance(value, dict)},
                'tombstones': {key: value
                               for key, value in (data.get('tombstones') or {}).items()
                               if isinstance(value, dict)}}
    entries = {key: value for key, value in data.items() if isinstance(value, dict)}
    if not entries:
        return None
    return {'high_water': 0, 'entries': entries, 'tombstones': {}}


class OperationJournal:
    """Durable ``operation_id -> response envelope`` journal for canonical mutations.

    The store is a SQLite database (stdlib ``sqlite3``) with one indexed row per
    identity. One transaction per :meth:`reserve`/:meth:`complete`/:meth:`lookup`
    replaces the old read/modify/rewrite of a whole JSON document, so a keyed
    operation costs an indexed point operation plus one small write instead of several
    full-document re-serialisations under the project coordination lock.

    The public API and every identity invariant of the previous bounded policy are
    unchanged:

    * A **committed** receipt keeps its replayable envelope for
      :data:`JOURNAL_COMMITTED_RETENTION_SECONDS`. Inside it an exact retry replays
      the recorded result; it is never re-run.
    * An **uncertain** reservation (``in_progress``/``unknown``) is the only
      long-lived record. It stays live for :data:`JOURNAL_RETENTION_SECONDS`, keeps
      failing closed, and is never evicted while its window is open.
    * A record whose window has closed is **compacted to a tombstone** (same row,
      ``state='expired'``: operation id, request hash, principal, times) rather than
      dropped, so an exact retry after reclaim is refused as expired instead of
      re-executing.
    * **Retention is by time only.** A committed receipt is never compacted, deleted or
      otherwise evicted while its replay window is open, whatever the byte or tombstone
      count is; only a genuinely closed window is compacted. The byte budget
      (:data:`MAX_JOURNAL_BYTES`) and the tombstone bound
      (:data:`JOURNAL_TOMBSTONE_LIMIT`) are *reported* by :meth:`stats` — they never
      drive eviction and never block a write.
    * The only admission bound is the live-identity count (``limit``). A journal whose
      live identities genuinely cannot hold one more raises :class:`JournalFull`
      (``124``), which rolls back so the pre-existing store is untouched and no effect
      has run. Accumulated tombstones can never cause that refusal.
    * **No live identity is ever lost to a budget.** A tombstone is removed only when
      ``reclaimed_at + tombstone_seconds`` has genuinely passed against the trusted
      clock. When the tombstone bound prevents compaction, the closed-window record
      stays in place and is still refused as expired.
    * The persisted ``high_water`` mark never decreases and is never ratcheted toward
      a jumped-forward clock: while the clock is more than
      :data:`JOURNAL_MAX_SKEW_SECONDS` ahead of it the mark is left unchanged, so
      reclaim, compaction and expiry stay refused and no live identity is dropped.
      :meth:`reset_high_water` is the explicit operator recovery for a genuine clock
      correction.
    * :meth:`trusted_now` is the ONE clock every write and every replay/expiry decision
      uses, so a +8 day jump neither stamps a future ``at`` nor expires a live receipt
      before the correction is accepted.
    * Operator overrides are :meth:`reclaim_expired` (compact closed windows) and
      :meth:`prune` (hard-remove still-live identities after reconciling; the one
      action that can let an exact retry repeat).

    A response envelope larger than :data:`MAX_ENVELOPE_BYTES` is recorded by digest
    only, so an exact retry reports uncertainty instead of returning a truncated
    result.

    The store runs in WAL mode and keeps ``live_count``/``tombstone_count``/
    ``total_bytes`` running totals in ``meta``, updated inside the same transaction as
    every mutation, so a keyed operation never scans the table to decide admission and
    latency does not grow with the store's size.

    A ``.json`` path is accepted as the pre-revision-7 compatibility path: the SQLite
    store is opened beside the document the caller named (``<name>.sqlite3``), the
    document is imported **in the same transaction that creates the schema** and guarded
    by the persisted ``legacy_migrated`` marker, and only then is it renamed to
    ``<name>.json.migrated``.
    """

    def __init__(self, path, limit=JOURNAL_LIMIT, max_envelope=MAX_ENVELOPE_BYTES,
                 max_bytes=MAX_JOURNAL_BYTES, retention=JOURNAL_RETENTION_SECONDS,
                 committed_retention=JOURNAL_COMMITTED_RETENTION_SECONDS,
                 tombstone_seconds=JOURNAL_TOMBSTONE_SECONDS,
                 tombstone_limit=JOURNAL_TOMBSTONE_LIMIT,
                 max_skew=JOURNAL_MAX_SKEW_SECONDS, legacy_path=None):
        requested = Path(path)
        if legacy_path is None and requested.suffix == '.json':
            self.legacy_path = requested
            requested = requested.with_suffix('.sqlite3')
        elif legacy_path is not None:
            self.legacy_path = Path(legacy_path)
        else:
            self.legacy_path = requested.with_name(LEGACY_JOURNAL_FILENAME)
        self.path = requested
        self.limit = limit
        self.max_envelope = max_envelope
        self.max_bytes = max_bytes
        self.retention = retention
        self.committed_retention = committed_retention
        self.tombstone_seconds = tombstone_seconds
        self.tombstone_limit = tombstone_limit
        self.max_skew = max_skew
        self._ready = False
        #: The connection of the creation transaction while ``_migrate_legacy`` runs.
        #: It keeps that hook a no-argument method (so a crash-injection probe can
        #: replace it) while the import still lands inside the schema transaction.
        self._migration_connection = None

    # -- store -----------------------------------------------------------------
    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout = 30000')
            # WAL: readers do not block the single writer and the writer does not block
            # readers, and a point operation no longer pays a rollback-journal sync.
            connection.execute('PRAGMA journal_mode = WAL')
            connection.execute('PRAGMA synchronous = FULL')
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        """One write transaction; any exception rolls the whole mutation back."""
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
            except BaseException:
                connection.execute('ROLLBACK')
                raise
            connection.execute('COMMIT')

    # The retained columns. An older store gains the revision-8 columns in place via
    # ``_ensure_columns``; the directed actor, route and the two precomputed expiry
    # timestamps make the journal introspectable without recomputing a window.
    _COLUMNS = (
        ('operation_id', 'TEXT PRIMARY KEY'),
        ('state', 'TEXT NOT NULL'),
        ('request_hash', 'TEXT'),
        ('principal', 'TEXT'),
        ('at', 'REAL NOT NULL'),
        ('envelope', 'TEXT'),
        ('envelope_sha256', 'TEXT'),
        ('envelope_omitted', 'INTEGER NOT NULL DEFAULT 0'),
        ('bytes', 'INTEGER NOT NULL DEFAULT 0'),
        ('reclaimed_at', 'REAL'),
        ('actor', 'TEXT'),
        ('route', 'TEXT'),
        ('replay_until', 'REAL'),
        ('expires_at', 'REAL'),
    )

    def _create_schema(self, connection):
        connection.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value)')
        connection.execute('CREATE TABLE IF NOT EXISTS operations (%s)'
                           % ', '.join('%s %s' % column for column in self._COLUMNS))
        connection.execute('CREATE INDEX IF NOT EXISTS operations_state_at '
                           'ON operations (state, at)')
        connection.execute('CREATE INDEX IF NOT EXISTS operations_tombstone_age '
                           'ON operations (reclaimed_at)')
        self._ensure_columns(connection)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
            (JOURNAL_SCHEMA,))

    def _ensure_columns(self, connection):
        """Add any revision-8 column an existing store is missing, then backfill it."""
        present = {row[1] for row in connection.execute('PRAGMA table_info(operations)')}
        for name, declaration in self._COLUMNS:
            if name in present:
                continue
            # PRAGMA output cannot be parameterised; the names are module constants.
            connection.execute('ALTER TABLE operations ADD COLUMN %s %s' % (name, declaration))
        connection.execute(
            'UPDATE operations SET replay_until = at + CASE state '
            "WHEN 'committed' THEN ? WHEN 'expired' THEN 0 ELSE ? END "
            'WHERE replay_until IS NULL',
            (self.committed_retention, self.retention))
        connection.execute(
            'UPDATE operations SET expires_at = CASE WHEN state = ? '
            'THEN COALESCE(reclaimed_at, at) + ? '
            "ELSE at + CASE state WHEN 'committed' THEN ? ELSE ? END END "
            'WHERE expires_at IS NULL',
            ('expired', self.tombstone_seconds, self.committed_retention, self.retention))

    def _ensure_store(self):
        """Create the schema and import a legacy JSON document in ONE transaction.

        The ``legacy_migrated`` marker is checked on every open, so a store left behind
        by a process that crashed between schema creation and the legacy import is
        completed on the next open: the marker is absent and the legacy document is
        still there. A crash at any point therefore leaves either the untouched legacy
        document (this transaction rolled back) or a fully migrated store — never an
        empty store that silently ignores the JSON.
        """
        if self._ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rename = False
        with self._transaction() as connection:
            self._create_schema(connection)
            marker = connection.execute(
                "SELECT value FROM meta WHERE key = 'legacy_migrated'").fetchone()
            if marker is None:
                previous = self._migration_connection
                self._migration_connection = connection
                try:
                    imported = self._migrate_legacy()
                finally:
                    self._migration_connection = previous
                if imported is not None:
                    connection.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES "
                        "('legacy_migrated', ?)", (JOURNAL_SCHEMA,))
                    rename = True
            self._ensure_totals(connection)
        if rename and self.legacy_path is not None and self.legacy_path.is_file():
            try:
                self.legacy_path.replace(
                    self.legacy_path.with_name(self.legacy_path.name + '.migrated'))
            except OSError:
                pass
        self._ready = True

    def _migrate_legacy(self):
        """Import the legacy JSON document inside the open creation transaction.

        Returns the number of identities imported, or ``None`` when the marker must NOT
        be set (no document, or a document that could not be read), so a later open
        retries instead of abandoning a file that is still present.
        """
        connection = self._migration_connection
        legacy = self.legacy_path
        if connection is None or legacy is None or not legacy.is_file():
            return 0
        document = read_legacy_journal(legacy)
        if document is None:
            return None
        return self._import_records(connection, document)

    @staticmethod
    def _record_bytes(operation_id, record):
        try:
            return len(json.dumps({operation_id: record}, ensure_ascii=False).encode('utf-8'))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _high_water(connection):
        row = connection.execute("SELECT value FROM meta WHERE key = 'high_water'").fetchone()
        try:
            return float(row[0]) if row is not None and row[0] is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _meta(connection, key, default=None):
        row = connection.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return default if row is None or row[0] is None else row[0]

    @staticmethod
    def _meta_number(connection, key):
        row = connection.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        try:
            return int(float(row[0])) if row is not None and row[0] is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _adjust(connection, *, live=0, tombstones=0, bytes_=0):
        """Apply a delta to the running totals (same transaction as the row write)."""
        if not (live or tombstones or bytes_):
            return
        for key, delta in (('live_count', live), ('tombstone_count', tombstones),
                           ('total_bytes', bytes_)):
            if not delta:
                continue
            current = OperationJournal._meta_number(connection, key) or 0
            connection.execute(
                'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
                (key, int(current + delta)))

    @staticmethod
    def _recount(connection):
        """Recompute the running totals from the table (repair/migration only)."""
        row = connection.execute(
            'SELECT COUNT(*), COALESCE(SUM(bytes), 0), '
            "COALESCE(SUM(CASE WHEN state = 'expired' THEN 1 ELSE 0 END), 0) "
            'FROM operations').fetchone()
        total, size, tombstones = int(row[0]), int(row[1]), int(row[2])
        live = total - tombstones
        for key, value in (('live_count', live), ('tombstone_count', tombstones),
                           ('total_bytes', size)):
            connection.execute(
                'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))
        return {'total': live, 'entries': live, 'tombstones': tombstones, 'bytes': size}
    @classmethod
    def _ensure_totals(cls, connection):
        """Establish the running totals when any of them is missing."""
        if all(cls._meta_number(connection, key) is not None
               for key in ('live_count', 'tombstone_count', 'total_bytes')):
            return False
        cls._recount(connection)
        return True

    @classmethod
    def _totals(cls, connection):
        """Running totals — one indexed meta read, never a table scan.

        The write path must stay flat as the store grows, so admission, capacity
        messages and the reported size all read the counters that the same transaction
        as every row write maintains. :meth:`_recount` (a full scan) is the repair path
        and is asserted equal to these counters by the tests.
        """
        if cls._ensure_totals(connection):
            pass
        live = cls._meta_number(connection, 'live_count') or 0
        tombstones = cls._meta_number(connection, 'tombstone_count') or 0
        size = cls._meta_number(connection, 'total_bytes') or 0
        return {'total': live, 'entries': live, 'tombstones': tombstones, 'bytes': size}

    @staticmethod
    def _row_record(row):
        record = {'state': row['state'], 'request_hash': row['request_hash'],
                  'principal': row['principal'], 'at': row['at'],
                  'envelope': json.loads(row['envelope']) if row['envelope'] else None,
                  'envelope_sha256': row['envelope_sha256']}
        if row['envelope_omitted']:
            record['envelope_omitted'] = True
        if row['reclaimed_at'] is not None:
            record['reclaimed_at'] = row['reclaimed_at']
        for column in ('actor', 'route'):
            if row[column] is not None:
                record[column] = row[column]
        for column in ('replay_until', 'expires_at'):
            if row[column] is not None:
                record[column] = row[column]
        return record

    def _upsert(self, connection, operation_id, record):
        """Write one identity row and keep the running totals in step.

        The old row (if any) is read by primary key first so the counters change by a
        delta instead of a table scan; ``at`` is stamped with the trusted clock so a
        skewed write can never store a future timestamp.
        """
        state = record.get('state')
        if state not in ('in_progress', 'committed', 'unknown', 'expired'):
            state = 'unknown'
        stored = record.get('envelope')
        envelope = None if stored is None else self._serialized(stored)
        at = self._clamp_at(connection, record.get('at'))
        normalised = {'state': state, 'request_hash': record.get('request_hash'),
                      'principal': record.get('principal'), 'at': at}
        if stored is not None:
            normalised['envelope'] = stored
        if record.get('envelope_sha256') is not None:
            normalised['envelope_sha256'] = record['envelope_sha256']
        if record.get('envelope_omitted'):
            normalised['envelope_omitted'] = True
        reclaimed = record.get('reclaimed_at')
        if state == 'expired' and reclaimed is None:
            reclaimed = at
            normalised['reclaimed_at'] = reclaimed
        window = self.window(normalised)
        replay_until = at + window
        if state == 'expired':
            expires_at = float(reclaimed) + self.tombstone_seconds
        else:
            expires_at = replay_until
        size = self._record_bytes(operation_id, normalised)
        previous = connection.execute(
            'SELECT state, bytes FROM operations WHERE operation_id = ?',
            (operation_id,)).fetchone()
        connection.execute(
            'INSERT OR REPLACE INTO operations (operation_id, state, request_hash, '
            'principal, at, envelope, envelope_sha256, envelope_omitted, bytes, '
            'reclaimed_at, actor, route, replay_until, expires_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (operation_id, state, normalised['request_hash'], normalised['principal'],
             normalised['at'], envelope, normalised.get('envelope_sha256'),
             1 if normalised.get('envelope_omitted') else 0, size,
             float(reclaimed) if reclaimed is not None else None,
             record.get('actor'), record.get('route'), replay_until, expires_at))
        was_tombstone = previous is not None and previous['state'] == 'expired'
        is_tombstone = state == 'expired'
        old_bytes = int(previous['bytes']) if previous is not None else 0
        live = (0 if is_tombstone else 1) - (0 if previous is None else
                                             (0 if was_tombstone else 1))
        tombstones = (1 if is_tombstone else 0) - (1 if was_tombstone else 0)
        self._adjust(connection, live=live, tombstones=tombstones,
                     bytes_=size - old_bytes)
        return True

    def _import_records(self, connection, document, high_water=None):
        """Write one legacy/exported document's rows inside an OPEN transaction."""
        if not isinstance(document, dict):
            return 0
        if isinstance(document.get('entries'), dict) or \
                isinstance(document.get('tombstones'), dict):
            entries = document.get('entries') or {}
            tombstones = document.get('tombstones') or {}
            mark = document.get('high_water')
        else:
            entries, tombstones, mark = document, {}, None
        records = [(key, value) for key, value in list(entries.items())
                   if isinstance(key, str) and isinstance(value, dict)]
        for key, value in list(tombstones.items()):
            if isinstance(key, str) and isinstance(value, dict):
                tombstone = dict(value)
                tombstone.setdefault('state', 'expired')
                records.append((key, tombstone))
        imported = 0
        for key, record in records:
            if self._upsert(connection, key, record):
                imported += 1
        if high_water is not None:
            mark = high_water
        try:
            requested = float(mark) if mark else 0.0
        except (TypeError, ValueError):
            requested = 0.0
        current = self._high_water(connection)
        if requested > current:
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('high_water', ?)",
                (requested,))
        elif current <= 0:
            # A legacy flat document has no mark: establish it at the current clock,
            # exactly as the previous reader did.
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('high_water', ?)",
                (time.time(),))
        return imported

    def import_document(self, document, *, high_water=None):
        """Import or merge identities into the store (migration and operator restore).

        ``document`` is either this journal's own ``{'high_water', 'entries',
        'tombstones'}`` shape or a legacy flat ``operation_id -> record`` mapping.
        Existing rows are replaced. Returns the number of identities imported; the
        import is one transaction, so it either lands whole or not at all.
        """
        self._ensure_store()
        with self._transaction() as connection:
            return self._import_records(connection, document, high_water)

    def _blank(self):
        return {'schema': JOURNAL_SCHEMA, 'high_water': self._document_high_water(),
                'entries': self._load(), 'tombstones': self._tombstones()}

    def _document_high_water(self):
        with self._connection() as connection:
            return self._high_water(connection)

    def _document(self):
        """The journal in its documented document shape (operator/inspection view)."""
        return self._blank()

    def _load(self):
        """The live entries, for callers that only need the identity records."""
        self._ensure_store()
        with self._connection() as connection:
            return {row['operation_id']: self._row_record(row) for row in connection.execute(
                "SELECT * FROM operations WHERE state != 'expired'")}

    def _tombstones(self):
        self._ensure_store()
        with self._connection() as connection:
            return {row['operation_id']: self._row_record(row) for row in connection.execute(
                "SELECT * FROM operations WHERE state = 'expired'")}

    def export_document(self):
        """Export the store in the legacy document shape (backup/audit/migration)."""
        return self._document()

    def _advance_high_water(self, connection, now):
        """Advance the non-decreasing high-water mark, never toward a jumped clock.

        While ``now`` is more than ``max_skew`` ahead of the persisted mark the mark is
        left unchanged: a forward jump must not certify itself as the new truthful
        time, so reclaim and compaction keep refusing instead of dropping a live
        identity. When the clock is within tolerance the mark advances to it, and it
        never decreases.
        """
        high = self._high_water(connection)
        if high <= 0:
            high = now
        elif now <= high + self.max_skew:
            high = max(high, now)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('high_water', ?)",
            (float(high),))
        return high

    def reset_high_water(self, now=None):
        """Operator recovery for a genuine clock correction. Returns the new mark.

        After a real forward correction (an NTP fix, a restored host) reclaim stays
        refused while the persisted mark is more than ``max_skew`` behind the real
        clock. This explicit action accepts the current clock as truthful. It removes
        no identity by itself: a subsequent ``reclaim_expired`` still compacts a
        closed window to a tombstone rather than dropping it.
        """
        moment = time.time() if now is None else now
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('high_water', ?)",
                (float(moment),))
        return moment

    @staticmethod
    def _serialized(data):
        return json.dumps(data, ensure_ascii=False)

    def _bounded(self, envelope):
        try:
            text = self._serialized(envelope)
        except (TypeError, ValueError):
            return None, hashlib.sha256(b'').hexdigest(), True
        raw = text.encode('utf-8')
        if len(raw) > self.max_envelope:
            return None, hashlib.sha256(raw).hexdigest(), True
        return envelope, hashlib.sha256(raw).hexdigest(), False

    def window(self, entry):
        """The retention window that applies to one record."""
        state = entry.get('state') if isinstance(entry, dict) else None
        if state == 'expired':
            return 0
        if state == 'committed':
            return self.committed_retention
        return self.retention

    def expired(self, entry, now=None):
        """Whether ``entry``'s idempotency receipt window has closed.

        The decision uses the SAME trusted clock as a write (:meth:`trusted_now`), so a
        forward jump that has not been accepted cannot expire a receipt that is still
        inside its window: during a +8 day jump a 60-second-old committed receipt
        replays and a live uncertain reservation still reports uncertainty (``124``).
        """
        if not isinstance(entry, dict):
            return False
        if entry.get('state') == 'expired':
            return True
        return entry.get('at', 0) + self.window(entry) <= self.trusted_now(now)

    # -- trusted clock ---------------------------------------------------------
    def _trusted_from(self, now, high):
        """``min(now, high + max_skew)``, taken at the strict end.

        While the clock is within :data:`JOURNAL_MAX_SKEW_SECONDS` of the persisted
        non-decreasing high-water mark it is accepted as-is. Once it is implausibly
        ahead the mark itself is used: the jump has not been accepted (reclaim refuses
        too), so no live identity may be expired or stamped with it. ``high <= 0`` means
        the mark is not established yet, so the clock is used unchanged.
        """
        if high is None or high <= 0:
            return now
        if now > high + self.max_skew:
            return high
        return now

    def trusted_now(self, now=None, connection=None, high=None):
        """The one clock used to stamp rows and to decide replay vs expired.

        Reading the high-water mark opens a short connection when one is not supplied;
        that is one indexed ``meta`` point read, not a table scan.
        """
        moment = time.time() if now is None else now
        if high is None:
            if connection is not None:
                high = self._high_water(connection)
            else:
                with self._connection() as opened:
                    high = self._high_water(opened)
        return self._trusted_from(moment, high)

    def _clamp_at(self, connection, at):
        """Stamp a stored ``at`` with the trusted clock (never a future timestamp)."""
        moment = time.time() if at is None else float(at)
        return self._trusted_from(moment, self._high_water(connection))

    # -- compaction / bounds ---------------------------------------------------
    def _skewed(self, high, now):
        return high > 0 and now > high + self.max_skew

    def _over(self, connection):
        """Whether the store exceeds the one bound that can refuse a write.

        Only the live-identity count can refuse: the byte budget and the tombstone
        count are reported, never enforced by eviction and never an admission trigger,
        so a store full of tombstones can never block a keyed write.
        """
        return self._totals(connection)['entries'] > self.limit

    def _capacity_message(self, connection):
        totals = self._totals(connection)
        return ('Operation journal is at capacity (%d live identities out of %d; '
                '%d tombstones, %d bytes) and cannot hold another live identity. '
                'Reclaim closed receipt windows, let tombstones age out, or raise the '
                'reviewed JOURNAL_LIMIT'
                % (totals['entries'], self.limit, totals['tombstones'], totals['bytes']))

    def _tombstone(self, connection, operation_id, now):
        """Compact one CLOSED-WINDOW record to a tombstone, unless a bound forbids it.

        The caller only passes records whose own retention window has genuinely closed
        (see :meth:`_reclaim`). Returns False, leaving the record in place, when the
        tombstone count bound is reached: the record then stays a closed-window entry
        that is still refused as expired, so no identity is lost and no write is
        blocked by the tombstones already present.
        """
        row = connection.execute('SELECT * FROM operations WHERE operation_id = ?',
                                 (operation_id,)).fetchone()
        if row is None or row['state'] == 'expired':
            return False
        totals = self._totals(connection)
        if totals['tombstones'] + 1 > self.tombstone_limit:
            return False
        record = {'state': 'expired', 'request_hash': row['request_hash'],
                  'principal': row['principal'], 'at': row['at'], 'reclaimed_at': now,
                  'actor': row['actor'], 'route': row['route']}
        size = self._record_bytes(operation_id, record)
        window = self.window({'state': 'expired'})
        expires_at = float(now) + self.tombstone_seconds
        connection.execute(
            "UPDATE operations SET state = 'expired', envelope = NULL, "
            'envelope_omitted = 0, bytes = ?, reclaimed_at = ?, replay_until = ?, '
            'expires_at = ? WHERE operation_id = ?',
            (size, now, float(row['at']) + window, expires_at, operation_id))
        delta = size - int(row['bytes'])
        self._adjust(connection, live=-1, tombstones=1, bytes_=delta)
        return True

    def _reclaim(self, connection, now, high):
        """Compact every CLOSED-WINDOW identity to a tombstone. Returns the count.

        A committed receipt is a candidate only once its own
        :data:`JOURNAL_COMMITTED_RETENTION_SECONDS` window has genuinely closed against
        the trusted clock; an in-window receipt is never a candidate, whatever the byte
        or tombstone budget is. Returns 0 without touching anything while the clock is
        implausibly far ahead of the high-water mark. Stops early when the tombstone
        bound is reached and leaves the remaining closed-window records in place, where
        an exact retry is still refused as expired, so no identity is lost.
        """
        if self._skewed(high, now):
            return 0
        trusted = self._trusted_from(now, high)
        # Compare the bare indexed column against a precomputed threshold (``at <= ?``,
        # not ``at + ? <= ?``) so the (state, at) index is an index range scan rather
        # than a full-table scan on every write.
        committed_before = trusted - self.committed_retention
        active_before = trusted - self.retention
        compacted = 0
        while True:
            rows = connection.execute(
                "SELECT operation_id FROM operations WHERE state = 'committed' "
                'AND at <= ? ORDER BY at ASC LIMIT 500', (committed_before,)).fetchall()
            rows += connection.execute(
                "SELECT operation_id FROM operations WHERE state IN ('in_progress', "
                "'unknown') AND at <= ? ORDER BY at ASC LIMIT 500",
                (active_before,)).fetchall()
            if not rows:
                break
            progress = False
            for row in rows:
                if not self._tombstone(connection, row['operation_id'], trusted):
                    return compacted
                compacted += 1
                progress = True
            if not progress:
                break
        return compacted

    def _drop_stale_tombstones(self, connection, now, high):
        """Remove only tombstones genuinely outside their refusal window by age.

        This is the *only* place an identity row is deleted by a budget-free policy. It
        uses the trusted clock, so a forward jump cannot expire one, and it is never
        used to satisfy the byte or count budget: a still-live tombstone stays.
        """
        trusted = self._trusted_from(now, high)
        # ``reclaimed_at <= ?`` keeps this an index range scan on operations_tombstone_age.
        stale_before = trusted - self.tombstone_seconds
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM operations "
            "WHERE state = 'expired' AND reclaimed_at <= ?", (stale_before,)).fetchone()
        removed, size = int(row[0]), int(row[1])
        if not removed:
            return 0
        connection.execute(
            "DELETE FROM operations WHERE state = 'expired' AND reclaimed_at <= ?",
            (stale_before,))
        self._adjust(connection, tombstones=-removed, bytes_=-size)
        return removed

    def _fit(self, connection, now, high):
        """Reclaim what time has genuinely closed; report whether a write may proceed.

        There is no eviction path for a live identity here. The only thing that can make
        this return False is the live-identity count: the byte budget and the tombstone
        count are reported by :meth:`stats`, never enforced by compacting an in-window
        receipt or dropping a tombstone.
        """
        if not self._skewed(high, now):
            self._reclaim(connection, now, high)
            self._drop_stale_tombstones(connection, now, high)
        return not self._over(connection)

    def lookup(self, operation_id):
        """One indexed point lookup: the live record or its tombstone, else ``None``."""
        self._ensure_store()
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM operations WHERE operation_id = ?',
                                     (operation_id,)).fetchone()
        return self._row_record(row) if row is not None else None

    # -- mutations -------------------------------------------------------------
    def reserve(self, operation_id, request_hash, principal):
        """Reserve a new identity, compacting terminal receipts under pressure.

        Raises :class:`JournalFull` only when the live identities plus this new one
        genuinely exceed the live-identity bound. No live identity is ever evicted to
        make room, so the only thing that can refuse is a journal that is really full.
        The transaction is rolled back, so the pre-existing store is left exactly as it
        was and no effect has run.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError('operation_id must be a non-empty string')
        self._ensure_store()
        with self._transaction() as connection:
            now = time.time()
            high = self._high_water(connection)
            trusted = self._trusted_from(now, high)
            self._upsert(connection, operation_id, {
                'state': 'in_progress', 'request_hash': request_hash,
                'principal': principal, 'at': trusted,
                'actor': self._actor, 'route': self._route})
            high = self._advance_high_water(connection, now)
            if not self._fit(connection, now, high):
                raise JournalFull(self._capacity_message(connection))

    def complete(self, operation_id, envelope, request_hash, principal):
        """Record the committed response envelope for an identity (one transaction)."""
        self._ensure_store()
        with self._transaction() as connection:
            now = time.time()
            trusted = self._trusted_from(now, self._high_water(connection))
            stored, digest, omitted = self._bounded(envelope)
            record = {'state': 'committed', 'request_hash': request_hash,
                      'principal': principal, 'at': trusted, 'envelope': stored,
                      'envelope_sha256': digest,
                      'actor': self._actor, 'route': self._route}
            if omitted:
                record['envelope_omitted'] = True
            self._upsert(connection, operation_id, record)
            high = self._advance_high_water(connection, now)
            if not self._fit(connection, now, high):
                raise JournalFull(self._capacity_message(connection))

    #: The directed actor label and canonical route of the guarded mutation. The
    #: journal created by :func:`run_guarded` carries them so every row records which
    #: actor and route the identity was directed at, not only the principal digest.
    _actor = None
    _route = None

    def mark_unknown(self, operation_id):
        """Keep a reservation whose outcome is not known to be pre-effect."""
        self._ensure_store()
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM operations WHERE operation_id = ?',
                                     (operation_id,)).fetchone()
            if row is None or row['state'] == 'expired':
                return
            now = time.time()
            record = self._row_record(row)
            record['state'] = 'unknown'
            record['at'] = self._trusted_from(now, self._high_water(connection))
            self._upsert(connection, operation_id, record)
            self._advance_high_water(connection, now)

    def discard(self, operation_id):
        """Release a reservation that is proven pre-effect."""
        self._ensure_store()
        with self._transaction() as connection:
            row = connection.execute(
                'SELECT state, bytes FROM operations WHERE operation_id = ?',
                (operation_id,)).fetchone()
            connection.execute('DELETE FROM operations WHERE operation_id = ?',
                               (operation_id,))
            if row is not None:
                tombstone = row['state'] == 'expired'
                self._adjust(connection, live=0 if tombstone else -1,
                             tombstones=-1 if tombstone else 0,
                             bytes_=-int(row['bytes']))
            self._advance_high_water(connection, time.time())

    def reclaim_expired(self, now=None):
        """Compact every closed-window identity to a tombstone. Returns the count.

        This is the automatic maintenance step on every write and the operator action
        for a deployment that wants to compact the journal before the limit is reached.
        Only a record whose OWN window has genuinely closed is a candidate: an in-window
        committed receipt is never compacted, whatever the byte budget says. Unlike
        :meth:`prune` it never lets an exact retry repeat: the tombstone keeps refusing
        the reclaimed identity as expired. Compaction stops at the tombstone bound and
        leaves the remaining closed-window records in place, where they are still
        refused as expired, so no identity is lost.
        """
        self._ensure_store()
        moment = time.time() if now is None else now
        with self._transaction() as connection:
            high = self._high_water(connection)
            removed = self._reclaim(connection, moment, high)
            self._drop_stale_tombstones(connection, moment, high)
        return removed

    def prune(self, before):
        """Hard-remove identities last touched before ``before`` (epoch seconds).

        This is the explicit operator override for a still-live identity (for example
        after reconciling a stuck unknown). It is deliberately the *only* action that
        removes a record without a tombstone, so after a prune an exact retry of the
        pruned operation can repeat the effect: reconcile canonical state first, then
        have the client use a fresh ``operation_id``. Returns the count removed.
        """
        self._ensure_store()
        with self._transaction() as connection:
            live_row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM operations "
                "WHERE state != 'expired' AND at < ?", (before,)).fetchone()
            tomb_row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM operations "
                "WHERE state = 'expired' AND at < ?", (before,)).fetchone()
            removed = int(live_row[0])
            connection.execute(
                "DELETE FROM operations WHERE state != 'expired' AND at < ?", (before,))
            connection.execute(
                "DELETE FROM operations WHERE state = 'expired' AND at < ?", (before,))
            self._adjust(connection, live=-removed, tombstones=-int(tomb_row[0]),
                         bytes_=-(int(live_row[1]) + int(tomb_row[1])))
        return removed

    def stats(self, now=None):
        """Operator-visible size and state of the store — report, never evict.

        Every number is authoritative: the by-state counts and byte sums come from the
        table (this is an operator/admin call, not the write path), the running totals
        are cross-checked against a recount, and ``live_bytes``/``tombstone_bytes`` plus
        ``over_bytes``/``over_tombstones``/``over_limit`` show where the store stands
        against the configured bounds. ``journal_mode`` reports the SQLite mode (``wal``)
        and ``integrity`` the SQLite integrity check.
        """
        self._ensure_store()
        moment = time.time() if now is None else now
        with self._connection() as connection:
            high = self._high_water(connection)
            trusted = self._trusted_from(moment, high)
            states = {'in_progress': 0, 'committed': 0, 'unknown': 0}
            live_bytes = 0
            tombstone_bytes = 0
            tombstones = 0
            expired = 0
            for row in connection.execute('SELECT state, at, bytes FROM operations'):
                size = int(row['bytes'] or 0)
                if row['state'] == 'expired':
                    tombstones += 1
                    tombstone_bytes += size
                    continue
                if row['state'] in states:
                    states[row['state']] += 1
                live_bytes += size
                if self.expired({'state': row['state'], 'at': row['at']}, trusted):
                    expired += 1
            live = states['in_progress'] + states['committed'] + states['unknown']
            self._ensure_totals(connection)
            running = self._totals(connection)
            totals_bytes = live_bytes + tombstone_bytes
            recount = {'total': live, 'entries': live, 'tombstones': tombstones,
                       'bytes': totals_bytes}
            mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
            skewed = self._skewed(high, moment)
        totals_bytes = live_bytes + tombstone_bytes
        return {'total': live, 'limit': self.limit,
                'retention': self.retention,
                'committed_retention': self.committed_retention,
                'tombstone_seconds': self.tombstone_seconds,
                'expired': expired, 'reclaimable': 0 if skewed else expired,
                'states': states, 'tombstones': tombstones,
                'tombstone_limit': self.tombstone_limit,
                'high_water': high, 'clock_skewed': skewed,
                'bytes': totals_bytes,
                'live_bytes': live_bytes, 'tombstone_bytes': tombstone_bytes,
                'limit_bytes': self.max_bytes,
                'live': live,
                'over_limit': live > self.limit,
                'over_bytes': totals_bytes > self.max_bytes,
                'over_tombstones': tombstones > self.tombstone_limit,
                'running_totals': running,
                'recount': recount,
                'running_totals_match': running == recount,
                'journal_mode': mode,
                'file_bytes': self.path.stat().st_size if self.path.exists() else 0,
                'schema': JOURNAL_SCHEMA}


def _envelope(code, stderr='', **extra):
    payload = {'returncode': code, 'stdout': '', 'stderr': stderr}
    payload.update(extra)
    return payload


def run_guarded(request, journal_path, effect, authority_config=None,
                require_authority=False, runner=None, journal_options=None):
    """Run one canonical mutation through the live-authority and identity boundary.

    Returns the canonical response envelope (the same shape ``endpoint.py`` emits).

    * The live-authority store and lock come only from ``authority_config`` (the
      server-side launch configuration). Any ``store``/``lock`` fields in the request
      are ignored, so a hostile request cannot choose the file that is read or the
      lock that is created. Without a configuration the request's authority block is
      ignored entirely, which is the SSH compatibility path: it can neither
      authenticate nor assert a principal for the operation identity.
    * ``require_authority`` marks the trusted HTTP service's mutation path: a request
      that omits the descriptor is refused rather than silently skipping the check.
    * ``runner`` is the endpoint's :class:`NativeRunner`, the effect's only route to
      native state. It distinguishes a validation/refusal raised before any write
      (rc=2, real message, identity released) from an exception that may follow a
      committed write (rc=124, identity held). Without one, only an explicit
      :class:`PreEffectFailure` releases the reservation.
    * ``journal_options`` are optional :class:`OperationJournal` constructor overrides
      (limits, retention, skew allowance) for tests and operator tooling; the
      canonical endpoint passes none, so production uses the module defaults.
    * The authority lock is held across re-validation and the effect, so an HTTP-side
      revocation that committed first is observed here and the effect never runs.
    """
    authority = request.get('authority')
    if not isinstance(authority, dict):
        authority = None
    if require_authority and authority_config is None:
        return _envelope(126, stderr='Live authority is not configured\n',
                         authority_status=401)
    if require_authority and authority is None:
        return _envelope(126, stderr='Live authority descriptor required\n',
                         authority_status=401)

    trusted = authority_config is not None
    lock_path = authority_config.lock if authority_config is not None else None
    context = file_lock(lock_path) if lock_path else _no_lock()
    with context:
        if trusted and authority is not None:
            descriptor = {key: value for key, value in authority.items()
                          if key not in ('store', 'lock')}
            try:
                state = read_state(authority_config.store)
                decide(state, descriptor)
            except AuthorityDenied as denied:
                return _envelope(126, stderr='%s\n' % denied.message,
                                 authority_status=denied.status)
        operation_id = request.get('operation_id')
        journal = (OperationJournal(journal_path, **(journal_options or {}))
                   if operation_id else None)
        # The principal half of the identity comes from the request descriptor only
        # when this launch actually has a trusted authority store.
        principal = principal_key(request, trusted)
        if journal is not None:
            # The directed actor label and canonical route are recorded with the row, so
            # the journal reports what an identity was aimed at, not only its principal.
            actor = request.get('actor')
            route = request.get('route')
            journal._actor = actor if isinstance(actor, str) and actor else None
            journal._route = route if isinstance(route, str) and route else None
            request_hash = operation_hash(request, trusted)
            entry = journal.lookup(operation_id)
            if entry is not None:
                if entry.get('principal') != principal:
                    return _envelope(2, stderr='Operation identity belongs to a different '
                                               'principal\n')
                if entry.get('request_hash') != request_hash:
                    return _envelope(2, stderr='Operation identity reused with a different '
                                               'request\n')
                if journal.expired(entry):
                    # Same principal and request, but the receipt window has closed (or
                    # the record is a tombstone from an earlier reclaim): the identity is
                    # refused as expired rather than replayed or re-run.
                    return _envelope(2, stderr='Operation identity expired: its idempotency '
                                               'receipt window has closed. Reconcile '
                                               'canonical state and use a fresh '
                                               'operation_id.\n')
                if entry.get('state') == 'committed':
                    if entry.get('envelope_omitted') or entry.get('envelope') is None:
                        return _envelope(124, stderr='Operation is committed but its response '
                                                     'exceeded the retention bound; reconcile '
                                                     'canonical state before retrying.\n')
                    return entry.get('envelope')
                # The prior attempt reserved the identity and its outcome is unknown:
                # preserve uncertainty rather than repeating a possibly committed effect.
                return _envelope(124, stderr='Operation identity reserved; outcome unknown. '
                                             'Reconcile canonical state before retrying.\n')
            try:
                journal.reserve(operation_id, request_hash, principal)
            except JournalFull as full:
                return _envelope(124, stderr='%s; no effect was attempted. Reconcile state '
                                             'or reclaim the journal first.\n' % full)
        try:
            envelope = effect()
        except PreEffectFailure:
            # The effect proved it failed before writing anything, so the identity can
            # be released for a clean retry.
            if journal is not None:
                journal.discard(operation_id)
            raise
        except Exception as error:
            if runner is not None and not runner.attempted_write and \
                    isinstance(error, REFUSAL_EXCEPTIONS):
                # A refusal raised before the effect's first native write cannot have
                # changed canonical state: release the identity and let the real
                # validation error reach the caller with its original message.
                if journal is not None:
                    journal.discard(operation_id)
                raise
            # A timeout or mid-flow error can follow a committed native write. Keep
            # the reservation and report uncertainty; never discard on an exception
            # that is not proven pre-effect.
            if journal is not None:
                journal.mark_unknown(operation_id)
            return _envelope(124, stderr='Effect raised after the reservation (%s: %s); '
                                         'outcome unknown. Reconcile canonical state before '
                                         'retrying.\n' % (type(error).__name__, error))
        if journal is not None and isinstance(envelope, dict):
            code = envelope.get('returncode')
            if code in (None, 0):
                try:
                    journal.complete(operation_id, envelope, operation_hash(request, trusted),
                                     principal)
                except JournalFull:
                    journal.mark_unknown(operation_id)
                    return _envelope(124, stderr='Operation committed but the journal could '
                                                 'not retain the response; reconcile canonical '
                                                 'state before retrying.\n')
            elif code == 2:
                journal.discard(operation_id)
            else:
                # Non-validation failure: the effect may have committed; keep the
                # reservation so a retry reports uncertainty instead of duplicating.
                journal.mark_unknown(operation_id)
        return envelope


@contextmanager
def _no_lock():
    yield
