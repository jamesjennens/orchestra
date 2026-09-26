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

The existing endpoint serializes its handled mutations with a per-project
`.coordination.lock` (`fcntl.flock`). Checking a lease while holding that lock
is a candidate single-host enforcement point, not a multi-host guarantee.
Direct operator commands through `bd` or `admin.py` do not pass through the
contributor endpoint and would bypass that check unless separately guarded or
paused during strict enforcement. The kit has no actor lease today.
`sessions.py` records run start/heartbeat/end events with server UTC and
allows several concurrent runs for one actor. Its random `run_id` and
heartbeat format are useful precedent, but those activity records are not
exclusive leases: they do not fence writes or prevent concurrent runs.

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
| Holder ID | Identifies one independently running work process, not just a reusable actor name |
| Purpose | Human-readable reason for holding the lease |
| Issued and expiry instants | Server-recorded lifetime and display |
| Last operation ID | Supports exact retry reconciliation |
| State | Active, expired, released, or superseded |

The holder process mints a fresh, unpredictable run/holder ID at its own
process start, using a cryptographically secure random source (a UUIDv4 is a
possible representation). Separate scheduled and interactive processes mint
different IDs even when they use the same actor, checkout, and working
directory. A registered session's existing `run_id` may supply this identity
when the same process already records a run; otherwise use a lease-specific
ID. Do not treat a shared directory or cached handle file as process identity.

The lease ID and the separate lease secret/handle are unguessable
high-entropy values: possession of the secret authorizes the holder to present
the lease, so it is credential material. Keep the secret only in the owning
work process's memory (or an equivalent process-private secure facility),
never in source, shared files, URLs, command-line arguments, logs, or brief
and work output. Do not copy or share it between processes, including two
processes for the same actor and checkout. On process restart, acquire a new
lease; do not recover another process's secret from disk. If the holder exits,
expiry and fencing recover safely.

This has an important client boundary: the current `client.py` starts a new
process for each invocation and sends one endpoint request. That shape cannot
hold a private batch lease across commands without sharing the handle. A
lease-aware long-running worker/client must own the lease in the same process
that owns the work and issue its requests itself. Until that exists, a
one-shot CLI may acquire/use/release a short command-scoped lease for one
mutation, but that only serializes that command and must not be described as
exclusive ownership of the surrounding multi-command work session. Strict
workflow-wide enforcement must refuse to claim that a sequence of independent
CLI invocations is protected.

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

1. **Acquire.** The holder process supplies the actor, its newly minted holder
   ID, purpose, requested TTL, and operation ID. The server validates the
   request and any existing project authorization. In one atomic operation,
   it grants a lease only if no unexpired lease exists for the key, or the
   previous lease is expired according to canonical server time. Exactly one
   concurrent contender wins. A grant assigns a fresh opaque, unguessable
   lease ID, a separate unguessable secret handle, and a strictly increasing
   fencing generation. The server response carries a nonzero command result
   for conflict and explicit error text; the `client.py` CLI must exit with
   that same nonzero status. The SSH endpoint's outer JSON transport may
   complete normally while carrying that refusal, but it must not become a
   success-shaped `acquired:false` result with client exit code zero. A piped
   caller must be able to rely on normal failure propagation and inspect the
   error.
2. **Renew.** The same holder process supplies its secret handle, exact lease
   reference, holder ID, generation, and a new operation ID. Renewal succeeds
   only while that lease is still active and current. It extends the
   server-calculated expiry within server-enforced TTL bounds; it cannot
   revive an expired or superseded lease. Renewal never changes the holder or
   purpose.
3. **Release.** Release is conditional on the exact current lease reference,
   secret handle, holder, and generation. Releasing an already released lease
   is idempotent only for the exact original operation. A different holder
   cannot clear it. A missing response is resolved by reading the operation's
   canonical outcome, not by issuing a new release with changed content.

The server chooses and validates the effective TTL; client-supplied time
values are ignored. The allowed minimum, maximum, and default TTL remain
owner/operator decisions. Reacquiring with the same holder does not silently
extend an existing lease: clients use the explicit renew operation so a
retry cannot mask a stale process or an accidental second execution.

For an active manual work batch, renew on each accepted protected write using
the same holder and handle, extending expiry by a bounded server-side
sliding-TTL rule; a background renewer is not required. A long idle pause may
let the lease expire. On resume, the process must reacquire and may proceed
only if no other holder won; it cannot revive the old generation. A one-shot
command-scoped lease is released after that one mutation and protects no
subsequent command. Workers that need workflow-wide exclusion must use the
long-running same-process client boundary above. Explicit release occurs
when the batch yields or completes.

Lease operations use idempotency keys. A retry with the same actor, operation
ID, and canonical payload returns the recorded result. Reuse of an operation
ID with different content is rejected. If the response is uncertain, the
client reads the operation and lease state before attempting another
mutation.

## Write-command enforcement and fencing

Enforcement belongs at the shared server-side mutation boundary, after
transport authentication/authorization and before effects. It must cover all
commands that mutate project coordination state, regardless of whether they
arrive over SSH, a local endpoint, or HTTP. The initial endpoint candidate is
to validate while holding that project's existing `.coordination.lock` and
retain the lock through the protected native mutation. That provides
serialization only among endpoint requests on the same host/filesystem using
that lock; it does not cover direct operator `bd`/`admin.py` writes or
independent hosts. Strict multi-host use needs one shared transactional/fenced
authority for both lease state and writes. Do not imply otherwise.

Classification must occur in each action's parsed command/operation handler,
not by grepping arbitrary argument text. Explicit read-only examples are
`bd list`, `show`, `ready`, `search`, `count`, and `lint`; `comments ISSUE`
(the current CLI has no `comments list` read subcommand); `dep list`, `cycles`,
and `tree`; `brief`, `history`, `review TASK` without a payload, `work`,
`session show`, and `session run status`. Explicit mutations include
`bd create`, `update`, `close`, and `reopen`; `comments add`; dependency `add`,
`remove`, `relate`, `unrelate`, and the `--blocks` shorthand; `checkpoint`;
review contribution, request, response, and approval payloads; handoff
dispositions; session register/resume and run start/heartbeat/end; lifecycle
record; and coordination mutations. `view` is read-only. `refresh` writes
derived views but not canonical coordination state and is outside this lease's
write guarantee. A shared route does not imply a shared classification:
`brief` is read-only while `checkpoint` writes, and `review TASK` reads while
a review payload mutates. Comments and dependencies likewise require
recognizing the specific operation. Unknown or malformed subcommands,
ambiguous forms, and unclassified future mutations fail closed as writes; only
an explicitly recognized read-only operation bypasses a lease check.

Acquire, renew, release, and an explicitly authorized takeover are
lease-control operations with their own validation and audit path; their
effects are serialized with ordinary protected mutations.

For each opted-in actor, every ordinary write must present the current lease
reference, secret handle, holder ID, and fencing generation. The server
rejects missing, expired, released, or superseded credentials with a clear
conflict response that identifies the current holder and expiry where visible.
The response and client process exit status must be nonzero; it must not be
encoded as a successful result with a false acquisition flag. A generic actor
string is not a lease credential. Lease enforcement does not authenticate the
actor; existing access controls remain responsible for deciding who may use
an actor or perform a takeover.

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

The lease reference, holder ID, secret handle, and fencing generation must
travel with every protected request. The existing SSH/local `client._wire`
JSON envelope has no lease field; the proposal is to add a typed top-level
lease context there and pass it unchanged to the endpoint guard. HTTP requests
must carry the same context in dedicated authenticated headers or a typed
request field over TLS; never place a secret in a URL, native `bd` argv,
shell word, or log. Adapters normalize both transports to the same guard
input, and actor identity still comes from the transport's existing
authorization/binding rules. Missing or malformed lease context is a
conflict in strict mode, not a fallback.

Classify and enforce first in warn/audit-only mode, logging would-be refused
operations and the selected actor without recording the secret. Then allow
strict opt-in per actor (not by inventing a role) while non-opted-in actors
retain existing behavior; only after compatibility evidence should an owner
consider broader enforcement. Strict opt-in refuses old clients that omit
the lease for that actor. Turning enforcement off removes the guarantee and
must itself be authorized and audited. Direct operator writes remain a bypass
unless the operator path is guarded or explicitly paused for opted-in actors.

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
timestamps. Persist a canonical UTC high-water mark and the last monotonic
clock sample observed by the running authority. During one uptime, compare
wall-clock elapsed time with monotonic elapsed time; a forward or backward
wall-clock step beyond an operator-selected tolerance marks lease time
unhealthy. Reject timestamps below the persisted high-water mark. After
restart, monotonic time has a new origin, so it cannot alone validate
previous deadlines: if the wall clock is behind the persisted high-water
mark, suspend grants and protected writes; if a forward jump exceeds the
configured plausible downtime bound, require explicit reconciliation rather
than expiring all holders. These checks cannot prove the cause of a long
offline interval; the clock source, thresholds, and recovery procedure remain
operational decisions. A detected discontinuity must never silently expire
every holder or extend leases.

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

`brief TASK` should show the current lease for that task's currently assigned
actor; `work` should show actor leases for the selected owner/actor scope, not
imply that leases are keyed by task. Both show no-lease, unavailable, or
stale-observation state explicitly. When held, show holder ID, purpose,
issued/expiry time, server-calculated time remaining, and whether the current
caller is the holder. A listing should allow a coordinator to discover active
leases without querying each task. Views reuse existing project read
authorization and must not disclose the secret lease handle, local
working-directory path, or new identity data.

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
the same canonical lease guard as other transports. The lease reference,
holder ID, secret handle, and generation travel in a typed HTTP header/request
context equivalent to the SSH/local JSON field; they do not replace the
session credential. Session expiry/revocation must not silently release a
lease; the lease expires or is explicitly released/taken over. An in-process
HTTP backend and per-process file locks do not provide multi-process or
multi-host safety; that scope requires a shared transactional lease-and-write
authority.

The personal-agent boundary (.22) proposes owner-bound agent identities whose
access cannot exceed their owner's existing project authority. A registered
agent may have a distinct actor ID and therefore a distinct actor lease. A
manual agent run mints and holds its own handle in the live work process; no
background renewer is assumed. Each protected action renews the active batch
lease. When the user stops to wait for feedback, the agent releases it or lets
it expire; on manual resume, it reacquires and may be refused if another
holder is current. If the intended guarantee is one active process across all
agents of an owner, that is a different owner-keyed policy and must be decided
explicitly. A lease must not reveal a private working-directory hint or make
an agent a project role. Disabling an owner/agent or revoking a credential
should prevent future authorized writes under existing authorization rules;
it does not rewrite the history of already granted leases.

SSH remains a supported transport. It must reach the same server-side command
guard and keep its existing project/actor selection explicit. Its existing
one-request JSON stdin envelope needs the lease context added by a
lease-aware client; lease IDs/generation and secret handle must not be
smuggled in native command arguments. Enforcement cannot be HTTP-only, nor
may old SSH clients bypass it for an opted-in actor. During warn-only rollout,
unmodified clients continue to work with audit warnings; strict opt-in actors
must use the lease-aware path. Read access and transport choice remain
separate from lease ownership.

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
- Separate processes for the same actor and checkout mint distinct holder IDs;
  no test or supported flow writes a handle to a shared cache. A piped acquire
  refusal yields nonzero status and actionable stderr.
- Wrong holder, actor, lease ID, generation, or operation payload is refused;
  exact uncertain retries reconcile without duplicate events.
- Expired/released/superseded holders cannot write, including a delayed write
  racing with acquisition or forced takeover.
- Renewal cannot revive expiry; release cannot clear another holder; TTL
  bounds are enforced server-side; manual work batches renew on protected
  writes and resume only after reacquiring.
- A crash/restart, unavailable store, malformed state, and simulated clock
  rollback/forward discontinuity fail safely and recover explicitly.
- Forced takeover requires authorization and reason/evidence, increments the
  generation, and leaves a queryable audit event.
- `brief`/`work` show active, absent, expired, and unavailable states without
  exposing lease credentials or private agent paths.
- Parsed-operation classification distinguishes read-only `list`/`show` and
  review/brief reads (including `comments ISSUE`, not nonexistent
  `comments list`) from `create`/`update`, comment/dependency mutations,
  checkpoint, and review/handoff writes; unknown forms fail closed. Exercise
  every supported SSH/local and HTTP wire path with absent, malformed, and
  valid lease context.
- SSH, local, and HTTP mutation paths all enforce the same policy; HTTP
  principal binding and agent owner caps continue to apply.
- The endpoint guard under `.coordination.lock` serializes same-host endpoint
  writes, while tests/documentation demonstrate that direct `bd`/`admin.py`
  and independent hosts are outside that guarantee unless separately guarded.
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
before implementation. No actor lease exists in the kit today. This note is
not owner acceptance or authorization to alter existing merge-slot behavior,
and is not evidence of implementation, testing, review, integration,
deployment, or live verification.
