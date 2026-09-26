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
  retention is a bounded, tombstoned receipt policy: a committed receipt is
  replayable for a short window and then compacted to a durable tombstone that still
  refuses an exact retry, an uncertain reservation stays live (and fails closed) for
  the long window, and a clock jump larger than the skew allowance stops reclaim from
  dropping a live identity. Ordinary load therefore never refuses a write, while a
  reclaimed identity is refused as expired rather than re-executed.

Nothing here imports ``fcntl`` at module import time, so the same module imports on a
Windows workstation and a Linux office host.
"""
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# ------------------------------------------------------- operation journal bounds
#: Hard capacity of one project's *live* operation journal, in entries. It is a
#: safety backstop, not the ordinary bound: a closed receipt window is compacted to a
#: tombstone, and under capacity pressure the oldest terminal (committed) receipts
#: are compacted before a write is refused, so ordinary load never refuses a write.
#: Only genuinely live reservations (``in_progress``/``unknown``) fail closed with
#: ``124`` when the journal cannot also hold them.
JOURNAL_LIMIT = 100000
#: How long an *uncertain* reservation (``in_progress``/``unknown``) stays live. This
#: is the long-lived window: such an identity may correspond to a committed native
#: effect whose outcome was never observed, so it is retained (and an exact retry
#: reports uncertainty) until an operator reconciles it or this window closes.
JOURNAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
#: How long a *committed* receipt keeps its replayable response envelope. It is
#: deliberately far shorter than the uncertain window: a lost-response retry happens
#: within seconds or minutes, while holding every committed envelope for a week is
#: what let a busy project reach the entry limit. Once this window closes the record
#: is compacted to a tombstone and an exact retry is refused as expired, never re-run.
JOURNAL_COMMITTED_RETENTION_SECONDS = 24 * 60 * 60
#: How long a compact tombstone (operation id + request hash + principal) keeps
#: refusing a retry of a reclaimed identity. A tombstone is the durable replacement
#: for "the record was dropped", so reclaim no longer turns a duplicate into a new
#: effect.
JOURNAL_TOMBSTONE_SECONDS = 30 * 24 * 60 * 60
#: Count bound for the compact tombstone set; the oldest tombstones are dropped past
#: it. Generous relative to the entry bound because a tombstone is ~an order of
#: magnitude smaller than a receipt.
JOURNAL_TOMBSTONE_LIMIT = 20000
#: A wall-clock jump larger than this is treated as implausible: reclaim refuses to
#: drop an identity while ``now`` is this far ahead of the persisted non-decreasing
#: high-water mark, so a forward clock jump cannot reclaim a live identity.
JOURNAL_MAX_SKEW_SECONDS = 24 * 60 * 60
#: Largest serialized response envelope retained for replay. A larger envelope is
#: recorded by digest only, so a retry reports uncertainty instead of a truncated
#: result.
MAX_ENVELOPE_BYTES = 65536
#: Largest serialized journal document accepted after a mutation.
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
#: On-disk schema of the journal document (entries + tombstones + high-water mark).
#: A legacy flat ``operation_id -> record`` document is read transparently and
#: rewritten in this shape on the next mutation.
JOURNAL_SCHEMA = 2

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


class OperationJournal:
    """Durable ``operation_id -> response envelope`` journal for canonical mutations.

    The caller holds the project coordination lock, so a plain read/modify/write of
    one JSON document is serialized; the write is an atomic replace.

    One coherent, bounded policy replaces both the old hard count lockout (a busy
    project refused writes above 2000 live identities) and the old "reclaim drops the
    record" path (a reclaimed retry re-ran its effect):

    * A **committed** receipt keeps its replayable envelope for
      :data:`JOURNAL_COMMITTED_RETENTION_SECONDS`. Inside it an exact retry replays
      the recorded result; it is never re-run.
    * An **uncertain** reservation (``in_progress``/``unknown``) is the only
      long-lived record. It stays live for :data:`JOURNAL_RETENTION_SECONDS`, keeps
      failing closed, and is never evicted while its window is open.
    * A record whose window has closed is **compacted to a tombstone** (operation id,
      request hash, principal, times) rather than dropped, so an exact retry after
      reclaim is refused as expired instead of re-executing.
    * Under capacity pressure the oldest terminal (committed) receipts are compacted
      to tombstones first, so ordinary load never refuses a write. Only a journal
      whose live uncertain reservations cannot be held raises :class:`JournalFull`
      (``124``): that is the genuinely fail-closed case.
    * The persisted ``high_water`` mark never decreases. Reclaim refuses to drop an
      identity while the wall clock is more than :data:`JOURNAL_MAX_SKEW_SECONDS`
      ahead of it, so a forward clock jump cannot reclaim a live receipt.
    * Operator overrides are :meth:`reclaim_expired` (compact closed windows) and
      :meth:`prune` (hard-remove still-live identities after reconciling; the one
      action that can let an exact retry repeat).

    A response envelope larger than :data:`MAX_ENVELOPE_BYTES` is recorded by digest
    only, so an exact retry reports uncertainty instead of returning a truncated
    result.
    """

    def __init__(self, path, limit=JOURNAL_LIMIT, max_envelope=MAX_ENVELOPE_BYTES,
                 max_bytes=MAX_JOURNAL_BYTES, retention=JOURNAL_RETENTION_SECONDS,
                 committed_retention=JOURNAL_COMMITTED_RETENTION_SECONDS,
                 tombstone_seconds=JOURNAL_TOMBSTONE_SECONDS,
                 tombstone_limit=JOURNAL_TOMBSTONE_LIMIT,
                 max_skew=JOURNAL_MAX_SKEW_SECONDS):
        self.path = Path(path)
        self.limit = limit
        self.max_envelope = max_envelope
        self.max_bytes = max_bytes
        self.retention = retention
        self.committed_retention = committed_retention
        self.tombstone_seconds = tombstone_seconds
        self.tombstone_limit = tombstone_limit
        self.max_skew = max_skew

    # -- document shape --------------------------------------------------------
    @staticmethod
    def _blank():
        return {'schema': JOURNAL_SCHEMA, 'high_water': 0.0,
                'entries': {}, 'tombstones': {}}

    def _document(self):
        """The journal document, reading a legacy flat schema-1 file transparently."""
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return self._blank()
        if not isinstance(data, dict):
            return self._blank()
        if data.get('schema') == JOURNAL_SCHEMA and isinstance(data.get('entries'), dict):
            document = self._blank()
            document['high_water'] = float(data.get('high_water') or 0) or time.time()
            document['entries'] = {key: value for key, value in data['entries'].items()
                                   if isinstance(value, dict)}
            document['tombstones'] = {
                key: value for key, value in (data.get('tombstones') or {}).items()
                if isinstance(value, dict)}
            return document
        # Legacy flat document (schema 1): operation_id -> record, no high-water mark.
        # Treat the first read as establishing the mark at the current clock.
        document = self._blank()
        document['high_water'] = time.time()
        document['entries'] = {key: value for key, value in data.items()
                               if isinstance(value, dict)}
        return document

    def _load(self):
        """The live entries, for callers that only need the identity records."""
        return self._document()['entries']

    def _save(self, document):
        self._advance_high_water(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps(document), encoding='utf-8')
        temporary.replace(self.path)

    def _advance_high_water(self, document):
        """Advance the non-decreasing high-water mark by at most ``max_skew``.

        Bounding the step means a single forward clock jump cannot certify itself as
        the new truthful time: reclaim keeps refusing until the clock comes back near
        the mark or ordinary writes slowly re-establish it.
        """
        now = time.time()
        high = float(document.get('high_water') or 0)
        if high <= 0:
            document['high_water'] = now
        else:
            document['high_water'] = max(high, min(now, high + self.max_skew))

    @staticmethod
    def _serialized(data):
        return json.dumps(data, ensure_ascii=False)

    def _document_bytes(self, document):
        try:
            return len(self._serialized(document).encode('utf-8'))
        except (TypeError, ValueError):
            return self.max_bytes + 1

    def _over(self, document):
        return (len(document['entries']) > self.limit or
                self._document_bytes(document) > self.max_bytes)

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
        """Whether ``entry``'s idempotency receipt window has closed."""
        if not isinstance(entry, dict):
            return False
        if entry.get('state') == 'expired':
            return True
        moment = time.time() if now is None else now
        return entry.get('at', 0) + self.window(entry) <= moment

    # -- compaction / bounds ---------------------------------------------------
    def _skewed(self, document, now):
        high = float(document.get('high_water') or 0)
        return high > 0 and now > high + self.max_skew

    def _trusted_now(self, document, now):
        """``now`` clamped to the high-water mark plus the skew allowance.

        Age-based drops use this so a forward clock jump cannot expire a record (or
        drop a tombstone) whose real age is still inside its window.
        """
        high = float(document.get('high_water') or 0)
        if high <= 0:
            return now
        return min(now, high + self.max_skew)

    def _tombstone(self, document, key, record, now):
        document['tombstones'][key] = {
            'state': 'expired', 'request_hash': record.get('request_hash'),
            'principal': record.get('principal'), 'at': record.get('at', now),
            'reclaimed_at': now}
        document['entries'].pop(key, None)

    def _reclaim(self, document, now):
        """Compact every closed-window identity to a tombstone. Returns the count.

        Returns 0 without touching anything while the clock is implausibly far ahead
        of the high-water mark, so a forward jump cannot drop a live identity.
        """
        if self._skewed(document, now):
            return 0
        entries = document['entries']
        stale = [key for key, value in entries.items()
                 if isinstance(value, dict) and self.expired(value, now)]
        for key in stale:
            self._tombstone(document, key, entries[key], now)
        return len(stale)

    def _compact(self, document, now, protect=None):
        """Compact the oldest terminal receipt to a tombstone. Never a live one.

        Refuses under an implausible clock: a forward jump plus capacity pressure must
        not compact a committed receipt that is still inside its replay window.
        """
        if self._skewed(document, now):
            return False
        entries = document['entries']
        candidates = [key for key, value in entries.items()
                      if key != protect and isinstance(value, dict)
                      and value.get('state') == 'committed']
        if not candidates:
            return False
        oldest = min(candidates, key=lambda key: entries[key].get('at', 0))
        self._tombstone(document, oldest, entries[oldest], now)
        return True

    @staticmethod
    def _drop_oldest_tombstone(tombstones):
        oldest = min(tombstones, key=lambda key: tombstones[key].get('reclaimed_at', 0))
        tombstones.pop(oldest, None)

    def _bound_tombstones(self, document, now):
        """Drop only stale/over-limit tombstones. Returns the count dropped."""
        tombstones = document['tombstones']
        dropped = 0
        trusted = self._trusted_now(document, now)
        stale = [key for key, value in tombstones.items()
                 if isinstance(value, dict) and
                 value.get('reclaimed_at', 0) + self.tombstone_seconds <= trusted]
        for key in stale:
            tombstones.pop(key, None)
            dropped += 1
        while len(tombstones) > self.tombstone_limit:
            self._drop_oldest_tombstone(tombstones)
            dropped += 1
        if not tombstones:
            return dropped
        current = self._document_bytes(document)
        if current <= self.max_bytes:
            return dropped
        sizes = []
        for key, value in tombstones.items():
            try:
                size = len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            except (TypeError, ValueError):
                size = 0
            sizes.append((value.get('reclaimed_at', 0), key, size))
        sizes.sort()
        for _, key, size in sizes:
            if current <= self.max_bytes:
                break
            tombstones.pop(key, None)
            current -= size + len(key) + 6
            dropped += 1
        return dropped

    def _fit(self, document, now, protect=None):
        """Bring the document back inside its bounds without losing an identity."""
        self._reclaim(document, now)
        self._bound_tombstones(document, now)
        guard = 0
        while self._over(document) and guard <= len(document['entries']):
            if not self._compact(document, now, protect):
                break
            guard += 1
            self._bound_tombstones(document, now)
        return not self._over(document)

    def lookup(self, operation_id):
        document = self._document()
        entry = document['entries'].get(operation_id)
        if isinstance(entry, dict):
            return entry
        tombstone = document['tombstones'].get(operation_id)
        return tombstone if isinstance(tombstone, dict) else None

    # -- mutations -------------------------------------------------------------
    def reserve(self, operation_id, request_hash, principal):
        """Reserve a new identity, compacting terminal receipts under pressure.

        Raises :class:`JournalFull` only when live uncertain reservations plus this
        new one cannot be held inside the bounds; committed receipts and closed
        windows never cause a refusal by themselves.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError('operation_id must be a non-empty string')
        document = self._document()
        now = time.time()
        document['entries'][operation_id] = {
            'state': 'in_progress', 'request_hash': request_hash,
            'principal': principal, 'at': now}
        if not self._fit(document, now, protect=operation_id):
            raise JournalFull('Operation journal is at capacity (%d live identities)'
                              % self.limit)
        self._save(document)

    def complete(self, operation_id, envelope, request_hash, principal):
        document = self._document()
        now = time.time()
        stored, digest, omitted = self._bounded(envelope)
        record = {'state': 'committed', 'request_hash': request_hash,
                  'principal': principal, 'at': now, 'envelope': stored,
                  'envelope_sha256': digest}
        if omitted:
            record['envelope_omitted'] = True
        document['entries'][operation_id] = record
        fitted = self._fit(document, now, protect=operation_id)
        if not fitted and not omitted:
            # Keep the identity and its digest, drop only the oversized body.
            record['envelope'] = None
            record['envelope_omitted'] = True
            fitted = self._fit(document, now, protect=operation_id)
        if not fitted:
            raise JournalFull('Operation journal is at capacity (%d live identities)'
                              % self.limit)
        self._save(document)

    def mark_unknown(self, operation_id):
        """Keep a reservation whose outcome is not known to be pre-effect."""
        document = self._document()
        record = document['entries'].get(operation_id)
        if isinstance(record, dict):
            record['state'] = 'unknown'
            record['at'] = time.time()
            self._save(document)

    def discard(self, operation_id):
        document = self._document()
        if document['entries'].pop(operation_id, None) is not None:
            self._save(document)

    def reclaim_expired(self, now=None):
        """Compact every closed-window identity to a tombstone. Returns the count.

        This is automatic under capacity pressure; it is also the operator action for
        a deployment that wants to compact the journal before the limit is reached.
        Unlike :meth:`prune` it never lets an exact retry repeat: the tombstone keeps
        refusing the reclaimed identity as expired.
        """
        document = self._document()
        moment = time.time() if now is None else now
        removed = self._reclaim(document, moment)
        dropped = self._bound_tombstones(document, moment)
        if removed or dropped:
            self._save(document)
        return removed

    def prune(self, before):
        """Hard-remove identities last touched before ``before`` (epoch seconds).

        This is the explicit operator override for a still-live identity (for example
        after reconciling a stuck unknown). It is deliberately the *only* action that
        removes a record without a tombstone, so after a prune an exact retry of the
        pruned operation can repeat the effect: reconcile canonical state first, then
        have the client use a fresh ``operation_id``. Returns the count removed.
        """
        document = self._document()
        removed = [key for key, value in document['entries'].items()
                   if isinstance(value, dict) and value.get('at', 0) < before]
        for key in removed:
            document['entries'].pop(key, None)
        stales = [key for key, value in document['tombstones'].items()
                  if isinstance(value, dict) and value.get('at', 0) < before]
        for key in stales:
            document['tombstones'].pop(key, None)
        if removed or stales:
            self._save(document)
        return len(removed)

    def stats(self, now=None):
        document = self._document()
        entries = document['entries']
        moment = time.time() if now is None else now
        states = {'in_progress': 0, 'committed': 0, 'unknown': 0}
        expired = 0
        for value in entries.values():
            if isinstance(value, dict) and value.get('state') in states:
                states[value['state']] += 1
            if isinstance(value, dict) and self.expired(value, moment):
                expired += 1
        skewed = self._skewed(document, moment)
        return {'total': len(entries), 'limit': self.limit,
                'retention': self.retention,
                'committed_retention': self.committed_retention,
                'expired': expired, 'reclaimable': 0 if skewed else expired,
                'states': states, 'tombstones': len(document['tombstones']),
                'tombstone_limit': self.tombstone_limit,
                'high_water': document['high_water'], 'clock_skewed': skewed,
                'bytes': self.path.stat().st_size if self.path.exists() else 0}


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
