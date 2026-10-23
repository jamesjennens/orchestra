#!/usr/bin/env python3
"""Identity, authorization and persistence core for the office HTTP transport.

The HTTP adapter (``http_service.py``) owns the wire protocol; this module owns the
security boundary described in ``docs/HTTP_TRANSPORT_DESIGN.md``:

* local accounts with a memory-hard password verifier (stdlib ``scrypt``),
* server-side browser sessions (hashed, idle + absolute expiry, revocation, CSRF),
* project-scoped worker credentials whose secret is shown once and stored hashed,
* single-use password-reset values,
* auth expiry (session idle and absolute, worker credentials, reset values) evaluated on
  a monotone clock, ``max(raw now, RecordStore high_water)``, so a backward clock step
  can never revive an expired item while a forward step still fails closed, *and* on the
  raw clock as a real-lifetime bound, so a floor pinned ahead by a corrected forward jump
  cannot stretch an item's real lifetime either,
* project membership and the owner/contributor/viewer action matrix,
* server-derived principals with actor labels treated as attribution only,
* idempotency records bound to principal + project + route with a separate request
  hash, including explicit unknown-outcome reconciliation,
* an append-only audit stream that never records a secret.

Only the Python standard library is used, and nothing here imports ``fcntl`` or any
POSIX-only module, so the same code runs on a Windows workstation and a Linux office
service. Authorization state is one JSON document written with an atomic replace;
idempotency receipts and canonical result replays live in a SQLite record store beside
it (:class:`RecordStore`) with time-only retention, so the JSON document no longer
grows with the number of keyed operations and no keyed operation rewrites it.
Multi-process concurrency is out of scope for the disposable validation build; the
service is a single process with a per-process lock, and the deployment runbook pins
that.
"""
import hashlib
import copy
from contextlib import contextmanager
import hmac
from server_address import ps_quote, sh_quote
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import closing
import sys
import threading
import time
import unicodedata
from pathlib import Path

import claim_limit
from http_authority import (ALL_CAPABILITIES, CAP_ACCOUNTS_ADMIN, CAP_AGENT_SELF, CAP_AGENTS, CAP_APPROVE,
                            CAP_CHECKPOINTS, CAP_FEEDBACK, CAP_PROJECT_ADMIN, CAP_PROPOSALS,
                            CAP_ROOMS_POST,
                            CAP_PROJECT_CREATE, CAP_PROJECT_HOST_CREATE, CAP_READ, CAP_RECORDS,
                            CAP_REVIEWS, CAP_TASKS,
                            CAP_COORDINATE, CAP_RELEASE, coordinator_grant, release_grant,
                            GRANT_LIMIT_DEFAULT, GRANT_LIMIT_MAX, created_projects, project_grant,
                            CREDENTIAL_FORBIDDEN_CAPABILITIES, CREDENTIAL_SCOPES, RANK,
                            ROLE_CAPABILITIES, ROLES, SCOPE_CAPABILITIES, SCHEMA_VERSION,
                            JOURNAL_MAX_SKEW_SECONDS, JOURNAL_SUSPECT_SETTLE_SECONDS,
                            CREDENTIAL_EXPIRED_MESSAGE, CREDENTIAL_MAX_AGE_DAYS,
                            CREDENTIAL_REVOKED_MESSAGE,
                            credential_age_expired, credential_expiry_cap,
                            AuthorityDenied, authority_request, clock_advance,
                            clock_persist, clock_report, clock_state,
                            clock_trusted, decide, file_lock,
                            actor_person as http_actor_person,
                            actor_parties as full_actor_parties, namespace_holds)

# Password verifier policy. n=2**14, r=8 needs ~16 MiB per hash; scrypt is the
# memory-hard scheme available in the standard library without third-party wheels.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SCRYPT_DKLEN, SCRYPT_SALT = 32, 16
#: The installation's password rule (kittrial-5bb.363). The minimum is a setting,
#: default 1: any non-empty password is accepted. The maximum is a bound on the work
#: one request may ask for, not a policy about what a password should look like.
MIN_PASSWORD, MAX_PASSWORD = 1, 1024

# Provisional local lifecycle defaults from the reviewed design, all configurable
# at the Service boundary.
SESSION_IDLE_SECONDS = 30 * 60
SESSION_ABSOLUTE_SECONDS = 12 * 60 * 60
#: The bounds of an installation's session limits (kittrial-5bb.330). The idle window is
#: 5 minutes to 24 hours and the absolute limit 1 hour to 7 days, and the absolute limit is
#: never shorter than the idle window. The settings are read in whole minutes and hours;
#: the Service keeps and compares them in seconds. Nothing is clamped: a value outside the
#: bounds is refused when the setting is read, and an installation that sets neither keeps
#: the two defaults above exactly as they were.
SESSION_IDLE_MIN_MINUTES, SESSION_IDLE_MAX_MINUTES = 5, 24 * 60
SESSION_ABSOLUTE_MIN_HOURS, SESSION_ABSOLUTE_MAX_HOURS = 1, 7 * 24
#: The one sentence both setting surfaces refuse with, so the web service's flags and the
#: office configuration say the same thing and never clamp a value into the bounds.
SESSION_LIMIT_SENTENCE = (
    'Refusing the session limits: the idle limit must be a whole number of minutes from %d to '
    '%d (5 minutes to 24 hours), the absolute limit a whole number of hours from %d to %d '
    '(1 hour to 7 days), and the absolute limit must not be shorter than the idle limit '
    '(got idle %r and absolute %r).')
CREDENTIAL_TTL_SECONDS = 30 * 24 * 60 * 60
AGENT_CREDENTIAL_DAYS_DEFAULT = 30
#: The same ceiling the shared read cap applies (kittrial-5bb.294 item 4): one source.
AGENT_CREDENTIAL_DAYS_MAX = CREDENTIAL_MAX_AGE_DAYS
_AGENT_LIFETIME_DEFAULT = object()
RESET_TTL_SECONDS = 30 * 60
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_ATTEMPTS = 10
#: Account lookups (``GET /v1/accounts/lookup``) one principal may make per window. Any
#: session can create a project and so administer one; the bound keeps the exact-match
#: lookup from becoming a fast username enumerator. Every lookup is also audited.
LOOKUP_WINDOW_SECONDS = 10 * 60
LOOKUP_MAX_PER_WINDOW = 20
AUDIT_LIMIT = 10000
#: An agent's history on the web (kittrial-5bb.365): at most this many events, newest first.
AGENT_HISTORY_MAX = 50
#: How long a committed canonical result stays replayable in the record store. It is a
#: time-only retention: nothing is ever evicted by entry count or serialized bytes, and
#: once this window has closed the record is dropped, after which the durable endpoint
#: operation journal still refuses or replays the same identity instead of repeating it.
RESULT_RETENTION_SECONDS = 24 * 60 * 60
#: SQLite sidecar holding the idempotency receipts and committed results, beside the
#: service state document. Both used to live inside the JSON document with a count/byte
#: eviction (results) or unbounded growth (idempotency).
RECORD_STORE_SUFFIX = '.records.sqlite3'

# Personal-agent bounds and defaults (kittrial-5bb.22). An agent is a personal
# identity owned by one user, not a project role; every field is a bounded,
# free-text hint the server stores and never executes.
AGENT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}')
AGENT_PROJECT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
AGENT_TOOL_MAX = 120
AGENT_MACHINE_MAX = 120
AGENT_NOTES_MAX = 1000
AGENT_DIRECTORY_MAX = 512
AGENT_PROJECTS_MAX = 64
AGENT_MAX_PER_OWNER = 100
AGENT_MAX_CREDENTIALS = 20
#: How many credentials that no longer work (revoked or expired) are kept per agent, the newest
#: ones, as the short history its card shows. Older ones are deleted from the state: the issue and
#: revocation metadata remain in the shared audit tail, capped by AUDIT_LIMIT.
AGENT_DEAD_CREDENTIALS_KEPT = 5
AGENT_DEFAULT_SCOPES = ('tasks', 'checkpoints', 'reviews', 'feedback')
AGENT_CONFIG_PATH = '.orchestra/agent.json'
AGENT_GUIDE_PATH = '.orchestra/AGENT.md'
AGENT_GITIGNORE_LINE = '.orchestra/'
#: The per-agent limit on claimed, undelivered tasks (kittrial-5bb.276). The
#: default, the editable bounds and the wording live in ``claim_limit``, so the
#: route, the owner's page and the gate cannot disagree about any of them.
CLAIM_LIMIT_DEFAULT = claim_limit.DEFAULT_LIMIT

AGENT_SECRET_ENV = 'ORCHESTRA_AGENT_SECRET'


def agent_slug(name):
    """File-name-safe slug of an agent name: lowercase ``[a-z0-9-]``, at most 40 chars.

    Must stay identical to ``slug()`` in ``web/js/agentSetup.js`` (tests compare them).
    """
    slug = re.sub(r'[^a-z0-9]+', '-', str(name or '').lower()).strip('-')[:40].strip('-')
    return slug or 'agent'


def agent_secret_file(name):
    """Where the owner keeps one agent's secret: a per-agent curl config file in the
    user profile holding one ``header = "Authorization: Bearer <secret>"`` line.

    Owner's practical update of 2026-09-29 (supersedes the "VS Code secret storage or
    OS credential store first" wording of owner decision 9 on kittrial-5bb.22): an
    agent running in VS Code cannot read those stores, but curl reads this file with
    ``-K``, so the secret never reaches a command line, the agent folder or Git.
    """
    file_name = '.orchestra-agent-%s.curlrc' % agent_slug(name)
    return {'name': file_name,
            'windows': '%USERPROFILE%\\' + file_name,
            'windows_powershell': '$env:USERPROFILE\\' + file_name,
            'posix': '~/' + file_name}

# Capabilities are the single authority vocabulary for every route. A route names
# the capability it needs; the Service decides whether the live principal holds it.
# Roles grant capabilities to interactive sessions; credential scopes grant a
# strictly smaller set that can never include administration or approval. The
# vocabulary and the decision function live in ``http_authority`` so the canonical
# endpoint can apply exactly the same rule immediately before an effect.
PERIODS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?$')


# --------------------------------------------------------------------------- errors
class HttpError(Exception):
    """An error that maps to one clean JSON response. Never carries a secret."""

    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail

    #: Optional: every problem a refusal found, one sentence each (kittrial-5bb.113). It is
    #: beside ``detail``, which keeps its text, so a client that reads ``detail`` is unaffected.
    problems = None

    def body(self, request_id):
        error = {'code': self.code, 'message': self.message}
        if self.detail is not None:
            error['detail'] = self.detail
        if self.problems:
            error['problems'] = list(self.problems)
        return {'error': error, 'request_id': request_id}


def unauthenticated(message='Authentication required'):
    return HttpError(401, 'unauthenticated', message)


def forbidden(message='Not permitted', detail=None):
    return HttpError(403, 'forbidden', message, detail)


def not_found(message='Not found'):
    return HttpError(404, 'not_found', message)


def conflict(message, detail=None):
    return HttpError(409, 'conflict', message, detail)


def too_large(message='Payload too large'):
    return HttpError(413, 'payload_too_large', message)


def invalid(message, detail=None):
    return HttpError(422, 'invalid_payload', message, detail)


def unsupported(message='Unsupported media type'):
    return HttpError(415, 'unsupported_media_type', message)


def not_implemented(message='Not implemented'):
    return HttpError(501, 'not_implemented', message)


def throttled(message='Too many attempts'):
    return HttpError(429, 'rate_limited', message)


def uncertain(message='Outcome unknown'):
    return HttpError(503, 'uncertain', message)


#: Said on every route when the server was occupied (kittrial-5bb.149). It names no lock and no
#: path, and it does not say that nothing was done: that is true for almost every route, and
#: was exactly what was untrue for a creation whose project was already made.
BUSY = 'The server is busy and this request was not completed. Send it again in a moment.'


def busy(message=BUSY, retry_after=30):
    """503 for a request that was not completed because the server was occupied: safe to send again.

    Unlike ``uncertain`` there is no outcome to reconcile. ``retry_after`` becomes the
    ``Retry-After`` header.
    """
    error = HttpError(503, 'busy', message)
    error.retry_after = retry_after
    return error


# --------------------------------------------------------------------- token helpers
def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    if not isinstance(token, str) or not token:
        return ''
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def request_hash(payload):
    """Canonical request hash used by the idempotency record (payload separate from key)."""
    return hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()


def now_iso(timestamp):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(timestamp))


def address_group(address):
    """What the limit per address counts as one client.

    An IPv4 address is itself; an IPv6 address is its /64, because one line is given a whole
    /64 and its holder can use any address in it; an IPv4 address written as IPv6
    (``::ffff:a.b.c.d``) is that IPv4 address. Anything that is not an address is itself.
    """
    text = str(address)
    try:
        parsed = ipaddress.ip_address(text.split('%', 1)[0])
    except ValueError:
        return text
    if parsed.version == 6:
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network((int(parsed) >> 64 << 64, 64)))
    return str(parsed)


# ------------------------------------------------------------------- password verifier
class _PasswordWorker:
    """Every scrypt computation of the process runs in this one long-lived thread (kittrial-5bb.170).

    One computation takes 16 MiB. The web service serves each connection in a thread of
    its own, and the C allocator keeps a freed block of that size in the arena of the
    thread that asked for it: 190 log-in attempts on 190 connections left 3 GB resident
    (measured; with one arena, 71 MB), although the checks ran one after another. With
    one thread asking, one arena holds it: what stays is one computation's worth, however
    many connections ask and whatever the allocator's settings are.

    The caller waits for its own result; an error of the computation is raised in the
    caller, as if it had computed there. A process that forks gets a new worker.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = None
        self._thread = None
        self._pid = None

    def _start(self):
        import queue
        self._jobs = queue.Queue()
        self._pid = os.getpid()
        self._thread = threading.Thread(target=self._run, args=(self._jobs,), name='password-worker', daemon=True)
        self._thread.start()

    @staticmethod
    def _run(jobs):
        while True:
            arguments, done = jobs.get()
            try:
                done['result'] = hashlib.scrypt(arguments[0], **arguments[1])
            except BaseException as error:  # noqa: BLE001 - handed to the caller, which raises it
                done['error'] = error
            done['event'].set()

    def scrypt(self, password, **parameters):
        with self._lock:
            if self._thread is None or self._pid != os.getpid() or not self._thread.is_alive():
                self._start()
            jobs = self._jobs
        done = {'event': threading.Event()}
        jobs.put(((password, parameters), done))
        done['event'].wait()
        if 'error' in done:
            raise done['error']
        return done['result']


_PASSWORD_WORKER = _PasswordWorker()


def hash_password(password, *, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, minimum=MIN_PASSWORD):
    _validate_password(password, minimum)
    salt = os.urandom(SCRYPT_SALT)
    derived = _PASSWORD_WORKER.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                                      dklen=SCRYPT_DKLEN)
    return 'scrypt$%d$%d$%d$%s$%s' % (n, r, p, salt.hex(), derived.hex())


def verify_password(verifier, password):
    if not isinstance(verifier, str) or not isinstance(password, str):
        return False
    try:
        scheme, n, r, p, salt, digest = verifier.split('$')
        if scheme != 'scrypt':
            return False
        salt_bytes, expected = bytes.fromhex(salt), bytes.fromhex(digest)
        derived = _PASSWORD_WORKER.scrypt(password.encode('utf-8'), salt=salt_bytes, n=int(n),
                                          r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError, MemoryError, OverflowError):
        return False
    return hmac.compare_digest(derived, expected)


def needs_rehash(verifier):
    if not isinstance(verifier, str):
        return True
    try:
        scheme, n, r, p, _, _ = verifier.split('$')
    except ValueError:
        return True
    return scheme != 'scrypt' or int(n) < SCRYPT_N or int(r) < SCRYPT_R or int(p) < SCRYPT_P


def validate_password_min_length(value):
    """The installation's password minimum: a whole number from 1 to ``MAX_PASSWORD``.

    One rule for the web service flag and the office configuration, so neither clamps
    a value to the bounds (kittrial-5bb.363). The default is 1: any non-empty password.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_PASSWORD <= value <= MAX_PASSWORD:
        raise ValueError('password_min_length must be a whole number from %d to %d'
                         % (MIN_PASSWORD, MAX_PASSWORD))
    return value


def _validate_password(password, minimum=MIN_PASSWORD):
    # A password of nothing but spaces is a mistake, not a choice: refused here for every
    # surface that sets one (change, reset redeem, bootstrap), in one sentence (5bb.363).
    if isinstance(password, str) and password and not password.strip():
        raise invalid('A password cannot be only spaces.')
    if not isinstance(password, str) or not minimum <= len(password) <= MAX_PASSWORD:
        raise invalid('Password must be %d-%d characters' % (minimum, MAX_PASSWORD))


# A fixed verifier used to spend the same work when an account does not exist, so a
# caller cannot tell "no such user" from "wrong password" by response timing.
_DUMMY_VERIFIER = None


def _dummy_verifier():
    global _DUMMY_VERIFIER
    if _DUMMY_VERIFIER is None:
        _DUMMY_VERIFIER = hash_password('orchestra-dummy-password')
    return _DUMMY_VERIFIER


# ------------------------------------------------------------------------- principal
class Principal:
    """A server-derived authenticated identity. Submitted actor labels never grant authority."""

    __slots__ = ('user_id', 'display_name', 'superuser', 'via', 'credential_id',
                 'credential_project', 'scopes', 'actor', 'csrf', 'session_hash',
                 'agent_id')

    def __init__(self, user_id, display_name, superuser, via, actor, *, credential_id=None,
                 credential_project=None, scopes=(), csrf=None, session_hash=None,
                 agent_id=None):
        self.user_id = user_id
        self.display_name = display_name
        self.superuser = superuser
        self.via = via
        self.credential_id = credential_id
        self.credential_project = credential_project
        self.scopes = tuple(scopes)
        self.actor = actor
        self.csrf = csrf
        self.session_hash = session_hash
        self.agent_id = agent_id

    def __repr__(self):
        return 'Principal(user_id=%r, via=%r, credential_id=%r)' % (
            self.user_id, self.via, self.credential_id)


class ActorNames(dict):
    """Actor label -> display name; a lookup with a non-string key finds nothing."""

    def get(self, key, default=None):
        if not isinstance(key, str):
            return default
        return dict.get(self, key, default)


# ----------------------------------------------------------------------------- store
def _blank_state():
    return {
        'schema_version': SCHEMA_VERSION,
        'cursor_key': secrets.token_hex(32),
        'users': {},
        'usernames': {},
        'sessions': {},
        'credentials': {},
        'credential_tokens': {},
        'reset_tokens': {},
        'projects': {},
        'memberships': {},
        'agents': {},
        'audit': [],
        'canonical': {'tasks': {}, 'checkpoints': {}, 'contributions': {}, 'feedback': {}},
    }


#: Sentinel meaning "the record store holds no such result", so a canonical result that
#: legitimately *is* ``None`` is still reported as a hit.
_RESULT_MISSING = object()


class RecordStore:
    """SQLite sidecar for the service's keyed records with TIME-ONLY retention.

    Revision 7 kept the HTTP idempotency receipts in ``state['idempotency']`` (deleted
    only when the same key was looked up again after its TTL, so it grew without bound)
    and the committed canonical results in ``state['canonical']['results']`` with a
    2048-entry / 2 MiB count+byte eviction. Both made every keyed operation rewrite the
    whole JSON document. Both now live here, in one SQLite table with a ``kind``
    discriminator, in WAL mode:

    * ``get``/``put``/``delete`` are indexed point operations on ``(kind, key)``.
    * ``purge`` removes only records whose own ``expires_at`` has genuinely passed.
      There is deliberately no count or byte eviction: the durable endpoint operation
      journal is what makes a retry safe, so a record is dropped by *age* alone.
    * The store uses the same **trusted clock** as the operation journal
      (``http_authority`` module docstring): ``meta`` holds ``high_water``,
      ``suspect``, ``anchor`` and ``suspect_since``; every write transaction observes
      the raw clock; whether a record has expired is decided against the trusted clock
      (while suspect, the anchor plus the time elapsed since the step, capped at
      ``anchor + max_skew``); ``created_at`` is stamped with the raw clock.
    * **Records expire on the confirmed timeline**, exactly like journal tombstones
      (``aged_from``). The store keeps its OWN trusted-clock state and ``jump_credit``
      in its own ``meta`` table (a mirror of the journal's rules, not shared rows: the
      record store is one file per service, the journal one per project, and each
      observes the same host clock through its own writes). Each row stores
      ``expires_confirmed = expires_at - jump_credit`` (credit at write time); a record
      is expired, and deleted, only when ``expires_confirmed < now - jump_credit``
      (with the clamped trusted clock while suspect), and nothing is deleted by age
      while the store is suspect. An accepted forward jump therefore never ages an
      idempotency record out, so an exact retry of a service-local route (credential
      issue, account/project create, membership changes) replays or is refused, never
      re-executed, while the total uncredited forward clock error stays below 24 h.
    * **Auth expiry is evaluated on a monotone clock**, ``max(raw now, high_water)``
      (:meth:`monotonic_now`). ``high_water`` never decreases and every auth observation
      persists it, so a backward step can never revive an expired session, credential or
      reset value, while a forward step expires one immediately (fail closed, re-issue).
      This is deliberately *not* :meth:`trusted_now`: while suspect that clock is held
      near the anchor so an idempotency record inside its real window can still replay,
      which would keep an expired auth item alive.

      The monotone clock is only one half of an auth expiry. After a forward jump that is
      later corrected, ``high_water`` stays ahead of the raw host clock and any item
      issued in the meantime is stamped on that pinned floor, so its monotone deadline is
      stretched by the jump. An item therefore also carries its raw issuance (and, for an
      idle refresh, raw last-use) stamp, and expires when either its monotone deadline is
      reached OR its real age reaches its lifetime (``Service._expired``). The raw half
      only adds refusals; it never replaces the monotone one.
    """

    def __init__(self, path, clock=time.time, max_skew=JOURNAL_MAX_SKEW_SECONDS,
                 settle=JOURNAL_SUSPECT_SETTLE_SECONDS):
        self.path = Path(path)
        self.clock = clock
        self.max_skew = max_skew
        self.settle = settle
        self._ensure()

    @contextmanager
    def _connection(self):
        """One SQLite connection, committed and CLOSED when the ``with`` block ends.

        ``sqlite3``'s own context manager commits or rolls back but never closes (P3,
        kittrial-5bb.174): the connection used to go out with the garbage collector, so
        the -wal and -shm files could linger and the database file's time could move at
        an arbitrary moment. ``contextlib.closing`` around it keeps that commit and adds
        the close. Nothing keeps a connection between calls: each ``with`` is its own.
        """
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout = 30000')
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA synchronous = FULL')
        with closing(connection), connection:
            yield connection

    #: Schema of the record store. Schema 2 (revision 11) adds ``expires_confirmed``,
    #: the record's expiry on the confirmed timeline, and its ``(kind,
    #: expires_confirmed)`` index (replacing ``(kind, expires_at)``).
    SCHEMA = 2

    def _ensure(self):
        """Create or upgrade the store in ONE transaction.

        An older store (schema 1: ``expires_at`` only) gains ``expires_confirmed``
        backfilled to ``expires_at`` (no step has been credited: ``jump_credit`` starts
        at 0), the new index replaces the old one, and the schema marker is written, all
        in the same ``BEGIN IMMEDIATE`` transaction, so a crash leaves either the old
        store or the fully upgraded one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('CREATE TABLE IF NOT EXISTS meta '
                                   '(key TEXT PRIMARY KEY, value)')
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'records_schema'").fetchone()
                if row is None or int(float(row[0])) < self.SCHEMA:
                    self._upgrade(connection)
            except BaseException:
                connection.execute('ROLLBACK')
                raise
            connection.execute('COMMIT')

    def _upgrade(self, connection):
        connection.execute(
            'CREATE TABLE IF NOT EXISTS records ('
            'kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT, '
            'created_at REAL NOT NULL, expires_at REAL, expires_confirmed REAL, '
            'PRIMARY KEY (kind, key))')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(records)')}
        if 'expires_confirmed' not in columns:
            connection.execute('ALTER TABLE records ADD COLUMN expires_confirmed REAL')
        connection.execute('UPDATE records SET expires_confirmed = expires_at '
                           'WHERE expires_confirmed IS NULL')
        connection.execute('CREATE INDEX IF NOT EXISTS records_confirmed '
                           'ON records (kind, expires_confirmed)')
        connection.execute('DROP INDEX IF EXISTS records_expiry')
        if connection.execute("SELECT 1 FROM meta WHERE key = 'jump_credit'").fetchone() \
                is None:
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                               "('jump_credit', 0.0)")
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                           "('records_schema', ?)", (self.SCHEMA,))

    def _now(self):
        return self.clock()

    def _observe(self, connection, now):
        """Advance and persist the trusted-clock state inside a write transaction."""
        state = clock_advance(clock_state(connection), now, self.max_skew, self.settle)
        clock_persist(connection, state)
        return state

    def _view(self, connection, moment):
        return clock_advance(clock_state(connection), moment, self.max_skew, self.settle)

    def trusted_now(self, now=None, connection=None):
        """The trusted clock the next write would see (expiry decisions only)."""
        moment = self._now() if now is None else now
        if connection is None:
            with self._connection() as opened:
                view = self._view(opened, moment)
        else:
            view = self._view(connection, moment)
        return clock_trusted(view, moment, self.max_skew)

    def confirmed_now(self, now=None, connection=None):
        """``trusted_now - jump_credit``: the current time on the confirmed timeline.

        A record has expired only when ``expires_confirmed < confirmed_now``. While the
        store is suspect the trusted clock is held near the anchor *and* the pending
        step is already in the credit, so a record only looks younger (it replays
        rather than being refused) - never older.
        """
        moment = self._now() if now is None else now
        if connection is None:
            with self._connection() as opened:
                view = self._view(opened, moment)
        else:
            view = self._view(connection, moment)
        return clock_trusted(view, moment, self.max_skew) - float(view['jump_credit'] or 0.0)

    def monotonic_now(self, now=None):
        """``max(raw now, persisted high_water)``: the clock auth expiry uses.

        ``high_water`` is the largest raw clock any observation of this store has seen
        and never decreases, so this clock never runs backwards: an item whose expiry was
        compared against it stays expired across a clock correction (or a host that
        booted before NTP), while a forward step still expires one immediately (fail
        closed, re-issue). The observation is persisted, so a rejection that performs no
        other write still records the step and the floor survives a restart.

        This is deliberately NOT :meth:`trusted_now`: while the store is suspect that
        clock is held near the anchor (so an idempotency receipt inside its real window
        can replay), which would keep an expired auth item alive.
        """
        moment = float(self._now() if now is None else now)
        with self._connection() as connection:
            previous = clock_state(connection)
            state = clock_advance(previous, moment, self.max_skew, self.settle)
            if state != previous:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    state = self._observe(connection, moment)
                except BaseException:
                    connection.execute('ROLLBACK')
                    raise
                connection.execute('COMMIT')
        high = state.get('high_water')
        return moment if high is None else max(moment, float(high))

    def get(self, kind, key):
        """The stored record, or ``None``. Never returns an expired record."""
        moment = self._now()
        with self._connection() as connection:
            row = connection.execute(
                'SELECT payload, expires_confirmed FROM records WHERE kind = ? AND key = ?',
                (kind, key)).fetchone()
            confirmed = self.confirmed_now(moment, connection) if row is not None else None
        if row is None:
            return None
        if row['expires_confirmed'] is not None and row['expires_confirmed'] < confirmed:
            self.delete(kind, key)
            return None
        try:
            record = json.loads(row['payload'])
        except (TypeError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def put(self, kind, key, record, ttl=None):
        """Insert or replace one record, honouring ``record['expires_at']`` if given.

        The raw ``expires_at`` is converted to the confirmed timeline with the credit at
        write time (``expires_confirmed = expires_at - jump_credit``). Rewriting an
        existing record with the same ``expires_at`` (commit/unknown of a reservation)
        keeps its original ``expires_confirmed``, so a step credited in between can
        never shorten it.
        """
        moment = self._now()
        expires = record.get('expires_at') if isinstance(record, dict) else None
        if not isinstance(expires, (int, float)):
            expires = moment + float(ttl if ttl is not None else IDEMPOTENCY_TTL_SECONDS)
        expires = float(expires)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                clock = self._observe(connection, moment)
                credit = float(clock['jump_credit'] or 0.0)
                previous = connection.execute(
                    'SELECT expires_at, expires_confirmed FROM records '
                    'WHERE kind = ? AND key = ?', (kind, key)).fetchone()
                if previous is not None and previous['expires_at'] == expires and \
                        previous['expires_confirmed'] is not None:
                    confirmed = float(previous['expires_confirmed'])
                else:
                    confirmed = expires - credit
                connection.execute(
                    'INSERT OR REPLACE INTO records (kind, key, payload, created_at, '
                    'expires_at, expires_confirmed) VALUES (?, ?, ?, ?, ?, ?)',
                    (kind, key, json.dumps(record, ensure_ascii=False, sort_keys=True),
                     moment, expires, confirmed))
                # Time-only retention is enforced here, on the same transaction, so the
                # table cannot grow without a matching expiry sweep. It is skipped while
                # the clock is suspect and compares the confirmed timeline, so a clock
                # jump can never age a record out early.
                if not clock['suspect']:
                    connection.execute(
                        'DELETE FROM records WHERE kind = ? AND expires_confirmed < ?',
                        (kind, moment - credit))
            except BaseException:
                connection.execute('ROLLBACK')
                raise
            connection.execute('COMMIT')
        return record

    def delete(self, kind, key):
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._observe(connection, self._now())
            connection.execute('DELETE FROM records WHERE kind = ? AND key = ?',
                               (kind, key))
            connection.execute('COMMIT')

    def purge(self, kind=None, now=None):
        """Delete only records past their own expiry on the confirmed timeline.

        A write transaction: it observes the clock and deletes nothing while the store
        is suspect; otherwise it deletes ``expires_confirmed < now - jump_credit``.
        Returns the count.
        """
        moment = self._now() if now is None else now
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            clock = self._observe(connection, moment)
            if clock['suspect']:
                connection.execute('COMMIT')
                return 0
            threshold = moment - float(clock['jump_credit'] or 0.0)
            if kind is None:
                row = connection.execute(
                    'SELECT COUNT(*) FROM records WHERE expires_confirmed < ?',
                    (threshold,)).fetchone()
                connection.execute('DELETE FROM records WHERE expires_confirmed < ?',
                                   (threshold,))
            else:
                row = connection.execute(
                    'SELECT COUNT(*) FROM records WHERE kind = ? AND expires_confirmed < ?',
                    (kind, threshold)).fetchone()
                connection.execute(
                    'DELETE FROM records WHERE kind = ? AND expires_confirmed < ?',
                    (kind, threshold))
            connection.execute('COMMIT')
        return int(row[0])

    def reset_high_water(self, now=None):
        """Operator recovery after a clock correction (never reduces ``jump_credit``)."""
        moment = float(self._now() if now is None else now)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            credit = clock_state(connection)['jump_credit']
            clock_persist(connection, {'high_water': moment, 'suspect': False,
                                       'anchor': None, 'suspect_since': None,
                                       'jump_credit': credit})
            connection.execute('COMMIT')
        return moment

    def stats(self, now=None):
        """Operator-visible record counts, byte sizes and trusted-clock state."""
        moment = self._now() if now is None else now
        with self._connection() as connection:
            clock = clock_state(connection)
            view = clock_advance(clock, moment, self.max_skew, self.settle)
            trusted = clock_trusted(view, moment, self.max_skew)
            confirmed = trusted - float(view['jump_credit'] or 0.0)
            kinds = {}
            for row in connection.execute(
                    'SELECT kind, COUNT(*) AS total, '
                    'COALESCE(SUM(LENGTH(payload)), 0) AS bytes, '
                    'SUM(CASE WHEN expires_confirmed < ? THEN 1 ELSE 0 END) AS due '
                    'FROM records GROUP BY kind', (confirmed,)):
                kinds[row['kind']] = {'total': int(row['total']),
                                      'bytes': int(row['bytes']),
                                      'expired': int(row['due'] or 0)}
            mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
            total = connection.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        report = {'records': int(total), 'kinds': kinds, 'journal_mode': mode,
                  'trusted_now': trusted, 'confirmed_now': confirmed,
                  'schema': self.SCHEMA,
                  'file_bytes': self.path.stat().st_size if self.path.exists() else 0}
        report.update(clock_report(view))
        report['clock_persisted'] = clock_report(clock)
        return report


class Store:
    """Atomic JSON persistence plus the SQLite record store. One writer process."""

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.RLock()
        #: Since when the state in memory holds something the file does not, on this store's
        #: clock: a save could not have the state lock. None when the file is current.
        self.unsaved_since = None
        self.state = self._load()
        self.records = RecordStore(
            self.path.with_name(self.path.name + RECORD_STORE_SUFFIX),
            clock=lambda: self.clock())
        self._migrate_records()

    def now(self):
        return self.clock()

    def _load(self):
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return _blank_state()
        data = json.loads(text)
        if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('Unsupported or corrupt service store schema')
        base = _blank_state()
        created_cursor_key = not isinstance(data.get('cursor_key'), str) or len(data['cursor_key']) < 32
        for key, value in base.items():
            data.setdefault(key, value)
        if created_cursor_key:
            data['cursor_key'] = base['cursor_key']
            self.state = data
            self.save()
        return data

    def _migrate_records(self):
        """Move a pre-revision-8 ``idempotency``/``results`` document into the store.

        A state document written by revision 7 or earlier still carries both maps. They
        are imported once into the record store (honouring each recorded ``expires_at``)
        and then removed from the JSON document, so a restart keeps every live receipt
        without ever rewriting the document again.
        """
        moved = False
        legacy_idempotency = self.state.pop('idempotency', None)
        if isinstance(legacy_idempotency, dict) and legacy_idempotency:
            for digest, record in legacy_idempotency.items():
                if isinstance(digest, str) and isinstance(record, dict):
                    self.records.put('idempotency', digest, record,
                                     ttl=IDEMPOTENCY_TTL_SECONDS)
            moved = True
        canonical = self.state.get('canonical')
        if isinstance(canonical, dict):
            legacy_results = canonical.pop('results', None)
            if isinstance(legacy_results, dict) and legacy_results:
                for key, value in legacy_results.items():
                    if isinstance(key, str):
                        self.records.put('result', key, {'result': value},
                                         ttl=RESULT_RETENTION_SECONDS)
                moved = True
        if moved:
            self.save()

    #: How long :meth:`save_soon` waits for the state lock the first time (kittrial-5bb.156).
    SOON_WAIT = 5.0

    def save_soon(self, what='a last-use stamp'):
        """Save, without making the caller wait a minute for it. Returns whether it was saved.

        For what a request's answer does not depend on: the last-use stamp every
        authenticated request leaves, and the audit entry of a refusal. The first time the
        state lock cannot be had within :attr:`SOON_WAIT` seconds the change stays in
        memory, and until a save succeeds every further call tries once without waiting,
        so requests sent together do not queue behind one another. The whole state is
        written by every save, so the next save that succeeds writes what was left; a
        write that goes to the endpoint saves first (``EndpointBackend._endpoint``),
        because the endpoint reads a session's idle deadline from the file.

        What follows from a stamp that is not saved, both ways: if the service stops
        before the next save the last use is lost and the session reads as idle sooner
        (the safe side); nothing lets a session live past its deadline.
        """
        with self.lock:
            first = self.unsaved_since is None
            try:
                self.save(wait=self.SOON_WAIT if first else 0.0)
                return True
            except TimeoutError as waited:
                if first:
                    print('busy: %s was not saved, the state lock could not be had; it is kept in memory and '
                          'written with the next save: %s' % (what, ascii(str(waited)[:400])),
                          file=sys.stderr, flush=True)
                return False

    def save(self, wait=60.0):
        # One unique temporary per write, then an atomic replace. A fixed
        # ``<name>.tmp`` would let a second writer (or a stale process) clobber an
        # in-flight snapshot before it is renamed, so the name carries the pid and a
        # random suffix. The lock is re-entrant, so callers that already hold it
        # (the whole mutation boundary) still get a consistent snapshot.
        #
        # ``<state>.lock`` is a *cross-process* lock: the canonical endpoint takes the
        # same file around live-authority re-validation plus its effect, so an
        # authority change persisted here (revocation, membership, disable) is either
        # committed before the endpoint's check or serialized after the effect.
        with self.lock:
            try:
                with file_lock(str(self.path) + '.lock', timeout=wait):
                    self._write()
            except TimeoutError:
                # Whatever this save was to write is in memory only, until a save succeeds.
                if self.unsaved_since is None:
                    self.unsaved_since = self.clock()
                raise

    def _write(self):
        """Write the whole state to the file. The caller holds both locks."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            '%s.%d.%s.tmp' % (self.path.name, os.getpid(), secrets.token_hex(4)))
        # Credential rollback holds the old audit prefix until saving succeeds.
        # Serialize its bounded tail without dropping that rollback evidence in memory.
        state = self.state
        if len(state['audit']) > AUDIT_LIMIT:
            state = dict(state, audit=state['audit'][-AUDIT_LIMIT:])
        text = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if self.unsaved_since is not None:
                self.unsaved_since = None
                print('The state is saved again; what was kept in memory is written.', file=sys.stderr, flush=True)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------- service
AGENT_ROLES = ('worker', 'reviewer')


def validate_claim_limit(value):
    """The stored form of one owner-supplied per-agent claim limit.

    ``None`` clears the setting back to the default; a whole number inside the
    editable bounds is kept. Anything else names what was wrong, so the route can
    answer 422 with the field and the bounds instead of storing a value that would
    silently mean something else.
    """
    try:
        return claim_limit.validate_limit(value)
    except ValueError as error:
        raise invalid(str(error)) from None


def validate_agent_role(value):
    """The stored form of an agent's role ('worker' or 'reviewer', or None to clear)."""
    if value is None:
        return None
    if isinstance(value, str):
        clean = value.strip()
        if clean in AGENT_ROLES:
            return clean
    raise invalid("role must be 'worker' or 'reviewer'")


def validate_agent_credential_days(value):
    """The installation's agent-only lifetime; changing it rewrites no credential."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= AGENT_CREDENTIAL_DAYS_MAX:
        raise ValueError('agent_credential_days must be a whole number from 1 to 366')
    return value


def session_limit_refusal(idle_minutes=None, absolute_hours=None):
    """The one sentence a refused session limit is refused with, naming what was given."""
    return SESSION_LIMIT_SENTENCE % (SESSION_IDLE_MIN_MINUTES, SESSION_IDLE_MAX_MINUTES,
                                     SESSION_ABSOLUTE_MIN_HOURS, SESSION_ABSOLUTE_MAX_HOURS,
                                     idle_minutes, absolute_hours)


def session_limit_seconds(idle_minutes=None, absolute_hours=None):
    """The installation's session idle and absolute limits, in seconds (kittrial-5bb.330).

    The surface of a setting is whole minutes for the idle window and whole hours for the
    absolute limit; the ``Service`` keeps and compares them in seconds, which is what this
    returns as ``(session_idle, session_absolute)``. ``None`` means the installation did not
    set that limit, so the reviewed default applies unchanged.

    A value outside its bounds, a value that is not a whole number, and an absolute limit
    shorter than the idle window are all refused with the same one sentence
    (:func:`session_limit_refusal`). Nothing is clamped: a caller that reads a setting
    refuses it, and no state is opened or written before that refusal. Changing the limits
    never rewrites a live session; a session issued before the change keeps the deadlines it
    was stamped with, and the next log-in carries the new ones.
    """
    for value, low, high in ((idle_minutes, SESSION_IDLE_MIN_MINUTES, SESSION_IDLE_MAX_MINUTES),
                             (absolute_hours, SESSION_ABSOLUTE_MIN_HOURS, SESSION_ABSOLUTE_MAX_HOURS)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or not low <= value <= high):
            raise ValueError(session_limit_refusal(idle_minutes, absolute_hours))
    session_idle = SESSION_IDLE_SECONDS if idle_minutes is None else idle_minutes * 60
    session_absolute = SESSION_ABSOLUTE_SECONDS if absolute_hours is None else absolute_hours * 3600
    if session_absolute < session_idle:
        raise ValueError(session_limit_refusal(idle_minutes, absolute_hours))
    return session_idle, session_absolute


class Service:
    """The authenticated authorization boundary.

    Callers must invoke a public method with a :class:`Principal` obtained from
    :meth:`authenticate`. Every project-scoped method re-reads current membership;
    no client-supplied role or actor label is trusted.
    """

    def __init__(self, store, *, session_idle=SESSION_IDLE_SECONDS,
                 session_absolute=SESSION_ABSOLUTE_SECONDS,
                 credential_ttl=CREDENTIAL_TTL_SECONDS, reset_ttl=RESET_TTL_SECONDS,
                 idempotency_ttl=IDEMPOTENCY_TTL_SECONDS,
                 result_retention=RESULT_RETENTION_SECONDS,
                 login_max_attempts=LOGIN_MAX_ATTEMPTS, public_url=None,
                 lookup_max=LOOKUP_MAX_PER_WINDOW, lookup_window=LOOKUP_WINDOW_SECONDS,
                 approval_by_another_party=False, agent_credential_days=_AGENT_LIFETIME_DEFAULT,
                 password_min_length=MIN_PASSWORD):
        self.store = store
        #: Per-agent locks spanning the claim gate's held-set read and the claim write
        #: (kittrial-5bb.276 revision; per-agent in kittrial-5bb.300). Keyed by the
        #: agent id so concurrent claims by one agent serialize while unrelated agents'
        #: claims do not wait for each other. One service process is the supported setup.
        self._claim_gates_guard = threading.Lock()
        self._claim_gates = {}
        #: The installation's setting "nobody approves work of their own party" (slice 1a of
        #: docs/WEB_COORDINATOR_DESIGN.md, kittrial-5bb.199). OFF unless the operator turns it
        #: on: with it off every review rule is exactly what it was.
        self.approval_by_another_party = approval_by_another_party is True
        self.session_idle = session_idle
        self.session_absolute = session_absolute
        #: The installation's password minimum (kittrial-5bb.363), default 1. Every
        #: writer goes through hash_password/redeem_reset with this value, so the rule
        #: cannot drift between the bootstrap prompt and the web forms.
        self.password_min_length = validate_password_min_length(password_min_length)
        self.credential_ttl = credential_ttl
        # Preserve the existing test/embedding injection of credential_ttl when no new
        # option was supplied. The command line always supplies the installation days.
        self.agent_credential_ttl = (credential_ttl if agent_credential_days is _AGENT_LIFETIME_DEFAULT
                                     else validate_agent_credential_days(agent_credential_days) * 86400)
        self.reset_ttl = reset_ttl
        self.idempotency_ttl = idempotency_ttl
        self.result_retention = result_retention
        self.login_max_attempts = login_max_attempts
        #: Canonical base URL of this service, used only to render the copyable agent
        #: setup/resume snippets. It is deployment configuration, never request data.
        # kittrial-5bb.360: the address goes into the set-up commands people paste into a shell.
        if public_url:
            from server_address import public_url_problem
            problem = public_url_problem(public_url, 'the service start (--public-url)')
            if problem:
                raise ValueError(problem)
        self.public_url = (public_url or '').rstrip('/') or None
        self._failures = {}
        self._audit_defer_trim = 0
        self._logins_guard = threading.Lock()
        self._logins_room = threading.Condition(self._logins_guard)   # told whenever a log-in ends
        self._logins_waiting = {}        # address group -> its log-ins waiting for one of its places
        self._logins = 0
        self._logins_by_address = {}     # address group -> its log-ins in flight
        self.logins_turned_away = 0
        self.logins_turned_away_for_address = 0
        self.lookup_max = lookup_max
        self.lookup_window = lookup_window
        self._lookups = {}

    # -- helpers ---------------------------------------------------------------
    def claim_gate(self, agent_id):
        """The per-agent lock spanning the claim gate's held-set read and write (kittrial-5bb.300).

        Keyed by ``agent_id`` (the agent's stable id and actor namespace head), so two
        concurrent claims or self-assignments by the SAME agent (including under a
        ``<agent>/<label>`` sub-label) serialize while claims by unrelated agents proceed
        concurrently without waiting for each other's tracker reads.
        One service process is the supported setup; this lock is in-memory in that process.
        """
        key = str(agent_id or '')
        if '/' in key:
            key = key.split('/', 1)[0]
        with self._claim_gates_guard:
            gate = self._claim_gates.get(key)
            if gate is None:
                gate = self._claim_gates[key] = threading.RLock()
            return gate

    @property
    def state(self):
        return self.store.state

    def cursor_key(self):
        """The per-installation HMAC key for pagination cursors (kept in service state, never in a cursor or response)."""
        with self.store.lock:
            key = self.state.get('cursor_key')
            if not isinstance(key, str) or len(key) < 32:
                key = self.state['cursor_key'] = secrets.token_hex(32)
                self.store.save()
            return key

    def _now(self):
        return self.store.now()

    def _expiry_now(self):
        """The monotone clock every auth expiry is evaluated against.

        ``max(raw now, persisted high_water)`` from the record store
        (:meth:`RecordStore.monotonic_now`). Unlike the store's suspect-aware
        ``trusted_now`` this never runs backwards, so a backward clock step cannot
        revive an expired session, worker credential or reset value, and a forward step
        expires one at once (fail closed; re-issue). Timestamps that are only
        informational (audit events, ``created_at``) stay on the raw clock.

        This is the *monotone half* of an auth expiry; :meth:`_expired` adds the item's
        real age on the raw host clock as the other half, so a floor pinned ahead by a
        corrected forward jump cannot stretch a lifetime.
        """
        return self.store.records.monotonic_now()

    def _raw_now(self):
        """The raw host clock (:meth:`Store.now`).

        It is *not* a decision clock on its own: only :meth:`_expired` may use it, as the
        real-lifetime half of an expiry comparison.
        """
        return self.store.now()

    def _expired(self, moment, *, expires_at=None, issued_raw=None, lifetime=None):
        """Whether one auth item is expired on EITHER clock.

        ``moment`` is :meth:`_expiry_now`, the monotone decision clock: reaching
        ``expires_at`` expires the item, which is the guarantee that a backward step can
        never revive something already expired. ``issued_raw``/``lifetime`` add the item's
        real lifetime on the raw host clock: ``_raw_now() - issued_raw >= lifetime`` also
        expires it, so a monotone floor left ahead by a corrected forward jump (which is
        where a new item's stamp comes from while the raw clock catches up) can no longer
        stretch a session idle/absolute, worker credential or reset lifetime.

        The monotone comparison is never dropped; the raw one only adds refusals. A
        record that predates the raw stamp (``issued_raw is None``) keeps the monotone
        rule alone rather than failing open.
        """
        if expires_at is not None and moment >= expires_at:
            return True
        if issued_raw is None or lifetime is None:
            return False
        return self._raw_now() - float(issued_raw) >= float(lifetime)

    def _credential_expired(self, credential, moment):
        """Agent deadlines belong to the key; worker/legacy clock rules stay unchanged.

        Older kits cap issued_raw with their own fixed30-day rule. A longer-lived agent
        key uses their existing legacy path (no issued_raw); this reader still enforces
        BOTH its stored monotone expiry and its explicit raw deadline. Default30 keys
        keep issued_raw too. New fields never change worker/session/reset expiry.

        The stored expiry is additionally capped at 366 days from the most recent
        legitimate write ON READ (kittrial-5bb.294 item 4): issuance and every extension
        stamp ``agent_written_raw``, so a hand-edited store of years is not honoured here
        and an owner's latest extension always buys its full lifetime. A record without
        the field falls back to its issuance stamp. The same shared cap is applied by the
        canonical endpoint.
        """
        if credential_age_expired(credential, moment):
            return True
        if credential.get('agent_id') and 'agent_raw_expires_at' in credential:
            values = (credential.get('expires_at'), credential['agent_raw_expires_at'])
            try:
                if any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                       not math.isfinite(x) for x in values):
                    return True
                return moment >= values[0] or self._raw_now() >= values[1]
            except (TypeError, ValueError, OverflowError):
                return True
        return self._expired(moment, expires_at=credential.get('expires_at'),
                             issued_raw=credential.get('issued_raw'), lifetime=self.credential_ttl)

    def _agent_expiry_metadata(self, moment=None):
        """Calculate representable deadlines before any agent/credential write."""
        moment = self._expiry_now() if moment is None else moment
        raw = self._raw_now()
        expires = moment + self.agent_credential_ttl
        raw_expires = raw + self.agent_credential_ttl
        try:
            for stamp in (expires, raw_expires):
                if not math.isfinite(stamp):
                    raise ValueError('non-finite expiry')
                if not 1 <= time.gmtime(stamp).tm_year <= 9999:
                    raise ValueError('expiry year is outside RFC3339')
                now_iso(stamp)
        except (ValueError, OverflowError, OSError):
            raise invalid('Agent credential expiry is outside the representable timestamp range') from None
        result = {'expires_at': expires, 'agent_raw_expires_at': raw_expires,
                  'agent_issued_raw': raw, 'agent_written_raw': raw}
        if self.agent_credential_ttl <= self.credential_ttl:
            result['issued_raw'] = raw
        return result

    def _live_item_expired(self, moment, principal):
        """Whether the live session/credential behind ``principal`` is expired on either
        clock.

        The read-only companion of :meth:`_refresh_authority` for decisions that do not
        take ``store.lock`` (a capability listing). Revocation is included so the two
        paths agree.
        """
        if principal is None:
            return True
        if principal.via == 'session':
            session = self.state['sessions'].get(principal.session_hash)
            return session is None or session.get('revoked') or self._expired(
                moment, expires_at=session.get('absolute_expires'),
                issued_raw=session.get('issued_raw'), lifetime=self.session_absolute) or \
                self._expired(moment, expires_at=session.get('idle_expires'),
                              issued_raw=session.get('last_used_raw'),
                              lifetime=self.session_idle)
        if principal.via == 'credential':
            credential = self.state['credentials'].get(principal.credential_id)
            if credential is None or credential.get('revoked') or self._credential_expired(credential, moment):
                return True
            if credential.get('agent_id'):
                agent = self.state['agents'].get(credential['agent_id'])
                return not isinstance(agent, dict) or not agent.get('enabled')
            return False
        return True

    def audit(self, request_id, principal, action, outcome, *, project_id=None, reason=None,
              actor=None, requirement_id=None, original_operation_id=None, recovery_operation_id=None):
        """Append one redacted audit event. Secrets and payloads must never reach here."""
        event = {
            'time': now_iso(self._now()),
            'request_id': request_id,
            'user_id': principal.user_id if principal else None,
            'actor': actor,
            'credential_id': principal.credential_id if principal else None,
            'project_id': project_id,
            'action': action,
            'outcome': outcome,
            'reason': (str(reason)[:200] if reason else None),
        }
        if requirement_id is not None:
            event.update(requirement_id=requirement_id, original_operation_id=original_operation_id,
                         recovery_operation_id=recovery_operation_id)
        with self.store.lock:
            self.state['audit'].append(event)
            if not self._audit_defer_trim and len(self.state['audit']) > AUDIT_LIMIT:
                del self.state['audit'][:len(self.state['audit']) - AUDIT_LIMIT]
        return event

    def _username(self, username):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{1,63}',
                                                            username):
            raise invalid('Username must be 2-64 characters of letters, digits, . _ @ -')
        return username

    def _user(self, user_id):
        user = self.state['users'].get(user_id)
        if user is None:
            raise not_found('Account not found')
        return user

    # -- bootstrap (operator, out of band; never an HTTP route) -----------------
    @classmethod
    def bootstrap_superuser(cls, store, username, password, display_name=None,
                            password_min_length=MIN_PASSWORD):
        """Create the first protected superuser. Refuses once any account exists."""
        minimum = validate_password_min_length(password_min_length)
        with store.lock:
            if store.state['users']:
                raise conflict('A superuser already exists')
            service = cls(store)
            username = service._username(username)
            user = {
                'id': 'usr_' + secrets.token_hex(8),
                'username': username,
                'display_name': display_name or username,
                'password': hash_password(password, minimum=minimum),
                'superuser': True,
                'disabled': False,
                'created_at': now_iso(store.now()),
            }
            store.state['users'][user['id']] = user
            store.state['usernames'][username.lower()] = user['id']
            store.save()
            service.audit(None, None, 'bootstrap', 'committed', reason='superuser created')
            return user

    # -- accounts --------------------------------------------------------------
    def create_user(self, principal, username, display_name=None):
        self._require_superuser(principal)
        username = self._username(username)
        with self.store.lock:
            if username.lower() in self.state['usernames']:
                raise conflict('Username already exists')
            user = {
                'id': 'usr_' + secrets.token_hex(8),
                'username': username,
                'display_name': display_name or username,
                'password': None,
                'superuser': False,
                'disabled': False,
                'created_at': now_iso(self._now()),
            }
            self.state['users'][user['id']] = user
            self.state['usernames'][username.lower()] = user['id']
            self.store.save()
        return self._public_user(user)

    def list_users(self, principal):
        self._require_superuser(principal)
        return [self._public_user(u) for u in self.state['users'].values()]

    def _public_user(self, user):
        view = {'id': user['id'], 'username': user['username'],
                'display_name': user['display_name'], 'disabled': user['disabled'],
                'superuser': user['superuser'], 'created_at': user['created_at']}
        # Additive (kittrial-5bb.118 part 2): the "may create projects" grant, and how
        # many of this account's projects count toward it. This view is what a superuser's
        # account list returns and what an account is told about itself at sign-in.
        grant = project_grant(user)
        view['project_grant'] = ({'limit': grant['limit'], 'granted_by': grant['granted_by'],
                                  'granted_at': grant.get('granted_at')} if grant else None)
        view['projects_created'] = len(created_projects(self.state, user['id']))
        return view

    # -- "may create projects" (kittrial-5bb.118 part 2) -------------------------
    def set_project_grant(self, principal, user_id, limit=None, request_id=None):
        """A superuser lets a named account create projects, up to ``limit`` at a time.

        Session authority only. The grant is per account, revocable, and audited with
        who granted it; an agent or worker credential never has it (``decide`` refuses
        the capability for every credential, whoever issued it).
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change who may create projects')
        if limit is None:
            limit = GRANT_LIMIT_DEFAULT
        if type(limit) is not int or not 1 <= limit <= GRANT_LIMIT_MAX:
            raise invalid('limit must be a whole number from 1 to %d' % GRANT_LIMIT_MAX)
        with self.store.lock:
            self._refresh_authority(principal)
            if not principal.superuser:
                raise forbidden('Superuser authority required')
            user = self._user(user_id)
            if user.get('disabled'):
                raise conflict('That account is disabled')
            before = project_grant(user)
            user['project_grant'] = {'limit': limit, 'granted_by': principal.user_id,
                                     'granted_at': now_iso(self._now())}
            self.audit(request_id, principal, 'accounts.project-grant', 'committed',
                       reason='account=%s %s limit=%d%s' % (
                           user_id, 'changed' if before else 'granted', limit,
                           ' (was %d)' % before['limit'] if before else ''))
            self.store.save()
            return self._public_user(user)

    def clear_project_grant(self, principal, user_id, request_id=None):
        """Take the grant away. Projects the account already created are untouched."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change who may create projects')
        with self.store.lock:
            self._refresh_authority(principal)
            if not principal.superuser:
                raise forbidden('Superuser authority required')
            user = self._user(user_id)
            had = project_grant(user)
            user.pop('project_grant', None)
            self.audit(request_id, principal, 'accounts.project-grant', 'committed',
                       reason='account=%s %s' % (user_id, 'revoked (limit was %d)' % had['limit'] if had
                                                 else 'revoked (none held)'))
            self.store.save()
            return self._public_user(user)

    def host_creation(self, principal):
        """Whether this principal may create a project on the host now, and its numbers.

        ``{'allowed', 'limit', 'used', 'reason'}``; ``limit`` is None for a superuser.
        Never raises: the page uses it to decide what to show.
        """
        if principal is None or principal.via == 'credential':
            return {'allowed': False, 'limit': None, 'used': 0, 'reason': 'credential'}
        with self.store.lock:
            user = self.state['users'].get(principal.user_id) or {}
            used = len(created_projects(self.state, principal.user_id))
            if user.get('superuser'):
                return {'allowed': True, 'limit': None, 'used': used, 'reason': None}
            grant = project_grant(user)
            if grant is None:
                return {'allowed': False, 'limit': None, 'used': used, 'reason': 'no-grant'}
            return {'allowed': used < grant['limit'], 'limit': grant['limit'], 'used': used,
                    'reason': None if used < grant['limit'] else 'limit'}

    def register_host_created(self, principal, project_id, name, creation, operation=None):
        """Write the web record of a project the host has just finished creating.

        The creator is the only member, as owner. ``registered_by`` is set so that this
        kit and the one before it serve the record (the earlier kit reads that mark as
        "a superuser stood behind this mapping"; here a superuser stood behind the
        grant). ``host_created`` says how the record came to be; its ``operation`` is the
        digest of the request's identity, by which the creator's exact repeat of that
        request is known again (kittrial-5bb.156).
        """
        with self.store.lock:
            self._refresh_authority(principal)
            existing = self.state['projects'].get(project_id)
            if existing is not None:
                if existing.get('created_by') == principal.user_id and existing.get('host_created'):
                    return self.project_view(principal, project_id)      # the same creation, written already
                raise conflict('A project with that identifier is already registered')
            self._validate_project_name(name)
            user = self.state['users'].get(principal.user_id) or {}
            grant = project_grant(user)
            self.state['projects'][project_id] = {
                'id': project_id, 'name': name, 'created_by': principal.user_id,
                'created_at': now_iso(self._now()), 'archived': False,
                'registered_by': principal.user_id,
                'host_created': {'by': principal.user_id, 'at': now_iso(self._now()),
                                 'adopted': bool(creation.get('adopted')), 'operation': operation,
                                 'grant_limit': None if user.get('superuser') else (grant or {}).get('limit'),
                                 'granted_by': None if user.get('superuser') else (grant or {}).get('granted_by')},
            }
            self.state['memberships'][project_id] = {principal.user_id: 'owner'}
            self.store.save()
            return self.project_view(principal, project_id)

    def change_password(self, principal, user_id, current_password, new_password):
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required for password changes')
        with self.store.lock:
            self._refresh_authority(principal)
            user = self._user(user_id)
            if not principal.superuser and principal.user_id != user_id:
                raise not_found('Account not found')
            if not principal.superuser:
                if not verify_password(user.get('password'), current_password or ''):
                    raise forbidden('Current password is incorrect')
            if verify_password(user.get('password'), new_password):
                raise invalid('New password must differ from the current password')
            user['password'] = hash_password(new_password, minimum=self.password_min_length)
            if user.get('reset_token'):
                user.pop('reset_token')
            self._revoke_sessions_for(user_id)
            self._revoke_credentials_for(user_id)
            self.store.save()
        return {'id': user_id, 'changed': True}

    def issue_reset(self, principal, user_id, request_id=None):
        self._require_superuser(principal)
        with self.store.lock:
            user = self._user(user_id)
            token = new_token()
            # Issued on the monotone clock, so a value issued during a forward jump that
            # is later corrected never lives past its TTL on that same clock. The raw
            # issuance is stored too, so the real TTL still bounds it while the monotone
            # floor is pinned ahead of the corrected host clock.
            moment = self._expiry_now()
            raw = self._raw_now()
            # Reissuing invalidates any previous pending reset for the account.
            for existing, record in list(self.state['reset_tokens'].items()):
                if record['user_id'] == user_id and not record['used']:
                    del self.state['reset_tokens'][existing]
            self.state['reset_tokens'][token_hash(token)] = {
                'user_id': user_id,
                'issued_at': moment,
                'issued_raw': raw,
                'expires_at': moment + self.reset_ttl,
                'used': False,
                'issued_by': principal.user_id,
            }
            self.store.save()
        # The value is returned once, to the authenticated superuser, and never stored.
        return {'id': user_id, 'reset_value': token,
                'expires_at': now_iso(moment + self.reset_ttl), 'single_use': True}

    def redeem_reset(self, user_id, reset_value, new_password):
        _validate_password(new_password, self.password_min_length)
        digest = token_hash(reset_value)
        with self.store.lock:
            record = self.state['reset_tokens'].get(digest)
            if record is None or record['user_id'] != user_id or record['used'] or \
                    self._expired(self._expiry_now(), expires_at=record['expires_at'],
                                  issued_raw=record.get('issued_raw'),
                                  lifetime=self.reset_ttl):
                raise unauthenticated('Invalid or expired reset value')
            user = self._user(user_id)
            record['used'] = True
            user['password'] = hash_password(new_password, minimum=self.password_min_length)
            self._revoke_sessions_for(user_id)
            self._revoke_credentials_for(user_id)
            self.store.save()
        return {'id': user_id, 'password_set': True}

    def _assert_not_final_owner(self, user_id):
        """Refuse any change that would leave an active project with zero owners.

        This applies to every caller including a superuser: the invariant protects
        the project record, not the caller. Recovery is an explicit, accepted
        operation: assign ``owner`` to another member first (which atomically
        creates a second owner), then demote or disable the previous one.
        """
        for pid, members in self.state['memberships'].items():
            if members.get(user_id) == 'owner' and \
                    sum(1 for role in members.values() if role == 'owner') <= 1:
                raise conflict('The final active project owner cannot be removed, demoted '
                               'or disabled; assign another owner first', {'project': pid})

    def disable_user(self, principal, user_id):
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to disable an account')
        with self.store.lock:
            self._refresh_authority(principal)
            user = self._user(user_id)
            if not principal.superuser and principal.user_id != user_id:
                raise not_found('Account not found')
            if user['superuser']:
                raise conflict('The superuser account cannot be disabled')
            # The final-owner invariant is enforced for every caller, including a
            # superuser disabling a project's sole owner.
            self._assert_not_final_owner(user_id)
            user['disabled'] = True
            self._revoke_sessions_for(user_id)
            self._revoke_credentials_for(user_id)
            self.store.save()
        return {'id': user_id, 'disabled': True}

    def _require_superuser(self, principal):
        with self.store.lock:
            self._refresh_authority(principal)
            if not principal.superuser:
                raise forbidden('Superuser authority required')

    def _revoke_sessions_for(self, user_id):
        for digest, session in self.state['sessions'].items():
            if session['user_id'] == user_id:
                session['revoked'] = True

    def _revoke_credentials_for(self, user_id):
        for credential in self.state['credentials'].values():
            if credential['user_id'] == user_id:
                credential['revoked'] = True

    # -- login, sessions and credentials ---------------------------------------
    def _throttle_key(self, username, source):
        return '%s|%s' % ((username or '').lower(), source or 'local')

    def _check_throttle(self, username, source):
        key = self._throttle_key(username, source)
        cutoff = self._now() - LOGIN_WINDOW_SECONDS
        attempts = [t for t in self._failures.get(key, []) if t >= cutoff]
        self._failures[key] = attempts
        if len(attempts) >= self.login_max_attempts:
            raise throttled()

    def _record_failure(self, username, source):
        key = self._throttle_key(username, source)
        self._failures.setdefault(key, []).append(self._now())

    #: Log-in attempts in flight at once: one being checked and the rest waiting their turn
    #: (the check is made under the state lock, one at a time, about 0.1 s each). One more is
    #: answered busy at once, before any check, so that a flood of attempts costs neither
    #: memory nor a queue without end (kittrial-5bb.170).
    LOGINS_AT_ONCE = 16
    LOGIN_BUSY = 'Too many people are logging in at this moment. Try again in a few seconds.'
    #: Of those places, how many one client address may hold (its ``address_group``; the
    #: address is the forwarded one only when a trusted proxy sent it). Without it about 20
    #: looping connections from one address, with no credentials, held all 16 places and
    #: kept every log-in out for as long as they ran (review of kittrial-5bb.170). A quarter:
    #: whatever one address sends, twelve places are left to the others, somebody with a
    #: place waits behind at most fifteen checks, and it takes four addresses acting
    #: together to fill them all. (Half was tried and measured: two addresses were then
    #: enough, and a person at another address waited several seconds to not at all behind
    #: one flooding address with a full audit log.) 0: no share per address (the service's
    #: own settings do not offer it: they take 1 to LOGINS_AT_ONCE).
    LOGINS_PER_ADDRESS = 4
    #: A log-in over its address's share is not turned away at once: it waits this long for
    #: one of its address's places. An office behind one router is one address, and its
    #: people logging in together at nine in the morning are each checked in a tenth of a
    #: second: the fifth to the tenth wait a moment and get in, where an instant refusal sent
    #: six of ten away. A flooding address gains nothing: it still holds four places and no
    #: more. After the wait the answer is the refusal it always was.
    LOGIN_WAIT_SECONDS = 2.0
    #: How many log-ins of one address may wait like that at once; one more is refused at
    #: once. Each waiter holds its connection's thread, so this bounds what a flooding
    #: address can park: 4 in flight and 12 waiting, however many connections it has.
    LOGIN_WAITERS_PER_ADDRESS = 12
    LOGIN_BUSY_ADDRESS = ('Too many log-ins from your address are being checked at this moment. '
                          'Try again in a few seconds.')

    def login(self, username, password, source='local', request_id=None, shared_source=False):
        """Uniform failure response; never reveals whether the account exists.

        ``shared_source``: the source is not one client's address but the address everybody
        arrives from (a trusted proxy that forwarded no address: the SSH tunnel of the
        first install). Such a log-in has no share per address to be held to, as its
        connection has no limit per address; the places in all still bound it.
        """
        group = None if shared_source else address_group(source or 'local')
        with self._logins_room:
            until, waiting = None, False
            while True:
                mine = self._logins_by_address.get(group, 0)
                if group is not None and self.LOGINS_PER_ADDRESS and mine >= self.LOGINS_PER_ADDRESS:
                    # Over its address's share: a short wait for one of that address's places.
                    if until is None:
                        until = time.monotonic() + self.LOGIN_WAIT_SECONDS
                        waiting = self._logins_waiting.get(group, 0) < self.LOGIN_WAITERS_PER_ADDRESS
                        if waiting:
                            self._logins_waiting[group] = self._logins_waiting.get(group, 0) + 1
                    left = until - time.monotonic()
                    if waiting and left > 0:
                        self._logins_room.wait(left)
                        continue
                    refused = self.LOGIN_BUSY_ADDRESS
                    self.logins_turned_away_for_address += 1
                elif self._logins >= self.LOGINS_AT_ONCE:
                    refused = self.LOGIN_BUSY
                else:
                    refused = None
                    self._logins += 1
                    if group is not None:
                        self._logins_by_address[group] = mine + 1
                break
            if waiting:
                parked = self._logins_waiting.get(group, 0) - 1
                if parked > 0:
                    self._logins_waiting[group] = parked
                else:
                    self._logins_waiting.pop(group, None)
            if refused:
                self.logins_turned_away += 1
        if refused:
            # Nothing was checked and nothing is counted against the name or the address, and
            # no count is cleared either. Not audited per attempt: a flood must not fill the
            # audit log.
            raise busy(refused, retry_after=5)
        try:
            return self._login(username, password, source, request_id)
        finally:
            with self._logins_room:
                self._logins -= 1
                if group is not None:
                    left = self._logins_by_address.get(group, 0) - 1
                    if left > 0:
                        self._logins_by_address[group] = left
                    else:
                        self._logins_by_address.pop(group, None)
                self._logins_room.notify_all()            # a place is free: whoever waits for one of this address's looks again

    def _login(self, username, password, source, request_id):
        try:
            self._check_throttle(username, source)
        except HttpError:
            self.audit(request_id, None, 'login', 'throttled', reason='rate limited')
            self.store.save()
            raise
        with self.store.lock:
            user_id = self.state['usernames'].get((username or '').lower())
            user = self.state['users'].get(user_id) if user_id else None
            verifier = user.get('password') if user else _dummy_verifier()
            good = verify_password(verifier, password or '')
            if not user or not good or user['disabled'] or not user.get('password'):
                self._record_failure(username, source)
                self.audit(request_id, None, 'login', 'failed', reason='invalid credentials')
                self.store.save()
                raise unauthenticated('Invalid credentials')
            if needs_rehash(user['password']):
                user['password'] = hash_password(password)
            token, csrf = new_token(), new_token()
            # The session's lifecycle is stamped on the monotone clock, so a session
            # issued during a forward jump that is later corrected does not live late.
            # The raw issuance/last-use stamps preserve the real idle and absolute
            # lifetimes while that monotone floor is still pinned ahead.
            moment = self._expiry_now()
            raw = self._raw_now()
            self.state['sessions'][token_hash(token)] = {
                'user_id': user['id'],
                'issued_at': moment,
                'issued_raw': raw,
                'last_used': moment,
                'last_used_raw': raw,
                'idle_expires': moment + self.session_idle,
                'absolute_expires': moment + self.session_absolute,
                'revoked': False,
                'csrf': csrf,
            }
            self._failures.pop(self._throttle_key(username, source), None)
            self.audit(request_id, Principal(user['id'], user['display_name'],
                                             user['superuser'], 'session', user['id']),
                       'login', 'committed')
            self.store.save()
        return {'session_token': token, 'csrf_token': csrf,
                'expires_at': now_iso(moment + self.session_absolute),
                'idle_expires_at': now_iso(moment + self.session_idle),
                'user': self._public_user(user)}

    def authenticate(self, token, *, source='local', required_scope=None):
        """Resolve a bearer session/credential value to a principal, or raise 401.

        Session and credential expiry are evaluated on the monotone clock
        (:meth:`_expiry_now`), never on the raw clock alone, so a backward step cannot
        revive an expired item; the item's raw issuance/last-use stamp adds its real
        lifetime as a second, independent expiry (:meth:`_expired`). An idle refresh
        stamps the new idle deadline on the monotone clock and the new raw last-use.
        """
        digest = token_hash(token)
        moment = self._expiry_now()
        with self.store.lock:
            session = self.state['sessions'].get(digest)
            if session is not None:
                if session['revoked'] or self._expired(
                        moment, expires_at=session['absolute_expires'],
                        issued_raw=session.get('issued_raw'),
                        lifetime=self.session_absolute) or self._expired(
                        moment, expires_at=session['idle_expires'],
                        issued_raw=session.get('last_used_raw'),
                        lifetime=self.session_idle):
                    raise unauthenticated('Session expired or revoked')
                user = self.state['users'].get(session['user_id'])
                if user is None or user['disabled']:
                    raise unauthenticated('Session expired or revoked')
                session['last_used'] = moment
                session['last_used_raw'] = self._raw_now()
                session['idle_expires'] = moment + self.session_idle
                # The answer of a request does not wait a minute for this stamp (kittrial-5bb.156).
                self.store.save_soon()
                return Principal(user['id'], user['display_name'], user['superuser'],
                                 'session', user['id'], csrf=session['csrf'],
                                 session_hash=digest)
            credential_id = self.state['credential_tokens'].get(digest)
            credential = self.state['credentials'].get(credential_id) if credential_id else None
            if credential is not None:
                # Revocation and expiry answer separately, and the expiry sentence names who
                # can act without naming any other credential (kittrial-5bb.294 item 2).
                if credential['revoked']:
                    raise unauthenticated(CREDENTIAL_REVOKED_MESSAGE)
                if self._credential_expired(credential, moment):
                    raise unauthenticated(CREDENTIAL_EXPIRED_MESSAGE)
                user = self.state['users'].get(credential['user_id'])
                if user is None or user['disabled']:
                    raise unauthenticated('Authentication is no longer valid')
                if required_scope and required_scope not in credential['scopes']:
                    raise forbidden('Credential scope does not permit this operation')
                credential['last_used'] = moment
                actor = credential.get('actor') or user['id']
                # An agent credential resolves to its agent record on every request: a
                # missing or disabled agent stops it even when the token itself was never
                # individually revoked, and a successful use stamps ``last_seen_at`` (the
                # manual-cadence signal the owner's web view shows). No background job or
                # poller exists; this is only written when the agent actually calls in.
                agent_id = credential.get('agent_id')
                if agent_id:
                    agent = self.state['agents'].get(agent_id)
                    if not isinstance(agent, dict) or not agent.get('enabled'):
                        raise unauthenticated('Agent is disabled')
                    agent['last_seen_at'] = now_iso(self._now())
                self.store.save_soon()
                # A credential carries ONLY the authority granted by its type, project
                # and scopes. It never inherits the issuing account's global superuser
                # authority: ``superuser`` is always False here, and scopes are the
                # only source of capability.
                return Principal(user['id'], user['display_name'], False,
                                 'credential', actor, credential_id=credential['id'],
                                 credential_project=credential['project_id'],
                                 scopes=credential['scopes'], agent_id=agent_id)
        raise unauthenticated('Authentication required')

    # -- live authority --------------------------------------------------------
    def _refresh_authority(self, principal):
        """Re-read the live session/credential behind ``principal``.

        The caller must hold ``store.lock``. This is the authority half of the
        serializable authorization/revocation boundary: a revocation that commits
        before this call is observed here, so an in-flight mutation can never
        inherit authority the caller has already lost. Derived fields are refreshed
        from live state rather than trusted from the original request.
        """
        if principal is None:
            raise unauthenticated()
        moment = self._expiry_now()
        if principal.via == 'session':
            session = self.state['sessions'].get(principal.session_hash)
            if session is None or session['revoked'] or self._expired(
                    moment, expires_at=session['absolute_expires'],
                    issued_raw=session.get('issued_raw'),
                    lifetime=self.session_absolute) or self._expired(
                    moment, expires_at=session['idle_expires'],
                    issued_raw=session.get('last_used_raw'),
                    lifetime=self.session_idle):
                raise unauthenticated('Session expired or revoked')
            user = self.state['users'].get(session['user_id'])
            if user is None or user['disabled']:
                raise unauthenticated('Authentication is no longer valid')
            principal.superuser = bool(user['superuser'])
            principal.display_name = user['display_name']
            return user
        if principal.via == 'credential':
            credential = self.state['credentials'].get(principal.credential_id)
            if credential is None:
                raise unauthenticated(CREDENTIAL_EXPIRED_MESSAGE)
            if credential['revoked']:
                raise unauthenticated(CREDENTIAL_REVOKED_MESSAGE)
            if self._credential_expired(credential, moment):
                raise unauthenticated(CREDENTIAL_EXPIRED_MESSAGE)
            user = self.state['users'].get(credential['user_id'])
            if user is None or user['disabled']:
                raise unauthenticated('Authentication is no longer valid')
            agent_id = credential.get('agent_id')
            if agent_id:
                agent = self.state['agents'].get(agent_id)
                if not isinstance(agent, dict) or not agent.get('enabled'):
                    raise unauthenticated('Agent is disabled')
            principal.superuser = False
            principal.scopes = tuple(credential['scopes'])
            principal.credential_project = credential['project_id']
            principal.actor = credential.get('actor') or principal.actor
            principal.agent_id = agent_id
            return user
        raise unauthenticated()

    def capabilities_for(self, principal, project_id):
        """The capabilities the live principal holds for ``project_id``.

        This routes through the same :func:`http_authority.decide` rule used by the
        route boundary and the canonical endpoint, so a credential can never exceed
        its issuer's current project role. The decision's ``now`` is the monotone clock,
        so a capability listing cannot outlive an item that has already expired, and the
        live item's raw real lifetime is applied first (:meth:`_live_item_expired`), so a
        pinned monotone floor does not list capabilities for a genuinely expired item.
        """
        moment = self._expiry_now()
        if self._live_item_expired(moment, principal):
            return frozenset()
        try:
            decide(self.state, authority_request(principal, project_id, CAP_READ),
                   now=moment)
        except AuthorityDenied:
            return frozenset()
        caps = set()
        for capability in (CAP_READ, CAP_TASKS, CAP_CHECKPOINTS, CAP_REVIEWS, CAP_FEEDBACK,
                           CAP_PROPOSALS, CAP_APPROVE, CAP_PROJECT_ADMIN):
            try:
                decide(self.state, authority_request(principal, project_id, capability),
                       now=moment)
                caps.add(capability)
            except AuthorityDenied:
                continue
        return frozenset(caps)

    def check_authority(self, principal, project_id, capability, *, allow_self_user=None):
        """Authorize one capability against live authority, or raise 401/403/404.

        This is the single boundary every route uses. When called inside a mutation
        it runs while ``store.lock`` is held and before the canonical write, so a
        revocation cannot interleave between the check and the write. The decision
        itself is :func:`http_authority.decide`, the same function the canonical
        endpoint runs immediately before the effect.
        """
        with self.store.lock:
            return self._check_authority_locked(principal, project_id, capability,
                                                allow_self_user=allow_self_user)

    def _check_authority_locked(self, principal, project_id, capability, *,
                                allow_self_user=None):
        self._refresh_authority(principal)
        try:
            decision = decide(self.state,
                              authority_request(principal, project_id, capability,
                                                now=self._expiry_now()),
                              allow_self_user=allow_self_user)
        except AuthorityDenied as denied:
            raise HttpError(denied.status, denied.code, denied.message, denied.detail)
        if allow_self_user is not None and principal.user_id == allow_self_user:
            return None, decision['role']
        if capability in (CAP_ACCOUNTS_ADMIN, CAP_PROJECT_CREATE, CAP_AGENTS):
            return None, decision['role']
        return self.state.get('projects', {}).get(project_id), decision['role']

    def revalidate_authority(self, principal):
        """Refresh live authority or raise; used by the backend write boundary."""
        with self.store.lock:
            return self._refresh_authority(principal)

    def session_expiry_view(self, principal):
        """The caller's own session window and deadlines, or ``None`` (kittrial-5bb.330).

        The authenticated session read is the light touch a browser keeps an open set-up
        wizard alive with, so it answers what that page needs: the idle window this
        installation configured (``idle_seconds``) and the live deadlines of this session
        (``idle_expires_at``, refreshed by every authenticated request, and
        ``absolute_expires_at``, which no use extends). Nothing about another session is
        read and no secret is included. A credential (a worker or agent key) has no browser
        session, so it gets ``None`` and the read answers exactly what it did before.
        """
        if principal is None or principal.via != 'session' or not principal.session_hash:
            return None
        session = self.state['sessions'].get(principal.session_hash)
        if not isinstance(session, dict):
            return None
        view = {'idle_seconds': self.session_idle, 'absolute_seconds': self.session_absolute}
        for key, stored in (('idle_expires_at', 'idle_expires'),
                            ('absolute_expires_at', 'absolute_expires')):
            deadline = session.get(stored)
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                    or not math.isfinite(deadline):
                # A damaged deadline is not invented: the read says nothing about it rather
                # than a time nobody can rely on. Authentication has already refused the
                # session itself when its own deadline is unusable.
                return None
            try:
                view[key] = now_iso(deadline)
            except (ValueError, OverflowError, OSError):
                return None
        return view

    def logout(self, principal, request_id=None):
        if principal is None or principal.session_hash is None:
            raise unauthenticated('No current session')
        with self.store.lock:
            session = self.state['sessions'].get(principal.session_hash)
            if session is not None:
                session['revoked'] = True
                self.audit(request_id, principal, 'logout', 'committed')
                self.store.save()
        return {'logged_out': True}

    def revoke_session(self, session_hash):
        with self.store.lock:
            session = self.state['sessions'].get(session_hash)
            if session:
                session['revoked'] = True
                self.store.save()

    # -- actor binding ---------------------------------------------------------
    def bind_actor(self, principal, submitted_actor):
        """Validate an optional actor label. It is attribution, never authority."""
        if submitted_actor is None:
            return principal.actor
        if not isinstance(submitted_actor, str) or not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}', submitted_actor):
            raise invalid('Invalid actor label')
        if principal.via == 'credential':
            namespace = principal.actor
            if submitted_actor != namespace and not submitted_actor.startswith(namespace.rstrip('/') + '/'):
                raise forbidden('Actor label is outside the credential attribution namespace')
            return submitted_actor
        if submitted_actor != principal.actor:
            raise forbidden('Actor label does not match the authenticated account')
        return submitted_actor

    # -- projects and membership ----------------------------------------------
    def create_project(self, principal, name, project_id=None, registered_by=None):
        """Write the web record of a project the caller creates in the ordinary way.

        ``registered_by`` is the mark the endpoint backend's register route keeps on a
        record a superuser stood behind (kittrial-5bb.80). It is written inside this one
        save, so no window remains between the project record and its mark in which a
        failure or a kill leaves a project on disk without it (kittrial-5bb.255). The
        ordinary create route passes none, so its records carry no such mark.

        A save that raises takes the record and the membership back out of memory
        before the error is re-raised (kittrial-5bb.255, the register half of R1):
        otherwise the next save of anything -- a GET's last-use stamp is enough --
        would make an unaudited registration durable, leaving a usable project with no
        committed ``projects.create`` audit entry whose key, and every new key, refuse
        as "already registered". Both callers (the ordinary create route and the
        register route) get the same undo, because both come through here.
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('A worker credential cannot create projects')
        self._validate_project_name(name)
        with self.store.lock:
            self._refresh_authority(principal)
            if any(p['name'].lower() == name.lower() and not p['archived']
                   for p in self.state['projects'].values()):
                raise conflict('A project with that name already exists')
            pid = project_id or 'proj_' + secrets.token_hex(8)
            if pid in self.state['projects']:
                raise conflict('Project identifier already exists')
            record = {
                'id': pid, 'name': name, 'created_by': principal.user_id,
                'created_at': now_iso(self._now()), 'archived': False,
            }
            if registered_by is not None:
                record['registered_by'] = registered_by
            # The record is new (the id was just shown absent), so it is removed
            # exactly; a membership under this id can only be stale state, so it is
            # put back exactly as it was rather than assumed absent.
            prior_members = self.state['memberships'].get(pid)
            self.state['projects'][pid] = record
            self.state['memberships'][pid] = {principal.user_id: 'owner'}
            try:
                self.store.save()
            except BaseException:
                self.state['projects'].pop(pid, None)
                if prior_members is None:
                    self.state['memberships'].pop(pid, None)
                else:
                    self.state['memberships'][pid] = prior_members
                raise
        return self.project_view(principal, pid)

    @staticmethod
    def _validate_project_name(name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _.-]{1,63}',
                                                         name or ''):
            raise invalid('Project name must be 2-64 characters of letters, digits, space, . _ -')

    def list_projects(self, principal):
        with self.store.lock:
            self._refresh_authority(principal)
            if principal.via == 'credential':
                if principal.agent_id:
                    # An agent credential reads exactly its live grant list, and only the
                    # projects its owner can still open: a narrowed grant or a lost
                    # membership drops a project from this list immediately.
                    agent = self.state['agents'].get(principal.agent_id)
                    visible = []
                    for pid in (agent.get('projects') if agent else []) or []:
                        try:
                            visible.append(self.project_view(principal, pid))
                        except HttpError:
                            continue
                    return visible
                pid = principal.credential_project
                if pid not in self.state['projects']:
                    return []
                return [self.project_view(principal, pid)]
            visible = []
            for pid in self.state['projects']:
                if principal.superuser or principal.user_id in self.state['memberships'].get(pid, {}):
                    visible.append(self.project_view(principal, pid))
            return visible

    #: Where a project's repository is (kittrial-5bb.118): a LABEL an owner writes, shown
    #: to members and delivered to agents as data. Only its shape is checked. It is never
    #: fetched, cloned or run by the kit, and nothing checks that it exists or is reachable.
    REPOSITORY_MAX = 300
    #: The four accepted forms of a repository location, each matched in full. Nothing
    #: else is accepted, so a remote-helper form (``ext::``, ``fd::``), a one-slash
    #: scheme, a relative path, an option and a host that starts with ``-`` all fail.
    #: Every pattern is ASCII-only and case-sensitive (kittrial-5bb.123): a case-insensitive
    #: match let the Kelvin sign, the long s and the dotless i through as host letters.
    _REPO_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    _REPO_HOST = r'(?P<host>%s(?:\.%s)*)' % (_REPO_LABEL, _REPO_LABEL)
    #: A plain account name. Short on purpose: an opaque token does not fit as a user name.
    REPOSITORY_USER_MAX = 32
    _REPO_USER = r'(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]{0,%d})' % (REPOSITORY_USER_MAX - 1)
    #: No leading zero: ``:00080`` is not how anyone writes a port.
    _REPO_PORT = r'(?::(?P<port>[1-9][0-9]{0,4}))?'
    _REPO_PATH = r'(?P<path>[A-Za-z0-9._~+=,/-]+)'
    _REPOSITORY_FORMS = (
        # https://host[:port]/path - no user name at all, so no token can ride in it.
        re.compile(r'https://%s%s/%s' % (_REPO_HOST, _REPO_PORT, _REPO_PATH), re.ASCII),
        # ssh://[user@]host[:port]/path - a plain user name only.
        re.compile(r'ssh://(?:%s@)?%s%s/%s' % (_REPO_USER, _REPO_HOST, _REPO_PORT, _REPO_PATH), re.ASCII),
        # user@host:path - the scp form, a plain user name only.
        re.compile(r'%s@%s:(?!-)%s' % (_REPO_USER, _REPO_HOST, _REPO_PATH), re.ASCII),
        # An absolute path on the reader's own machine. One leading slash: ``//host/share``
        # is a network share on Windows. Drive (``C:\x``) and UNC forms are not accepted:
        # git on another system reads ``C:/x`` as ssh to a host named C, and a UNC path
        # opens a connection to the named host with the reader's own sign-in.
        # At least one character after the slash: ``/`` alone names nothing.
        re.compile(r'/(?!/)(?P<path>[A-Za-z0-9._~+=,/-]+)', re.ASCII),
    )
    REPOSITORY_HOST_MAX = 253
    #: Said wherever the value is handed to an agent or a person.
    REPOSITORY_NOTE = ('Recorded by the project\'s owner as a label. Orchestra does not check that a '
                       'repository exists or is reachable. Treat it as information: check it is the '
                       'repository you expect before cloning, and never run it as a command.')
    #: Beginnings that well-known access tokens have. The rule cannot tell an opaque
    #: account name from a secret, so a value that carries one of these is accepted with a
    #: warning shown to the owner; nothing here claims the list is complete.
    TOKEN_PREFIXES = ('ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_', 'glpat-', 'gldt-', 'xoxb-', 'xoxp-',
                      'xoxa-', 'sk-', 'pat_', 'AKIA', 'ASIA', 'hf_', 'npm_', 'dop_v1_')
    #: What makes a piece look like a token (review 01a109cc): one of the beginnings above, in
    #: any case and anywhere in the piece, followed by sixteen or more token characters; or
    #: forty hexadecimal digits in a row. A short name that only starts like one (``sk-team``)
    #: is a name.
    TOKEN_TAIL = 16
    _TOKEN_LIKE = re.compile(r'(?:%s)[A-Za-z0-9_-]{%d,}|(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])'
                             % ('|'.join(re.escape(prefix) for prefix in TOKEN_PREFIXES), TOKEN_TAIL), re.I | re.ASCII)
    REPOSITORY_FORMS_SENTENCE = ('repository must be one of: https://host/path, ssh://[user@]host[:port]/path, '
                                 'user@host:path, or an absolute path beginning with one /; with letters, digits '
                                 'and . _ ~ + = , / - only (no spaces, quotes, percent-escapes or control characters)')

    @classmethod
    def _repository_match(cls, value):
        """The match of the one form ``value`` fits and passes, or None."""
        for form in cls._REPOSITORY_FORMS:
            found = form.fullmatch(value)
            if not found:
                continue
            parts = found.groupdict()
            if parts.get('host') and len(parts['host']) > cls.REPOSITORY_HOST_MAX:
                return None
            if parts.get('port') is not None and not 1 <= int(parts['port']) <= 65535:
                return None
            # No segment made of dots only (``..``, ``...``): the first climbs out of the path,
            # the others name nothing a repository is at.
            if any(len(segment) > 1 and not segment.strip('.') for segment in (parts.get('path') or '').split('/')):
                return None
            # ``c:x.git`` after a user name: a one-letter host is how a drive is written, and
            # git on another system reads it as one.
            if form is cls._REPOSITORY_FORMS[2] and len(parts['host']) < 2:
                return None
            return found
        return None

    @classmethod
    def validate_repository(cls, value):
        """A repository location, or None to clear it. Raises ``invalid`` otherwise.

        Accepted, and nothing else (the whole value must match one form):

        * ``https://host[:port]/path`` - with no user name, so no token or password;
        * ``ssh://[user@]host[:port]/path`` and ``user@host:path`` - a plain user name of
          at most REPOSITORY_USER_MAX characters;
        * an absolute path beginning with exactly one ``/``.

        ASCII only; the scheme in lower case; a host is dot-separated labels that each
        start and end with a letter or digit (two characters at least in the
        ``user@host:path`` form); a port is 1 to 65535 with no leading zero; no path
        segment made of dots only; no percent-escape, space, quote, control or format character; at most
        REPOSITORY_MAX characters. The kit does not judge where a host points
        (``localhost`` and an address are hosts like any other). The refusal never
        repeats the value: it may hold a secret.
        """
        if value is None or value == '':
            return None
        if not isinstance(value, str) or len(value) > cls.REPOSITORY_MAX:
            raise invalid('repository must be text of at most %d characters' % cls.REPOSITORY_MAX)
        if re.match(r'https?://[^/]*@', value, re.I) or re.match(r'[A-Za-z][A-Za-z0-9+.-]*://[^/@]*:[^/@]*@', value) \
                or re.match(r'[^/@:]*:[^/@]*@', value):
            raise invalid('repository must not contain a user name, token or password on an https URL, or a '
                          'password anywhere; give the location only')
        if cls._repository_match(value) is None:
            raise invalid(cls.REPOSITORY_FORMS_SENTENCE)
        return value

    @classmethod
    def repository_warning(cls, value):
        """A sentence for the owner when an accepted value looks as if it carries a token, else None.

        The value is accepted either way: a user name or a path segment that begins like
        a well-known access token may be an ordinary name. The sentence never repeats it.
        """
        found = cls._repository_match(value) if isinstance(value, str) else None
        if found is None:
            return None
        parts = found.groupdict()
        pieces = [parts.get('user') or ''] + (parts.get('path') or '').split('/')
        if any(cls._TOKEN_LIKE.search(piece) for piece in pieces if piece):
            return ('Part of this value looks like an access token. Every member and agent of the project can '
                    'read it. If it is a token, replace the value with the location only and revoke the token.')
        return None

    @classmethod
    def stored_repository(cls, project):
        """``(value, needs_attention)`` for what a project record holds (kittrial-5bb.123).

        The rule has changed since some values were stored. A stored value that no longer
        passes is not shown as the repository and is not delivered to agents: the record
        needs an owner's attention instead. Nothing is rewritten on read.
        """
        value = project.get('repository') if isinstance(project, dict) else None
        if not value:
            return None, False
        try:
            return cls.validate_repository(value), False
        except HttpError:
            return None, True

    def set_project_repository(self, principal, project_id, repository, request_id=None):
        """Record (or clear) where the project's repository is. Owners only."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change a project')
        value = self.validate_repository(repository)
        with self.store.lock:
            self._refresh_authority(principal)
            project, _ = self.require_project(principal, project_id, 'owner')
            if project.get('repository') != value:
                if value is None:
                    project.pop('repository', None)
                else:
                    project['repository'] = value
                self.audit(request_id, principal, 'projects.repository', 'committed', project_id=project_id,
                           reason='set' if value else 'cleared')
                self.store.save()
        return {'id': project_id, 'repository': value}

    def project_view(self, principal, project_id):
        project, role = self.require_project(principal, project_id)
        view = dict(project)
        made = view.get('host_created')
        if isinstance(made, dict) and 'operation' in made:
            # The digest that recognises the creator's own repeat is the service's own: no other
            # account or body can match it, and no reader needs it (kittrial-5bb.156 review).
            view['host_created'] = {key: value for key, value in made.items() if key != 'operation'}
        # The repository as today's rule reads it (kittrial-5bb.123): a stored value that
        # no longer passes is withheld and flagged; a passing one carries the note that it
        # is information, wherever a member or an agent credential reads the project.
        value, attention = self.stored_repository(project)
        view['repository'] = value
        view['repository_note'] = self.REPOSITORY_NOTE if value else None
        view['repository_needs_attention'] = attention
        view['repository_warning'] = self.repository_warning(value) if value else None
        view['role'] = role
        view['members'] = sorted(self.state['memberships'].get(project_id, {}))
        return view

    def require_project(self, principal, project_id, minimum='viewer'):
        """Role/capability gate kept for internal callers.

        ``minimum`` maps onto the capability model so a credential can never borrow
        the issuing account's role: viewer -> read, contributor -> a write
        capability, owner -> project administration (credentials never hold it).
        """
        capability = {'viewer': CAP_READ, 'contributor': CAP_TASKS,
                      'owner': CAP_PROJECT_ADMIN}.get(minimum, CAP_READ)
        return self.check_authority(principal, project_id, capability)

    def set_member(self, principal, project_id, user_id, role, request_id=None):
        if role not in ROLES:
            raise invalid('Role must be one of %s' % ', '.join(ROLES))
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required for membership changes')
        with self.store.lock:
            self._refresh_authority(principal)
            project, actor_role = self.require_project(principal, project_id, 'owner')
            self._user(user_id)
            members = self.state['memberships'][project_id]
            current = members.get(user_id)
            if role == 'owner' and not principal.superuser:
                raise forbidden('Only a superuser may assign the owner role')
            if current == 'owner' and role != 'owner':
                if not principal.superuser:
                    raise forbidden('Only a superuser may remove a project owner')
                # Even a superuser cannot demote the sole owner: that would leave the
                # project with no owner. Ownership must be replaced, not deleted.
                if sum(1 for r in members.values() if r == 'owner') <= 1:
                    raise conflict('The final active project owner cannot be demoted; '
                                   'assign another owner first', {'project': project_id})
            members[user_id] = role
            self.store.save()
        return {'project': project_id, 'user': user_id, 'role': role}

    def remove_member(self, principal, project_id, user_id, request_id=None):
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required for membership changes')
        with self.store.lock:
            self._refresh_authority(principal)
            _, _ = self.require_project(principal, project_id, 'owner')
            members = self.state['memberships'].get(project_id, {})
            if user_id not in members:
                raise not_found('Membership not found')
            if members[user_id] == 'owner':
                if not principal.superuser:
                    raise forbidden('Only a superuser may remove a project owner')
                self._assert_not_final_owner(user_id)
            del members[user_id]
            # An account that leaves a project takes its agents' coordinator grants there with
            # it (kittrial-5bb.261): a grant left on record was effective again, with no new
            # decision, once an owner added the account back as a coordinator. Lowering a role
            # keeps the grant on record without effect, as before.
            for agent in list((self.state.get('agents') or {}).values()):
                if isinstance(agent, dict) and agent.get('owner') == user_id and isinstance(agent.get('id'), str):
                    self._end_coordinator_grants(agent, [project_id], principal, request_id,
                                                 'the account was removed from the project')
            self.store.save()
        return {'project': project_id, 'user': user_id, 'removed': True}

    def archive_project(self, principal, project_id):
        if principal is None or principal.via == 'credential':
            raise forbidden('A worker credential cannot archive a project')
        with self.store.lock:
            self._refresh_authority(principal)
            project, role = self.require_project(principal, project_id, 'owner')
            if project['archived']:
                raise conflict('Project is already archived')
            project['archived'] = True
            self.store.save()
        return {'id': project_id, 'archived': True}

    # -- worker credentials ----------------------------------------------------
    def issue_credential(self, principal, project_id, *, label=None, scopes=None,
                         actor=None, request_id=None, actor_checked=False, actor_waived=None):
        if principal is None or principal.via == 'credential':
            raise forbidden('A worker credential cannot issue another credential')
        with self.store.lock:
            self._refresh_authority(principal)
            project, role = self.require_project(principal, project_id, 'owner')
            requested = self._scope_names(scopes) or ('tasks', 'checkpoints', 'reviews', 'feedback')
            label = self._credential_label(label)
            if actor is not None and (not isinstance(actor, str) or not re.fullmatch(
                    r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,63}', actor)):
                raise invalid('Invalid credential actor namespace')
            # The id shapes are what make a record read as written by that account or
            # agent (kittrial-5bb.70 review 01a10262): the only one an owner may name is
            # their own account id. Compared case-folded (kittrial-5bb.199 round 2, N8), so
            # an id in capitals is the same id rather than a name that only looks like it.
            claimed = actor.split('/', 1)[0] if actor is not None else None
            if claimed is not None and re.fullmatch(r'(?:usr|agent)_[0-9a-f]{16}', claimed.casefold()) \
                    and claimed.casefold() != str(principal.user_id).casefold():
                raise invalid('A credential actor namespace cannot have the shape of an account or agent id '
                              'other than your own account id')
            if actor is not None:
                # One name has one issuer at a time (kittrial-5bb.199 review): with the party
                # rule on, work under a name is of every account that ever issued it, so a
                # second account must not be able to take a name that is in use, written
                # under yet or not. The same account may: that is how it replaces a lane's
                # credential without a gap, and both are its own party.
                moment = self._expiry_now()
                for other in self.state['credentials'].values():
                    if not isinstance(other, dict) or other.get('agent_id') or other.get('project_id') != project_id \
                            or other.get('user_id') == principal.user_id or not self._credential_live(other, moment):
                        continue
                    theirs = other.get('actor')
                    if self._holds(theirs, actor) or self._holds(actor, theirs):
                        raise conflict('The name %s is held by a working credential of this project that another account '
                                       'issued (%s, issued by %s). A name has one issuer at a time: have that credential '
                                       'revoked first, or choose another name'
                                       % (actor, theirs, self._owner_name(other.get('user_id'))),
                                       {'held_by_credential': other.get('id')})
            secret = new_token()
            # ``created_at`` is informational and stays on the raw clock; the credential's
            # expiry is stamped on the monotone clock, so a credential issued during a
            # forward jump that is later corrected does not live late. ``issued_raw`` keeps
            # the real issuance instant, so the TTL still bounds it while the monotone floor
            # is pinned ahead of the corrected host clock.
            moment = self._expiry_now()
            credential = {
                'id': 'cred_' + secrets.token_hex(8),
                'user_id': principal.user_id,
                'project_id': project_id,
                'label': label or 'worker',
                'scopes': list(requested),
                'actor': actor,
                # The backend read the project's tracker rows and this name was free when it
                # was issued: every row under it from now on is this credential's own, so no
                # later write needs to read the tracker again (kittrial-5bb.188 item 1). A
                # credential issued before that rule has no mark and is judged against the
                # tracker ONCE, at its first write (item 5).
                'actor_rows_checked': bool(actor_checked),
                'token_hash': token_hash(secret),
                'created_at': now_iso(self._now()),
                'issued_raw': self._raw_now(),
                'last_used': None,
                'expires_at': moment + self.credential_ttl,
                'revoked': False,
            }
            if isinstance(actor_waived, dict):
                # A superuser allowed a name the tracker already holds (kittrial-5bb.188 item
                # 4). Its own mark, NOT actor_rows_checked: who used the waiver, why, and when.
                credential['actor_waived'] = {
                    'by': actor_waived.get('by'),
                    'reason': actor_waived.get('reason'),
                    'at': now_iso(self._now()),
                }
            self.state['credentials'][credential['id']] = credential
            self.state['credential_tokens'][token_hash(secret)] = credential['id']
            self.store.save()
        answer = {'id': credential['id'], 'project': project_id, 'label': credential['label'],
                  'scopes': list(requested), 'actor': actor, 'secret': secret,
                  'expires_at': now_iso(credential['expires_at']), 'secret_available': True}
        if 'actor_waived' in credential:
            answer['actor_waived'] = dict(credential['actor_waived'])
        return answer

    def credential_effective_expiry(self, credential):
        """The stored deadline AS READ, capped from the most recent legitimate write.

        The cap is the shared :func:`http_authority.credential_expiry_cap`: 366 days from
        ``agent_written_raw`` (issuance or the last extension), falling back to the
        issuance stamp for a record written before the field existed (kittrial-5bb.294
        item 4). Records with no readable stamp, or no numeric deadline, are returned
        untouched, so the older legacy reader keeps its meaning.
        """
        expires = credential.get('expires_at')
        cap = credential_expiry_cap(credential)
        if cap is None or type(expires) is bool or not isinstance(expires, (int, float)):
            return expires
        if not math.isfinite(expires):
            return expires
        return min(float(expires), cap)

    def credential_effective_raw_expiry(self, credential):
        """The raw deadline AS READ, capped like :meth:`credential_effective_expiry`."""
        raw = credential.get('agent_raw_expires_at')
        cap = credential_expiry_cap(credential)
        if cap is None or type(raw) is bool or not isinstance(raw, (int, float)):
            return raw
        if not math.isfinite(raw):
            return raw
        return min(float(raw), cap)

    def agent_credential_expiry_view(self, credential):
        """Secret-free expiry metadata, also safe for a project's owner controls."""
        moment = self._expiry_now()
        working = self._credential_live(credential, moment)
        expires = self.credential_effective_expiry(credential)
        deadline = expires if isinstance(expires, (int, float)) and \
            not isinstance(expires, bool) else None
        remaining = deadline - moment if deadline is not None else None
        if credential.get('agent_id') and 'agent_raw_expires_at' in credential and working:
            raw = self.credential_effective_raw_expiry(credential)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                raw_remaining = raw - self._raw_now()
                remaining = raw_remaining if remaining is None else min(remaining, raw_remaining)
        return {'id': credential['id'],
                'expires_at': now_iso(deadline) if deadline is not None else None,
                'working': bool(working), 'revoked': bool(credential.get('revoked')),
                'expires_soon': bool(working and remaining is not None and remaining <= 7 * 86400)}

    def credential_view(self, credential):
        view = {'id': credential['id'], 'project': credential['project_id'],
                'agent': credential.get('agent_id'),
                'label': credential['label'], 'scopes': list(credential['scopes']),
                'actor': credential.get('actor'), 'revoked': credential['revoked'],
                'created_at': credential['created_at'], 'last_used': credential.get('last_used'),
                'expires_at': now_iso(credential['expires_at'])}
        if credential.get('agent_id'):
            view.update(self.agent_credential_expiry_view(credential))
        # A waived name is shown as allowed, by whom and when (kittrial-5bb.188 item 4): it is
        # not colliding and it does write. Never actor_rows_checked: the two are distinct.
        if isinstance(credential.get('actor_waived'), dict):
            view['actor_waived'] = dict(credential['actor_waived'])
        return view

    def revoke_credential(self, principal, project_id, credential_id, request_id=None):
        if principal is None or principal.via == 'credential':
            raise forbidden('A worker credential cannot revoke credentials')
        with self.store.lock:
            self._refresh_authority(principal)
            self.require_project(principal, project_id, 'viewer')
            credential = self.state['credentials'].get(credential_id)
            if not isinstance(credential, dict) or credential.get('project_id') != project_id:
                raise not_found('Credential not found')
            own = credential['user_id'] == principal.user_id
            if not (principal.superuser or own):
                _, role = self.require_project(principal, project_id, 'owner')
            if credential['revoked']:
                return {'id': credential_id, 'revoked': True}
            credential['revoked'] = True
            # The revocation instant bounds a later renewal's claim to this credential's rows
            # (kittrial-5bb.188 item 3). A record revoked before this field existed carries no
            # time and lends nothing, which is the fail-closed reading.
            credential['revoked_at'] = now_iso(self._now())
            self.store.save()
        return {'id': credential_id, 'revoked': True}

    # -- web interface reads (kittrial-5bb.20) ----------------------------------
    #
    # Each read re-checks live authority itself (the route checks it too), so a
    # caller that reaches these methods some other way still meets the boundary.
    def list_members(self, principal, project_id):
        """The project's members with their display identity and role.

        Readable by anyone who may read the project (``CAP_READ``): the same
        principals already see the member ids in :meth:`project_view`. Only public
        account fields are returned; never a verifier, session or credential.
        """
        with self.store.lock:
            self.check_authority(principal, project_id, CAP_READ)
            # Account state (disabled, superuser) is administrative detail: only a
            # principal that administers this project's membership receives it.
            try:
                self.check_authority(principal, project_id, CAP_PROJECT_ADMIN)
                admin = True
            except HttpError:
                admin = False
            members = self.state['memberships'].get(project_id, {})
            items = []
            for user_id, role in members.items():
                user = self.state['users'].get(user_id) or {}
                item = {'user_id': user_id, 'username': user.get('username'),
                        'display_name': user.get('display_name') or user_id, 'role': role}
                if admin:
                    item['disabled'] = bool(user.get('disabled'))
                    item['superuser'] = bool(user.get('superuser'))
                items.append(item)
        items.sort(key=lambda m: ((m['display_name'] or '').lower(), m['user_id']))
        return items

    def list_worker_credentials(self, principal, project_id):
        """Metadata of the project's worker credentials: never a secret or its hash.

        Project administration (owner/superuser) only, the same authority that may
        issue them. Personal-agent credentials are governed on the agent routes and
        are not listed here.
        """
        with self.store.lock:
            self.check_authority(principal, project_id, CAP_PROJECT_ADMIN)
            items = []
            for credential in self.state['credentials'].values():
                if not isinstance(credential, dict) or credential.get('agent_id') or \
                        credential.get('project_id') != project_id:
                    continue
                view = self.credential_view(credential)
                view['user_id'] = credential.get('user_id')
                view['user_name'] = self._owner_name(credential.get('user_id'))
                if isinstance(view.get('actor_waived'), dict):
                    # The owner list names the person who allowed the name, not only their id
                    # (kittrial-5bb.188 item 4).
                    view['actor_waived']['by_name'] = self._owner_name(view['actor_waived'].get('by'))
                items.append(view)
        items.sort(key=lambda c: (c['created_at'] or '', c['id']))
        return items

    def lookup_account(self, principal, project_id, username, request_id=None):
        """Exact username lookup for adding a member to ``project_id``.

        Only a principal that may administer that project's membership can ask. The
        match is exact (case-insensitive, as usernames are unique that way); a
        missing, disabled or malformed-but-valid name all give the same 404.

        Residual risk: any session may create a project and so administer one, so a
        determined account holder can still test usernames one at a time. Each
        principal is therefore held to :data:`LOOKUP_MAX_PER_WINDOW` lookups per
        :data:`LOOKUP_WINDOW_SECONDS` (429 beyond it), and every authorized lookup is
        audited on the project with a keyed digest of the queried name (see
        :meth:`_lookup_digest`), never the name.
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to look up accounts')
        if not isinstance(username, str) or not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9_.@-]{1,63}', username):
            raise invalid('username must be 2-64 characters of letters, digits, . _ @ -')
        with self.store.lock:
            self.check_authority(principal, project_id, CAP_PROJECT_ADMIN)
            digest = 'username_hmac=' + self._lookup_digest(username)
            cutoff = self._now() - self.lookup_window
            recent = [t for t in self._lookups.get(principal.user_id, []) if t >= cutoff]
            if len(recent) >= self.lookup_max:
                self._lookups[principal.user_id] = recent
                self.audit(request_id, principal, 'accounts.lookup', 'throttled',
                           project_id=project_id, reason=digest)
                self.store.save()
                raise throttled('Too many account lookups; wait a few minutes and try again')
            recent.append(self._now())
            self._lookups[principal.user_id] = recent
            user_id = self.state['usernames'].get(username.lower())
            user = self.state['users'].get(user_id) if user_id else None
            found = bool(user) and not user.get('disabled')
            self.audit(request_id, principal, 'accounts.lookup',
                       'found' if found else 'not_found', project_id=project_id, reason=digest)
            self.store.save()
            if not found:
                raise not_found('No active account with that username')
            return {'id': user['id'], 'username': user['username'],
                    'display_name': user['display_name']}

    def _lookup_digest(self, username):
        """HMAC-SHA256 (first 16 hex) of the lower-cased username under a deployment key.

        The key is 32 random bytes generated once and kept only in the private service
        state (``lookup_audit_key``); it is never logged, audited or returned. A reader
        of the audit can see that the same name was probed repeatedly but cannot
        reverse a digest by hashing a dictionary of likely usernames. Call with
        ``store.lock`` held.
        """
        key = self.state.get('lookup_audit_key')
        if not isinstance(key, str) or len(key) < 32:
            key = self.state['lookup_audit_key'] = secrets.token_hex(32)
            self.store.save()
        return hmac.new(bytes.fromhex(key), username.lower().encode('utf-8'),
                        hashlib.sha256).hexdigest()[:16]

    def actor_names(self, actors):
        """Display names for task actors (account ids and agent ids), best effort.

        Anything that is neither a known account nor a known agent keeps its label.
        Canonical rows may carry a structured (non-string) author; looking one of
        those up simply finds no name.
        """
        names = ActorNames()
        with self.store.lock:
            for actor in actors:
                if not isinstance(actor, str) or actor in names:
                    continue
                user = self.state['users'].get(actor)
                agent = self.state['agents'].get(actor)
                if isinstance(user, dict):
                    names[actor] = user.get('display_name') or actor
                elif isinstance(agent, dict):
                    names[actor] = '%s (agent of %s)' % (agent.get('name') or actor,
                                                         self._owner_name(agent.get('owner')))
                else:
                    names[actor] = actor
        return names

    def actor_person(self, actor):
        """The account behind a task actor: an agent's owner, or the account itself.

        Anything that is neither a known agent nor a known account is its own label, so
        two unknown labels are the same person only when they are equal.
        """
        with self.store.lock:
            return http_actor_person(self.state, actor)

    def actor_parties(self, actor, project_id=None, full=False):
        """The accounts a task actor's work belongs to: its PARTY, as a set.

        With the setting off this is ``{actor_person(actor)}`` and nothing else, so every rule
        that asks it answers as before. With it on, one party is an account, every agent that
        account made, and every worker credential that account issued (kittrial-5bb.199):

        * an agent: its owner; an account: itself;
        * a name whose FIRST SEGMENT is an account or an agent (``ACCOUNT/x``, ``AGENT/x``):
          that account, before any credential is looked at. Only that account's own
          credentials can write such a name: one issued with no name writes under its
          issuer's account id and any label below it, and an agent's under the agent's;
        * any other name written under a WORKER CREDENTIAL of ``project_id``: every account
          that has ever issued a credential whose actor namespace holds the name (the name
          itself, or ``name/...``), working, revoked or expired. Nothing on a record says
          which credential wrote it, and a moment cannot say it either (the tracker's stamp
          is up to a second later than this service's clock), so where more than one account
          has held a name, the work is of each of them: that only ever refuses more;
        * anything else: the name itself. A writer that is no account, no agent and under no
          credential record (a name of the host route) is its own party.
        """
        # ``full``: an agent that approves is judged by party whatever the setting says
        # (kittrial-5bb.209); a person's approval follows the setting.
        with self.store.lock:
            if not (self.approval_by_another_party or full):
                return {http_actor_person(self.state, actor)}
            return full_actor_parties(self.state, actor, project_id)

    @staticmethod
    def _holds(namespace, name):
        """Whether a credential issued with ``namespace`` may write under ``name``."""
        # A credential issued with NO name has ``actor`` None and holds no name: it writes under its
        # issuer's account id, which no other account can be issued (review of kittrial-5bb.199, round 2:
        # a None here raised AttributeError and answered 500 to every other account's named credential).
        #
        # The head is compared CASE-FOLDED, as the tracker's own name rule folds it (kittrial-5bb.199
        # round 2, N2: actor_names.head). ``IT-A745`` and ``it-a745`` are one name: a second account may
        # not take the other spelling, and work under either spelling belongs to everyone who held it.
        # Without this, the first holder's credentials could be revoked and the other account issued the
        # lower-case name with no waiver, after which old work belonged to both and neither could approve.
        return namespace_holds(namespace, name)

    def note_settings(self):
        """Record what the installation's settings are at this start; answers ``(was, is)``.

        Turning ``approval_by_another_party`` on or off is done outside the service (a flag,
        or the office configuration) and used to leave no trace. The state keeps what the
        last start had, and a start whose rule differs from a readable record writes one
        audit entry. A record that is anything but a JSON ``true`` or ``false`` -- absent,
        null, a string such as ``"yes"`` or ``"false"``, a list, an object or a number -- is
        UNKNOWN, not off (kittrial-5bb.199 round 2, N5): the entry and the start line say so
        rather than inventing an ``off -> on`` change or staying silent after a lost ON.
        """
        with self.store.lock:
            seen = self.state.get('settings_seen')
            raw = seen.get('approval_by_another_party') if isinstance(seen, dict) else None
            was = raw if isinstance(raw, bool) else None
            now = bool(self.approval_by_another_party)
            if was != now:
                self.audit(None, None, 'settings.approval_by_another_party', 'committed',
                           reason='%s -> %s at service start'
                                  % ('unknown' if was is None else ('on' if was else 'off'),
                                     'on' if now else 'off'))
                self.state['settings_seen'] = {'approval_by_another_party': now}
                self.store.save()
            return was, now

    def username_of(self, user_id):
        """The account's username, read under the store lock (``None`` if unknown)."""
        with self.store.lock:
            user = self.state['users'].get(user_id)
            return user.get('username') if isinstance(user, dict) else None

    # -- personal agents -------------------------------------------------------
    #
    # An agent is a personal identity owned by exactly one user: "Kestrel, agent of
    # Priya". It is NOT a project role and NOT a user account. It may be granted access
    # to projects its owner belongs to, and every grant is capped at the owner's LIVE
    # role on that project by the same ``http_authority.decide`` rule the .19 worker
    # credentials use, so a demotion, removal or account disable narrows or stops the
    # agent on its next request. ``working_directory`` is a free-text hint for the
    # owner's own machine; the server stores it and never reads it, and only the owner
    # (or a superuser) ever receives it in a response.
    def agent_approves(self, agent_id, project_id):
        """Whether a live credential of the agent may approve in the project now: it holds the
        coordinator grant there and its account is a coordinator or an owner (kittrial-5bb.209).
        Used to word what the agent is shown; it grants nothing."""
        from http_authority import credential_capabilities
        with self.store.lock:
            agent = self.state['agents'].get(agent_id)
            if not isinstance(agent, dict) or not agent.get('enabled') or \
                    project_id not in (agent.get('projects') or []):
                return False
            owner = self.state['users'].get(agent.get('owner'))
            if not isinstance(owner, dict) or owner.get('disabled'):
                return False
            moment = self._expiry_now()
            for credential in self.state.get('credentials', {}).values():
                if not isinstance(credential, dict) or credential.get('agent_id') != agent_id or \
                        credential.get('revoked') or credential.get('expires_at', 0) <= moment:
                    continue
                try:
                    if CAP_APPROVE in credential_capabilities(self.state, credential, owner, project_id):
                        return True
                except AuthorityDenied:
                    continue
            return False

    def agent_review_standing(self, agent_id, project_id):
        """``(may_review, owner_approves)`` for one agent in one project, from live state.

        ``may_review``: a live credential of the agent holds the reviews capability there
        (its scope, capped by its owner's current role). ``owner_approves``: the agent's
        owner can approve in that project. Used to decide which review work an agent is
        shown (kittrial-5bb.115); it grants nothing.
        """
        from http_authority import credential_capabilities
        with self.store.lock:
            agent = self.state['agents'].get(agent_id)
            if not isinstance(agent, dict) or not agent.get('enabled') or \
                    project_id not in (agent.get('projects') or []):
                return False, False
            owner = self.state['users'].get(agent.get('owner'))
            if not isinstance(owner, dict) or owner.get('disabled'):
                return False, False
            role = self.state.get('memberships', {}).get(project_id, {}).get(owner['id'])
            approves = bool(owner.get('superuser')) or CAP_APPROVE in ROLE_CAPABILITIES.get(role, frozenset())
            moment = self._expiry_now()
            for credential in self.state.get('credentials', {}).values():
                if not isinstance(credential, dict) or credential.get('agent_id') != agent_id or \
                        credential.get('revoked') or credential.get('expires_at', 0) <= moment:
                    continue
                try:
                    if CAP_REVIEWS in credential_capabilities(self.state, credential, owner, project_id):
                        return True, approves
                except AuthorityDenied:
                    continue
            return False, approves

    def agent_actor(self, agent):
        """The stable attribution label an agent's API writes carry."""
        return agent['id']

    @staticmethod
    def _agent_name(name):
        if not isinstance(name, str) or not AGENT_NAME.fullmatch(name.strip()):
            raise invalid('Agent name must be 1-64 characters of letters, digits, '
                          'space, . _ -')
        # The name also names the owner's secret file (agent_secret_file); a name with
        # no letter or digit would fall back to the generic "agent" file.
        if not re.search(r'[A-Za-z0-9]', name):
            raise invalid('Agent name must contain a letter or digit')
        return name.strip()

    def _check_agent_file_name(self, owner_id, name, exclude_id=None):
        """Refuse a name whose secret file would clash with another of the owner's agents.

        The secret file is named from the agent name (``agent_slug``: lowercase letters,
        digits and hyphens, at most 40 characters), so "Build bot", "build-bot" and
        "Build_Bot" share one file, as do names that agree in their first 40
        characters. Two agents of the same owner live in the same user profile, so a
        second one would overwrite the first one's secret: refuse it with 409. Other
        owners may reuse the name (different profile). Existing clashes in old state are
        left alone and stay readable. Call with ``store.lock`` held.
        """
        slug = agent_slug(name)
        for other in self.state['agents'].values():
            if other.get('owner') == owner_id and other.get('id') != exclude_id and \
                    agent_slug(other.get('name')) == slug:
                raise conflict(
                    'Agent name "%s" would share the secret file %s with your agent "%s" (%s). '
                    'Choose a name that differs in more than case or punctuation.'
                    % (name, agent_secret_file(name)['name'], other.get('name'), other.get('id')),
                    {'clashes_with': other.get('id'),
                     'secret_file': agent_secret_file(name)['name']})

    @staticmethod
    def _has_forbidden_text_char(value):
        for c in value:
            code = ord(c)
            if code < 32 or 0x7f <= code <= 0x9f or not c.isprintable():
                return True
            if unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp'):
                return True
        return False

    @classmethod
    def _agent_text(cls, value, field, limit):
        if value is None:
            return None
        if not isinstance(value, str):
            raise invalid('%s must be text' % field)
        if cls._has_forbidden_text_char(value):
            raise invalid('%s must be at most %d printable characters' % (field, limit))
        text = value.strip()
        if not text:
            return None
        if len(text) > limit:
            raise invalid('%s must be at most %d printable characters' % (field, limit))
        return text

    #: Bounded Windows/POSIX path root: POSIX `/`, home `~` (`~` or `~/...`), Windows drive
    #: `[A-Za-z]:\` / `[A-Za-z]:/`, or UNC `\\host\share`.
    _WORKING_DIR_ROOT = re.compile(
        r'^(?:[A-Za-z]:[\\/]+|\\\\[A-Za-z0-9._-]+[\\/]+[A-Za-z0-9._$-]+(?:[\\/]+|$)|~(?:[\\/]+|$)|/+)'
    )
    #: Bounded folder segment: letters, digits, `.`, `_`, `-`, and single internal spaces,
    #: never ending in `.`, never containing `..` or `. ` (sentence punctuation), and at most
    #: 3 spaces per segment so prose sentences cannot masquerade as a folder path.
    _WORKING_DIR_SEGMENT = re.compile(r'^[A-Za-z0-9._-]+(?: [A-Za-z0-9._-]+){0,3}$')

    @classmethod
    def _agent_working_directory(cls, value, *, allow_empty=True):
        if value is None:
            if allow_empty:
                return None
            raise invalid('working_directory must be a valid Windows or POSIX folder path (1-%d characters)'
                          % AGENT_DIRECTORY_MAX)
        if not isinstance(value, str):
            raise invalid('working_directory must be text')
        if cls._has_forbidden_text_char(value):
            raise invalid('working_directory must not contain control, non-printable, or bidi characters')
        text = value.strip()
        if not text:
            if allow_empty:
                return None
            raise invalid('working_directory must be a valid Windows or POSIX folder path (1-%d characters)'
                          % AGENT_DIRECTORY_MAX)
        if len(text) > AGENT_DIRECTORY_MAX:
            raise invalid('working_directory must be at most %d printable characters' % AGENT_DIRECTORY_MAX)
        match = cls._WORKING_DIR_ROOT.match(text)
        if not match:
            raise invalid('working_directory must be an absolute or home-relative Windows or POSIX path')
        rest = text[match.end():].rstrip('/\\')
        if rest:
            segments = re.split(r'[\\/]+', rest)
            for seg in segments:
                if (not seg or len(seg) > 128 or seg in ('.', '..')
                        or '..' in seg or '. ' in seg or seg.endswith('.')
                        or not cls._WORKING_DIR_SEGMENT.fullmatch(seg)):
                    raise invalid('working_directory must be a valid Windows or POSIX folder path, not prose')
        return text

    def _visible_project_ids(self, principal):
        """Project ids ``principal`` may read, without raising.

        The same visibility :meth:`list_projects` answers with: a superuser sees every
        project, a session the projects its membership names, and a credential only the
        projects its grant names while its issuer is still a member. Used where one
        account is told something about ANOTHER account's memberships
        (kittrial-5bb.355): the answer is intersected with this set, so it never names a
        project the caller could not already see.
        """
        if principal is None:
            return set()
        projects = self.state['projects']
        if principal.via == 'credential':
            if principal.agent_id:
                agent = self.state['agents'].get(principal.agent_id) or {}
                named = [pid for pid in (agent.get('projects') or []) if pid in projects]
            else:
                pid = principal.credential_project
                named = [pid] if pid in projects else []
            return {pid for pid in named
                    if principal.superuser or
                    principal.user_id in self.state['memberships'].get(pid, {})}
        if principal.superuser:
            return set(projects)
        return {pid for pid in projects
                if principal.user_id in self.state['memberships'].get(pid, {})}

    def _owner_grantable_projects(self, owner_id, principal):
        """Projects the agent's owner may be granted, as THIS reader may learn them.

        :meth:`_agent_projects` admits a project the owner is a live member of, or any
        existing project when the owner is a superuser (the pre-existing owner rule).
        The answer is limited to the projects ``principal`` may read, so a view of
        another account's agent never reveals that its owner belongs to a project this
        reader cannot see (kittrial-5bb.355, the coordinator's addition).
        """
        visible = self._visible_project_ids(principal)
        owner = self.state['users'].get(owner_id) or {}
        if owner.get('superuser'):
            return sorted(visible)
        return sorted(pid for pid in visible
                      if owner_id in self.state['memberships'].get(pid, {}))

    def _agent_projects(self, principal, projects, owner_id=None):
        """Validate a requested agent grant against the *owner's* live membership.

        A grant may only name projects the agent's owner can already read. For the
        owner's own request that is the pre-existing rule; for a superuser changing
        somebody else's agent it means a superuser may **not** store a project outside
        the owner's membership, because such a grant can only ever be a leak waiting to
        be read. The stored list is a *ceiling*: the live role cap in
        :func:`http_authority.decide` is still applied on every request, and attention
        re-authorizes each project at read time.

        The refusal is a plain ``404 Project not found`` for a caller who cannot see the
        project (no leak). When the caller CAN see it (a superuser, or a member of that
        project) the same not-found path says plainly that the agent's owner is not a
        member (kittrial-5bb.355): the rule is unchanged, only the sentence is clearer.
        """
        if projects is None:
            return []
        if not isinstance(projects, list) or len(projects) > AGENT_PROJECTS_MAX:
            raise invalid('projects must be a list of at most %d project ids'
                          % AGENT_PROJECTS_MAX)
        granted = []
        for pid in projects:
            if not isinstance(pid, str) or not AGENT_PROJECT_ID.fullmatch(pid):
                raise invalid('Invalid project id in agent grant')
            if pid not in granted:
                granted.append(pid)
        owner_id = owner_id or principal.user_id
        owner = self.state['users'].get(owner_id) or {}
        owner_is_superuser = bool(owner.get('superuser'))
        for pid in granted:
            if pid not in self.state['projects']:
                raise not_found('Project not found')
            if not owner_is_superuser and \
                    owner_id not in self.state['memberships'].get(pid, {}):
                # Only a caller who can already see the project is told whose
                # membership is missing; everyone else keeps the plain sentence, so
                # the answer never reveals a project it did not reveal before.
                if pid in self._visible_project_ids(principal):
                    raise not_found(self._owner_not_member_message(owner_id, pid))
                raise not_found('Project not found')
        return granted

    def _owner_not_member_message(self, owner_id, project_id):
        """The clearer refusal for a caller who can see the project (kittrial-5bb.355).

        Same 404 path as ``Project not found``; the sentence names the agent's owner
        and the project, and the move that fixes it, because this caller already sees
        both. It is never used for a caller who cannot see the project.
        """
        project = self.state['projects'].get(project_id) or {}
        return "%s is not a member of %s: add them under the project's Members first" % (
            self._owner_name(owner_id), project.get('name') or project_id)

    def check_agent_grant(self, principal, projects, owner_id=None):
        """Judge an agent grant without writing anything (kittrial-5bb.183 review item 1).

        ``create_agent`` calls :meth:`_agent_projects` on the way in; the route calls this
        first so that a caller with no right over a named project keeps the service's own
        404 (a project the owner cannot see, or one that does not exist) instead of being
        answered the route's unknown-field refusal and its field list.
        """
        with self.store.lock:
            self._refresh_authority(principal)
            return self._agent_projects(principal, projects, owner_id=owner_id)

    def _agent(self, agent_id):
        agent = self.state['agents'].get(agent_id)
        if not isinstance(agent, dict):
            raise not_found('Agent not found')
        return agent

    def _agent_owned(self, principal, agent_id):
        """Return the agent when ``principal`` may administer it, else 404.

        A non-owner must not learn whether the id exists, so an unauthorized read is
        reported exactly like a missing agent.
        """
        agent = self._agent(agent_id)
        if not (principal.superuser or agent['owner'] == principal.user_id):
            raise not_found('Agent not found')
        return agent

    def _owner_name(self, user_id):
        user = self.state['users'].get(user_id) or {}
        return user.get('display_name') or user_id

    def _agent_credentials(self, agent):
        return [c for c in self.state['credentials'].values()
                if c.get('agent_id') == agent['id']]

    def _credential_live(self, credential, moment=None):
        """Whether a credential still works: not revoked, and not expired on either clock."""
        moment = self._expiry_now() if moment is None else moment
        return not credential.get('revoked') and not self._credential_expired(credential, moment)

    def _scope_names(self, scopes):
        """A caller's list of scopes as a tuple in the order given, or None when none was sent.

        Anything that is not a list of known scope names is refused: a number or ``true`` used
        to answer 500, an object was read as its keys. A name given twice counts once.
        """
        if scopes is None or (isinstance(scopes, (list, tuple)) and not scopes):
            return None
        if not isinstance(scopes, (list, tuple)):
            raise invalid('scopes must be a list of scope names')
        names = []
        for scope in scopes:
            if not isinstance(scope, str) or scope not in CREDENTIAL_SCOPES:
                raise invalid('Unknown credential scope %r' % (scope,))
            if scope not in names:
                names.append(scope)
        return tuple(names)

    def agent_scopes(self, agent):
        """What an agent may do, and where that is known from: ``(scopes, source)``.

        ``set``: the list on the agent record, written when the agent is made and by a renewal
        that sends a list. Once it is there no credential decides anything, working or dead
        (kittrial-5bb.208 review: a revoked credential that was wider used to seed the next
        renewal, so two requests got round the refusal to widen).

        An agent made before the record kept them has none. ``inferred``: its working
        credentials all carry the same list. This is an inference, not proof of its intended
        scopes: revocation or expiry may have left only an unintended credential working.
        Only its own account confirms it through plain renewal; a read writes nothing.
        ``unknown`` (scopes None): none of its credentials
        works, or they do not carry the same, which is what an agent looks like that was
        renewed from the page before this was fixed. Nothing is guessed then: never the
        default four, never a credential that no longer works.
        """
        if 'scopes' in agent:
            stored = agent['scopes']
            # Corruption is not a legacy record. Never infer authority from credentials
            # when a stored value is malformed, including duplicate names.
            if not isinstance(stored, list) or any(not isinstance(scope, str) or scope not in CREDENTIAL_SCOPES
                                                  for scope in stored) or len(stored) != len(set(stored)):
                return None, 'unknown'
            return tuple(stored), 'inferred' if agent.get('scopes_source') == 'inferred' else 'set'
        moment = self._expiry_now()
        lists = {tuple(scope for scope in CREDENTIAL_SCOPES if scope in (credential.get('scopes') or ()))
                 for credential in self._agent_credentials(agent) if self._credential_live(credential, moment)}
        if len(lists) == 1:
            return next(iter(lists)), 'inferred'
        return None, 'unknown'

    def _revoke_agent_credentials(self, agent):
        for credential in self._agent_credentials(agent):
            credential['revoked'] = True
        self._prune_agent_credentials(agent)

    @staticmethod
    def _credential_age(credential):
        return (credential.get('agent_issued_raw', credential.get('issued_raw')) or 0,
                credential.get('created_at') or '', credential.get('id') or '')

    def _prune_agent_credentials(self, agent):
        """Delete all but the newest few credentials of the agent that no longer work.

        Caller holds ``store.lock`` and saves. Without it the state grew by a record for every
        renewal for good, and every read of the agent carried them all (kittrial-5bb.208
        review). What goes is the record (label, scopes, when made and last used); that it was
        issued and revoked, by whom and when, remains until the shared audit tail's
        fixed AUDIT_LIMIT retention evicts it.
        """
        moment = self._expiry_now()
        dead = sorted((c for c in self._agent_credentials(agent) if not self._credential_live(c, moment)),
                      key=self._credential_age)
        for credential in dead[:max(0, len(dead) - AGENT_DEAD_CREDENTIALS_KEPT)]:
            self.state['credentials'].pop(credential['id'], None)
            self.state['credential_tokens'].pop(credential.get('token_hash'), None)

    def _agent_credentials_shown(self, agent):
        """What an agent read carries: bounded whether or not anything was pruned yet (a
        credential also stops working by expiry, with no write)."""
        moment = self._expiry_now()
        rows = sorted(self._agent_credentials(agent), key=self._credential_age)
        live = [c for c in rows if self._credential_live(c, moment)]
        dead = [c for c in rows if not self._credential_live(c, moment)][-AGENT_DEAD_CREDENTIALS_KEPT:]
        shown = sorted(live + dead, key=self._credential_age)
        return [dict(self.credential_view(c), working=self._credential_live(c, moment)) for c in shown]

    # -- an agent's history (kittrial-5bb.365) ---------------------------------------
    #: The audit actions that make an agent's history, and the word each is shown as.
    AGENT_HISTORY_KINDS = {
        'agents.created': 'created',
        'agents.credentials.issued': 'credential issued',
        'agents.credentials.revoked': 'credential revoked',
        'agents.credentials.extended': 'credential extended',
        'agents.disable': 'disabled',
        'agents.enable': 'enabled',
        'agents.update': 'changed',
        'accounts.disable': 'owner account disabled',
        'agents.project.granted': 'given the project',
        'agents.project.revoke': 'removed from the project',
        'agents.coordinator.grant': 'coordinator grant given',
        'agents.coordinator.revoke': 'coordinator grant ended',
        'agents.release.grant': 'release grant given',
        'agents.release.revoke': 'release grant ended',
    }
    #: Project-bound kinds: a project's view lists them only for its own project.
    AGENT_HISTORY_PROJECT_KINDS = ('agents.project.granted', 'agents.project.revoke',
                                   'agents.coordinator.grant', 'agents.coordinator.revoke',
                                   'agents.release.grant', 'agents.release.revoke')
    AGENT_HISTORY_DROPPED = ('Older history is no longer kept: the service keeps the last %d audit events '
                             'for all projects together.' % AUDIT_LIMIT)
    AGENT_HISTORY_GRANT_UNKNOWN = ('This agent was given this project before the time of a grant was recorded, so its '
                                   'earlier history is not shown here; its owner sees it on My agents.')

    def _audit_project_grants(self, principal, agent, added, request_id):
        """One ``agents.project.granted`` event per newly granted project: the grant time a
        project's view of the agent's history starts from (kittrial-5bb.365)."""
        for project_id in added:
            event = self.audit(request_id, principal, 'agents.project.granted', 'committed',
                               project_id=project_id, actor=self.agent_actor(agent))
            event.update(agent_id=agent['id'])

    @staticmethod
    def _names_agent(event, agent_id):
        if event.get('agent_id') == agent_id or agent_id in (event.get('agent_ids') or []):
            return True
        reason = event.get('reason') or ''
        return reason == 'agent ' + agent_id or reason.startswith('agent %s:' % agent_id)

    def agent_history(self, principal, agent_id, project_id=None):
        """The agent's history from the audit tail, newest first, at most
        :data:`AGENT_HISTORY_MAX` rows.

        With no ``project_id`` it is the agent's own view (its owner or a superuser, the
        :meth:`_agent_owned` rule; anyone else gets the plain 404 an unknown id gets): every
        committed event about the agent, with each credential's label and the scopes before
        and after. With a ``project_id`` it is that project's view (the route holds
        ``CAP_PROJECT_ADMIN``; an agent not granted the project is the same 404 as
        :meth:`get_project_agent`): no label, no scopes, no other project's events, and only
        events from the time the agent was given THIS project. That time is its newest
        ``agents.project.granted`` event; where none is kept, only events strictly after the
        agent's newest create or update are listed (either could have been the grant), and
        the answer says so. Stored events are never changed.
        """
        with self.store.lock:
            self._refresh_authority(principal)
            if project_id is None:
                agent = self._agent_owned(principal, agent_id)
            else:
                agent = self._agent(agent_id)
                if project_id not in (agent.get('projects') or []):
                    raise not_found('Agent not found')
            events = [(index, event) for index, event in enumerate(self.state['audit'])
                      if event.get('outcome') == 'committed' and event.get('action') in self.AGENT_HISTORY_KINDS
                      and self._names_agent(event, agent_id)]
            notes = []
            if project_id is not None:
                events, note = self._since_project_grant(events, project_id)
                if note:
                    notes.append(note)
            if not any(event.get('action') == 'agents.created' for _index, event in events) and project_id is None:
                notes.append(self.AGENT_HISTORY_DROPPED)
            newest = list(reversed(events))
            shown = newest[:AGENT_HISTORY_MAX]
            names = self.actor_names([event.get('user_id') for _index, event in shown])
            labels = {cid: c.get('label') for cid, c in self.state['credentials'].items()
                      if c.get('agent_id') == agent_id}
            items = [self._history_row(event, names, labels, project_id is None) for _index, event in shown]
            return {'agent': agent_id, 'project': project_id, 'items': items, 'total': len(events),
                    'omitted': max(0, len(events) - len(shown)), 'notes': notes}

    def _since_project_grant(self, events, project_id):
        """The project's view: its own project events, and the rest only since the grant."""
        own = [(i, e) for i, e in events if e.get('action') not in self.AGENT_HISTORY_PROJECT_KINDS
               or e.get('project_id') == project_id]
        grants = [i for i, e in own if e.get('action') == 'agents.project.granted' and e.get('project_id') == project_id]
        if grants:
            return [(i, e) for i, e in own if i >= grants[-1]], None
        # No grant event is kept (a grant made before kittrial-5bb.365, or one the audit cap
        # dropped). An earlier update event does not say which projects it changed, so no
        # point in the log can be shown to follow the grant: nothing is listed, and the
        # answer says why. It can show less, never the agent's history from another project.
        return [], self.AGENT_HISTORY_GRANT_UNKNOWN

    def _history_row(self, event, names, labels, full):
        action = event.get('action')
        row = {'at': event.get('time'), 'kind': self.AGENT_HISTORY_KINDS[action], 'action': action,
               'by': names.get(event.get('user_id')) or event.get('user_id')}
        if event.get('target_credential_id'):
            row['credential'] = event['target_credential_id']
        if event.get('target_credential_ids') is not None:
            row['revoked_count'] = len(event.get('target_credential_ids') or [])
        if action in self.AGENT_HISTORY_PROJECT_KINDS:
            row['project'] = event.get('project_id')
        if full:
            if row.get('credential'):
                # The label is read from the credential record, never stored in the audit
                # (the audit carries no credential payload). A pruned record shows its id only.
                row['label'] = labels.get(row['credential'])
            if event.get('scopes_before') is not None or event.get('scopes_after') is not None:
                row['scopes_before'] = event.get('scopes_before')
                row['scopes_after'] = event.get('scopes_after')
                if event.get('scopes_before') != event.get('scopes_after'):
                    row['kind'] = 'scopes changed'
        return row

    def project_agent_audit(self, project_id):
        """The project audit's agent rows (kittrial-5bb.365): the events of agents whose live
        grant names ``project_id``, cut to the project's view, each tagged with its agent.
        A view at read time: nothing stored is changed or gains a project."""
        with self.store.lock:
            agents = [a for a in self.state['agents'].values() if project_id in (a.get('projects') or [])]
            rows = []
            for agent in agents:
                events = [(index, event) for index, event in enumerate(self.state['audit'])
                          if event.get('outcome') == 'committed' and event.get('action') in self.AGENT_HISTORY_KINDS
                          and self._names_agent(event, agent['id'])]
                events, _note = self._since_project_grant(events, project_id)
                # The project's own events (its grant, its coordinator grants) are already in
                # the project audit under their project; only the agent's other events are added.
                for _index, event in [(i, e) for i, e in events if e.get('project_id') != project_id]:
                    rows.append({'time': event.get('time'), 'action': event.get('action'), 'outcome': 'committed',
                                 'user_id': event.get('user_id'), 'project_id': None, 'reason': None,
                                 'agent': {'id': agent['id'], 'name': agent['name']},
                                 'revoked_count': (len(event.get('target_credential_ids') or [])
                                                   if event.get('target_credential_ids') is not None else None)})
            return rows

    def agent_view(self, agent, principal):
        """The owner-visible agent record. The path never leaks to anyone else."""
        owner_sees_path = bool(principal.superuser or
                               principal.user_id == agent['owner'])
        scopes, source = self.agent_scopes(agent)
        view = {
            'id': agent['id'], 'name': agent['name'], 'owner': agent['owner'],
            'owner_display_name': self._owner_name(agent['owner']),
            'tool': agent.get('tool'), 'machine': agent.get('machine'),
            'notes': agent.get('notes'), 'enabled': bool(agent.get('enabled')),
            'actor': self.agent_actor(agent),
            'projects': list(agent.get('projects') or []),
            # The projects the owner may be granted, as THIS reader may learn them
            # (kittrial-5bb.355): the page greys out the rest and says why. Limited to
            # projects the reader can already see, so another account's unseen
            # memberships are never revealed.
            'owner_grantable_projects': self._owner_grantable_projects(agent['owner'], principal),
            'created_at': agent.get('created_at'),
            'last_seen_at': agent.get('last_seen_at'),
            # Every credential that works (at most AGENT_MAX_CREDENTIALS) and the newest few that
            # do not; ``working`` says which is which (an expired one is not ``revoked``).
            'credentials': self._agent_credentials_shown(agent),
            # What the agent may do, which is what a new credential for it carries when no list
            # is sent (kittrial-5bb.208); null when nothing says. ``scopes_source``: ``set`` (kept on
            # the agent record: given when it was made or by a renewal that sent a list),
            # ``inferred`` (an agent made before the record kept them: read from its working
            # credentials, which all carry the same) or ``unknown``.
            'scopes': None if scopes is None else list(scopes),
            'scopes_source': source,
            # The per-agent limit on claimed, undelivered tasks (kittrial-5bb.276): the
            # stored setting, or its default when the owner has not set one. A read
            # and the gate use this one value, so a page cannot show 1 while the
            # service enforces 3.
            'claim_limit': claim_limit.default_limit(agent),
            # The different scope lists its WORKING credentials carry, when they do not all carry
            # the same; else []. A read for whoever reads the agent record: its account, a
            # superuser, and the agent itself on /v1/agents/me. It says only that they differ:
            # an account may have narrowed its agent on purpose and left the older credential.
            'scopes_differ': self.agent_scope_differences(agent),
        }
        if agent.get('created_by'):
            view['created_by'] = agent['created_by']
            view['created_by_display_name'] = self._owner_name(agent['created_by'])
        if agent.get('role') in AGENT_ROLES:
            view['role'] = agent['role']
        if owner_sees_path:
            view['working_directory'] = agent.get('working_directory')
        else:
            # Explicit, so a client can tell "hidden from you" from "not set".
            view['working_directory_hidden'] = True
        # kittrial-5bb.387: the projects this agent holds a coordinator grant in, with whether the grant
        # has effect now, so the set-up wizard writes a coordinator AGENT.md. Only for the agent's owner
        # (or a superuser) signed in as a person: never for another member, and never for a credential,
        # the agent's own included. Computed live; only projects the owner is a member of.
        if principal.credential_id is None and (principal.superuser or principal.user_id == agent['owner']):
            view['coordinates'] = self.agent_coordinates(agent)
        return view

    def agent_coordinates(self, agent):
        """``[{project, effective}]``: the agent's coordinator grants in projects its owner is a member of."""
        owner_id = agent.get('owner')
        found = []
        for project_id in sorted(self.state.get('memberships', {})):
            if owner_id not in self.state['memberships'].get(project_id, {}):
                continue
            if coordinator_grant(self.state, project_id, agent['id']) is None:
                continue
            found.append({'project': project_id, 'effective': self._may_coordinate(project_id, agent)})
        return found

    def project_agent_view(self, agent, project_id=None):
        """The *project-owner* view of an agent active in their project.

        Deliberately narrower than :meth:`agent_view`. A project owner governs
        whether an agent may work in their project, but is not that agent's owner, so
        the response carries identity, attribution and liveness only. The
        ``working_directory`` hint never appears - and neither does
        ``working_directory_hidden``, which would still disclose that a path exists -
        nor do the agent's other project grants or its machine/notes. Credential rows
        expose only id, expiry, working/revoked and seven-day warning, so this owner
        can extend a live key without seeing its label, scopes, path, hash or secret.
        """
        view = {
            'id': agent['id'],
            'name': agent['name'],
            'owner': agent['owner'],
            'owner_display_name': self._owner_name(agent['owner']),
            'actor': self.agent_actor(agent),
            'enabled': bool(agent.get('enabled')),
            'last_seen_at': agent.get('last_seen_at'),
        }
        shown = {c['id'] for c in self._agent_credentials_shown(agent)}
        view['credentials'] = [self.agent_credential_expiry_view(c)
                               for c in self._agent_credentials(agent) if c['id'] in shown]
        if project_id is not None:
            # Whether this agent coordinates THIS project (kittrial-5bb.209): who granted it and
            # when, and whether the grant has effect now (its account must itself be a
            # coordinator or an owner here); null when it holds none.
            view['coordinator'] = self.coordinator_view(project_id, agent)
            # Whether this agent may RECORD A RELEASE here (kittrial-5bb.378): the same
            # shape as the coordinator row, so one panel can show both grants.
            view['release'] = self.release_view(project_id, agent)
        return view

    def coordinator_view(self, project_id, agent):
        grant = coordinator_grant(self.state, project_id, agent['id'])
        if grant is None:
            return None
        effective = self._may_coordinate(project_id, agent)
        owner = self.state['users'].get(agent.get('owner')) or {}
        # Why a grant on record has no effect now; null when it has. One word, for the page.
        why = None if effective else ('agent-disabled' if not agent.get('enabled') else
                                      'account-disabled' if owner.get('disabled') else 'account-role')
        return {'granted_by': grant['granted_by'], 'granted_by_display_name': self._owner_name(grant['granted_by']),
                'granted_at': grant.get('granted_at'), 'effective': effective, 'no_effect_because': why}

    def release_view(self, project_id, agent):
        """Whether this agent may record a release in this project now (kittrial-5bb.378).

        Mirrors :meth:`coordinator_view`, but the grant only takes effect while the
        agent's account still holds ``project.admin`` here (owner, or a superuser). Null
        when the agent holds no release grant.
        """
        grant = release_grant(self.state, project_id, agent['id'])
        if grant is None:
            return None
        effective = self._may_release(project_id, agent)
        owner = self.state['users'].get(agent.get('owner')) or {}
        why = None if effective else ('agent-disabled' if not agent.get('enabled') else
                                      'account-disabled' if owner.get('disabled') else 'account-role')
        return {'granted_by': grant['granted_by'], 'granted_by_display_name': self._owner_name(grant['granted_by']),
                'granted_at': grant.get('granted_at'), 'effective': effective, 'no_effect_because': why}

    def _may_release(self, project_id, agent):
        """Whether the agent's account holds the owner's capabilities in the project now."""
        owner = self.state['users'].get(agent.get('owner')) or {}
        if owner.get('disabled') or not agent.get('enabled'):
            return False
        role = self.state.get('memberships', {}).get(project_id, {}).get(agent.get('owner'))
        return bool(owner.get('superuser')) or CAP_PROJECT_ADMIN in ROLE_CAPABILITIES.get(role, frozenset())

    def coordinators_of(self, principal, project_id):
        """Who coordinates the project, for any reader of it (kittrial-5bb.198). Changes nothing.

        ``people``: the members whose role coordinates (``coordinator`` and ``owner``), with the
        fields :meth:`list_members` gives the same reader. A disabled account is listed as the
        members list lists it; only a reader who administers the project is told so, because
        account state is administrative detail there too. ``agents``: the agents whose grant
        has effect now, by name and account name. A grant on record without effect is left out
        for every reader, and why is said only on the owner's agent panel.
        """
        with self.store.lock:
            self.check_authority(principal, project_id, CAP_READ)
            try:
                self.check_authority(principal, project_id, CAP_PROJECT_ADMIN)
                admin = True
            except HttpError:
                admin = False
            people = []
            for user_id, role in sorted(self.state['memberships'].get(project_id, {}).items()):
                if CAP_COORDINATE not in ROLE_CAPABILITIES.get(role, frozenset()):
                    continue
                user = self.state['users'].get(user_id) or {}
                item = {'user_id': user_id, 'display_name': user.get('display_name') or user_id, 'role': role}
                if admin:
                    item['disabled'] = bool(user.get('disabled'))
                people.append(item)
            agents = []
            for agent in sorted(self.state['agents'].values(), key=lambda a: a['id']):
                if project_id not in (agent.get('projects') or []):
                    continue
                grant = coordinator_grant(self.state, project_id, agent['id'])
                if grant is None or not self._may_coordinate(project_id, agent):
                    continue
                agents.append({'name': agent['name'], 'owner_display_name': self._owner_name(agent['owner']),
                               'granted_by_display_name': self._owner_name(grant['granted_by']),
                               'granted_at': grant.get('granted_at')})
            return {'people': people, 'agents': agents}

    def _may_coordinate(self, project_id, agent):
        """Whether the agent's account holds the coordinator's capabilities in the project now."""
        owner = self.state['users'].get(agent.get('owner')) or {}
        if owner.get('disabled') or not agent.get('enabled'):
            return False
        role = self.state.get('memberships', {}).get(project_id, {}).get(agent.get('owner'))
        return bool(owner.get('superuser')) or CAP_COORDINATE in ROLE_CAPABILITIES.get(role, frozenset())

    NOT_A_COORDINATOR = ('This agent\'s account is not a coordinator or an owner of this project, so the grant would '
                         'have no effect. Make the account a coordinator first (Members), then grant the agent.')

    def grant_coordinator(self, principal, project_id, agent_id, request_id=None):
        """Let one agent act as coordinator in one project (slice 1b, kittrial-5bb.209).

        The route holds the ``CAP_PROJECT_ADMIN`` boundary: an owner of the project or a
        superuser, never a credential. The agent must be a personal agent granted this
        project (anything else answers like a missing agent, as the other routes here do),
        and its account must itself be a coordinator or an owner of the project: a grant
        that could not take effect is not written.
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change a coordinator grant')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(agent_id)
            if project_id not in (agent.get('projects') or []):
                raise not_found('Agent not found')
            if not agent.get('enabled'):
                raise conflict('A disabled agent cannot be granted')
            if not self._may_coordinate(project_id, agent):
                raise conflict(self.NOT_A_COORDINATOR)
            stored = self.state.get('coordinator_grants')
            # An ABSENT key is every installation before its first grant, and the grant below
            # creates it. A key that is there with anything but an object, an explicit null
            # included, is damage (kittrial-5bb.261): null used to pass this check and answer 500.
            if 'coordinator_grants' in self.state and (not isinstance(stored, dict) or not isinstance(stored.get(project_id, {}), dict)):
                raise HttpError(503, 'unavailable',
                                'The record of coordinator grants in the service state is damaged (it is not an object), '
                                'so no grant was written. An operator must repair the state file')
            grants = self.state.setdefault('coordinator_grants', {}).setdefault(project_id, {})
            if coordinator_grant(self.state, project_id, agent_id) is None:
                grants[agent_id] = {'granted_by': principal.user_id, 'granted_at': now_iso(self._now())}
                self.store.save()
            return self.project_agent_view(agent, project_id)

    def _end_coordinator_grants(self, agent, project_ids, principal, request_id, reason):
        """Delete the agent's coordinator AND release grants in each of ``project_ids``.

        The ONE place for every way an agent stops being in a project: out of the project,
        out of its coordinators, out of its release grant. A grant must not wait in the
        state for the project to be put back. Caller holds ``store.lock`` and saves. Each
        grant that was there is audited as a revoke in its project, with who did what
        ended it and why. ``release_grants`` (kittrial-5bb.378) is the same family: an
        agent that leaves takes its release authority with it.
        """
        grants = self.state.get('coordinator_grants')
        releases = self.state.get('release_grants')
        ended = []
        for project_id in project_ids:
            held = grants.get(project_id) if isinstance(grants, dict) else None
            if isinstance(held, dict) and agent['id'] in held:
                del held[agent['id']]
                if not held:
                    del grants[project_id]
                ended.append(project_id)
                self.audit(request_id, principal, 'agents.coordinator.revoke', 'committed', project_id=project_id,
                           reason='agent %s: %s' % (agent['id'], reason))
            released = releases.get(project_id) if isinstance(releases, dict) else None
            if isinstance(released, dict) and agent['id'] in released:
                del released[agent['id']]
                if not released:
                    del releases[project_id]
                self.audit(request_id, principal, 'agents.release.revoke', 'committed', project_id=project_id,
                           reason='agent %s: %s' % (agent['id'], reason))
        return ended

    def revoke_coordinator(self, principal, project_id, agent_id, request_id=None):
        """Take the coordinator grant away. From the next request the agent approves nothing here."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change a coordinator grant')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(agent_id)
            grants = self.state.get('coordinator_grants')
            held = grants.get(project_id) if isinstance(grants, dict) else None
            if not isinstance(held, dict) or agent_id not in held:
                raise not_found('Agent not found' if project_id not in (agent.get('projects') or [])
                                else 'This agent holds no coordinator grant in this project')
            del held[agent_id]
            if not held:
                del grants[project_id]
            self.store.save()
            return self.project_agent_view(agent, project_id)

    NOT_A_RELEASE_OWNER = ('This agent\'s account is not an owner of this project, so a release grant would '
                           'have no effect. Make the account an owner first (Members), then grant the agent.')

    def grant_release(self, principal, project_id, agent_id, request_id=None):
        """Let one agent record releases in one project (kittrial-5bb.378).

        The route holds the ``CAP_PROJECT_ADMIN`` boundary: an owner of the project or a
        superuser, never a credential. The agent must be a personal agent granted this
        project, enabled, and its account must itself be an owner of the project (or a
        superuser): a release grant that could not take effect is not written. Stored
        under ``release_grants[project][agent]`` beside the coordinator grant; read live
        on every release write, so a revocation or a demotion refuses the next request.
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change a release grant')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(agent_id)
            if project_id not in (agent.get('projects') or []):
                raise not_found('Agent not found')
            if not agent.get('enabled'):
                raise conflict('A disabled agent cannot be granted')
            if not self._may_release(project_id, agent):
                raise conflict(self.NOT_A_RELEASE_OWNER)
            stored = self.state.get('release_grants')
            # An ABSENT key is every installation before its first release grant. A key
            # that is there with anything but an object is damage and fails closed.
            if 'release_grants' in self.state and (not isinstance(stored, dict)
                                                   or not isinstance(stored.get(project_id, {}), dict)):
                raise HttpError(503, 'unavailable',
                                'The record of release grants in the service state is damaged (it is not an object), '
                                'so no grant was written. An operator must repair the state file')
            grants = self.state.setdefault('release_grants', {}).setdefault(project_id, {})
            if release_grant(self.state, project_id, agent_id) is None:
                grants[agent_id] = {'granted_by': principal.user_id, 'granted_at': now_iso(self._now())}
                self.store.save()
            return self.project_agent_view(agent, project_id)

    def revoke_release(self, principal, project_id, agent_id, request_id=None):
        """Take the release grant away. From the next request the agent releases nothing here."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change a release grant')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(agent_id)
            grants = self.state.get('release_grants')
            held = grants.get(project_id) if isinstance(grants, dict) else None
            if not isinstance(held, dict) or agent_id not in held:
                raise not_found('Agent not found' if project_id not in (agent.get('projects') or [])
                                else 'This agent holds no release grant in this project')
            del held[agent_id]
            if not held:
                del grants[project_id]
            self.store.save()
            return self.project_agent_view(agent, project_id)

    def list_project_agents(self, principal, project_id):
        """Agents whose live grant names ``project_id`` (owner decision 4).

        Authorization is the route's ``CAP_PROJECT_ADMIN`` check (project owner or
        admin, never a worker/agent credential), so this method only assembles the
        safe :meth:`project_agent_view`.
        """
        with self.store.lock:
            self._refresh_authority(principal)
            agents = [a for a in self.state['agents'].values()
                      if project_id in (a.get('projects') or [])]
            agents.sort(key=lambda a: a['id'])
            return [self.project_agent_view(a, project_id) for a in agents]

    def get_project_agent(self, principal, project_id, agent_id):
        """One agent as its project's owner/admin sees it.

        Like :meth:`list_project_agents`, the route holds the ``CAP_PROJECT_ADMIN``
        boundary. An agent whose grant does not name this project is reported exactly
        like a missing agent, so a project owner cannot probe the wider registry: the
        not-granted case raises the *same* ``Agent not found`` message a nonexistent id
        gets from :meth:`_agent`, and the response body is byte-identical.
        """
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(agent_id)
            if project_id not in (agent.get('projects') or []):
                # One message for every agent 404 on this route: "exists but is not
                # granted here" must be indistinguishable from "no such id".
                raise not_found('Agent not found')
            return self.project_agent_view(agent, project_id)

    def revoke_agent_project(self, principal, project_id, agent_id, request_id=None):
        """Remove one project from an agent's grant (owner decision 4).

        The project owner/admin boundary is the route's ``CAP_PROJECT_ADMIN`` check.
        Only the grant changes: the agent keeps its other projects, its credentials,
        its ``enabled`` state and every control its own owner holds. Because
        :func:`http_authority.decide` reads the *live* grant on every request, the
        agent credential is refused on this project on its next request and the
        project drops out of ``/v1/agents/me/next``.

        An agent whose grant does not name this project is reported exactly like a
        missing agent - both raise the same ``Agent not found`` message, so a project
        owner cannot tell the two apart.

        This revoke is deliberately **not sticky**: it removes one project from the
        agent's grant and nothing more. The agent's own owner still administers it and
        can immediately add this project back (while the owner is still a member of
        it), just as they could grant it directly. Whether a project owner should be
        able to block an agent from being re-granted is an OWNER DECISION that is
        still pending; this method implements no such block.
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change an agent grant')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(agent_id)
            projects = list(agent.get('projects') or [])
            if project_id not in projects:
                # Exactly like a missing agent: one message, so a non-participant
                # learns nothing and a project owner cannot probe for the id.
                raise not_found('Agent not found')
            agent['projects'] = [pid for pid in projects if pid != project_id]
            self._end_coordinator_grants(agent, [project_id], principal, request_id,
                                         'a project owner took the agent out of the project')
            self.store.save()
            view = self.project_agent_view(agent, project_id)
        return {'operation': 'agents.project.revoke', 'project': project_id, 'agent': view}

    def agent_setup(self, agent, secret, projects=None):
        """The one-time setup payload. It never contains the secret itself.

        The secret is returned once beside this payload; ``.orchestra/agent.json``
        holds only the server URL, the agent id and the project ids, so the directory
        the agent runs in is safe to keep. The snippet leads with VS Code secret
        storage or the OS credential store (owner decision 9)... superseded by the
        owner's practical update of 2026-09-29: the secret lives in a per-agent curl
        config file in the user profile (:func:`agent_secret_file`), Windows steps
        first, and every call hands curl that file with ``-K``, so no command line
        ever carries the secret (shell history, process list). The environment
        variable remains a secondary fallback.
        """
        server_url = self.public_url
        config = {
            'server_url': server_url,
            'agent_id': agent['id'],
            'projects': list(projects if projects is not None else agent.get('projects') or []),
            'name': agent['name'],
        }
        server = server_url or '<ORCHESTRA_SERVER_URL>'
        secret_file = agent_secret_file(agent['name'])
        snippet = (
            "# 1. Store the secret (once; outside the agent folder, never in Git).\n"
            "#    Windows: open Notepad and paste this one line, with the secret shown once:\n"
            "#      header = \"Authorization: Bearer <YOUR_AGENT_SECRET>\"\n"
            "#    then File > Save As, \"Save as type: All files (*.*)\", file name:\n"
            "#      %(win)s\n"
            "#    macOS/Linux: save the same line to %(posix)s\n"
            "#    in a text editor, then: chmod 600 %(posix)s (mode 600).\n"
            "# 2. Test it (success prints the agent's JSON; 401 means a wrong secret or header\n"
            "#    line; \"cannot read config\" means a wrong file name or a .txt extension):\n"
            "#    PowerShell:  curl.exe -fsS -K \"%(ps)s\" %(me_ps)s\n"
            "#    macOS/Linux: curl -fsS -K %(posix)s %(me_sh)s\n"
            "# 3. In the agent folder (shell shown; the web page can save these files for you):\n"
            "mkdir -p .orchestra\n"
            "cat > %(config_path)s <<'JSON'\n%(config)s\nJSON\n"
            "[ -f .gitignore ] && ! grep -qx '.orchestra/' .gitignore && printf '\\n.orchestra/\\n' >> .gitignore\n"
            "# Every call hands curl the config file (-K), so the secret never reaches a\n"
            "# command line, shell history or the process list:\n"
            "curl -fsS -K %(posix)s %(next_sh)s\n"
            "# Secondary fallback only, for a machine where that file cannot be kept: the\n"
            "# %(env)s environment variable may hold the secret. Never write the secret\n"
            "# on a command line, in the agent folder, in agent.json or in Git.\n"
        ) % {'win': secret_file['windows'], 'ps': secret_file['windows_powershell'],
             'posix': secret_file['posix'], 'config_path': AGENT_CONFIG_PATH,
             # kittrial-5bb.360: the address is single-quoted in every command (nothing in it expands).
             'me_ps': ps_quote(server + '/v1/agents/me'), 'me_sh': sh_quote(server + '/v1/agents/me'),
             'next_sh': sh_quote(server + '/v1/agents/me/next'),
             'config': json.dumps(config, indent=2, sort_keys=True), 'env': AGENT_SECRET_ENV}
        # Where each granted project's repository is, as its owner recorded it
        # (kittrial-5bb.118). A label for the person setting the agent up: it is listed
        # beside the setup text, never inside the commands, and nothing runs it.
        # Read once per project, as today's rule reads it: a stored value that no longer
        # passes is left out (kittrial-5bb.123).
        with self.store.lock:
            readable = ((pid, self.stored_repository(self.state['projects'].get(pid))[0])
                        for pid in config['projects'])
            repositories = [{'project': pid, 'repository': value} for pid, value in readable if value]
        return {
            'config_path': AGENT_CONFIG_PATH,
            'guide_path': AGENT_GUIDE_PATH,
            'gitignore_line': AGENT_GITIGNORE_LINE,
            'config': config,
            'repositories': repositories,
            'repositories_note': self.REPOSITORY_NOTE,
            'config_contains_secret': False,
            'secret_env_var': AGENT_SECRET_ENV,
            'secret_file': secret_file,
            'setup_snippet': snippet,
            'guidance': 'Keep .orchestra/ out of Git. Store the secret, shown once, in a '
                        'per-agent curl config file in your user profile (%s on Windows, '
                        '%s on macOS/Linux, mode 600) holding one line: header = '
                        '"Authorization: Bearer <secret>". Every call hands curl that file '
                        'with -K, so the secret never appears on a command line, in the '
                        'agent folder or in Git. The %s environment variable is only a '
                        'secondary fallback. The secret is shown only once.'
                        % (secret_file['windows'], secret_file['posix'], AGENT_SECRET_ENV),
        }

    def agent_scope_differences(self, agent):
        """The distinct scope lists of an agent's working credentials, when there is more than one."""
        moment = self._expiry_now()
        lists = {tuple(scope for scope in CREDENTIAL_SCOPES if scope in (credential.get('scopes') or ()))
                 for credential in self._agent_credentials(agent) if self._credential_live(credential, moment)}
        return [list(item) for item in sorted(lists)] if len(lists) > 1 else []

    #: Said when nothing says what the agent may do. The page then asks its account to choose.
    SCOPES_NEEDED = ('Nothing says what this agent may do: its record holds no scopes, and its credentials '
                     'that still work do not say (there is none, or they do not all allow the same). Send the '
                     'scopes it should have, for example "scopes": ["read"] for an agent that only reads')
    SCOPES_OWNER_REQUIRED = ('Only this agent\'s own account may choose or confirm its scopes '
                             'when they are unknown or inferred. Ask that account before issuing a new secret.')

    @staticmethod
    def _credential_label(label):
        """Optional display text, never an arbitrary JSON value or unbounded payload."""
        if label is None:
            return None
        if not isinstance(label, str) or len(label) > 64 or any(not c.isprintable() for c in label):
            raise invalid('Credential label must be at most 64 printable characters')
        return label.strip() or None

    @contextmanager
    def _agent_credential_transaction(self, agent, *, rollback=True):
        """Hold the HTTP credential boundary through state save and receipt commit.

        Caller holds store.lock. Snapshot only this agent's records and the audit's
        append position; retention waits until the boundary exits. The yielded callback
        marks a successful state save, after which a receipt failure must not undo it.
        Revocation deliberately keeps its effect if saving fails.
        """
        previous_agent = copy.deepcopy(agent)
        credentials = copy.deepcopy({key: value for key, value in self.state['credentials'].items()
                                     if value.get('agent_id') == agent['id']})
        tokens = {value['token_hash']: key for key, value in credentials.items()}
        audit_start = len(self.state['audit'])
        unsaved = self.store.unsaved_since
        saved = [False]
        def state_saved():
            saved[0] = True
        self._audit_defer_trim += 1
        try:
            yield state_saved
        except BaseException as error:
            if not saved[0] and rollback:
                # Inspect only this operation's appended entries, never the old audit.
                refusals = [event for event in self.state['audit'][audit_start:]
                            if event.get('outcome') in ('denied', 'rejected')]
                for key, value in list(self.state['credentials'].items()):
                    if value.get('agent_id') == agent['id']:
                        self.state['credential_tokens'].pop(value.get('token_hash'), None)
                        del self.state['credentials'][key]
                self.state['credentials'].update(credentials)
                self.state['credential_tokens'].update(tokens)
                del self.state['audit'][audit_start:]
                self.state['audit'].extend(refusals)
                agent.clear()
                agent.update(previous_agent)
                if refusals:
                    # A refusal save may have flushed earlier pending state, or may
                    # itself be pending. Keep its actual result through rollback.
                    if not isinstance(error, HttpError) and self.store.unsaved_since is None:
                        self.store.unsaved_since = self.store.clock()
                else:
                    self.store.unsaved_since = unsaved
            elif not saved[0] and not isinstance(error, HttpError) and self.store.unsaved_since is None:
                self.store.unsaved_since = self.store.clock()
            raise
        finally:
            self._audit_defer_trim -= 1
            if not self._audit_defer_trim and len(self.state['audit']) > AUDIT_LIMIT:
                del self.state['audit'][:len(self.state['audit']) - AUDIT_LIMIT]

    def _audit_agent_credential(self, principal, agent, credential, action, before, request_id):
        """Metadata independent of credential pruning, within fixed audit retention."""
        after = self.agent_scopes(agent)
        event = self.audit(request_id, principal, action, 'committed', actor=self.agent_actor(agent))
        event.update(agent_id=agent['id'], target_credential_id=credential['id'],
                     credential_scopes=list(credential['scopes']),
                     scopes_before=None if before[0] is None else list(before[0]),
                     scopes_after=None if after[0] is None else list(after[0]),
                     scopes_source_before=before[1], scopes_source_after=after[1])

    def _issue_agent_credential_locked(self, principal, agent, scopes=None, label=None, expiry=None):
        """Create one agent credential. Caller holds ``store.lock`` and has authorized.

        With no list it carries exactly what the agent has (:meth:`agent_scopes`), and where
        nothing says what that is, it is refused with a sentence that asks for the list. A
        list sets what the agent has from now on. Only its own account may confirm
        inferred or unknown scopes, with or without a list. Once confirmed, another
        authorized account may narrow them, but widening remains owner-only.
        """
        requested = self._scope_names(scopes)
        label = self._credential_label(label)
        has, source = self.agent_scopes(agent)
        if requested is not None and source in ('inferred', 'unknown') and principal.user_id != agent['owner']:
            raise forbidden(self.SCOPES_OWNER_REQUIRED)
        if requested is None:
            if has is None:
                message = self.SCOPES_NEEDED if principal.user_id == agent['owner'] else self.SCOPES_OWNER_REQUIRED
                raise conflict(message, {'scopes_needed': True})
            if source == 'inferred' and principal.user_id != agent['owner']:
                raise conflict('These scopes are inferred from working credentials, not confirmed. '
                               'Only the agent\'s own account may confirm them.',
                               {'scopes_needed': True})
        else:
            more = [scope for scope in CREDENTIAL_SCOPES if scope in requested and scope not in (has or ())]
            if more and principal.user_id != agent['owner']:
                raise forbidden('This agent has %s. Only its own account may give it more (asked for beyond that: %s)'
                                % (', '.join(has) if has else 'no scopes on record', ', '.join(more)))
        # Twenty that still work. A revoked or expired credential is not counted: counted,
        # the twentieth renewal of an agent was refused for good.
        moment = self._expiry_now()
        expiry = self._agent_expiry_metadata(moment) if expiry is None else expiry
        if sum(1 for credential in self._agent_credentials(agent)
               if self._credential_live(credential, moment)) >= AGENT_MAX_CREDENTIALS:
            raise conflict('An agent may hold at most %d credentials that still work; revoke one first'
                           % AGENT_MAX_CREDENTIALS)
        if requested is not None:
            agent['scopes'], agent['scopes_source'] = list(requested), 'set'
        elif source == 'inferred':
            agent['scopes'], agent['scopes_source'] = list(has), 'set'
        requested = tuple(agent['scopes'])
        secret = new_token()
        credential = {
            'id': 'cred_' + secrets.token_hex(8),
            'user_id': agent['owner'],
            'project_id': None,
            'agent_id': agent['id'],
            'label': label or ('agent:%s' % agent['name'])[:64],
            'scopes': list(requested),
            'actor': self.agent_actor(agent),
            'token_hash': token_hash(secret),
            'created_at': now_iso(self._now()),
            'last_used': None,
            'revoked': False,
        }
        credential.update(expiry)
        self.state['credentials'][credential['id']] = credential
        self.state['credential_tokens'][token_hash(secret)] = credential['id']
        self._prune_agent_credentials(agent)
        return credential, secret

    def agent_creation_owner(self, principal, owner):
        """Resolve a real enabled account before inspecting any target grants."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to create an agent')
        with self.store.lock:
            self.check_authority(principal, None, CAP_AGENTS)
            if not isinstance(owner, str) or not owner:
                raise invalid('owner must be an account id')
            if owner != principal.user_id and not principal.superuser:
                raise forbidden('Only a superuser may create an agent for another account')
            user = self._user(owner)
            if user.get('disabled'):
                raise conflict('A disabled account cannot receive an agent')
            return owner

    def create_agent(self, principal, *, name, tool=None, working_directory=None,
                     machine=None, notes=None, projects=None, scopes=None,
                     request_id=None, owner=None, claim_limit=None, role=None):
        """Create an owner-bound agent and its first (one-time) credential."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to create an agent')
        clean_name = self._agent_name(name)
        tool = self._agent_text(tool, 'tool', AGENT_TOOL_MAX)
        working_directory = self._agent_working_directory(working_directory, allow_empty=True)
        machine = self._agent_text(machine, 'machine', AGENT_MACHINE_MAX)
        notes = self._agent_text(notes, 'notes', AGENT_NOTES_MAX)
        clean_role = validate_agent_role(role)
        with self.store.lock:
            self._refresh_authority(principal)
            owner_id = self.agent_creation_owner(principal, principal.user_id if owner is None else owner)
            owned = [a for a in self.state['agents'].values()
                     if a['owner'] == owner_id]
            if len(owned) >= AGENT_MAX_PER_OWNER:
                raise conflict('At most %d agents per account' % AGENT_MAX_PER_OWNER)
            self._check_agent_file_name(owner_id, clean_name)
            granted = self._agent_projects(principal, projects, owner_id=owner_id)
            # What the agent may do is kept on its record from here on (kittrial-5bb.208): the
            # list asked for, or the default. Judged before anything is written.
            made_with = self._scope_names(scopes) or tuple(AGENT_DEFAULT_SCOPES)
            expiry = self._agent_expiry_metadata()
            agent_id = 'agent_' + secrets.token_hex(8)
            agent = {
                'id': agent_id, 'name': clean_name, 'owner': owner_id,
                'created_by': principal.user_id,
                'tool': tool, 'working_directory': working_directory,
                'machine': machine, 'notes': notes, 'enabled': True,
                'projects': granted, 'created_at': now_iso(self._now()),
                # The per-agent claim limit (kittrial-5bb.276): one undelivered claim
                # by default, editable by the owner.
                'claim_limit': (CLAIM_LIMIT_DEFAULT if claim_limit is None
                                else validate_claim_limit(claim_limit)),
                'last_seen_at': None,
                'scopes': list(made_with), 'scopes_source': 'set',
            }
            if clean_role is not None:
                agent['role'] = clean_role
            self.state['agents'][agent_id] = agent
            # The grants first, so a project's view of the history includes the first credential.
            self._audit_project_grants(principal, agent, list(granted or []), request_id)
            credential, secret = self._issue_agent_credential_locked(principal, agent, expiry=expiry)
            self._audit_agent_credential(principal, agent, credential, 'agents.credentials.issued',
                                         self.agent_scopes(agent), request_id)
            event = self.audit(request_id, principal, 'agents.created', 'committed',
                               actor=self.agent_actor(agent))
            event.update(agent_id=agent_id, owner_id=owner_id, created_by=principal.user_id)
            self.store.save()
            # The one-time secret travels only in this response.
            public = {
                'operation': 'agents.create',
                'agent': self.agent_view(agent, principal),
                'credential': dict(self.credential_view(credential), secret=secret,
                                   secret_available=True),
                'setup': self.agent_setup(agent, secret),
                'secret_available': True,
            }
        return public

    def list_agents(self, principal):
        """Agents the principal may administer: its own, or every agent for a superuser."""
        with self.store.lock:
            self._refresh_authority(principal)
            agents = [a for a in self.state['agents'].values()
                      if principal.superuser or a['owner'] == principal.user_id]
            agents.sort(key=lambda a: a['id'])
            return [self.agent_view(a, principal) for a in agents]

    def agent_page(self, principal, offset, limit, *, unconfirmed=False):
        """Select a bounded owner-visible page before building credential/card views."""
        with self.store.lock:
            self._refresh_authority(principal)
            agents = [a for a in self.state['agents'].values()
                      if principal.superuser or a['owner'] == principal.user_id]
            if unconfirmed:
                agents = [a for a in agents if self.agent_scopes(a)[1] != 'set']
            agents.sort(key=lambda a: a['id'])
            return [self.agent_view(a, principal) for a in agents[offset:offset + limit]], len(agents)

    def get_agent(self, principal, agent_id):
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            return self.agent_view(agent, principal)

    def agent_for_credential(self, principal):
        """The agent behind an agent credential, for ``GET /v1/agents/me``."""
        if principal is None or principal.via != 'credential' or not principal.agent_id:
            raise forbidden('An agent credential is required')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(principal.agent_id)
            return self.agent_view(agent, principal)

    def update_agent(self, principal, agent_id, payload, request_id=None):
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change an agent')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            # The limit is judged with the other fields, before any of them is applied.
            if 'claim_limit' in payload:
                validate_claim_limit(payload.get('claim_limit'))
            if 'role' in payload:
                validate_agent_role(payload.get('role'))
            # Validate every field first and apply nothing until all of them pass, so a
            # refused change (an ungranted project, an over-long field, a clashing name)
            # leaves the live record exactly as it was. A rename in particular must not
            # stick on its own: it also renames the owner's secret file.
            changes = {}
            if 'name' in payload and payload.get('name') is not None:
                new_name = self._agent_name(payload.get('name'))
                self._check_agent_file_name(agent['owner'], new_name, exclude_id=agent['id'])
                changes['name'] = new_name
            for field, limit in (('tool', AGENT_TOOL_MAX), ('machine', AGENT_MACHINE_MAX),
                                 ('notes', AGENT_NOTES_MAX)):
                if field in payload:
                    changes[field] = self._agent_text(payload.get(field), field, limit)
            if 'working_directory' in payload:
                changes['working_directory'] = self._agent_working_directory(
                    payload.get('working_directory'), allow_empty=True)
            if 'projects' in payload:
                changes['projects'] = self._agent_projects(
                    principal, payload.get('projects'), owner_id=agent['owner'])
            if 'enabled' in payload and payload.get('enabled') is not None:
                changes['enabled'] = bool(payload.get('enabled'))
            if 'claim_limit' in payload:
                # Validated above; ``None`` clears it back to the default, which is what
                # an owner who empties the field on My agents sends.
                changes['claim_limit'] = validate_claim_limit(payload.get('claim_limit'))
            left = [pid for pid in (agent.get('projects') or []) if pid not in changes.get('projects', agent.get('projects') or [])]
            added = [pid for pid in changes.get('projects', []) if pid not in (agent.get('projects') or [])]
            agent.update(changes)
            self._audit_project_grants(principal, agent, added, request_id)
            if 'role' in payload:
                clean_role = validate_agent_role(payload.get('role'))
                if clean_role is None:
                    agent.pop('role', None)
                else:
                    agent['role'] = clean_role
            # An agent that leaves a project by THIS route leaves its coordinators too (review of
            # kittrial-5bb.209): the grant stayed in the state, unseen by the project's owner, and
            # took effect again when the project was put back, with no owner's decision.
            self._end_coordinator_grants(agent, left, principal, request_id,
                                         'the agent\'s projects were changed (PATCH /v1/agents)')
            if changes.get('enabled') is False:
                self._revoke_agent_credentials(agent)
            self.store.save()
            return self.agent_view(agent, principal)

    def lower_own_claim_limit(self, principal, value):
        """An agent credential lowers the claim limit on its OWN record (kittrial-5bb.302)."""
        return self.update_own_agent(principal, {'claim_limit': value})

    def update_own_agent(self, principal, payload):
        """An agent credential updates allowed fields on its OWN record (kittrial-5bb.302, .329).

        ``claim_limit`` may only be lowered (a whole number from 1 up to the CURRENT
        effective limit; raising stays the owner's or a superuser's move, refused with a
        sentence naming both and the My agents page). ``working_directory`` records the
        folder the agent runs in, validated with :meth:`_agent_working_directory` as a
        bounded Windows or POSIX folder path (1 to :data:`AGENT_DIRECTORY_MAX` characters,
        no control/bidi characters or prose). ``role`` records whether the
        agent runs as ``'worker'`` or ``'reviewer'``. Every requested field is validated
        before any is applied, so a refused field leaves the record untouched. The identity
        is the credential's own agent; no target id is taken, so this cannot reach another
        agent's record.
        """
        if principal is None or principal.via != 'credential' or not principal.agent_id:
            raise forbidden('An agent credential is required to change its own record')
        has_limit = 'claim_limit' in payload
        has_dir = 'working_directory' in payload
        has_role = 'role' in payload
        new_dir = None
        if has_dir:
            new_dir = self._agent_working_directory(payload.get('working_directory'),
                                                    allow_empty=False)
        new_role = None
        if has_role:
            new_role = validate_agent_role(payload.get('role'))
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent(principal.agent_id)
            current = claim_limit.default_limit(agent)
            wanted = None
            if has_limit:
                value = payload.get('claim_limit')
                try:
                    wanted = claim_limit.validate_limit(value)
                except ValueError as error:
                    raise invalid(str(error)) from None
                if wanted is None:
                    # The owner's route clears a value back to the default; on the agent's own
                    # route the body must be the number it will hold, never an absence. Name the
                    # field's own range (1..50), not the current value: a null is a malformed
                    # value, and the older sentence read "1 to <current>" (kittrial-5bb.303).
                    raise invalid('claim_limit: expected a whole number from %d to %d; an agent '
                                  'may only lower its own limit, so it must also be at most the '
                                  'current %d' % (claim_limit.LIMIT_MIN, claim_limit.LIMIT_MAX,
                                                  current))
                if wanted > current:
                    raise forbidden(claim_limit.raise_refusal(current, wanted),
                                    {'claim_limit': current, 'requested': wanted,
                                     'raise_by': claim_limit.RAISE_BY})
            old_dir = agent.get('working_directory')
            old_role = agent.get('role')
            if has_limit:
                agent['claim_limit'] = wanted
            if has_dir:
                agent['working_directory'] = new_dir
            if has_role:
                if new_role is None:
                    agent.pop('role', None)
                else:
                    agent['role'] = new_role
            self.store.save()
            operation = ('agents.claim_limit.lower' if has_limit and not has_dir and not has_role
                         else 'agents.working_directory.set' if has_dir and not has_limit and not has_role
                         else 'agents.role.set' if has_role and not has_limit and not has_dir
                         else 'agents.me.update')
            result = {'operation': operation,
                      'agent': self.agent_view(agent, principal)}
            if has_limit:
                result.update({'old_limit': current, 'new_limit': wanted,
                               'raise_by': claim_limit.RAISE_BY})
            if has_dir:
                result.update({'old_working_directory': old_dir,
                               'new_working_directory': new_dir})
            if has_role:
                result.update({'old_role': old_role, 'new_role': new_role})
            return result

    def disable_agent(self, principal, agent_id):
        """Disable an agent and revoke every credential it holds, immediately."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to disable an agent')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            agent['enabled'] = False
            self._revoke_agent_credentials(agent)
            self.store.save()
            return self.agent_view(agent, principal)

    def enable_agent(self, principal, agent_id):
        """Re-enable an agent. Previously revoked credentials stay revoked."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to enable an agent')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            agent['enabled'] = True
            self.store.save()
            return self.agent_view(agent, principal)

    def issue_agent_credential(self, principal, agent_id, *, scopes=None, label=None,
                               request_id=None):
        """Mutate under the HTTP boundary, which owns saving and rollback."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to issue an agent credential')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            if not agent.get('enabled'):
                raise conflict('A disabled agent cannot receive a credential')
            before = self.agent_scopes(agent)
            credential, secret = self._issue_agent_credential_locked(
                principal, agent, scopes=scopes, label=label)
            self._audit_agent_credential(principal, agent, credential, 'agents.credentials.issued',
                                         before, request_id)
            return {
                'operation': 'agents.credentials.issue',
                'agent': agent['id'],
                'credential': dict(self.credential_view(credential), secret=secret,
                                   secret_available=True),
                'setup': self.agent_setup(agent, secret),
                'secret_available': True,
            }

    def agent_extension_target(self, principal, agent_id, credential_id, project_id=None):
        """Recheck live authority and exact target even for an already completed retry.

        Caller holds store.lock through this check, mutation, state save and receipt.
        A project's owner only reaches agents still granted there. Coordinator roles,
        grants and both credential kinds never confer this human authority.
        """
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to extend an agent credential')
        with self.store.lock:
            self.check_authority(principal, project_id,
                                 CAP_AGENTS if project_id is None else CAP_PROJECT_ADMIN)
            agent = self._agent_owned(principal, agent_id) if project_id is None else self._agent(agent_id)
            if project_id is not None:
                if project_id not in (agent.get('projects') or []):
                    raise not_found('Agent not found')
                # The owner's live account must still be entitled to this grant.
                self._agent_projects(principal, [project_id], owner_id=agent['owner'])
            owner = self.state['users'].get(agent['owner'])
            if not owner or owner.get('disabled'):
                raise conflict('A disabled or missing account cannot extend a credential')
            if not agent.get('enabled'):
                raise conflict('A disabled agent cannot extend a credential')
            credential = self.state['credentials'].get(credential_id)
            if not isinstance(credential, dict) or credential.get('agent_id') != agent['id'] or \
                    credential.get('user_id') != agent['owner'] or credential.get('project_id') is not None:
                raise not_found('Credential not found')
            if not self._credential_live(credential):
                raise conflict('An expired or revoked credential cannot be extended; issue a new secret')
            return agent, credential

    def extend_agent_credential(self, principal, agent_id, credential_id, *, project_id=None,
                                request_id=None):
        """Extend this same key to max(old expiry, now + configured life), never revive.

        No secret is made or returned; identity, hash, scopes, actor, issuer and grants
        remain unchanged. Existing route transaction owns persistence and rollback.

        The extension is a legitimate write, so it stamps ``agent_written_raw`` with this
        raw instant (kittrial-5bb.294 item 4). The read cap then measures from here, not
        from first issuance, and the new expiry is at most ``now + lifetime`` (lifetime is
        capped at 366 days), so a legitimately extended key is never cut by its own write.
        """
        with self.store.lock:
            agent, credential = self.agent_extension_target(principal, agent_id, credential_id, project_id)
            moment = self._expiry_now()
            expiry = self._agent_expiry_metadata(moment)
            previous = credential['expires_at']
            expires = max(previous, expiry['expires_at'])
            raw = self._raw_now()
            old_raw = credential.get('agent_raw_expires_at')
            if old_raw is None:
                issued = credential.get('issued_raw')
                old_raw = float(issued) + self.credential_ttl if issued is not None else raw + previous - moment
            raw_expires = max(old_raw, raw + expires - moment)
            try:
                for stamp in (expires, raw_expires):
                    if not math.isfinite(stamp) or not 1 <= time.gmtime(stamp).tm_year <= 9999:
                        raise ValueError('unrepresentable expiry')
                    now_iso(stamp)
            except (ValueError, OverflowError, OSError):
                raise invalid('Agent credential expiry is outside the representable timestamp range') from None
            credential.update(expires_at=expires, agent_raw_expires_at=raw_expires,
                              agent_written_raw=raw)
            if 'agent_issued_raw' not in credential:
                credential['agent_issued_raw'] = credential.get('issued_raw', raw)
            # An older kit has a fixed raw-age cap: beyond that cap it must use its
            # existing stored-expiry reader. Never forge a later issuance timestamp.
            if 'issued_raw' in credential and raw_expires > float(credential['issued_raw']) + self.credential_ttl:
                del credential['issued_raw']
            event = self.audit(request_id, principal, 'agents.credentials.extended', 'committed',
                               actor=self.agent_actor(agent), project_id=project_id)
            event.update(agent_id=agent_id, target_credential_id=credential_id,
                         expires_before=now_iso(previous), expires_after=now_iso(expires))
            return {'operation': 'agents.credentials.extend', 'agent': agent_id,
                    'credential': self.agent_credential_expiry_view(credential),
                    'secret_available': False}

    def revoke_agent_credential(self, principal, agent_id, credential_id,
                                request_id=None):
        """Revoke in memory; the HTTP boundary saves without resurrecting access."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to revoke an agent credential')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            credential = self.state['credentials'].get(credential_id)
            if not isinstance(credential, dict) or \
                    credential.get('agent_id') != agent['id']:
                raise not_found('Credential not found')
            if credential.get('revoked'):
                # A retry after a failed save can persist the pending revocation,
                # without claiming another revocation or appending a second event.
                return {'id': credential_id, 'agent': agent['id'], 'revoked': True}
            before = self.agent_scopes(agent)
            credential['revoked'] = True
            self._prune_agent_credentials(agent)
            self._audit_agent_credential(principal, agent, credential, 'agents.credentials.revoked',
                                         before, request_id)
        return {'id': credential_id, 'agent': agent['id'], 'revoked': True}

    # -- idempotency -----------------------------------------------------------
    def _idempotency_key(self, principal, project_id, route, key):
        # Bind the namespace to the *full semantic target* (principal, credential,
        # project, route target and key). ``route`` is supplied as
        # "<operation> <method> <path>" so two different account/member/task targets
        # can never share a receipt even when the payload and key are identical.
        scope = '%s|%s|%s|%s|%s' % (principal.user_id, principal.credential_id or '-',
                                    project_id or '-', route, key)
        return hashlib.sha256(scope.encode('utf-8')).hexdigest()

    def idempotency_check(self, principal, project_id, route, key, body_hash):
        """Return 'replay' result, 'unknown', or None to proceed. Raises on conflict."""
        if key is None:
            return None
        digest = self._idempotency_key(principal, project_id, route, key)
        with self.store.lock:
            record = self.store.records.get('idempotency', digest)
            if record is None:
                return None
            if record['request_hash'] != body_hash:
                raise conflict('Idempotency key reused with a different request payload')
            if record['state'] == 'committed':
                return ('replay', record['status'], record['response'])
            if record['state'] == 'unknown':
                return ('unknown', None, None)
            raise conflict('An identical request is already in progress')
        return None

    def idempotency_reserve(self, principal, project_id, route, key, body_hash, *,
                            canonical=False):
        """Atomically check and reserve one idempotency key in a single lock hold.

        Returns ``('replay', status, response)`` for a committed exact retry,
        ``('reconcile', digest, None)`` when a prior attempt reserved the identity but
        did not record a result (only the canonical path can reconcile) and
        ``('new', digest, None)`` when this caller now owns the key. A second
        concurrent identical request can therefore never overwrite the reservation:
        it sees ``in_progress`` and either reconciles (canonical) or gets a 409.

        The record lives in the SQLite record store, not in the JSON state document, so
        a keyed operation no longer rewrites that document and its retention is by time
        only (``purge`` deletes past ``expires_at``; there is no count/byte eviction).
        """
        if key is None:
            return ('new', None, None)
        digest = self._idempotency_key(principal, project_id, route, key)
        with self.store.lock:
            record = self.store.records.get('idempotency', digest)
            if record is not None:
                if record['request_hash'] != body_hash:
                    raise conflict('Idempotency key reused with a different request payload')
                if record['state'] == 'committed':
                    return ('replay', record['status'], record['response'])
                if record['state'] == 'unknown' or record.get('canonical'):
                    return ('reconcile', digest, None)
                raise conflict('An identical request is already in progress')
            self.store.records.put('idempotency', digest, {
                'principal': principal.user_id, 'project_id': project_id, 'route': route,
                'request_hash': body_hash, 'state': 'in_progress', 'status': None,
                'response': None, 'canonical': bool(canonical),
                'created_at': now_iso(self._now()),
                'expires_at': self._now() + self.idempotency_ttl,
            })
        return ('new', digest, None)

    def idempotency_begin(self, principal, project_id, route, key, body_hash):
        if key is None:
            return None
        digest = self._idempotency_key(principal, project_id, route, key)
        with self.store.lock:
            self.store.records.put('idempotency', digest, {
                'principal': principal.user_id, 'project_id': project_id, 'route': route,
                'request_hash': body_hash, 'state': 'in_progress', 'status': None,
                'response': None, 'created_at': now_iso(self._now()),
                'expires_at': self._now() + self.idempotency_ttl,
            })
        return digest

    def idempotency_commit(self, digest, status, response, written_at=None):
        """Keep the answer of a committed write for a retry; ``written_at`` is the server's time of the write.

        The time is kept beside the answer and not only in it, because an answer that is a
        JSON list or empty has no field for it and its retry must carry the same header
        (kittrial-5bb.97).
        """
        if digest is None:
            return
        with self.store.lock:
            record = self.store.records.get('idempotency', digest)
            if record is not None:
                record.update(state='committed', status=status, response=response)
                if written_at is not None:
                    record['written_at'] = written_at
                self.store.records.put('idempotency', digest, record)

    def idempotency_written_at(self, principal, project_id, route, key):
        """The server's time kept with the committed answer of this request, or None.

        An answer stored before the time was kept beside it has it only in its body, when
        that is an object with ``server_time``.
        """
        if key is None:
            return None
        digest = self._idempotency_key(principal, project_id, route, key)
        with self.store.lock:
            record = self.store.records.get('idempotency', digest)
        if not isinstance(record, dict) or record.get('state') != 'committed':
            return None
        kept = record.get('written_at')
        if isinstance(kept, str):
            return kept
        body = record.get('response')
        inside = body.get('server_time') if isinstance(body, dict) else None
        return inside if isinstance(inside, str) else None

    def idempotency_unknown(self, digest):
        if digest is None:
            return
        with self.store.lock:
            record = self.store.records.get('idempotency', digest)
            if record is not None:
                record['state'] = 'unknown'
                self.store.records.put('idempotency', digest, record)

    def idempotency_release(self, digest):
        if digest is None:
            return
        with self.store.lock:
            self.store.records.delete('idempotency', digest)

    # -- canonical result replay (record store, time-only retention) -------------
    def result_get(self, key):
        """The committed canonical result for ``key``, or ``_RESULT_MISSING``."""
        record = self.store.records.get('result', key)
        if record is None:
            return _RESULT_MISSING
        return record.get('result')

    def has_result(self, key):
        """Whether the record store holds a result for ``key`` (a stored ``None`` counts)."""
        return self.store.records.get('result', key) is not None

    def result_put(self, key, value, written_at=None):
        """Record one committed canonical result with time-only retention.

        ``written_at`` is the server's time of the write, kept with the result: a retry
        that is answered from this row (its idempotency row gone, or never committed) must
        carry the time of the write and not the time of the retry (kittrial-5bb.97).
        """
        record = {'result': value}
        if isinstance(written_at, str):
            record['written_at'] = written_at
        self.store.records.put('result', key, record, ttl=self.result_retention)

    def result_written_at(self, key):
        """The server's time kept with the result for ``key``; None when there is no row or it kept none."""
        record = self.store.records.get('result', key)
        kept = record.get('written_at') if isinstance(record, dict) else None
        return kept if isinstance(kept, str) else None

    # -- HTTP-facing copies (never expose secrets) -----------------------------
    def export_state(self):
        """A backup-safe snapshot with no plaintext secret is ever stored anyway."""
        return json.loads(json.dumps(self.state))
