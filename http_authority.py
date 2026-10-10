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
  closed) for the long window. Expiry and replay decisions use a *trusted* clock that
  holds a sudden forward step for :data:`JOURNAL_SUSPECT_SETTLE_SECONDS` before
  accepting it (see "Trusted clock" below); rows are stamped with the raw clock and
  reclaim/compaction run only while the clock is not suspect. Neither the byte budget
  nor the tombstone count can evict a live
  identity: the size is *reported* by :meth:`OperationJournal.stats` (and
  ``admin.py journal``) instead. Reclaim never *drops* an identity: the store is a
  SQLite database with one indexed row per identity, and an oversized response is kept
  as a digest instead of a body. The only admission bound is the live-identity count:
  when the journal genuinely cannot hold one more live identity the mutation fails
  closed (``124``, :class:`JournalFull`) with the pre-existing store untouched, so an
  exact retry is refused as expired rather than re-executed.

Trusted clock
-------------
Each store persists five values in its ``meta`` table: ``high_water`` (the largest raw
clock ever observed by a write; it never decreases), ``suspect`` (bool), ``anchor`` (the
``high_water`` at the moment suspicion began), ``suspect_since`` (the raw clock when
suspicion began) and ``jump_credit`` (a non-decreasing running total, in seconds, of
every suspect forward step). Every write transaction observes the raw clock ``now``
(:func:`clock_advance`):

* ``high_water`` unset: ``high_water = now``, not suspect.
* ``step = now - high_water > JOURNAL_MAX_SKEW_SECONDS`` (a step of more than 24 h):
  the store becomes suspect and ``jump_credit += step``. The first such step records
  ``anchor = high_water``; a further big step restarts ``suspect_since`` (and adds its
  own step to the credit) but keeps the original anchor.
* otherwise, a suspect store whose clock has run for
  :data:`JOURNAL_SUSPECT_SETTLE_SECONDS` (1 h) since the step is cleared: the step is
  accepted as real time.
* always ``high_water = max(high_water, now)``. A backward step is never suspect and
  never changes the credit.

``trusted_now`` is ``now`` when not suspect and, while suspect, only the time elapsed
since the step, counted from the anchor: ``clamp(anchor + (now - suspect_since),
anchor, anchor + max_skew)`` (:func:`clock_trusted`). It is used for every expiry and
replay-window decision, on the read path (evaluated against the transition the next
write would make, so a read before the first write after a jump is already protected)
and on the write path, and by ``http_auth.RecordStore`` too. Rows are stamped with the
RAW clock: a future stamp left by a later-corrected jump only lengthens that row's
retention. Receipt reclaim (committed -> tombstone after the replay window, uncertain ->
tombstone after the long window) runs only while the store is not suspect, against the
raw clock; its worst case after an accepted jump is an exact retry refused as expired
(``rc=2``), never a re-execution.

**Tombstones age on the confirmed timeline.** A tombstone records ``aged_from =
reclaimed_at - jump_credit`` (the credit at the moment it was reclaimed) and is deleted
only when ``aged_from < now - jump_credit - JOURNAL_TOMBSTONE_SECONDS``, i.e. when its
age *excluding every suspect forward step since it was reclaimed* exceeds the horizon.
Tombstone ageing therefore never consumes unconfirmed forward time: an accepted jump
extends retention by the jump length, and an idle gap of more than 24 h (a weekend)
extends it by the gap, which is safe. :meth:`OperationJournal.reset_high_water` never
reduces the credit.

**Client retry contract.** An exact retry sent no more than
:data:`JOURNAL_RETRY_HORIZON_SECONDS` (``JOURNAL_TOMBSTONE_SECONDS -
JOURNAL_MAX_SKEW_SECONDS``, 29 days) after the original attempt replays, reports
``124`` or is refused as expired (``rc=2``) - it is never re-executed - **as long as
the total uncredited forward clock error stays below** ``JOURNAL_MAX_SKEW_SECONDS``
(24 h). A forward step of 24 h or less is never suspect and never credited, and such
steps compose: several false steps just under 24 h with no correction age tombstones
early by their sum (three false +23 h steps re-execute retries about 28.4-29.1 days
old). Keep the host clock disciplined (NTP); after finding and correcting a clock that
ran ahead, run ``admin.py journal PROJECT --reset-high-water`` to re-arm detection.
Tombstones already aged out during the error are not restored, so retries of those
identities are outside the guarantee. An older retry is unsupported: its tombstone may
have been aged out and the effect may run again. Use a fresh ``operation_id`` after
reconciling instead. The service-local HTTP routes (credential issue, account/project
create, membership changes) have no canonical journal behind them: their guarantee is
the HTTP idempotency record's window (``http_auth.IDEMPOTENCY_TTL_SECONDS``, 24 h),
kept on the same confirmed timeline by ``http_auth.RecordStore``.

**Sparse projects.** A project whose writes are always more than 24 h apart credits
every gap, so its ``jump_credit`` grows at about real time and its (few) tombstones
effectively never age out. That is safe - the retention cost is one small row per
identity - and it ends as soon as writes fall within 24 h of each other.

Effect: after an idle weekend the first write is suspect for one hour, with the
trusted clock held near the anchor (receipts older than their window may still replay
instead of being refused - harmless), then normal operation resumes with no operator
action. While consecutive writes stay more than 24 h apart the store stays suspect:
reclaim pauses and old receipts keep replaying until two writes fall within 24 h and an
hour passes. During a forward jump a 60-second-old receipt replays and an uncertain
reservation reports ``124``. **Residual risk:** a jump that persists for longer than
the settle period is accepted: receipts still inside their real window may then be
compacted to tombstones and an exact retry is refused as expired; no identity inside
the retry horizon is deleted and nothing is re-executed. A jump that is corrected
leaves ``high_water`` in the future, so the store stays suspect (trusted clock held at
the anchor, reclaim paused) for about the length of the jump, and a later repeat of the
same jump would not be detected again; after correcting a clock run ``admin.py journal
PROJECT --reset-high-water`` (``high_water = now``, clears suspicion, keeps the credit)
to re-arm detection.

Nothing here imports ``fcntl`` at module import time, so the same module imports on a
Windows workstation and a Linux office host.
"""
import hashlib
import json
import os
import re
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
#: Advisory size for the compact tombstone set. It is *reported only*
#: (``stats()['over_tombstones']``): it never stops reclaim, never drops a tombstone and
#: never blocks a write. Tombstone retention is by time alone
#: (:data:`JOURNAL_TOMBSTONE_SECONDS`); ``--prune-before`` is the explicit operator
#: override.
JOURNAL_TOMBSTONE_LIMIT = 100000
#: A raw-clock step larger than this since the last write (``now - high_water``) makes
#: the store *suspect* (see "Trusted clock" in the module docstring). While suspect the
#: trusted clock is capped at ``anchor + JOURNAL_MAX_SKEW_SECONDS`` and reclaim and
#: tombstone deletion do not run.
JOURNAL_MAX_SKEW_SECONDS = 24 * 60 * 60
#: How long the raw clock must keep running after a suspect step before the step is
#: accepted and suspicion clears on the next write.
JOURNAL_SUSPECT_SETTLE_SECONDS = 60 * 60
#: The client retry contract: an exact retry no older than this after the original
#: attempt is never re-executed, provided the total uncredited forward clock error
#: (forward steps of 24 h or less that were not real elapsed time) stays below
#: ``JOURNAL_MAX_SKEW_SECONDS``. An older retry is unsupported.
JOURNAL_RETRY_HORIZON_SECONDS = JOURNAL_TOMBSTONE_SECONDS - JOURNAL_MAX_SKEW_SECONDS
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
#: guarded by the persisted ``legacy_migrated`` marker. Schema 5 added the persisted
#: trusted-clock state; schema 6 adds ``jump_credit``, the ``aged_from`` tombstone column
#: and the ``(state, aged_from)`` index (replacing ``(state, reclaimed_at)``). An older
#: store is upgraded (columns, backfill, index, clock state) in one transaction on first
#: open, and an up-to-date store runs no DDL or backfill on open.
JOURNAL_SCHEMA = 6
#: The live operation-journal store, one SQLite database per project directory.
JOURNAL_FILENAME = '.http-operations.sqlite3'
#: The pre-revision-7 JSON journal document, kept only as a one-time migration source.
LEGACY_JOURNAL_FILENAME = '.http-operations.json'


def journal_path(project_dir):
    """The live operation-journal store for one project directory."""
    return Path(project_dir) / JOURNAL_FILENAME


# --------------------------------------------------------------- trusted clock
#: The ``meta`` keys of the persisted trusted-clock state.
CLOCK_KEYS = ('high_water', 'suspect', 'anchor', 'suspect_since', 'jump_credit')


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def clock_state(connection):
    """Read the persisted trusted-clock state from a store's ``meta`` table."""
    values = {}
    for row in connection.execute(
            'SELECT key, value FROM meta WHERE key IN (?, ?, ?, ?, ?)', CLOCK_KEYS):
        values[row[0]] = row[1]
    high = _float_or_none(values.get('high_water'))
    suspect = _float_or_none(values.get('suspect'))
    return {'high_water': high if high is not None and high > 0 else None,
            'suspect': bool(suspect),
            'anchor': _float_or_none(values.get('anchor')),
            'suspect_since': _float_or_none(values.get('suspect_since')),
            'jump_credit': _float_or_none(values.get('jump_credit')) or 0.0}


def clock_advance(state, now, max_skew=JOURNAL_MAX_SKEW_SECONDS,
                  settle=JOURNAL_SUSPECT_SETTLE_SECONDS):
    """The trusted-clock state after a write observes the raw clock ``now``.

    Pure function (see "Trusted clock" in the module docstring): the write path persists
    the result with :func:`clock_persist`; the read path evaluates it without persisting,
    so a read that precedes the first write after a jump is already protected.
    """
    now = float(now)
    high = state.get('high_water')
    suspect = bool(state.get('suspect'))
    anchor = state.get('anchor')
    since = state.get('suspect_since')
    credit = float(state.get('jump_credit') or 0.0)
    if high is None:
        return {'high_water': now, 'suspect': False, 'anchor': None, 'suspect_since': None,
                'jump_credit': credit}
    step = now - high
    if step > max_skew:
        if not suspect or anchor is None:
            anchor = high
        suspect = True
        since = now
        # Every suspect forward step is credited when it is observed, so tombstone
        # ageing never consumes it (see "Tombstones age on the confirmed timeline").
        credit += step
    elif suspect and since is not None and now - since >= settle:
        suspect, anchor, since = False, None, None
    return {'high_water': max(high, now), 'suspect': suspect,
            'anchor': anchor if suspect else None,
            'suspect_since': since if suspect else None,
            'jump_credit': credit}


def clock_trusted(state, now, max_skew=JOURNAL_MAX_SKEW_SECONDS):
    """The trusted clock: ``now`` when not suspect.

    While suspect only the time elapsed since the step counts, from the anchor:
    ``clamp(anchor + (now - suspect_since), anchor, anchor + max_skew)``. A receipt
    written just before a jump therefore still replays during it.
    """
    if state.get('suspect') and state.get('anchor') is not None:
        anchor = float(state['anchor'])
        since = state.get('suspect_since')
        elapsed = float(now) - float(since) if since is not None else 0.0
        return min(max(anchor + elapsed, anchor), anchor + max_skew)
    return float(now)


def clock_persist(connection, state):
    """Write the trusted-clock state inside the caller's open transaction."""
    for key in CLOCK_KEYS:
        value = state.get(key)
        if key == 'suspect':
            value = 1 if value else 0
        if value is None:
            connection.execute('DELETE FROM meta WHERE key = ?', (key,))
        else:
            connection.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
                               (key, value))


def clock_report(state):
    """The operator-visible clock fields (``stats()``/``admin.py journal``)."""
    return {'high_water': state.get('high_water') or 0.0,
            'suspect': bool(state.get('suspect')),
            'anchor': state.get('anchor'),
            'suspect_since': state.get('suspect_since'),
            'jump_credit': float(state.get('jump_credit') or 0.0),
            'clock_skewed': bool(state.get('suspect'))}

# --------------------------------------------------------------- capability model
CAP_READ = 'read'
CAP_TASKS = 'tasks.write'
CAP_CHECKPOINTS = 'checkpoints.write'
CAP_REVIEWS = 'reviews.write'
CAP_FEEDBACK = 'feedback.write'
CAP_APPROVE = 'reviews.approve'
#: Submit or revise a contributed requirement proposal (.58 slice 1b, kittrial-5bb.70).
#: Contributors and owners hold it, and the `proposals` credential scope grants it, so a
#: member or an agent can propose. Triage and decisions need CAP_APPROVE, which no
#: credential ever holds.
CAP_PROPOSALS = 'proposals.write'
CAP_PROJECT_ADMIN = 'project.admin'
CAP_PROJECT_CREATE = 'project.create'
CAP_ACCOUNTS_ADMIN = 'accounts.admin'
#: Creating a project ON THE HOST from the web interface (kittrial-5bb.118 part 2): a
#: superuser, or an account a superuser granted it to, within the grant's limit.
CAP_PROJECT_HOST_CREATE = 'project.host-create'
#: The canonical project name (``admin.validate_name``); only such ids are host projects.
CANONICAL_PROJECT_NAME = re.compile(r'[a-z][a-z0-9]{1,23}')
GRANT_LIMIT_MAX = 100
GRANT_LIMIT_DEFAULT = 5
#: Personal agent management. It is deliberately NOT project-scoped: every
#: authenticated session may manage the agents it owns, while no worker/agent
#: credential ever holds it (an agent cannot create or widen another identity).
CAP_AGENTS = 'agents.manage'

ROLES = ('viewer', 'contributor', 'owner')
RANK = {'viewer': 0, 'contributor': 1, 'owner': 2}

SCOPE_CAPABILITIES = {
    'read': frozenset({CAP_READ}),
    'tasks': frozenset({CAP_TASKS}),
    'checkpoints': frozenset({CAP_CHECKPOINTS}),
    'reviews': frozenset({CAP_REVIEWS}),
    'feedback': frozenset({CAP_FEEDBACK}),
    # .58's agent-proposal scope: recognised since slice 0 (kittrial-5bb.64), it grants
    # the proposal write capability since slice 1b (kittrial-5bb.70).
    'proposals': frozenset({CAP_PROPOSALS}),
}
CREDENTIAL_SCOPES = tuple(sorted(SCOPE_CAPABILITIES))

ROLE_CAPABILITIES = {
    'viewer': frozenset({CAP_READ}),
    'contributor': frozenset({CAP_READ, CAP_TASKS, CAP_CHECKPOINTS, CAP_REVIEWS, CAP_FEEDBACK,
                              CAP_PROPOSALS}),
    'owner': frozenset({CAP_READ, CAP_TASKS, CAP_CHECKPOINTS, CAP_REVIEWS, CAP_FEEDBACK,
                        CAP_PROPOSALS, CAP_APPROVE, CAP_PROJECT_ADMIN}),
}
CREDENTIAL_FORBIDDEN_CAPABILITIES = frozenset({CAP_APPROVE, CAP_PROJECT_ADMIN,
                                               CAP_PROJECT_CREATE, CAP_ACCOUNTS_ADMIN,
                                               CAP_AGENTS})
ALL_CAPABILITIES = frozenset(ROLE_CAPABILITIES['owner'] | {CAP_PROJECT_CREATE,
                                                           CAP_ACCOUNTS_ADMIN,
                                                           CAP_AGENTS})


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


def project_grant(user):
    """The account's "may create projects" grant as ``{'limit', 'granted_by', 'granted_at'}``, or None.

    A grant that is not exactly that shape is no grant: the check fails closed.
    """
    grant = user.get('project_grant') if isinstance(user, dict) else None
    if not isinstance(grant, dict) or type(grant.get('limit')) is not int or \
            not 1 <= grant['limit'] <= GRANT_LIMIT_MAX or not isinstance(grant.get('granted_by'), str):
        return None
    return grant


def created_projects(state, user_id):
    """The registered host projects an account created that still count toward its limit.

    Counted: a record this account created whose id is a canonical project name and which
    is not archived. Handing the project to another owner does not free the place;
    archiving it does (and an operator retires the host project separately).
    """
    return sorted(pid for pid, project in (state.get('projects') or {}).items()
                  if isinstance(project, dict) and project.get('created_by') == user_id
                  and not project.get('archived') and isinstance(pid, str)
                  and CANONICAL_PROJECT_NAME.fullmatch(pid))


#: Said to everyone who may not create a project, whatever name they sent.
HOST_CREATE_REFUSED = 'This account may not create projects. A superuser grants that, or creates the project.'


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
        if capability in (CAP_ACCOUNTS_ADMIN, CAP_PROJECT_CREATE, CAP_AGENTS, CAP_PROJECT_HOST_CREATE):
            raise deny(403, 'forbidden',
                       'A worker credential cannot perform administrative operations')
        if credential.get('agent_id'):
            # An agent credential is a personal identity: it is refused the moment the
            # agent record is gone or disabled, and its project reach is the agent's
            # LIVE grant list (not a snapshot taken at issue time), so narrowing the
            # grant takes effect on the next request.
            agent = state.get('agents', {}).get(credential.get('agent_id'))
            if not isinstance(agent, dict) or not agent.get('enabled'):
                raise deny(401, 'unauthenticated', 'Agent is disabled')
            if project_id not in (agent.get('projects') or []):
                raise deny(404, 'not_found', 'Project not found')
        elif credential.get('project_id') != project_id:
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
    if capability == CAP_PROJECT_HOST_CREATE:
        # Never an agent or worker credential (refused above), always the live record:
        # a grant taken away, or a limit lowered, is seen by the next decision.
        if user.get('superuser'):
            return {'role': 'superuser', 'limit': None, 'used': len(created_projects(state, user_id))}
        grant = project_grant(user)
        if grant is None:
            raise deny(403, 'forbidden', HOST_CREATE_REFUSED)
        used = len(created_projects(state, user_id))
        if used >= grant['limit']:
            raise deny(403, 'forbidden', 'The limit of %d project(s) for this account is reached (%d in use)'
                       % (grant['limit'], used))
        return {'role': 'grant', 'limit': grant['limit'], 'used': used}
    if capability == CAP_AGENTS:
        # Personal identity management, not a project operation: every authenticated
        # session may manage the agents it owns, and the service checks ownership of
        # the specific agent separately.
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
    ``file_lock`` create a lock at a caller-chosen path. ``service_namespace`` is the
    service's own actor namespace (``--actor-namespace``, default ``http``), carried for
    the same reason: at use the endpoint must refuse a name the service was started under,
    and only its launcher knows it (kittrial-5bb.188 item 3).
    """

    __slots__ = ('store', 'lock', 'service_namespace')

    def __init__(self, store, lock=None, service_namespace=None):
        if not isinstance(store, str) or not store:
            raise ValueError('AuthorityConfig.store must be a non-empty path')
        self.store = store
        self.lock = lock or (store + '.lock')
        if service_namespace is None:
            import actor_names
            service_namespace = actor_names.SERVICE_NAMESPACE
        self.service_namespace = service_namespace


def principal_key(request, authority_configured=False, account_identity=False):
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
            if account_identity:
                return 'owner-account:' + user_id
            return 'user:%s|cred:%s|session:%s' % (
                user_id, authority.get('credential_id') or '-',
                authority.get('session_hash') or '-')
    actor = request.get('actor')
    return 'actor:%s' % (actor if isinstance(actor, str) else '')


def operation_hash(request, authority_configured=False, account_identity=False):
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
               'principal': principal_key(request, authority_configured, account_identity),
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
                                'count', 'state', 'lint'})

#: Exceptions the canonical effect layer raises to refuse a request (bad attachment,
#: task mismatch, malformed payload, unknown flag). They release the identity only
#: when the runner has not attempted a write; a timeout, ``OSError`` or programming
#: error never does, because it may follow a committed native write.
REFUSAL_EXCEPTIONS = (ValueError, KeyError, TypeError, IndexError, UnicodeError)


def _is_plain_dry_run(argv):
    """True only for an unambiguous ``--dry-run`` before any ``--`` terminator.

    Any ``--dry-run=VALUE`` spelling counts as a possible write, so a value such as
    ``--dry-run=false`` can never be mistaken for a read.
    """
    options = list(argv[1:])
    if '--' in options:
        options = options[:options.index('--')]
    if any(isinstance(a, str) and a.startswith('--dry-run=') for a in options):
        return False
    return '--dry-run' in options


def _help_flags(argv):
    """The value of every help flag of a bd invocation, in order, or None when a flag cannot be resolved.

    A bare ``--help`` or ``-h`` is True; ``--help=VALUE`` and ``-h=VALUE`` are the text of
    VALUE. A token that is the value of another flag, or stands after ``--``, is not a
    flag. The inventories are the kit's own tables of bd's flags; a flag none of them
    knows makes the answer None, because its value could be what looks like the help flag.
    """
    import reserved_comments as rc
    verb = argv[0]
    value_long = set(rc.BD_GLOBAL_VALUE_FLAGS)
    bool_long = set(rc.BD_GLOBAL_BOOL_FLAGS)
    if verb == 'dep':
        value_long |= rc.BD_DEP_VALUE_LONG_FLAGS
        bool_long |= rc._DEP_BOOL_FLAGS
        table = rc._short_flag_table('dep', rc._dep_subcommand(list(argv)))
    elif verb == 'comments':
        value_long |= rc.BD_COMMENT_ADD_VALUE_FLAGS
        bool_long |= rc.BD_COMMENT_ADD_BOOL_FLAGS
        table = rc._short_flag_table('comments', 'add')
    else:
        value_long |= rc.BD_LONG_VALUE_FLAGS.get(verb, set())
        bool_long |= rc.BD_LONG_BOOL_FLAGS.get(verb, set())
        table = rc._short_flag_table(verb)
    found, index = [], 1
    while index < len(argv):
        token = argv[index]
        index += 1
        if not isinstance(token, str):
            return None
        if token == '--':
            break
        if len(token) > 1 and token.startswith('--'):
            name, joined, value = token.partition('=')
            if name == '--help':
                found.append(value if joined else True)
            elif name in value_long:
                index += 0 if joined else 1
            elif name not in bool_long:
                return None
        elif len(token) > 1 and token.startswith('-'):
            letters, joined, value = token[1:].partition('=')
            for position, letter in enumerate(letters):
                kind = table.get(letter)
                if kind is None:
                    return None
                if kind == 'value':
                    if position == len(letters) - 1 and not joined:
                        index += 1                       # its value is the next token
                    break                                # else the rest of the token is its value
                if letter == 'h':
                    found.append(value if joined and position == len(letters) - 1 else True)
    return found


def _asks_for_help(argv):
    """Whether bd will print help for this invocation and carry nothing out.

    bd prints help only when the flag is on. ``--help=false`` (also ``--help=0``,
    ``-h=false``) is a flag bd accepts and then carries the command out (review of
    kittrial-5bb.97, revision 2: every such spelling of a write was taken for a read, so it
    was not stamped and a failure after it released the operation's identity). So: help only
    when the help flag is on, bare or with a value bd reads as true; given more than once,
    the last one decides, as in bd (``--help=false --help`` prints help, ``--help
    --help=false`` carries the command out). Anything else, and anything that cannot be
    resolved, is judged as if no help had been asked: a help request taken for a write is
    stamped needlessly; a write taken for help is the fault.
    """
    try:
        from reserved_comments import _parse_go_bool
        found = _help_flags(argv)
        # bd takes the last one where the flag is given more than once.
        return bool(found) and _parse_go_bool(found[-1]) is True
    except Exception:  # noqa: BLE001 - what cannot be scanned stays what it was
        return False


def _writes_rows(argv):
    """Whether the kit's table of writing commands says this invocation writes rows; unknown is a write."""
    try:
        from reserved_comments import write_targets
        return write_targets(list(argv), {}) is not None
    except Exception:  # noqa: BLE001
        return True


def _comments_writes(argv):
    """Whether a ``bd comments`` invocation adds a comment: its subcommand is ``add``.

    The subcommand is the first operand, not the second token: bd accepts flags between
    the two (``comments --json add ID text``, ``comments --help=false add ID text``), and
    a test of the token right after ``comments`` took each of those writes for a read
    (review of kittrial-5bb.97, revision 3; the same on main). The kit's own reader of a
    ``comments`` invocation resolves it, as ``dep`` is resolved; what it cannot resolve
    (a flag it does not know, which could hide the subcommand) is a write.
    """
    try:
        from reserved_comments import _comments_parts
        parts = _comments_parts(list(argv))
    except Exception:  # noqa: BLE001 - ambiguous: never mistaken for a read
        return True
    return parts is None or parts[0] == 'add'


def is_mutating_invocation(argv):
    """Whether one injected ``bin/bd`` argv can change canonical/native state."""
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return True
    verb = argv[0]
    if _asks_for_help(argv):
        # `bd VERB ... --help` prints the help of the verb and writes nothing (review of
        # kittrial-5bb.97: `create --help` was taken for a write and its answer stamped).
        return False
    if verb == 'dep':
        # `dep add`, `remove`, `relate`, `unrelate` and the form without a subcommand store or
        # remove a dependency; `list`, `tree` and `cycles` read. The kit's own table of the
        # rows a command writes decides (reserved_comments.write_targets): the whole verb was
        # listed as read-only here, older than that table.
        return _writes_rows(argv)
    if verb in READ_ONLY_BD_VERBS:
        return False
    if verb in ('create', 'update') and _is_plain_dry_run(argv):
        # A validation preflight (`create ... --dry-run`) writes nothing, so a refusal
        # from it is a clean pre-effect failure, not an uncertain write.
        return False
    if verb == 'comments':
        return _comments_writes(argv)
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

    __slots__ = ('_dispatch', 'attempted_write', 'calls', 'wrote')

    def __init__(self, dispatch):
        self._dispatch = dispatch
        self.attempted_write = False
        self.calls = 0
        #: Set by an effect that wrote something that is not a bd row (the kit's handoff
        #: journal): the answer is then the answer of a write, with the server's time
        #: (kittrial-5bb.97). It says nothing about refusals, which `attempted_write` decides.
        self.wrote = False

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
      (:data:`MAX_JOURNAL_BYTES`) and the advisory tombstone size
      (:data:`JOURNAL_TOMBSTONE_LIMIT`) are *reported* by :meth:`stats` — they never
      drive eviction and never block a write.
    * The only admission bound is the live-identity count (``limit``). A journal whose
      live identities genuinely cannot hold one more raises :class:`JournalFull`
      (``124``), which rolls back so the pre-existing store is untouched and no effect
      has run. Accumulated tombstones can never cause that refusal.
    * **No live identity is ever lost to a budget.** A tombstone is removed only when
      its confirmed-timeline age (``aged_from``, excluding credited jumps) exceeds
      ``tombstone_seconds``, and only while the clock is not suspect. The tombstone count never gates compaction.
    * **Trusted clock** (module docstring): ``meta`` holds ``high_water`` (largest raw
      clock seen by a write), ``suspect``, ``anchor`` and ``suspect_since``. A step of
      more than :data:`JOURNAL_MAX_SKEW_SECONDS` makes the store suspect for
      :data:`JOURNAL_SUSPECT_SETTLE_SECONDS` and is added to ``jump_credit``;
      :meth:`trusted_now` (``now``, or the anchor plus the time elapsed since the step,
      capped at ``max_skew``, while suspect) decides every replay/expiry question, rows
      are stamped with the raw clock, reclaim and tombstone deletion run only while not
      suspect, and tombstones age on the confirmed timeline (``aged_from``) so a jump
      never ages one out. :meth:`reset_high_water` is the operator recovery.
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
                 max_skew=JOURNAL_MAX_SKEW_SECONDS,
                 settle=JOURNAL_SUSPECT_SETTLE_SECONDS, legacy_path=None):
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
        self.settle = settle
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
        # Schema 6: the tombstone's reclaim time on the confirmed timeline
        # (``reclaimed_at - jump_credit`` at reclaim); tombstone ageing compares it.
        ('aged_from', 'REAL'),
    )

    def _stored_schema(self, connection):
        """The schema version this store was last upgraded to (``None`` if new)."""
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                              "AND name = 'meta'").fetchone() is None:
            return None
        value = self._meta_number(connection, 'schema')
        return value

    def _create_schema(self, connection):
        """Create or upgrade the schema inside the caller's open transaction.

        Runs only when the stored schema is older than :data:`JOURNAL_SCHEMA` (or the
        store is new), so an up-to-date store pays no DDL and no backfill scan on open.
        """
        connection.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value)')
        connection.execute('CREATE TABLE IF NOT EXISTS operations (%s)'
                           % ', '.join('%s %s' % column for column in self._COLUMNS))
        connection.execute('CREATE INDEX IF NOT EXISTS operations_state_at '
                           'ON operations (state, at)')
        self._ensure_columns(connection)
        # Schema 6: tombstone ageing filters ``state = 'expired' AND aged_from < ?``, so
        # the index leads with the state and is a range scan over stale tombstones only.
        # It replaces the schema-5 ``(state, reclaimed_at)`` and schema-4
        # ``(reclaimed_at)`` indexes.
        connection.execute('CREATE INDEX IF NOT EXISTS operations_state_aged '
                           'ON operations (state, aged_from)')
        connection.execute('DROP INDEX IF EXISTS operations_state_reclaimed')
        connection.execute('DROP INDEX IF EXISTS operations_tombstone_age')
        self._upgrade_clock(connection)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
            (JOURNAL_SCHEMA,))

    def _upgrade_clock(self, connection):
        """Initialise the schema-5 trusted-clock state of an older store.

        A schema-4 (or older) store persisted ``high_water`` only. Its suspicion is
        initialised from the gap since that mark: a store idle for more than
        ``max_skew`` starts suspect with ``anchor = high_water`` and
        ``suspect_since = now``, exactly as if this open were the first write after the
        gap, so the upgrade can neither freeze nor jump the trusted clock.
        """
        state = clock_state(connection)
        if connection.execute("SELECT 1 FROM meta WHERE key = 'jump_credit'").fetchone() \
                is None:
            # Schema 5 -> 6 (and older): no step has been credited yet.
            connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                               "('jump_credit', 0.0)")
        if connection.execute("SELECT 1 FROM meta WHERE key = 'suspect'").fetchone():
            return
        high = state['high_water']
        if high is None:
            return
        # Exactly the transition of a write observing ``now`` on a store that is not yet
        # suspect: suspect with ``anchor = high_water`` and ``suspect_since = now`` after a
        # gap of more than ``max_skew``, and ``high_water = max(high_water, now)``.
        state = clock_advance({'high_water': high, 'suspect': False}, time.time(),
                              self.max_skew, self.settle)
        # ``suspect`` is persisted (as 0 when clear), so the upgrade happens once. The
        # schema-6 credit starts at zero: an older store has credited no step.
        state['jump_credit'] = 0.0
        clock_persist(connection, state)

    def _ensure_columns(self, connection):
        """Add any revision-8 column an older store is missing, then backfill it.

        Called only from the schema-upgrade transaction, never on an ordinary open: the
        two backfill statements scan the table.
        """
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
        # Schema 6: an existing tombstone was reclaimed with no credited step, so its
        # confirmed-timeline reclaim time is its raw reclaim time.
        connection.execute(
            "UPDATE operations SET aged_from = COALESCE(reclaimed_at, at) "
            "WHERE state = 'expired' AND aged_from IS NULL")

    def _ensure_store(self):
        """Create or upgrade the schema and import a legacy document in ONE transaction.

        An up-to-date store (``meta['schema'] == JOURNAL_SCHEMA``) only reads the schema
        version, the ``legacy_migrated`` marker and the running totals, all ``meta``
        point reads. Otherwise the schema is created or upgraded (indexes, revision-8
        columns and their backfill, the schema-5 trusted-clock state) in the same
        transaction as the legacy import. The ``legacy_migrated`` marker is checked on
        every open, so a store left behind by a process that crashed between schema
        creation and the legacy import is completed on the next open: the marker is
        absent and the legacy document is still there. A crash at any point therefore
        leaves either the untouched previous store/document (this transaction rolled
        back) or a fully upgraded store.
        """
        if self._ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rename = False
        with self._transaction() as connection:
            stored = self._stored_schema(connection)
            if stored is None or stored < JOURNAL_SCHEMA:
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
        for column in ('replay_until', 'expires_at', 'aged_from'):
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
        # Rows carry the RAW clock (never the trusted mark): a future stamp left by a
        # later-corrected jump only lengthens that row's retention.
        at = float(record['at']) if isinstance(record.get('at'), (int, float)) \
            else time.time()
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
        aged_from = None
        if state == 'expired':
            # An imported tombstone keeps its recorded confirmed-timeline time, else its
            # raw reclaim time (the most conservative value: it credits every step since).
            aged_from = record.get('aged_from')
            aged_from = float(aged_from) if isinstance(aged_from, (int, float)) \
                else float(reclaimed)
            normalised['aged_from'] = aged_from
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
            'reclaimed_at, actor, route, replay_until, expires_at, aged_from) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (operation_id, state, normalised['request_hash'], normalised['principal'],
             normalised['at'], envelope, normalised.get('envelope_sha256'),
             1 if normalised.get('envelope_omitted') else 0, size,
             float(reclaimed) if reclaimed is not None else None,
             record.get('actor'), record.get('route'), replay_until, expires_at,
             aged_from))
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

    # -- trusted clock ---------------------------------------------------------
    def _observe(self, connection, now):
        """Advance and persist the trusted-clock state for one write transaction.

        Returns the new state. Every write transaction calls this exactly once with the
        raw clock, inside the same transaction as its row writes.
        """
        state = clock_advance(clock_state(connection), now, self.max_skew, self.settle)
        clock_persist(connection, state)
        return state

    def clock(self, now=None):
        """The trusted-clock state the next write would persist (read-only view)."""
        self._ensure_store()
        moment = time.time() if now is None else now
        with self._connection() as connection:
            return clock_advance(clock_state(connection), moment, self.max_skew, self.settle)

    def reset_high_water(self, now=None):
        """Operator recovery after a clock correction. Returns the new mark.

        Sets ``high_water`` to the current clock and clears suspicion (``jump_credit``
        is kept, never reduced), which both
        accepts a corrected clock immediately and re-arms jump detection after a
        corrected forward jump left ``high_water`` in the future. It removes no identity
        by itself: a subsequent ``reclaim_expired`` still compacts a closed window to a
        tombstone rather than dropping it.
        """
        self._ensure_store()
        moment = float(time.time() if now is None else now)
        with self._transaction() as connection:
            # The jump credit is never reduced: tombstones keep ageing on the confirmed
            # timeline across a reset.
            credit = clock_state(connection)['jump_credit']
            clock_persist(connection, {'high_water': moment, 'suspect': False,
                                       'anchor': None, 'suspect_since': None,
                                       'jump_credit': credit})
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

        The decision uses the trusted clock (:meth:`trusted_now`): while the store is
        (or the next write would make it) suspect, only the time elapsed since the step
        counts, from the anchor (``clamp(anchor + (now - suspect_since), anchor,
        anchor + max_skew)``). An unaccepted forward jump therefore cannot expire a
        receipt that is still inside its real window: during a +8 day jump a
        60-second-old committed receipt replays and a live uncertain reservation still
        reports ``124``.
        """
        if not isinstance(entry, dict):
            return False
        if entry.get('state') == 'expired':
            return True
        return entry.get('at', 0) + self.window(entry) <= self.trusted_now(now)

    def trusted_now(self, now=None, connection=None):
        """The trusted clock for expiry and replay-window decisions.

        ``now`` when not suspect, ``clamp(anchor + (now - suspect_since), anchor,
        anchor + max_skew)`` while suspect, evaluated against the transition the next
        write would make (one ``meta`` read,
        not a table scan). Rows are never stamped with it; they carry the raw clock.
        """
        moment = time.time() if now is None else now
        if connection is not None:
            state = clock_state(connection)
        else:
            self._ensure_store()
            with self._connection() as opened:
                state = clock_state(opened)
        return clock_trusted(clock_advance(state, moment, self.max_skew, self.settle),
                             moment, self.max_skew)

    # -- compaction / bounds ---------------------------------------------------
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

    def _tombstone(self, connection, operation_id, now, credit=0.0):
        """Compact one CLOSED-WINDOW record to a tombstone stamped with raw ``now``.

        ``aged_from = now - credit`` places the tombstone on the confirmed timeline
        (``credit`` is the store's ``jump_credit``), so its ageing never consumes a
        suspect forward step.

        The caller only passes records whose own retention window has genuinely closed
        (see :meth:`_reclaim`). There is no count gate: the tombstone count is reported
        by :meth:`stats` but never stops compaction, so closed receipts never linger as
        live identities that count toward :data:`JOURNAL_LIMIT`.
        """
        row = connection.execute('SELECT * FROM operations WHERE operation_id = ?',
                                 (operation_id,)).fetchone()
        if row is None or row['state'] == 'expired':
            return False
        aged_from = float(now) - float(credit)
        record = {'state': 'expired', 'request_hash': row['request_hash'],
                  'principal': row['principal'], 'at': row['at'], 'reclaimed_at': now,
                  'actor': row['actor'], 'route': row['route'], 'aged_from': aged_from}
        size = self._record_bytes(operation_id, record)
        window = self.window({'state': 'expired'})
        expires_at = float(now) + self.tombstone_seconds
        connection.execute(
            "UPDATE operations SET state = 'expired', envelope = NULL, "
            'envelope_omitted = 0, bytes = ?, reclaimed_at = ?, replay_until = ?, '
            'expires_at = ?, aged_from = ? WHERE operation_id = ?',
            (size, now, float(row['at']) + window, expires_at, aged_from, operation_id))
        delta = size - int(row['bytes'])
        self._adjust(connection, live=-1, tombstones=1, bytes_=delta)
        return True

    def _reclaim(self, connection, now, clock):
        """Compact every CLOSED-WINDOW identity to a tombstone. Returns the count.

        Runs only while the store is not suspect, and then against the raw clock ``now``.
        A committed receipt is a candidate only once its own
        :data:`JOURNAL_COMMITTED_RETENTION_SECONDS` window has closed; an in-window
        receipt is never a candidate, whatever the byte or tombstone count is.
        """
        if clock.get('suspect'):
            return 0
        # Compare the bare indexed column against a precomputed threshold (``at <= ?``,
        # not ``at + ? <= ?``) so the (state, at) index is an index range scan rather
        # than a full-table scan on every write.
        committed_before = now - self.committed_retention
        active_before = now - self.retention
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
            for row in rows:
                if self._tombstone(connection, row['operation_id'], now,
                                   clock.get('jump_credit') or 0.0):
                    compacted += 1
        return compacted

    def _drop_stale_tombstones(self, connection, now, clock):
        """Remove only tombstones older than the horizon on the CONFIRMED timeline.

        A tombstone is deleted only when ``aged_from < now - jump_credit - horizon``:
        its age excluding every suspect forward step credited since it was reclaimed.
        An accepted forward jump therefore extends retention by the jump length rather
        than ageing tombstones out, so no identity inside the client retry horizon
        (:data:`JOURNAL_RETRY_HORIZON_SECONDS`) is deleted by a credited jump; uncredited
        steps of 24 h or less are bounded by the retry contract. It also
        runs only while the store is not suspect, and is never used to satisfy the byte
        or count budget. This is the *only* place an identity row is deleted by policy.
        """
        if clock.get('suspect'):
            return 0
        # ``state = 'expired' AND aged_from < ?`` is a range scan over the stale
        # tombstones only, on the schema-6 (state, aged_from) index.
        stale_before = now - float(clock.get('jump_credit') or 0.0) - self.tombstone_seconds
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM operations "
            "WHERE state = 'expired' AND aged_from < ?", (stale_before,)).fetchone()
        removed, size = int(row[0]), int(row[1])
        if not removed:
            return 0
        connection.execute(
            "DELETE FROM operations WHERE state = 'expired' AND aged_from < ?",
            (stale_before,))
        self._adjust(connection, tombstones=-removed, bytes_=-size)
        return removed

    def _admit(self, connection, now, clock, operation_id, record):
        """Maintenance, then write one row unless the live-identity bound refuses it.

        Reclaim and tombstone ageing (both no-ops while suspect) run first and stay
        committed with the clock observation even when the row is refused, so a full
        journal still makes progress toward settling and reclaiming. The row itself is
        written under a savepoint: when the live identities would exceed ``limit`` it is
        rolled back and the capacity message is returned (the caller raises
        :class:`JournalFull` after the commit). There is no eviction path for a live
        identity here.
        """
        self._reclaim(connection, now, clock)
        self._drop_stale_tombstones(connection, now, clock)
        connection.execute('SAVEPOINT admit')
        self._upsert(connection, operation_id, record)
        refused = None
        if self._over(connection):
            connection.execute('ROLLBACK TO admit')
            refused = self._capacity_message(connection)
        connection.execute('RELEASE admit')
        return refused

    def lookup(self, operation_id):
        """One indexed point lookup: the live record or its tombstone, else ``None``."""
        self._ensure_store()
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM operations WHERE operation_id = ?',
                                     (operation_id,)).fetchone()
        return self._row_record(row) if row is not None else None

    def pending_owner_creates(self, principal):
        """Bounded candidates for the owner adapter's empty-create recovery read.

        This is only discovery. The adapter must validate the inner receipt and
        prove native absence under its project and authority locks before release.
        """
        self._ensure_store()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM operations WHERE principal = ? AND route = 'requirements.create' "
                "AND state IN ('unknown', 'in_progress') ORDER BY operation_id LIMIT 21",
                (principal,)).fetchall()
        return [dict(self._row_record(row), operation_id=row['operation_id']) for row in rows]

    # -- mutations -------------------------------------------------------------
    def reserve(self, operation_id, request_hash, principal):
        """Reserve a new identity, compacting closed receipt windows first.

        Raises :class:`JournalFull` only when the live identities plus this new one
        genuinely exceed the live-identity bound. No live identity is ever evicted to
        make room, so the only thing that can refuse is a journal that is really full.
        The identity row is rolled back (only the clock observation and time-based
        maintenance commit), so no pre-existing identity changes and no effect has run.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError('operation_id must be a non-empty string')
        self._ensure_store()
        with self._transaction() as connection:
            now = time.time()
            clock = self._observe(connection, now)
            refused = self._admit(connection, now, clock, operation_id, {
                'state': 'in_progress', 'request_hash': request_hash,
                'principal': principal, 'at': now,
                'actor': self._actor, 'route': self._route})
        if refused:
            raise JournalFull(refused)

    def complete(self, operation_id, envelope, request_hash, principal):
        """Record the committed response envelope for an identity (one transaction)."""
        self._ensure_store()
        with self._transaction() as connection:
            now = time.time()
            clock = self._observe(connection, now)
            stored, digest, omitted = self._bounded(envelope)
            record = {'state': 'committed', 'request_hash': request_hash,
                      'principal': principal, 'at': now, 'envelope': stored,
                      'envelope_sha256': digest,
                      'actor': self._actor, 'route': self._route}
            if omitted:
                record['envelope_omitted'] = True
            refused = self._admit(connection, now, clock, operation_id, record)
        if refused:
            raise JournalFull(refused)

    #: The directed actor label and canonical route of the guarded mutation. The
    #: journal created by :func:`run_guarded` carries them so every row records which
    #: actor and route the identity was directed at, not only the principal digest.
    _actor = None
    _route = None

    def mark_unknown(self, operation_id):
        """Keep a reservation whose outcome is not known to be pre-effect."""
        self._ensure_store()
        with self._transaction() as connection:
            now = time.time()
            self._observe(connection, now)
            row = connection.execute('SELECT * FROM operations WHERE operation_id = ?',
                                     (operation_id,)).fetchone()
            if row is None or row['state'] == 'expired':
                return
            record = self._row_record(row)
            record['state'] = 'unknown'
            record['at'] = now
            self._upsert(connection, operation_id, record)

    def discard(self, operation_id):
        """Release a reservation that is proven pre-effect."""
        self._ensure_store()
        with self._transaction() as connection:
            self._observe(connection, time.time())
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

    def reclaim_expired(self, now=None):
        """Compact every closed-window identity to a tombstone. Returns the count.

        This is the automatic maintenance step on every write and the operator action
        for a deployment that wants to compact the journal before the limit is reached.
        It is a write transaction, so it observes the clock like any write, and it does
        nothing while the store is suspect. Only a record whose OWN window has closed is
        a candidate; unlike :meth:`prune` it never lets an exact retry repeat: the
        tombstone keeps refusing the reclaimed identity as expired.
        """
        self._ensure_store()
        moment = time.time() if now is None else now
        with self._transaction() as connection:
            clock = self._observe(connection, moment)
            removed = self._reclaim(connection, moment, clock)
            self._drop_stale_tombstones(connection, moment, clock)
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
        against the configured bounds. The persisted trusted-clock state is reported as
        ``high_water``, ``suspect``, ``anchor`` and ``suspect_since``
        (``clock_skewed`` = ``suspect``); ``trusted_now`` is the clock the next decision
        would use. ``journal_mode`` reports the SQLite mode (``wal``).
        """
        self._ensure_store()
        moment = time.time() if now is None else now
        with self._connection() as connection:
            clock = clock_state(connection)
            view = clock_advance(clock, moment, self.max_skew, self.settle)
            trusted = clock_trusted(view, moment, self.max_skew)
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
                if row['at'] + self.window({'state': row['state']}) <= trusted:
                    expired += 1
            live = states['in_progress'] + states['committed'] + states['unknown']
            self._ensure_totals(connection)
            running = self._totals(connection)
            totals_bytes = live_bytes + tombstone_bytes
            recount = {'total': live, 'entries': live, 'tombstones': tombstones,
                       'bytes': totals_bytes}
            mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
        report = {'total': live, 'limit': self.limit,
                  'retention': self.retention,
                  'committed_retention': self.committed_retention,
                  'tombstone_seconds': self.tombstone_seconds,
                  'expired': expired, 'reclaimable': 0 if view['suspect'] else expired,
                  'states': states, 'tombstones': tombstones,
                  'tombstone_limit': self.tombstone_limit,
                  'max_skew': self.max_skew, 'settle_seconds': self.settle,
                  'trusted_now': trusted,
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
        # The clock fields describe the state as of ``now`` (what the next write would
        # persist and what ``trusted_now`` uses); ``clock_persisted`` is the stored row.
        report.update(clock_report(view))
        report['clock_persisted'] = clock_report(clock)
        return report


def _envelope(code, stderr='', **extra):
    payload = {'returncode': code, 'stdout': '', 'stderr': stderr}
    payload.update(extra)
    return payload


def server_time(now=None):
    """The server's clock for a write answer: UTC with its offset, whole seconds (kittrial-5bb.97)."""
    import datetime
    moment = datetime.datetime.now(datetime.timezone.utc) if now is None else \
        datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    return moment.replace(microsecond=0).isoformat()


def stamp_write(envelope):
    """Put ``server_time`` on the answer of a write that was carried out; the same answer is returned.

    Only a successful answer (return code 0) that has none yet: a refusal, a busy and an
    uncertain answer say nothing about when something was written, and an answer that is
    stamped keeps its time (a replayed write returns the time it was carried out).
    """
    if isinstance(envelope, dict) and envelope.get('returncode') in (None, 0) and 'server_time' not in envelope:
        envelope['server_time'] = server_time()
    return envelope


#: The ids the HTTP service allocates (http_auth: ``usr_``/``agent_`` + 16 hex). As a
#: declared actor the shape is reserved for that service (``reserved_comments.HTTP_ACTOR``).
HTTP_ACTOR_ID = re.compile(r'(?:usr|agent)_[0-9a-f]{16}')


def http_actor_id(actor):
    """The account or agent id an actor label claims (``usr_...`` or ``usr_.../label``), or None."""
    head = actor.split('/', 1)[0] if isinstance(actor, str) else None
    return head if head and HTTP_ACTOR_ID.fullmatch(head) else None


def http_shaped_names(names):
    """The names in a list (an operator allowlist) that have the exact reserved shape of an
    HTTP account or agent id. Only the exact shape is reserved: `usr_` or `agent_` and 16
    lowercase hex digits. A near miss (15 digits, `USR_`) is an ordinary name."""
    return [name for name in names or [] if isinstance(name, str) and http_actor_id(name) is not None]


def http_actor_denial(request, authority_config):
    """Why a request under an HTTP-shaped actor is refused, as the envelope, or None.

    An actor with the shape of an account or agent id is what makes a record read as
    written under HTTP authority, so EVERY endpoint action under one needs the verified
    live-authority descriptor (kittrial-5bb.70 review 01a10262), whether or not the
    process was launched with ``--require-authority``: the descriptor must be present,
    must pass ``decide`` against the live store, and must name that actor (the account
    itself, or the agent whose credential the descriptor carries). The denials are the
    ones ``run_guarded`` returns, so the HTTP service maps them identically; a guarded
    mutation is then re-validated under the authority lock as before.
    """
    head = http_actor_id(request.get('actor'))
    if head is None:
        return None
    if authority_config is None:
        return _envelope(126, stderr='Live authority is not configured\n', authority_status=401)
    authority = request.get('authority')
    if not isinstance(authority, dict):
        return _envelope(126, stderr='Live authority descriptor required: an HTTP account or agent actor acts '
                                     'only with one\n', authority_status=401)
    descriptor = {key: value for key, value in authority.items() if key not in ('store', 'lock')}
    try:
        state = read_state(authority_config.store)
        decide(state, descriptor)
    except AuthorityDenied as denied:
        return _envelope(126, stderr='%s\n' % denied.message, authority_status=denied.status)
    allowed = {descriptor.get('user_id')}
    credential = (state.get('credentials') or {}).get(descriptor.get('credential_id')) \
        if descriptor.get('credential_id') else None
    if isinstance(credential, dict) and credential.get('agent_id'):
        allowed.add(credential['agent_id'])
    if head not in allowed:
        return _envelope(126, stderr='The actor is not the account or agent the live-authority descriptor '
                                     'names\n', authority_status=403)
    return None


def descriptor_actor_denial(request, authority_config, reserved):
    """Why a request the web service sent under a name that is NOT a web id is refused, or None.

    ``http_actor_denial`` judges an actor with the shape of an account or agent id. Every
    other name went through unjudged, so the web service could be made to write under a
    host actor's name: by a member, with an ``actor`` in the body of a task write
    (kittrial-5bb.181, closed in the routes), and by a project owner, with a worker
    credential ISSUED under that name (kittrial-5bb.184). The tracker's rows carry the name
    and nothing else, and "is the caller the assignee, is it the author" is asked of the
    name, so such a credential was that actor.

    Launched by the service, with the live-authority descriptor, a name without the shape
    of a web id is written only

    * by a worker credential (the one the descriptor names, read from the live store),
    * inside that credential's own namespace (``NAME`` or ``NAME/...``),
    * when the namespace is nobody else's (``actor_names.collision``): not a session actor
      of the project or a name of that shape, not a name on the operator or verifier
      list, not the web service's own namespace -- the one it was launched with, which
      ``AuthorityConfig.service_namespace`` carries (kittrial-5bb.188 item 3), not only
      ``http`` -- and not a name that reads as another (a leading or trailing dot or
      dash, an ``@host``).

    A credential issued before kittrial-5bb.188 carries no ``actor_rows_checked`` mark, so
    it is judged again by the project's own tracker rows (one bd export): a name the
    tracker was already holding before the credential existed is somebody else's, and the
    write is refused. Rows inside the lifetime of an earlier credential of the SAME name,
    issued by the same owner for the same project and not itself refused by the row rule,
    are not held against it (kittrial-5bb.188 item 3); a credential the kit has since
    judged (``actor_rows_checked`` or ``actor_rows_refused``) or a superuser waived
    (``actor_waived``) carries a settled outcome and is not read against the tracker again
    (items 4 and 5). An export that yields no rows at all is a host fault answered
    ``fault: "tracker"`` (item 1), never "the tracker holds nothing".

    ``reserved`` is called only when a namespace has to be judged and returns the host's
    names (``sessions``, ``operators``, ``verifiers``, and ``authors`` from the tracker
    when asked with ``rows=`` and the earlier credentials' lifetimes with ``own=``). A
    request without a descriptor is not judged here: on the SSH path there is none, and the
    service's own reads carry none; a write the service sends without one is refused by
    ``run_guarded``.
    """
    if authority_config is None:
        return None
    authority = request.get('authority')
    actor = request.get('actor')
    if not isinstance(authority, dict) or http_actor_id(actor) is not None:
        return None
    if isinstance(actor, str) and actor and actor == authority.get('user_id'):
        return None                                  # the account itself, whatever its id looks like
    import actor_names
    try:
        state = read_state(authority_config.store)
    except AuthorityDenied as denied:
        return _envelope(126, stderr='%s\n' % denied.message, authority_status=denied.status)
    credential = (state.get('credentials') or {}).get(authority.get('credential_id')) \
        if authority.get('via') == 'credential' and authority.get('credential_id') else None
    if not isinstance(credential, dict) or credential.get('agent_id'):
        return _envelope(126, stderr='The actor is not the account or agent the live-authority descriptor '
                                     'names\n', authority_status=403)
    namespace = credential.get('actor')
    if not actor_names.inside(actor, namespace):
        return _envelope(126, stderr='The actor is outside the namespace of the credential the live-authority '
                                     'descriptor names\n', authority_status=403)
    service = getattr(authority_config, 'service_namespace', actor_names.SERVICE_NAMESPACE)
    service = (service, actor_names.SERVICE_NAMESPACE)
    reason = actor_names.collision(namespace, service=service, **reserved())
    if reason is None and credential.get('actor_rows_refused'):
        # Judged by the rows at an earlier write and refused then: refuse again without a
        # read (kittrial-5bb.188 item 5). The stored reason is the kit's own rule word.
        stored = credential.get('actor_rows_refused')
        reason = stored if isinstance(stored, str) and stored else actor_names.ROWS
    elif reason is None and not (credential.get('actor_rows_checked') or credential.get('actor_waived')):
        # Issued before the row rule, or never judged: the project's own rows decide
        # (kittrial-5bb.188 items 1 and 5). Rows inside an earlier same-name credential's own
        # lifetime that the row rule did not refuse are not held against this one (item 3);
        # a superuser's waiver (item 4) is its own settled mark and skips this.
        issued = credential.get('created_at')
        own = actor_names.own_intervals(state.get('credentials') or {}, namespace,
                                        credential.get('user_id'), credential.get('project_id'),
                                        exclude=authority.get('credential_id'))
        try:
            reason = actor_names.collision(namespace, service=service,
                                           **reserved(rows=issued if isinstance(issued, str) and issued else True,
                                                      own=own))
        except actor_names.TrackerRowsUnreadable as rows_unreadable:
            # The export was read but holds a row that cannot be parsed: its own mark and its
            # own sentence naming the row ids and the operator repair, never "try again
            # shortly" (kittrial-5bb.243 item N7). This arm is BEFORE the bare fault's
            # because the exception subclasses it. The ids ride as a bounded, checked field
            # for the service, never as a stderr tail (r2 review item 1).
            return _envelope(2, stderr='%s\n' % rows_unreadable, fault='unreadable-rows',
                             unreadable_rows=list(rows_unreadable.ids or ()),
                             unreadable_total=rows_unreadable.total)
        except actor_names.TrackerUnreadable as unreadable:
            # Not a refusal of the request: the tracker could not be read (item 1).
            return _envelope(2, stderr='%s\n' % unreadable, fault='tracker')
        except actor_names.TrackerMergeSlotMissing as missing:
            # The tracker was read but carries no merge slot (rows without it, or no rows at
            # all): its own mark, so the service says what to do (merge-create) instead of
            # "try again shortly" (kittrial-5bb.202 item 1; rev-3 item 3(c)).
            return _envelope(2, stderr='%s\n' % missing, fault='merge-slot')
    if reason is not None:
        return _envelope(126, stderr='%s\n' % actor_names.refusal(namespace, reason), authority_status=403)
    return None


def run_guarded(request, journal_path, effect, authority_config=None,
                require_authority=False, runner=None, journal_options=None, account_identity=False,
                identity_check=None):
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
    * ``account_identity`` is a server adapter option, never a request field. The
      owner requirements adapter uses it after checking a human owner session so
      a new login can recover the same account's request. Live session authority
      is still checked here, under the lock, before even a committed replay.
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
        if identity_check is not None:
            try:
                identity_check()
            except ValueError as denied:
                return _envelope(2, stderr=str(denied) + '\n')
        operation_id = request.get('operation_id')
        journal = (OperationJournal(journal_path, **(journal_options or {}))
                   if operation_id else None)
        # The principal half of the identity comes from the request descriptor only
        # when this launch actually has a trusted authority store.
        principal = principal_key(request, trusted, account_identity)
        if journal is not None:
            # The directed actor label and canonical route are recorded with the row, so
            # the journal reports what an identity was aimed at, not only its principal.
            actor = request.get('actor')
            route = request.get('route')
            journal._actor = actor if isinstance(actor, str) and actor else None
            journal._route = route if isinstance(route, str) and route else None
            request_hash = operation_hash(request, trusted, account_identity)
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
                    # The stored answer, marked as one: a caller that would otherwise put its
                    # own clock on a write answer must not do so for an answer that was
                    # stored without a time (by a kit from before the time was kept).
                    stored = entry.get('envelope')
                    return dict(stored, replayed=True) if isinstance(stored, dict) else stored
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
        # The time of the write, on the answer itself and so in what the journal keeps: the
        # same request sent again is answered with the time the write was carried out. A
        # guarded action that only read (its runner attempted no write) carries none.
        if runner is None or runner.attempted_write or runner.wrote:
            stamp_write(envelope)
        if journal is not None and isinstance(envelope, dict):
            code = envelope.get('returncode')
            if code in (None, 0):
                try:
                    journal.complete(operation_id, envelope, operation_hash(request, trusted, account_identity),
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
