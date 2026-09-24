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
  retention is a timed receipt window: inside it an exact retry replays or reports
  uncertainty, outside it the retry is refused as expired and the record becomes
  reclaimable, so a busy project cannot be locked out at the entry limit.

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
#: Hard capacity of one project's *live* operation journal. An identity whose
#: idempotency receipt window is still open is never evicted: at capacity a new
#: guarded mutation fails closed (``124``) rather than silently dropping a live
#: identity and letting a later retry repeat an effect. Once a window has closed the
#: identity is *expired*: a retry of it is refused as expired (never replayed and
#: never re-run), and the record is reclaimable so a busy project cannot be locked
#: out permanently. Recovery is automatic (reclaim on capacity pressure) and manual
#: (:meth:`OperationJournal.reclaim_expired` / :meth:`prune` through ``admin.py
#: journal``).
JOURNAL_LIMIT = 2000
#: How long an operation identity stays a replayable idempotency receipt, measured
#: from its last touch. Inside the window an exact retry replays a committed result
#: or reports uncertainty, and a not-yet-committed identity keeps its reservation.
#: Outside it the identity has expired: the endpoint refuses it as expired and the
#: record may be reclaimed. The window is the documented limit of the idempotency
#: guarantee; an operator can shorten it per command with ``admin.py journal
#: --retention``.
JOURNAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
#: Largest serialized response envelope retained for replay. A larger envelope is
#: recorded by digest only, so a retry reports uncertainty instead of a truncated
#: result.
MAX_ENVELOPE_BYTES = 65536
#: Largest serialized journal document accepted after a mutation.
MAX_JOURNAL_BYTES = 8 * 1024 * 1024

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

    Retention is a timed, documented boundary, not an implicit count eviction:

    * An identity stays a replayable idempotency receipt for
      :data:`JOURNAL_RETENTION_SECONDS` after its last touch. Inside that window an
      exact retry replays a committed envelope or reports uncertainty; it is never
      re-run.
    * Outside the window the identity is *expired*: :meth:`expired` is true, the
      endpoint refuses a retry of it as expired (never replayed, never re-run), and
      the record may be reclaimed. Reclamation happens automatically only under
      capacity pressure (:meth:`reserve`) and explicitly through
      :meth:`reclaim_expired` or :meth:`prune`, so an operator always has a
      deterministic recovery path.
    * Capacity for *live* identities still fails closed: if the journal is full of
      unexpired identities a new reservation raises :class:`JournalFull` and the
      guarded mutation returns ``124``.

    A response envelope larger than :data:`MAX_ENVELOPE_BYTES` is recorded by digest
    only, so an exact retry reports uncertainty instead of returning a truncated
    result.
    """

    def __init__(self, path, limit=JOURNAL_LIMIT, max_envelope=MAX_ENVELOPE_BYTES,
                 max_bytes=MAX_JOURNAL_BYTES, retention=JOURNAL_RETENTION_SECONDS):
        self.path = Path(path)
        self.limit = limit
        self.max_envelope = max_envelope
        self.max_bytes = max_bytes
        self.retention = retention

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(self.path)

    @staticmethod
    def _serialized(data):
        return json.dumps(data, ensure_ascii=False)

    def _enforce(self, data):
        if len(data) > self.limit:
            raise JournalFull('Operation journal is at capacity (%d entries)' % self.limit)
        try:
            text = self._serialized(data)
        except (TypeError, ValueError):
            raise JournalFull('Operation journal is not serializable')
        if len(text.encode('utf-8')) > self.max_bytes:
            raise JournalFull('Operation journal exceeds its byte budget')

    def _bounded(self, envelope):
        try:
            text = self._serialized(envelope)
        except (TypeError, ValueError):
            return None, hashlib.sha256(b'').hexdigest(), True
        raw = text.encode('utf-8')
        if len(raw) > self.max_envelope:
            return None, hashlib.sha256(raw).hexdigest(), True
        return envelope, hashlib.sha256(raw).hexdigest(), False

    def expired(self, entry, now=None):
        """Whether ``entry``'s idempotency receipt window has closed."""
        if not isinstance(entry, dict):
            return False
        moment = time.time() if now is None else now
        return entry.get('at', 0) + self.retention <= moment

    def _reclaim(self, data, now=None):
        """Drop only entries whose receipt window has closed. Returns the count."""
        moment = time.time() if now is None else now
        stale = [key for key, value in data.items()
                 if isinstance(value, dict) and self.expired(value, moment)]
        for key in stale:
            data.pop(key, None)
        return len(stale)

    def lookup(self, operation_id):
        entry = self._load().get(operation_id)
        return entry if isinstance(entry, dict) else None

    def reserve(self, operation_id, request_hash, principal):
        data = self._load()
        data[operation_id] = {'state': 'in_progress', 'request_hash': request_hash,
                              'principal': principal, 'at': time.time()}
        try:
            self._enforce(data)
        except JournalFull:
            # Capacity pressure. Live identities still fail closed; only identities
            # whose idempotency window has closed are reclaimed.
            if not self._reclaim(data):
                raise
            self._enforce(data)
        self._save(data)

    def complete(self, operation_id, envelope, request_hash, principal):
        data = self._load()
        stored, digest, omitted = self._bounded(envelope)
        record = {'state': 'committed', 'request_hash': request_hash, 'principal': principal,
                  'at': time.time(), 'envelope': stored, 'envelope_sha256': digest}
        if omitted:
            record['envelope_omitted'] = True
        data[operation_id] = record
        if omitted is False and len(self._serialized(data).encode('utf-8')) > self.max_bytes:
            # Keep the identity and its digest, drop only the oversized body.
            record['envelope'] = None
            record['envelope_omitted'] = True
        self._enforce(data)
        self._save(data)

    def mark_unknown(self, operation_id):
        """Keep a reservation whose outcome is not known to be pre-effect."""
        data = self._load()
        record = data.get(operation_id)
        if isinstance(record, dict):
            record['state'] = 'unknown'
            record['at'] = time.time()
            self._save(data)

    def discard(self, operation_id):
        data = self._load()
        if data.pop(operation_id, None) is not None:
            self._save(data)

    def reclaim_expired(self, now=None):
        """Remove every identity whose receipt window has closed. Returns the count.

        This is automatic under capacity pressure; it is also an operator action for
        a deployment that wants to compact the journal before the limit is reached.
        A caller retrying an expired identity is refused as expired while the record
        remains; once reclaimed the retry is indistinguishable from a new operation,
        so canonical state must be reconciled before reusing an old operation_id.
        """
        data = self._load()
        removed = self._reclaim(data, now)
        if removed:
            self._save(data)
        return removed

    def prune(self, before):
        """Explicitly remove entries last touched before ``before`` (epoch seconds).

        This is the operator override for identities that are still inside their
        receipt window (for example after reconciling a stuck unknown). After a prune
        an exact retry of a pruned operation can repeat the effect, so canonical state
        must be reconciled first. Returns the count removed.
        """
        data = self._load()
        removed = [key for key, value in data.items()
                   if isinstance(value, dict) and value.get('at', 0) < before]
        for key in removed:
            data.pop(key, None)
        if removed:
            self._save(data)
        return len(removed)

    def stats(self):
        data = self._load()
        states = {'in_progress': 0, 'committed': 0, 'unknown': 0}
        now = time.time()
        expired = 0
        for value in data.values():
            if isinstance(value, dict) and value.get('state') in states:
                states[value['state']] += 1
            if isinstance(value, dict) and self.expired(value, now):
                expired += 1
        return {'total': len(data), 'limit': self.limit, 'retention': self.retention,
                'expired': expired, 'reclaimable': expired, 'states': states,
                'bytes': self.path.stat().st_size if self.path.exists() else 0}


def _envelope(code, stderr='', **extra):
    payload = {'returncode': code, 'stdout': '', 'stderr': stderr}
    payload.update(extra)
    return payload


def run_guarded(request, journal_path, effect, authority_config=None,
                require_authority=False, runner=None):
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
        journal = OperationJournal(journal_path) if operation_id else None
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
                    # Same principal and request, but the receipt window has closed: the
                    # identity is refused as expired rather than replayed or re-run.
                    return _envelope(2, stderr='Operation identity expired: it is older than '
                                               'the %d second idempotency receipt window. '
                                               'Reconcile canonical state and use a fresh '
                                               'operation_id.\n' % journal.retention)
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
