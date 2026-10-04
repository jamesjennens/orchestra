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
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from http_authority import (ALL_CAPABILITIES, CAP_ACCOUNTS_ADMIN, CAP_AGENTS, CAP_APPROVE,
                            CAP_CHECKPOINTS, CAP_FEEDBACK, CAP_PROJECT_ADMIN, CAP_PROPOSALS,
                            CAP_PROJECT_CREATE, CAP_READ, CAP_REVIEWS, CAP_TASKS,
                            CREDENTIAL_FORBIDDEN_CAPABILITIES, CREDENTIAL_SCOPES, RANK,
                            ROLE_CAPABILITIES, ROLES, SCOPE_CAPABILITIES, SCHEMA_VERSION,
                            JOURNAL_MAX_SKEW_SECONDS, JOURNAL_SUSPECT_SETTLE_SECONDS,
                            AuthorityDenied, authority_request, clock_advance,
                            clock_persist, clock_report, clock_state,
                            clock_trusted, decide, file_lock)

# Password verifier policy. n=2**14, r=8 needs ~16 MiB per hash; scrypt is the
# memory-hard scheme available in the standard library without third-party wheels.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SCRYPT_DKLEN, SCRYPT_SALT = 32, 16
MIN_PASSWORD, MAX_PASSWORD = 8, 1024

# Provisional local lifecycle defaults from the reviewed design, all configurable
# at the Service boundary.
SESSION_IDLE_SECONDS = 30 * 60
SESSION_ABSOLUTE_SECONDS = 12 * 60 * 60
CREDENTIAL_TTL_SECONDS = 30 * 24 * 60 * 60
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
AGENT_DEFAULT_SCOPES = ('tasks', 'checkpoints', 'reviews', 'feedback')
AGENT_CONFIG_PATH = '.orchestra/agent.json'
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

    def body(self, request_id):
        error = {'code': self.code, 'message': self.message}
        if self.detail is not None:
            error['detail'] = self.detail
        return {'error': error, 'request_id': request_id}


def unauthenticated(message='Authentication required'):
    return HttpError(401, 'unauthenticated', message)


def forbidden(message='Not permitted'):
    return HttpError(403, 'forbidden', message)


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


# ------------------------------------------------------------------- password verifier
def hash_password(password, *, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    _validate_password(password)
    salt = os.urandom(SCRYPT_SALT)
    derived = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
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
        derived = hashlib.scrypt(password.encode('utf-8'), salt=salt_bytes, n=int(n),
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


def _validate_password(password):
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise invalid('Password must be %d-%d characters' % (MIN_PASSWORD, MAX_PASSWORD))


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

    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout = 30000')
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA synchronous = FULL')
        return connection

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
        for key, value in base.items():
            data.setdefault(key, value)
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

    def save(self):
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
        with self.lock, file_lock(str(self.path) + '.lock'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                '%s.%d.%s.tmp' % (self.path.name, os.getpid(), secrets.token_hex(4)))
            text = json.dumps(self.state, ensure_ascii=False, indent=2) + '\n'
            try:
                with open(temporary, 'w', encoding='utf-8') as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise


# --------------------------------------------------------------------------- service
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
                 lookup_max=LOOKUP_MAX_PER_WINDOW, lookup_window=LOOKUP_WINDOW_SECONDS):
        self.store = store
        self.session_idle = session_idle
        self.session_absolute = session_absolute
        self.credential_ttl = credential_ttl
        self.reset_ttl = reset_ttl
        self.idempotency_ttl = idempotency_ttl
        self.result_retention = result_retention
        self.login_max_attempts = login_max_attempts
        #: Canonical base URL of this service, used only to render the copyable agent
        #: setup/resume snippets. It is deployment configuration, never request data.
        self.public_url = (public_url or '').rstrip('/') or None
        self._failures = {}
        self.lookup_max = lookup_max
        self.lookup_window = lookup_window
        self._lookups = {}

    # -- helpers ---------------------------------------------------------------
    @property
    def state(self):
        return self.store.state

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
            if credential is None or credential.get('revoked') or self._expired(
                    moment, expires_at=credential.get('expires_at'),
                    issued_raw=credential.get('issued_raw'), lifetime=self.credential_ttl):
                return True
            if credential.get('agent_id'):
                agent = self.state['agents'].get(credential['agent_id'])
                return not isinstance(agent, dict) or not agent.get('enabled')
            return False
        return True

    def audit(self, request_id, principal, action, outcome, *, project_id=None, reason=None,
              actor=None):
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
        self.state['audit'].append(event)
        if len(self.state['audit']) > AUDIT_LIMIT:
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
    def bootstrap_superuser(cls, store, username, password, display_name=None):
        """Create the first protected superuser. Refuses once any account exists."""
        with store.lock:
            if store.state['users']:
                raise conflict('A superuser already exists')
            service = cls(store)
            username = service._username(username)
            user = {
                'id': 'usr_' + secrets.token_hex(8),
                'username': username,
                'display_name': display_name or username,
                'password': hash_password(password),
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

    @staticmethod
    def _public_user(user):
        return {'id': user['id'], 'username': user['username'],
                'display_name': user['display_name'], 'disabled': user['disabled'],
                'superuser': user['superuser'], 'created_at': user['created_at']}

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
            user['password'] = hash_password(new_password)
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
        _validate_password(new_password)
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
            user['password'] = hash_password(new_password)
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

    def login(self, username, password, source='local', request_id=None):
        """Uniform failure response; never reveals whether the account exists."""
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
                self.store.save()
                return Principal(user['id'], user['display_name'], user['superuser'],
                                 'session', user['id'], csrf=session['csrf'],
                                 session_hash=digest)
            credential_id = self.state['credential_tokens'].get(digest)
            credential = self.state['credentials'].get(credential_id) if credential_id else None
            if credential is not None:
                if credential['revoked'] or self._expired(
                        moment, expires_at=credential['expires_at'],
                        issued_raw=credential.get('issued_raw'),
                        lifetime=self.credential_ttl):
                    raise unauthenticated('Credential expired or revoked')
                user = self.state['users'].get(credential['user_id'])
                if user is None or user['disabled']:
                    raise unauthenticated('Credential expired or revoked')
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
                self.store.save()
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
            if credential is None or credential['revoked'] or self._expired(
                    moment, expires_at=credential['expires_at'],
                    issued_raw=credential.get('issued_raw'),
                    lifetime=self.credential_ttl):
                raise unauthenticated('Credential expired or revoked')
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
    def create_project(self, principal, name, project_id=None):
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
            self.state['projects'][pid] = {
                'id': pid, 'name': name, 'created_by': principal.user_id,
                'created_at': now_iso(self._now()), 'archived': False,
            }
            self.state['memberships'][pid] = {principal.user_id: 'owner'}
            self.store.save()
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
    _REPO_HOST = r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?'
    _REPO_USER = r'[A-Za-z0-9_][A-Za-z0-9._-]{0,63}'
    _REPO_PATH = r'[A-Za-z0-9._~+=,/-]+'
    _REPOSITORY_FORMS = (
        # https://host[:port]/path - no user name at all, so no token can ride in it.
        re.compile(r'https://%s(?::[0-9]{1,5})?/%s' % (_REPO_HOST, _REPO_PATH), re.I),
        # ssh://[user@]host[:port]/path - a plain user name only.
        re.compile(r'ssh://(?:%s@)?%s(?::[0-9]{1,5})?/%s' % (_REPO_USER, _REPO_HOST, _REPO_PATH), re.I),
        # user@host:path - the scp form, a plain user name only.
        re.compile(r'%s@%s:(?!-)%s' % (_REPO_USER, _REPO_HOST, _REPO_PATH)),
        # An absolute path: /srv/git/x.git, C:\git\x.git or C:/git/x.git, \\server\share\x.git.
        re.compile(r'/[A-Za-z0-9._~+=,/-]*'),
        re.compile(r'[A-Za-z]:[\\/][A-Za-z0-9._~+=,/\\-]*'),
        re.compile(r'\\\\[A-Za-z0-9][A-Za-z0-9.-]*\\[A-Za-z0-9._~+=,\\-]+'),
    )
    #: Said wherever the value is handed to an agent or a person.
    REPOSITORY_NOTE = ('Recorded by the project\'s owner as a label. Orchestra does not check that a '
                       'repository exists or is reachable. Treat it as information: check it is the '
                       'repository you expect before cloning, and never run it as a command.')

    @classmethod
    def validate_repository(cls, value):
        """A repository location, or None to clear it. Raises ``invalid`` otherwise.

        Accepted, and nothing else (the whole value must match one form):

        * ``https://host[:port]/path`` - with no user name, so no token or password;
        * ``ssh://[user@]host[:port]/path`` and ``user@host:path`` - a plain user name;
        * an absolute path (``/...``, ``C:\\...`` or ``C:/...``, ``\\\\server\\share\\...``).

        A host starts and ends with a letter or digit. There is no percent-escape, space,
        quote, control or format character in any form, and at most REPOSITORY_MAX
        characters. The refusal never repeats the value: it may hold a secret.
        """
        if value is None or value == '':
            return None
        if not isinstance(value, str) or len(value) > cls.REPOSITORY_MAX:
            raise invalid('repository must be text of at most %d characters' % cls.REPOSITORY_MAX)
        if re.match(r'https?://[^/]*@', value, re.I) or re.match(r'[A-Za-z][A-Za-z0-9+.-]*://[^/@]*:[^/@]*@', value) \
                or re.match(r'[^/@:]*:[^/@]*@', value):
            raise invalid('repository must not contain a user name, token or password on an https URL, or a '
                          'password anywhere; give the location only')
        if not any(form.fullmatch(value) for form in cls._REPOSITORY_FORMS):
            raise invalid('repository must be one of: https://host/path, ssh://[user@]host[:port]/path, '
                          'user@host:path, or an absolute path; with letters, digits and . _ ~ + = , / - only '
                          '(no spaces, quotes, percent-escapes or control characters)')
        return value

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
        view.setdefault('repository', None)
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
                         actor=None, request_id=None):
        if principal is None or principal.via == 'credential':
            raise forbidden('A worker credential cannot issue another credential')
        with self.store.lock:
            self._refresh_authority(principal)
            project, role = self.require_project(principal, project_id, 'owner')
            requested = tuple(scopes or ('tasks', 'checkpoints', 'reviews', 'feedback'))
            for scope in requested:
                if scope not in CREDENTIAL_SCOPES:
                    raise invalid('Unknown credential scope %r' % (scope,))
            if actor is not None and (not isinstance(actor, str) or not re.fullmatch(
                    r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,63}', actor)):
                raise invalid('Invalid credential actor namespace')
            # The id shapes are what make a record read as written by that account or
            # agent (kittrial-5bb.70 review 01a10262): the only one an owner may name is
            # their own account id.
            claimed = actor.split('/', 1)[0] if actor is not None else None
            if claimed is not None and re.fullmatch(r'(?:usr|agent)_[0-9a-f]{16}', claimed) \
                    and claimed != principal.user_id:
                raise invalid('A credential actor namespace cannot have the shape of an account or agent id '
                              'other than your own account id')
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
                'token_hash': token_hash(secret),
                'created_at': now_iso(self._now()),
                'issued_raw': self._raw_now(),
                'last_used': None,
                'expires_at': moment + self.credential_ttl,
                'revoked': False,
            }
            self.state['credentials'][credential['id']] = credential
            self.state['credential_tokens'][token_hash(secret)] = credential['id']
            self.store.save()
        return {'id': credential['id'], 'project': project_id, 'label': credential['label'],
                'scopes': list(requested), 'actor': actor, 'secret': secret,
                'expires_at': now_iso(credential['expires_at']), 'secret_available': True}

    def credential_view(self, credential):
        return {'id': credential['id'], 'project': credential['project_id'],
                'agent': credential.get('agent_id'),
                'label': credential['label'], 'scopes': list(credential['scopes']),
                'actor': credential.get('actor'), 'revoked': credential['revoked'],
                'created_at': credential['created_at'], 'last_used': credential.get('last_used'),
                'expires_at': now_iso(credential['expires_at'])}

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
    def _agent_text(value, field, limit):
        if value is None:
            return None
        if not isinstance(value, str):
            raise invalid('%s must be text' % field)
        text = value.strip()
        if not text:
            return None
        if len(text) > limit or any(ord(c) < 32 for c in text):
            raise invalid('%s must be at most %d printable characters' % (field, limit))
        return text

    def _agent_projects(self, principal, projects, owner_id=None):
        """Validate a requested agent grant against the *owner's* live membership.

        A grant may only name projects the agent's owner can already read. For the
        owner's own request that is the pre-existing rule; for a superuser changing
        somebody else's agent it means a superuser may **not** store a project outside
        the owner's membership, because such a grant can only ever be a leak waiting to
        be read. The stored list is a *ceiling*: the live role cap in
        :func:`http_authority.decide` is still applied on every request, and attention
        re-authorizes each project at read time.
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
                raise not_found('Project not found')
        return granted

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

    def _revoke_agent_credentials(self, agent):
        for credential in self._agent_credentials(agent):
            credential['revoked'] = True

    def agent_view(self, agent, principal):
        """The owner-visible agent record. The path never leaks to anyone else."""
        owner_sees_path = bool(principal.superuser or
                               principal.user_id == agent['owner'])
        view = {
            'id': agent['id'], 'name': agent['name'], 'owner': agent['owner'],
            'owner_display_name': self._owner_name(agent['owner']),
            'tool': agent.get('tool'), 'machine': agent.get('machine'),
            'notes': agent.get('notes'), 'enabled': bool(agent.get('enabled')),
            'actor': self.agent_actor(agent),
            'projects': list(agent.get('projects') or []),
            'created_at': agent.get('created_at'),
            'last_seen_at': agent.get('last_seen_at'),
            'credentials': [self.credential_view(c) for c in self._agent_credentials(agent)],
        }
        if owner_sees_path:
            view['working_directory'] = agent.get('working_directory')
        else:
            # Explicit, so a client can tell "hidden from you" from "not set".
            view['working_directory_hidden'] = True
        return view

    def project_agent_view(self, agent):
        """The *project-owner* view of an agent active in their project.

        Deliberately narrower than :meth:`agent_view`. A project owner governs
        whether an agent may work in their project, but is not that agent's owner, so
        the response carries identity, attribution and liveness only. The
        ``working_directory`` hint never appears - and neither does
        ``working_directory_hidden``, which would still disclose that a path exists -
        nor do the agent's other project grants, its machine/notes or its credentials.
        """
        return {
            'id': agent['id'],
            'name': agent['name'],
            'owner': agent['owner'],
            'owner_display_name': self._owner_name(agent['owner']),
            'actor': self.agent_actor(agent),
            'enabled': bool(agent.get('enabled')),
            'last_seen_at': agent.get('last_seen_at'),
        }

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
            return [self.project_agent_view(a) for a in agents]

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
            return self.project_agent_view(agent)

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
            self.store.save()
            view = self.project_agent_view(agent)
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
            "#    PowerShell:  curl.exe -fsS -K \"%(ps)s\" %(server)s/v1/agents/me\n"
            "#    macOS/Linux: curl -fsS -K %(posix)s %(server)s/v1/agents/me\n"
            "# 3. In the agent folder (shell shown; the web page can save these files for you):\n"
            "mkdir -p .orchestra\n"
            "cat > %(config_path)s <<'JSON'\n%(config)s\nJSON\n"
            "[ -f .gitignore ] && ! grep -qx '.orchestra/' .gitignore && printf '\\n.orchestra/\\n' >> .gitignore\n"
            "# Every call hands curl the config file (-K), so the secret never reaches a\n"
            "# command line, shell history or the process list:\n"
            "curl -fsS -K %(posix)s %(server)s/v1/agents/me/next\n"
            "# Secondary fallback only, for a machine where that file cannot be kept: the\n"
            "# %(env)s environment variable may hold the secret. Never write the secret\n"
            "# on a command line, in the agent folder, in agent.json or in Git.\n"
        ) % {'win': secret_file['windows'], 'ps': secret_file['windows_powershell'],
             'posix': secret_file['posix'], 'server': server, 'config_path': AGENT_CONFIG_PATH,
             'config': json.dumps(config, indent=2, sort_keys=True), 'env': AGENT_SECRET_ENV}
        # Where each granted project's repository is, as its owner recorded it
        # (kittrial-5bb.118). A label for the person setting the agent up: it is listed
        # beside the setup text, never inside the commands, and nothing runs it.
        repositories = [{'project': pid, 'repository': self.state['projects'][pid]['repository']}
                        for pid in config['projects']
                        if self.state['projects'].get(pid, {}).get('repository')]
        return {
            'config_path': AGENT_CONFIG_PATH,
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

    def _issue_agent_credential_locked(self, principal, agent, scopes=None, label=None):
        """Create one agent credential. Caller holds ``store.lock`` and has authorized."""
        requested = tuple(scopes or AGENT_DEFAULT_SCOPES)
        for scope in requested:
            if scope not in CREDENTIAL_SCOPES:
                raise invalid('Unknown credential scope %r' % (scope,))
        if len(self._agent_credentials(agent)) >= AGENT_MAX_CREDENTIALS:
            raise conflict('An agent may hold at most %d credentials'
                           % AGENT_MAX_CREDENTIALS)
        secret = new_token()
        moment = self._expiry_now()
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
            'issued_raw': self._raw_now(),
            'last_used': None,
            'expires_at': moment + self.credential_ttl,
            'revoked': False,
        }
        self.state['credentials'][credential['id']] = credential
        self.state['credential_tokens'][token_hash(secret)] = credential['id']
        return credential, secret

    def create_agent(self, principal, *, name, tool=None, working_directory=None,
                     machine=None, notes=None, projects=None, scopes=None,
                     request_id=None):
        """Create an owner-bound agent and its first (one-time) credential."""
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to create an agent')
        clean_name = self._agent_name(name)
        tool = self._agent_text(tool, 'tool', AGENT_TOOL_MAX)
        working_directory = self._agent_text(working_directory, 'working_directory',
                                             AGENT_DIRECTORY_MAX)
        machine = self._agent_text(machine, 'machine', AGENT_MACHINE_MAX)
        notes = self._agent_text(notes, 'notes', AGENT_NOTES_MAX)
        with self.store.lock:
            self._refresh_authority(principal)
            owned = [a for a in self.state['agents'].values()
                     if a['owner'] == principal.user_id]
            if len(owned) >= AGENT_MAX_PER_OWNER:
                raise conflict('At most %d agents per account' % AGENT_MAX_PER_OWNER)
            self._check_agent_file_name(principal.user_id, clean_name)
            granted = self._agent_projects(principal, projects)
            agent_id = 'agent_' + secrets.token_hex(8)
            agent = {
                'id': agent_id, 'name': clean_name, 'owner': principal.user_id,
                'tool': tool, 'working_directory': working_directory,
                'machine': machine, 'notes': notes, 'enabled': True,
                'projects': granted, 'created_at': now_iso(self._now()),
                'last_seen_at': None,
            }
            self.state['agents'][agent_id] = agent
            credential, secret = self._issue_agent_credential_locked(principal, agent,
                                                                     scopes=scopes)
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

    def update_agent(self, principal, agent_id, payload):
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to change an agent')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
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
                                 ('notes', AGENT_NOTES_MAX),
                                 ('working_directory', AGENT_DIRECTORY_MAX)):
                if field in payload:
                    changes[field] = self._agent_text(payload.get(field), field, limit)
            if 'projects' in payload:
                changes['projects'] = self._agent_projects(
                    principal, payload.get('projects'), owner_id=agent['owner'])
            if 'enabled' in payload and payload.get('enabled') is not None:
                changes['enabled'] = bool(payload.get('enabled'))
            agent.update(changes)
            if changes.get('enabled') is False:
                self._revoke_agent_credentials(agent)
            self.store.save()
            return self.agent_view(agent, principal)

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
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to issue an agent credential')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            if not agent.get('enabled'):
                raise conflict('A disabled agent cannot receive a credential')
            credential, secret = self._issue_agent_credential_locked(
                principal, agent, scopes=scopes, label=label)
            self.store.save()
            return {
                'operation': 'agents.credentials.issue',
                'agent': agent['id'],
                'credential': dict(self.credential_view(credential), secret=secret,
                                   secret_available=True),
                'setup': self.agent_setup(agent, secret),
                'secret_available': True,
            }

    def revoke_agent_credential(self, principal, agent_id, credential_id,
                                request_id=None):
        if principal is None or principal.via == 'credential':
            raise forbidden('Session authority required to revoke an agent credential')
        with self.store.lock:
            self._refresh_authority(principal)
            agent = self._agent_owned(principal, agent_id)
            credential = self.state['credentials'].get(credential_id)
            if not isinstance(credential, dict) or \
                    credential.get('agent_id') != agent['id']:
                raise not_found('Credential not found')
            credential['revoked'] = True
            self.store.save()
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

    def idempotency_commit(self, digest, status, response):
        if digest is None:
            return
        with self.store.lock:
            record = self.store.records.get('idempotency', digest)
            if record is not None:
                record.update(state='committed', status=status, response=response)
                self.store.records.put('idempotency', digest, record)

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

    def result_put(self, key, value):
        """Record one committed canonical result with time-only retention."""
        self.store.records.put('result', key, {'result': value},
                              ttl=self.result_retention)

    # -- HTTP-facing copies (never expose secrets) -----------------------------
    def export_state(self):
        """A backup-safe snapshot with no plaintext secret is ever stored anyway."""
        return json.loads(json.dumps(self.state))
