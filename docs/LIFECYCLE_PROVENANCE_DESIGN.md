# Lifecycle provenance and historical facts — proposed design

Status: **design for review; no implementation or rollout authorization**.
This document proposes the second lifecycle authority delivery. The separate
raw write delivery reserves lifecycle events and labels on the contributor route;
it is pending integration in the inspected baseline. This proposal adds evidence
of how a fact was written.
That boundary remains necessary even when every coordinator rule is off.

## 1. Existing behavior and intended guarantee

The inspected baseline is public main
`9a1220a88c25eeba7a5759e506b8db77b6533c65`. Its coordinator rules include
`lifecycle_authors`, `independent_approval` and `coordinator_writes`; see
[operator instructions](OPERATIONS.md#the-coordinator-rules-who-may-write-which-fact-and-who-may-approve)
and the [coordinator design](COORDINATORS_PER_PROJECT_DESIGN.md).
The rules govern writes. Current lifecycle readers check canonical payloads,
native author agreement, parent, scope, ordering and label agreement. These checks
cannot establish that the dedicated host action authorized the write: somebody
with raw write access can reproduce a consistent event and label.

Proposed guarantee: when a reader requires strict provenance, a lifecycle value
is usable only if a host-issued record binds its exact native event to the
dedicated lifecycle operation and the authorization decisions required by the
applicable rules. A missing or damaged binding makes the fact `unknown`.
Neither a successful request receipt nor a content hash alone proves this.

The trust boundary is the host's protected storage and configured entry points.
An unrestricted service-account shell can change native data and sidecars;
this design does not confine that account. A request-supplied principal or path
is never host authority. Unbound SSH actors remain attributed names, with their
existing authorization semantics; a receipt must not call them authenticated.

## 2. Proposed storage choice

Use a versioned project sidecar, provisionally `.lifecycle-provenance/`, with one
operation record per canonical lifecycle operation. Names derive from the
operation identity hash; callers cannot choose a path. The format, paths and
validators must be frozen and accepted by backup/restore readers before a writer
starts producing them. Record sizes and batch limits need explicit bounds.

The host constructs each record under the project coordination lock. It contains:

| Binding | Required content |
| --- | --- |
| Format and identity | Schema version, project lineage ID, operation ID, canonical request hash and originating host identity |
| Native effects | Exact event IDs and canonical payload hashes, task, dimension, value and complete scope; include intermediate events when a repeated value needs one |
| Caller and route | Request actor; trusted entry point; authenticated principal and its binding source when available, otherwise explicitly absent |
| Authorization | Versioned rule evaluator, normalized switch snapshot, clauses applied, allow decision, assignee snapshot and operator membership result used by the decision |
| Independent approval | For applicable reviewed/integrated decisions: exact contribution, approval record and author/assignee principal bindings used; enough bound content to verify the decision without today's registry |
| Durability and repair | Prepared/completed state, ordered effect plan, observed effects, timestamps and any linked operator repair or revocation record |

Persist only the minimum decision evidence, never credentials, SQL passwords,
private keys or complete service authority documents. Operator names/principals
are private runtime metadata; documentation and public proof use synthetic data.
Payload `provenance: performed|reported|...` remains evidence attribution and
cannot replace the host route record.

Host ownership protects the record. Its hashes detect mismatch and corruption;
they are not signatures. Copying a native event, receipt or operation ID to
another task, project lineage or installation without an authorized restore
binding must not make it trusted.

### Alternatives considered

| Alternative | Reason for rejecting or limiting it |
| --- | --- |
| Trust canonical JSON, native author and labels | They establish consistency and are reproducible through raw writes. They contain no host authorization decision. |
| Rejudge old actors against current lists and assignees | Reassignment, key changes and list edits would change the meaning of an old decision; old identity data can be absent. |
| Add an unkeyed receipt hash to the native event | A raw writer can reproduce both; it adds no protected authority. |
| Treat the existing HTTP operation journal as lifecycle evidence | `run_guarded` only opens that journal for a top-level operation ID; ordinary lifecycle payload identity is a separate identity. Its response receipts have retention/reclamation semantics, and do not bind every release event or the lifecycle decision. Reusing it would require a permanent lifecycle table and coverage of every route, effectively a new storage contract. |
| Sign every event | Adds key custody, rotation and restored-key history. It does not stop a trusted shell from issuing false records. Consider a separate signed export format if offline recipients need independent authentication later. |
| Automatically bless every consistent legacy row | It would turn a past forgery into host authority, and labels alone cannot supply scope or evidence. |

A dedicated sidecar separates permanent fact authority from bounded HTTP retry
receipts. It uses the existing complete coordination backup family, with an
explicit extension; it is not an unrelated backup system. Reuse atomic file
publication and existing locks where they provide the required guarantees.

## 3. Write, crash and retry contract

All legitimate fact writers must use the same provenance writer: single facts,
scope changes, release deploy/verify/rollback, enabled/live updates and any
operator historical repair. Inventory direct callers before implementation.
Standalone local tooling must either use the host adapter or be documented as
producing native history without strict qualification. It cannot silently claim
host authority. Existing contributor raw refusals stay in force.

1. Under the established lock order, validate identity, entire request, relevant
   policy and all batch targets before the first native write. Capture the
   decision snapshot here, not after the effects. Refusals leave no native effect.
2. Atomically persist and synchronize a prepared record containing the complete
   plan. A failed prepare prevents native writes. A retry with the same identity
   but different content or caller is refused.
3. Perform the existing native effects. A state update can create an intermediate
   event before the final one; both belong to the plan. Verify the returned IDs
   against native data before binding them. A native response lost after commit
   needs reconciliation, not an invented ID or another blindly repeated write.
4. Publish the completed record only after every planned effect is verified;
   synchronize it before returning success. Only completed records qualify facts.
   Request success and lifecycle fact trust remain separately checked.

For a release batch, completion qualifies the complete planned batch. A crash
halfway through leaves its effects unqualified until reconciliation. Do not
advertise transaction atomicity across native data and files. The proposal trades
temporary `unknown` for avoiding a partly authorized release being called complete.

| Observed interruption | Reader and recovery result |
| --- | --- |
| Before prepare persists | No native write; fresh validation may retry. |
| Prepared, no effect | Unknown; an exact retry validates present authority before continuing. |
| Some/all native effects, incomplete record | Unknown; reconcile exact payloads, IDs and complete plan. Never bless a lookalike from labels alone. |
| Completed, response lost | Exact identity returns the prior completed result after the existing live identity checks. No duplicate native fact. |
| Missing, malformed, mismatched or unsupported record | Unknown with a bounded reason; no legacy fallback in strict context. |

Recovery must not upgrade a prepared decision merely because today's policy is
less restrictive. The stored decision determines what the original operation
was authorized to do. Resuming new effects also requires present caller authority;
if that is lost, an operator explicitly reconciles or abandons the operation.
No background read completes a record. Completion, abandonment and correction
are linked audited operations; preserve the failed preparation and native history.

The outer HTTP idempotency journal still controls HTTP retries. Its completion
cannot qualify a pending lifecycle sidecar; a completed lifecycle record cannot
bypass a revoked HTTP credential. Test both orders of failure and recovery.

## 4. One reader context and one trust decision

Build a host-owned read context once per snapshot, using native rows, the
validated project provenance store and the applicable installation rules.
Readers receive this context explicitly. Caller JSON cannot turn strictness off,
select a journal path or inject a purported receipt.

Keep the current shape/hash/parent/scope/order/label checks, then apply one shared
qualification decision to each candidate scope and fact. An invalid newest
candidate stays unknown; do not discard it and revive an older passing value.
Retain existing current-scope versus historical-scope ordering, supersession,
rollback and liveness semantics. Trust the supporting scope, intermediate events
and cross-dimension dependencies as well as the final fact. A damaged store with
unknown coverage makes the affected snapshot unknown; isolate damage only when
its exact affected operations/tasks can be established safely.

`trusted_payloads` and `scoped_evidence` must share this decision. Thread its
context through `project_facts`, `enabled_states`, `integration_evidence`,
`evidence_owed`, release selection/query/rollback, native writer preconditions,
`brief`, `work`, generated views, review-state and follow-on contribution gates,
capability verification and operator preview reports. Existing integration-revert
journal checks remain additional requirements. Checking only the visible brief
would leave a release or historical follow-on base vulnerable.

Expose bounded reason codes such as `provenance-missing`, `operation-pending`,
`event-mismatch`, `policy-unqualified` and `restore-unbound`, with native IDs for
inspection. Avoid secrets or complete decision snapshots in contributor output.
These reasons explain an unknown fact; they do not rewrite the task or lifecycle.

### Proposed rule trigger and policy transitions

There is **no new installation switch**. Proposed strict route provenance applies
to every lifecycle dimension when **any** of the three coordinator rules is on.
This deliberately prevents one consumer from trusting rows that another consumer
rejects. It is a design decision for acceptance, not current behavior.

| Current switches | Qualification required |
| --- | --- |
| All three off | Exact existing legacy projection, without consulting provenance or actor registries for ordinary reads. |
| Any rule on | Completed host route binding for every fact and its supporting scope/effects. |
| `lifecycle_authors` on | Additionally, the write-time dimension decision must satisfy that rule: assignee or listed operator for implemented/tested/scope; listed operator for the protected dimensions/release. |
| `independent_approval` on | Additionally, reviewed/integrated must carry the applicable write-time independent approval decision from the existing evaluator, including its bound-principal applicability. Do not invent stronger rules for legacy unbound callers. |
| `coordinator_writes` alone | Require route binding; do not introduce a new lifecycle author restriction. Existing coordinator-action checks still apply to their own operations. |

Capture route receipts for legitimate writes even while all rules are off, but
do not add all-off authority lookups or alter its observable reader results.
Such receipts qualify the route; they do not claim an authorization clause that
was not evaluated. Enabling a new clause leaves facts without that qualification
unknown until explicitly reasserted or repaired. A fact already qualified under
that clause keeps its historical decision after reassignment, principal retirement
or allowlist changes; the decision is not recomputed using today's parties.
An audited revocation/correction can withdraw it explicitly.

Switching all rules off restores the legacy projection, including its weaker
trust of consistent old rows. State that consequence in the switch warning and
operator documentation. Turning rules on again reapplies qualification to the
same immutable history; it never silently issues receipts. Preserve the existing
coordinator-rule parser semantics for absent/false/malformed individual switches.
Unreadable configuration cannot be interpreted as permission to fall back.

### Standalone exports

A native export alone cannot establish strict provenance. Keep the existing
explicit legacy/offline projection for compatibility; it establishes no strict
authority. A requested strict offline read needs a matching validated export of
the host context and provenance family, including snapshot/lineage binding.
Missing context yields unknown, not an automatic legacy result. Locally supplied
context can test the algorithm but is not authenticated host evidence; an offline
recipient needs a trusted transfer or a separately accepted signing mechanism.
Host entry points always choose context themselves. Test absence of context at
every call boundary, so a missed adapter cannot disable a configured rule.

## 5. An installation with years of history

An upgrade does not migrate or qualify old facts on startup. With all rules off,
old native exports retain exactly the prior projections. Old shapes that already
read unknown (no payload actor, no scope, label without event, event without label,
ambiguous order) remain unknown; this design does not make them pass.

Before enabling a rule, an operator runs a proposed read-only qualification
preview. It reports current values beside strict proposed values, reasons and
counts; it includes historical integrated scopes used by follow-on gates and
deployed releases used for rollback. Paginated output gives exact totals and
snapshot identity. The existing `lifecycle-authors` preview checks current author
eligibility; it is not proof of historical route provenance and cannot substitute
for this preview.

On enablement, an old well-shaped fact with no binding becomes unknown. This may
pause follow-ons, release selection and capability verification despite an old
passing label. Ordinary task prose, status, contribution records and historical
evidence remain readable. Existing deployed services are not stopped by this
read change; the kit stops claiming qualified evidence for them.

### Explicit historical repair

Prefer recording fresh facts through the authorized lifecycle action, naming the
same actual commit/release and evidence after verification. Never replay a
deployment merely to repair its evidence. Historic integration evidence must
still satisfy the existing independent review and exact-source conditions.

Where reassertion would disturb current scope/labels, propose an operator-only
attestation operation that qualifies specifically named historical events and
their scope. It binds an inventory hash, event/payload hashes, source evidence,
the operator's identity and approval basis. This is **a new attestation now**,
not a claim that the original route or identity has been recovered. The shared
reader distinguishes this basis from original route provenance. It requires
explicit independent approval evidence for reviewed/integrated where applicable;
an operator's attestation alone cannot satisfy that clause.

No bulk “trust all old facts” operation. A dry run lists each eligible or refused
row before a bounded manifest is accepted. Missing original actor, scope or
evidence cannot be invented; those shapes require a new fully evidenced fact.
Concurrent edits invalidate the manifest. Repair retries use exact operation
identity; undo creates a linked revocation, preserves original data and makes
dependent strict facts unknown. Commands and final schema need separate design
acceptance and implementation review.

## 6. Backup, restore and rollback

Extend complete project backup to preserve native data, existing coordination
sidecars, the HTTP operation journal and every provenance prepare/completion/
repair/revocation record, including project lineage identity. Copying native rows
alone is a legacy/data-only restore and cannot retain strict qualification.

Backup must hold a consistent project boundary across native export/snapshot and
sidecar capture. Freeze and document lock ordering with the HTTP authority and
journal locks; prove no nested backup/writer deadlock. Validate the new path
allowlist, schema, identity hashes and all references before publishing the
complete manifest. Hash the manifest and its files. A native snapshot and receipts
from different backups are never a complete restore just because hashes parse.

Restore validates the entire set before making it available to readers. Preserve
the original lineage and issue IDs; when restoring to a different deployment or
project name, an operator-issued restore binding connects that exact manifest to
the destination. Do not weaken cross-project checks to permit arbitrary copies.
Never issue success receipts during restore or complete pending operations by
guessing. A restored pending operation remains pending for explicit reconciliation.
Active policy comes from the destination's operator configuration, as in the
existing restore contract; a backup does not silently enable/disable rules or
regrant lists. Report the destination's resulting qualification preview before
accepting writes.

| Recovery situation | Required result |
| --- | --- |
| Complete compatible backup | Restore bindings and pending history; qualify only completed valid operations under the destination rules. |
| Old backup with no provenance | Restore as legacy history; strict facts unknown, no synthetic backfill. |
| New family malformed or unsupported | Refuse complete restore before effects. Any explicit data-only escape reports missing authority and leaves strict qualification unknown. |
| New backup on an old kit without the path validator | Refuse the new sidecar family; use a compatible restore tool. Never strip it to make an old restore claim completeness. |
| Rollback to code predating strict readers | It can again trust consistent native rows. Treat this as a loss of the new guarantee, not a safe equivalent strict rollback. Preserve the last complete compatible backup. |
| Old writer used during rollback | Its new facts have no qualification; upgraded strict readers show them unknown until deliberate reassertion/repair. |

Rollout therefore starts with backward-compatible validators and backup support,
then the receipt writer and shared readers, then preview, then a deliberate
operator rule decision. Validate each supported rollback target before allowing
a writer to create the new family, following the existing sidecar pattern.
A restored journal or old-kit backup may omit new authority; do not discard the
last complete new-format backup while experimenting with rollback. This document
authorizes no backup of production, restore, deployment or switch change.

## 7. Evidence required before implementation delivery

Design acceptance must explicitly settle the any-rule trigger, permanent sidecar
schema, all-off receipt cost, historical attestation basis and restore binding.
Implementation then requires an acknowledged plan on the actual integrated base;
it must not replace the separate raw-write delivery before that review completes.

Required proof, using disposable synthetic data:

- Genuine endpoint/native operations, including release batches, retries and
  bound/unbound caller behavior; exact supported Beads/Dolt versions.
- Raw two-call forgery, forged principal/author/hash, copied or altered receipt,
  cross-task/project/restore replay and native changes after completion. Every
  strict consumer, including historical follow-on/release gates, must agree.
- Crash at each prepare/effect/completion/outer-journal boundary, native response
  loss, partial release, caller revocation, policy flip, reassignment, identity
  retirement, unsupported/damaged stores and explicit repair undo.
- A retained export from a real pinned tracker with synthetic modern and older
  shapes, including the four legacy shapes above. All-off before/after equality
  across all readers, even with irrelevant damaged authority configuration.
- Complete/legacy/data-only/mixed/damaged backup cases; cross-installation
  restore binding; named supported rollback targets and loss of qualification
  for old writers. No production or private reviewer export is a fixture.
- Meaningful assertion-killed mutations with passing controls; exact source/base
  Linux and Windows CI counts, named skips and precise unsupported behaviors.

This delivery is a document. These are planned acceptance tests, not claims that
the proposed storage, readers, repair or recovery mechanisms are implemented.
Private review evidence holds the source inspection and document checks.
