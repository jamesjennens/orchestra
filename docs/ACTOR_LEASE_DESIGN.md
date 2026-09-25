# Actor lease for single-process coordination

**Status: proposal for review; no owner decision or implementation is implied.**

This note proposes a server-side lease that prevents concurrent processes
acting as the same coordination actor from interleaving writes. It is a
coordination primitive, not a replacement for project authorization,
authentication, task claims, or repository permissions. The existing merge
slot is the closest workflow precedent: it has explicit acquire/check/release
operations, visible holder context, and no silent takeover. An actor lease is
different in scope: it protects one actor's writes rather than serializing
project integration.

No source behavior is claimed to have been reproduced or validated by this
design. The examples and failure cases below are generic. The related HTTP
session work (.19) and personal-agent work (.22) are considered only as
interface and identity boundaries; this proposal does not assume either is
accepted or complete.

## Goals and non-goals

The goal is to make a project's canonical coordination service the arbiter of
whether one actor may write at a time. A process acquires a lease with an
explicit holder identity and purpose, renews it while active, and releases it
when finished. Mutating commands are checked at the server boundary, and
current lease state is visible in brief/work views.

The lease must make concurrent acquisition atomic, reject writes from an
expired or superseded holder, and make uncertainty visible. A lost response
must not lead a client to assume that an acquire, renewal, release, or write
failed. Lease expiry is for recovery from abandoned holders, not proof that a
process stopped.

The design does not introduce roles, role hierarchy, new project membership,
strong multi-tenant isolation, or authenticated identity where none currently
exists. An actor name alone remains attribution unless the active transport
already binds it to an authenticated principal. A lease must not grant project
access to a caller who otherwise lacks it.

## Lease scope alternatives

| Scope | Semantics | Assessment |
| --- | --- | --- |
| Project + actor | One active holder for a canonical actor within one project | Recommended starting point. Directly addresses two processes reusing one actor and does not require a new authorization model. |
| Project + role | One active holder for a named role, shared by all actors assigned to it | Defer. The kit does not define a role hierarchy or stable role authorization. Role scope could block unrelated actors and must not infer permissions from labels or prose. |
| Project + owner or agent identity | One holder for a human owner or one registered personal agent | Possible future policy, not equivalent to actor scope. It changes which independent identities contend and needs an explicit principal mapping. |

The recommended key is `(project_id, actor_id)`. This guarantees exclusion
only when processes use the same canonical actor ID. It does not prevent one
person from bypassing exclusion by starting a process with another actor ID.
Choosing owner-wide exclusion instead is an unresolved product decision, not
an implicit behavior.

## Canonical state and record

The coordination service is the sole authority for active lease state. Clients
may cache their own lease handle for retries, but a local file, client clock,
generated Markdown view, HTTP session, or Git branch cannot establish that a
lease is active. Reads and mutations resolve against canonical state.

A lease record needs, at minimum:

| Field | Purpose |
| --- | --- |
| Project and actor key | Defines the exclusion scope |
| Lease ID and fencing generation | Distinguishes grants and rejects stale holders |
| Holder ID | Identifies one process/session, not just a reusable actor name |
| Purpose | Human-readable reason for holding the lease |
| Issued and expiry instants | Server-recorded lifetime and display |
| Last operation ID | Supports exact retry reconciliation |
| State | Active, expired, released, or superseded |

An append-only event trail should retain grants, renewals, releases, expiry
observations, forced takeovers, and administrative policy changes. The current
record is a projection of that history, not a replacement for it. Lease
handles must not be printed in shared views or stored in source-controlled
configuration.

The record and every protected write must use one canonical serialization and
durability boundary. The current merge-slot implementation is a workflow
pattern, not proof that every existing storage backend can atomically guard
arbitrary writes. If the backend cannot perform the lease check and protected
mutation as one serialized operation (or equivalent fenced transaction), it
must not claim to provide the exclusion guarantee.

## Acquire, renew, release

1. **Acquire.** The caller supplies the actor, unique holder ID, purpose,
   requested TTL, and an operation ID. The server validates the request and
   any existing project authorization. In one atomic operation, it grants a
   lease only if no unexpired lease exists for the key, or the previous lease
   is expired according to canonical server time. Exactly one concurrent
   contender wins. A grant assigns a fresh lease ID and strictly increasing
   fencing generation.
2. **Renew.** The caller supplies the exact lease ID, holder ID, generation,
   and a new operation ID. Renewal succeeds only while that lease is still
   active and current. It extends the server-calculated expiry within
   server-enforced TTL bounds; it cannot revive an expired or superseded
   lease. Renewal never changes the holder or purpose.
3. **Release.** Release is conditional on the exact current lease ID, holder,
   and generation. Releasing an already released lease is idempotent only for
   the exact original operation. A different holder cannot clear it. A
   missing response is resolved by reading the operation's canonical outcome,
   not by issuing a new release with changed content.

The server chooses and validates the effective TTL; client-supplied time
values are ignored. The allowed minimum, maximum, and default TTL remain
owner/operator decisions. Reacquiring with the same holder does not silently
extend an existing lease: clients use the explicit renew operation so a
retry cannot mask a stale process or an accidental second execution.

Lease operations use idempotency keys. A retry with the same actor, operation
ID, and canonical payload returns the recorded result. Reuse of an operation
ID with different content is rejected. If the response is uncertain, the
client reads the operation and lease state before attempting another
mutation.

## Write-command enforcement and fencing

Enforcement belongs at the shared server-side mutation boundary, after
transport authentication/authorization and before effects. It must cover all
commands that mutate project coordination state, regardless of whether they
arrive over SSH, a local endpoint, or HTTP. Read-only commands remain
available without holding a lease. Acquire, renew, release, and an explicitly
authorized takeover are lease-control operations with their own validation
and audit path.

In an enforcement-enabled project, every ordinary write made on behalf of an
actor must present the current lease ID, holder, and fencing generation for
that actor. The server rejects a missing, expired, released, or superseded
lease with a clear conflict response that identifies the current holder and
expiry where visible. A generic actor string is not a lease credential.
Lease enforcement does not authenticate the actor; the existing access
controls remain responsible for deciding who may use an actor or perform a
takeover.

The lease check cannot be a preliminary check followed by an unrelated write:
a takeover could occur between those two operations. The lease validation and
the protected mutation must share a transaction/lock, or the mutation path
must compare the fencing generation at its final commit point. Every newer
grant or takeover increments the generation. A late command from an older
holder is then rejected even if that process is still running.

If canonical lease state is unavailable, malformed, or cannot be read
consistently with the mutation, protected writes fail closed. The server
returns a visible error; it must not silently proceed without checking or
fall back to a local lock. Reads may report lease state as unavailable rather
than presenting an unverified lease as active or absent.

For migration, enforcement should be explicitly disabled until compatible
clients and the server-side guard are ready. A project can first expose
read-only lease status, then enable strict enforcement with a documented
cutover. Once enabled, an old client that omits a lease is refused rather
than bypassing the policy. The exact configuration mechanism, opt-in policy,
and transition behavior require review. Turning enforcement off removes the
guarantee and should itself be an authorized, audited policy change.

## Expiry, crashes, clocks, and takeover

**Crash or lost client.** A process that exits unexpectedly cannot be relied
upon to release its lease. Its lease remains active until release or expiry.
The client should renew while it is genuinely active and release on orderly
exit, but those are availability improvements, not safety assumptions.
Another holder may acquire after canonical expiry. Expiry alone does not
terminate the old process; the fencing check prevents its later writes.

**Stale holder.** A holder that is slow, paused, partitioned, or stuck is
indistinguishable from a dead process based only on missed renewals. Do not
infer consent or inactivity from elapsed time. Before expiry, normal acquire
fails with the active holder's visible metadata. After expiry, a new acquire
can proceed under the regular atomic path, increments the generation, and
fences old writes.

**Clock skew.** Clients do not calculate validity and their wall clocks do
not affect the decision. The canonical server/storage authority supplies
timestamps. Persisted expiry requires a wall-clock representation so it
survives service restart; monotonic time is useful only within a running
authority and cannot by itself recover a deadline after restart. The storage
adapter should provide a single serialized time source and prevent its
effective time from moving backwards. A detected material forward/backward
clock discontinuity must suspend lease grants and protected writes until the
authority is reconciled; it must not silently expire every holder or extend
leases. The acceptable skew/discontinuity threshold and recovery procedure
remain operational decisions.

**Forced takeover.** An active lease may be displaced only through a distinct,
explicit takeover operation, never because an operator waited “long enough”
or because a client reports the holder missing. It requires authorization
under the project's existing controls, a human-readable reason, and a durable
approval/evidence pointer where project policy requires one. The event records
the old/new holder, actor, lease ID and generation, request/operation ID,
server time, reason, and authorization evidence. The operation atomically
supersedes the old lease, increments the fencing generation, and grants the
new holder. Retrying the same operation returns its result; conflicting
retries fail. The exact actor(s) allowed to invoke takeover cannot be
invented by this design: if current project controls cannot establish that
authority, takeover must remain an operator-mediated action outside the
contributor endpoint.

## Visibility and user experience

`brief` and `work` should show the relevant current actor lease state or state
that no lease is held. When held, show holder ID, purpose, issued/expiry time,
server-calculated time remaining, and whether the caller is the holder. Also
show unavailable or stale-observation states explicitly. A listing should
allow a coordinator to discover active leases without querying each task.
Views reuse existing project read authorization and must not disclose a
secret lease handle, local working-directory path, or new identity data.

Acquisition refusal should be actionable: identify the current holder and
expiry, suggest a status read or coordination with that holder, and distinguish
an active lease from unavailable canonical state. Status output is advisory;
only the atomic write check determines whether a mutation is allowed. As with
the merge slot, a displayed holder does not create authority to release or
take over someone else's lease.

## HTTP sessions, personal agents, and SSH

The HTTP-session boundary (.19) and the lease boundary solve different
problems. A valid HTTP session may authorize a request but does not prove that
the caller is the only process using an actor. The HTTP adapter must derive the
effective actor from its existing authenticated principal binding and invoke
the same canonical lease guard as other transports. Session expiry/revocation
must not silently release a lease; the lease expires or is explicitly
released/taken over. Any backend that is only single-process must state that
limit; multi-process or multi-host use requires transactional lease and write
coordination rather than assuming an in-process lock is shared.

The personal-agent boundary (.22) proposes owner-bound agent identities whose
access cannot exceed their owner's existing project authority. A registered
agent may have a distinct actor ID and therefore a distinct actor lease. If
the intended guarantee is one active process across all agents of an owner,
that is a different owner-keyed policy and must be decided explicitly. A
lease must not reveal a private working-directory hint or make an agent a
project role. Disabling an owner/agent or revoking a credential should prevent
future authorized writes under existing authorization rules; it does not
rewrite the history of already granted leases.

SSH remains a supported transport. It must reach the same server-side command
guard and keep its existing project/actor selection explicit. Lease enforcement
cannot be HTTP-only, nor may old SSH clients bypass it after enforcement is
enabled. During migration, unmodified clients continue to work only while
enforcement is disabled; after cutover, clients must acquire and attach a
lease for protected writes. Read access and transport choice remain separate
from lease ownership.

## Durability, backup, and recovery

Lease state and its audit history are mutable canonical coordination data, not
feature-branch files or generated views. Any backup/restore design must
preserve the event history and operation receipts consistently with the
authoritative store. A restore must not make an old active lease valid in two
live deployments: restored active state should be marked for reconciliation
or invalidated with an auditable recovery event before writes resume. Restored
copies must not be operated as a second authority. The lease/token format,
retention policy, and backup-sidecar placement are implementation decisions
that must fit existing operations and recovery procedures.

## Test strategy and rollout

Implementation should use disposable fixtures and prove at least:

- Concurrent acquisition for one key yields exactly one active holder; a
  different actor/project remains independent.
- Wrong holder, actor, lease ID, generation, or operation payload is refused;
  exact uncertain retries reconcile without duplicate events.
- Expired/released/superseded holders cannot write, including a delayed write
  racing with acquisition or forced takeover.
- Renewal cannot revive expiry; release cannot clear another holder; TTL
  bounds are enforced server-side.
- A crash/restart, unavailable store, malformed state, and simulated clock
  rollback/forward discontinuity fail safely and recover explicitly.
- Forced takeover requires authorization and reason/evidence, increments the
  generation, and leaves a queryable audit event.
- `brief`/`work` show active, absent, expired, and unavailable states without
  exposing lease credentials or private agent paths.
- SSH, local, and HTTP mutation paths all enforce the same policy; HTTP
  principal binding and agent owner caps continue to apply.
- Backup/restore does not resurrect two active authorities, and rollback of
  enforcement is explicit and auditable.

Roll out only after selecting the storage transaction boundary, updating all
supported client paths, adding disposable concurrency/failure tests, and
reviewing migration and operator recovery. Start with status visibility and
compatibility checks; enable enforcement only by an explicit project decision.
Rollback may disable enforcement only through an authorized, audited action;
it cannot be represented as a safe rollback if a second writer may already
have acted.

## Decisions still required

1. Is the intended exclusion boundary one actor, one owner, or another
   explicitly defined principal; should the initial key remain project + actor?
2. Which existing server operation/storage layer can atomically couple the
   lease check and every protected mutation, including across service restart?
3. What TTL bounds, renewal cadence, clock-discontinuity threshold, and
   operator recovery procedure fit actual deployment constraints?
4. Which existing authority can approve an active-lease takeover, and what
   evidence must be retained? No new role hierarchy is assumed.
5. Is strict lease-required enforcement the target for every write, and how
   should projects opt in and migrate SSH/HTTP clients without an unsafe
   compatibility bypass?
6. What lease state and audit retention belong in native backups versus the
   coordination sidecar, and how should restored active leases be reconciled?
7. Which active lease details are visible to all existing project readers,
   and does any future owner-wide or agent-specific view need narrower
   visibility under existing authorization?

**Proposed disposition:** review this design and resolve the decisions above
before implementation. This note is not owner acceptance, authorization to
change existing lease/merge behavior, or evidence of implementation,
testing, review, integration, deployment, or live verification.
