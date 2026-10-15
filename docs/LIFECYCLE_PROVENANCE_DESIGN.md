# Lifecycle fact trust, audit and historical correction

Status: **reworked design for document review; no new implementation or rollout authorization**.
This replaces the rejected PR105/6051571 proposal. The inspected baseline is
`15b2ad0a10b204c11612dfa1d153efdb92813b44` (15 October 2026). Code citations below
refer to that exact tree, not the older review's f929787 line numbers.

The baseline already contains delivery1's contributor raw-write guard, the
coordinator rules, host-issued web integration marks, and lifecycle audit/VOID
and recovery support. This note describes their relationship and the remaining
optional design choices. It does not claim those implementations or their tests
as work performed by this document revision. The operating commands are in
[Installation and recovery](OPERATIONS.md#lifecycle-event-audit-and-host-corrections),
[raw lifecycle writes](LIFECYCLE_RAW_WRITES.md), and
[web integration marks](HTTP_DEPLOYMENT.md#passed-integration-marks-and-operator-recovery).

## 1. Decisions and trust boundary

The coordinator's six decisions in .252/01a12d53-4720 are retained:

1. `lifecycle_authors`, `independent_approval` and `coordinator_writes` govern
   writes. None enables strict historical reading or migrates existing facts.
   Any future strict reader needs its own explicit switch, initially OFF, and a
   preview that counts the affected events, tasks and decisions before enabling it.
2. Audit and retractable VOID come before general per-write provenance. A read
   creates no project store. Enabling a writer changes its installation setting
   and bounded setting audit; the correction family starts with the first actual
   correction, not with copying a kit or recording an ordinary fact.
3. Preserve legacy trust. Review questionable facts and void the few judged wrong;
   do not attest thousands of good facts one by one or bless them by timestamp.
4. A web mark belongs to a host-owned record, constructed from a verified route
   descriptor. It is never an extra lifecycle payload field.
5. Use one name-or-bound-principal definition for a listed operator. Keep the
   distinct authority of an installation setting switch explicit.
6. Every new family writer has a separate OFF-by-default gate; install compatible
   readers and complete backup/restore support everywhere that can hold or restore
   the project before enabling that writer.

Protected host files and configured entry points are the boundary. Hashes bind
content; they are not signatures or proof against the service-account shell.
An unbound SSH actor is an attributed name, not an authenticated person. HTTP
identity is authenticated by its service. Caller JSON cannot manufacture the
host snapshot, a route descriptor, an operator binding or an issuance record.
The payload's existing `provenance` field describes evidence attribution; it does
not establish host issuance or authorization.

### What is already enforced

- Contributor raw create/update/dep/close/reopen cannot manufacture, convert,
  edit or remove lifecycle state events or reserved labels. This guard is always
  on, including when the three coordinator rules are OFF. Ordinary task prose,
  status and assignment keep their existing routes. See `raw_lifecycle.py:46,80`
  and `endpoint.py:1783`.
- Canonical shape, native author/parent, scope, order and label agreement are
  checked by `lifecycle.py:358,418,467,602`. A consistent legacy record is not
  rejected merely because it lacks a new receipt.
- `lifecycle_voids` supplies explicit, event-bound negative corrections. A native
  correction comment alone is not a host-issued VOID. Its retained host entry
  and exact native/comment binding are required.
- `web_lifecycle_marks` supplies positive historical recorder eligibility for
  the specific web integrated fact it binds. It is not a receipt for every
  lifecycle dimension and does not make a forged lifecycle event valid.
- The follow-on base gate already makes an additional read-time decision: an
  integration needs an eligible recorder and a later native creation time. It
  accepts a current listed operator/bound actor OR an exact issued web mark;
  neither arbitrary actor names nor a plain export confer mark authority.

## 2. Attackers and mechanisms

| Attacker or incident | Mechanism and remaining limit |
| --- | --- |
| Pre-guard raw forgery | D1 prevents a new contributor forgery through the current endpoint; it cannot distinguish an old consistent forgery. Audit supplies leads; an operator judges evidence and issues a VOID. Legacy consistency alone is not fraud detection. |
| Listed operator's raw write | The raw guard intentionally exempts a listed operator/bound actor. Dedicated write rules do not prove how that exempt raw event was produced. Audit/VOID correct a judged mistake; a web mark proves only its particular issued route. |
| Older kit serving the same tracker | It may lack the raw guard or ignore corrections/marks. New-kit audit exposes suspicious history, but cannot confine that older writer. Preserve a final complete new-kit backup before downgrade; old reads can disagree. |
| Restored or foreign backup | Supported restore validates the native generation, family manifest, member hashes and event/comment bindings. A foreign or missing family does not gain authority from a copied operation ID. There is no invented cryptographic host identity or lineage service. |
| Caller replays a comment, mark or event | Bind task, event identity, exact payload/native bytes, native author and creation time; compare the retained host entry. Changed/deleted targets cannot expose an older pass by being dropped from event order. Exact retries reconcile only the same allowed operation. |
| Service-account shell | Out of scope: it can alter native data and protected files, including both sides of a hash comparison. Host ownership, operational access control and operator review remain necessary. |

An audit warning is not a declaration of forgery. Missing historical ownership,
unorderable times, a changed assignee or an unavailable approval snapshot are
reported as unknown evidence, not converted into fabricated historical authority.

## 3. Audit and corrections: the first step now on main

The current read-only command is
`admin.py --root ROOT lifecycle-audit PROJECT --limit 50 --offset 0`.
It lists event IDs and native dates, current assignee/operator relationship,
standing approval evidence where recoverable, and project-wide questionable and
unclassifiable totals. Events and corrections share a bounded page; `page_total`,
`next_offset`, omitted counts, `correction_status` and correction rows distinguish
complete, retracted, incomplete and abandoned history. No production audit or
production count was taken for this document revision.

The operator investigates a candidate using its payload actor/native recorder,
native time, assignee and review history, contribution/approval records and Git.
The present registry is not proof of ownership years ago. The operator can keep
a fact, obtain more evidence, or void one named event with an explicit reason and
evidence pointer. Good legacy facts require no per-fact migration.

### Current correction contract

`admin.py lifecycle-event-void` and `lifecycle-event-retract` use the separate
`lifecycle_void_writes` gate. Abandonment is a recovery for an incomplete native
correction and has its own documented reconciliation rules. All are host-only;
there is no contributor raw-comments shortcut. See `admin.py:989,1032` and
`lifecycle_voids.py:1224,1339`.

Under the established project/config locks, validate the entire request and
current authority before effects. Write the native correction comment first,
then one atomically published host record. The record binds the exact event,
including `created_at`, native author, parent/title/native hash, canonical
payload hash, correction comment identity/time, operator, reason and evidence.
No generic prepare/complete protocol or stored authorization-decision snapshot
is introduced. A cut after the native comment is uncertain: audit and backup
report it, and the exact operation/actor retry reconciles its matching comment.
A conflicting key or a second active VOID is refused; an unpaired comment is
never evidence that a correction was issued.

A VOID retains the original event and its ordering position. The selected value
becomes `unknown` with `reason: voided`; removing the event from the reader would
incorrectly revive an older pass. A linked retraction stops that correction only;
the original event must still satisfy normal shape/scope/label checks. Issued
corrections survive removal of their operator from today's list. A new correction
or retry still needs current authority. This differs intentionally from current
membership checks for a new write, and from recovery families whose existing
contract rechecks the current list.

Incomplete and lost entries are distinct recovery cases. Compare the last
complete backup manifest and native correction generation before abandoning an
unpaired comment: it may represent a LOST effective entry, which must be restored,
not abandoned. Current abandonment stores live beside the VOID family so an older
F0 reader does not encounter an unknown operation in its closed v1 family.
Use the recovery instructions in OPERATIONS rather than hand-editing either
store. Damaged abandonment evidence is reported and blocks backup; it is not
invented positive lifecycle authority.

Current bounds are 24,000 bytes per correction record, 1,000 family records and
4 MiB per family (`lifecycle_voids.py:30`). Retractions remain in the lifetime
budget: a void/retract loop is not unbounded free space. A record is created only
for a correction; ordinary fact traffic does not grow this store. The separate
abandonment format and its bounded backup validation are part of the current
recovery contract. Capacity exhaustion refuses before effects; it is not license
to delete old authority or silently prune a family. No production capacity
estimate is claimed.

## 4. One reader context and explicit unknown reasons

`lifecycle.host_snapshot` attaches protected family context once per native
snapshot (`lifecycle.py:1948`). `Snapshot` metadata is outside untrusted native
JSON. Family reads are cached by project/request, not repeated per task or fact;
`work` does not open N family files for N tasks. A family can still contain up to
the stated bound, so its validation cost is O(family size), not O(1). No new
release-write budget is claimed: the existing VOID/mark paths have their own
costs; a future per-fact writer would require measured budgets before adoption.

`classified_events` preserves corrected event positions. `trusted_payloads` and
`scoped_evidence` use that context for current and historical values. A damaged
VOID journal can make the affected snapshot unknown; readers must carry the
bounded reason, not silently fall back to an earlier pass. The operation index
also excludes blocked operations, so an unqualified matching operation ID is
not answered as a successful reconciliation.

### Consumer inventory at the pinned base

| Consumer | Exact source and required reading |
| --- | --- |
| Current scoped facts, enabled/live | `lifecycle.py:418,467,519,553`; shared classification/trust, no private second decision. |
| Historical scopes/integrations/evidence owed | `lifecycle.py:602,758,769`; preserve reasons per event/scope; any-pass history does not override a VOID. |
| Deployment/release selection and rollback | `lifecycle.py:1100,1186,1192,1220`; voided integration/deployment is not a release or rollback target. |
| Release planning/query/batch preconditions | `lifecycle.py:1447,1547,1602,1688,1822`; use the same classified snapshot, including supersede planning. |
| Ordinary fact and scope write preconditions | `lifecycle.py:1387,1881,1938`; a damaged correction context is not bypassed by recording another fact. |
| Retry reconciliation | `lifecycle.py:1356`; `_operation_index` records blocked IDs/reasons rather than reconciling a voided event. |
| Author audit and raw event inspection | `lifecycle.py:259`, `admin.py:857,871`, `lifecycle_voids.py:998`; audit retains raw attribution and separately reports effective corrections. A raw row is inspection evidence, not a trusted pass. |
| Task brief | `briefing.py:722,877,1633`; exported host snapshot and lifecycle reason accompany the projected value. |
| Work/ready/owned/queue/resume projections | `work.py:637,669,951,1078,1096,1118`; share snapshot; keep lifecycle reasons beside independent held/verification state. |
| Lane board | `lanes.py:287`; integration evidence comes from the same trusted scopes. |
| Rendered refresh/view | `render.py:29,82,94`; retain Snapshot context and display reason next to unknown. |
| Review/integration projection | `review_state.py:538,316`; current and historical integration reasons survive its reduction. |
| Follow-on base admissibility | `review_workflow.py:2386,2422,2441`; compare trusted integration, native created_at, reverts and `marks.snapshot_eligible`. Git ancestry remains a reviewer check. |
| Capability verification filter and integration cache | `capability_verification.py:212,220,235`; `is_lifecycle_row` retains relevant task/event rows, then `integrated_commits` consults corrections. A label prefilter is not the trust decision; memoization shares one view. |
| HTTP task detail and lifecycle next action | `http_service.py:2569,2651`; EndpointBackend maps the authoritative brief into value/reason/note, then derives actions from that projected state. It must not reinstate a passed fact from a raw label. |
| HTTP task/queue lists | `http_service.py:1451,3074`; `lifecycle_reasons` stays beside the row's lifecycle values, including next-action consumers. |
| Web lifecycle history/write UI data | `web_lifecycle.py:80,151,219,221,269`; task state/read/retry use the shared snapshot; history is not a second raw trust implementation. |
| Browser fact display | `web/js/ui.js:50`; displays the server's fact note. Browser pages consume the server projection, not host files or an independent native fact evaluator. |
| Endpoint lifecycle/merge/raw entry points | `endpoint.py:1524,1607,1718,1783`; host context is constructed at the trusted adapter; raw guard is structurally separate. |
| Strict development endpoint | `tools/strict_canonical_endpoint.py:400` calls `apply_native`; any use requiring host truth must supply the same protected project context. It is not authority for a caller-supplied context or a new production route. |
| Offline list, owed evidence and release dry run | `lifecycle.py:2114,2247,2443,2502,2519`; consult and bind a named `--journal` family, or explicitly disclose unconsulted corrections. An export alone cannot manufacture host metadata. |
| Structural raw namespace parser | `raw_lifecycle.py:46,80`; reserves incomplete/legacy event shapes to prevent later activation. It is a write guard, not a substitute for trusted lifecycle reading. |
| Correction/mark binding and restore | `lifecycle_voids.py:69,525,593,675,884`; `web_lifecycle_marks.py:45,49,110,152,192`; exact native/comment binding is checked before effective correction/recorder eligibility. |

The appendix lists every direct call to the enumerated lifecycle readers found
in production top-level Python and the strict development endpoint at this base.
It is a mechanical cross-check; the indirect HTTP/browser/release consumers above
were also inspected. Adding a reader requires the same context and reason
contract, not merely inclusion in a grep result.

### Values and surfaces

| Meaning | Fields and behavior |
| --- | --- |
| Explicit failed fact | `value: failed` with its event ID; failure is not unknown and is not changed by adding a receipt. |
| No recorded/usable fact | `value: unknown`; absent event ID or existing scope/order/label reason identifies lack of usable evidence. Do not label this `voided`. |
| Issued correction in effect | `value: unknown`, `event_id` naming the affected/newest dimension event, `reason: voided`. Other family/binding problems retain their specific `lifecycle-void-*` reason. |
| Offline family not established | Named correction targets become unknown with `reason: lifecycle-corrections-not-consulted`; `correction_note` and `corrections_consulted: false` explain the limit. Unrelated legacy rows keep their legacy reading. |

The host fact object carries `reason`; briefs expose `lifecycle[dimension].reason`.
Work rows and HTTP queue/task lists expose `lifecycle_reasons[dimension]` beside
values. Review reads expose `integration.reason` and `newest_reason` where
applicable. The web task detail carries both the fact's `reason` and `note`, and
`ui.js` shows the note. Next actions derive from the effective projected value;
the adjacent reason distinguishes voided from missing without treating a VOID as
a hold or an instruction to reapprove. Release dry runs report
`corrections_consulted`, `correction_note`, warnings and target skip reasons.
Offline owed-evidence refuses an apparently empty answer when an unconsulted
correction hides a release row. A proposed future strict reader must preserve
these same surface distinctions; it cannot return only bare `unknown`.

## 5. Operator eligibility, web marks and assignment

### One definition, with explicit exceptions

`review_workflow.listed_operator_actor` (`review_workflow.py:515`) accepts an exact
listed name, or an actor bound in this project's owner map to the same principal
as a listed operator. A plain listed name does not require a registry read;
unavailable/malformed principal bindings cannot turn a non-listed actor into an
operator. This definition is now shared by D1's raw exemption, lifecycle write
rules and VOID/retraction issuance. The rejected note described the earlier
plain-name differences; those are not proposed again here.

The follow-on gate uses `web_lifecycle_marks.snapshot_eligible`: the current
operator decision OR a valid exact historical web mark. This extension is
explicit, not an alteration of who is currently on the operator list. Current
web role/grant removal or switching future mark issuance OFF does not erase an
issued mark. Normal lifecycle consistency and an effective VOID still apply.
The mark binds project/task/event, exact payload/native hashes, native author
and created_at to the host-validated coordinate route descriptor. A descriptor
is made by the endpoint, never copied from caller JSON. The closed lifecycle
payload is unchanged; an extra key would make old readers reject its payload.

Installation switches deliberately require a plain deployment-listed operator
(`admin.py:989`, and the web-mark switch), not merely an actor bound to a project
principal. Distinguish this setting authority from project correction authority.
The existing `.integration-reverts`/other recovery contracts must be described
on their own terms; this note does not silently convert their revocation rules
into the VOID contract.

### Assignment decision from .254 group7

`lifecycle_authors` trusts the current assignee for implemented/tested/scope;
assignment itself has no new restriction. An authorized raw task update can
change the assignee under the existing task/claim rules. A worker taking itself
as assignee can therefore gain those author facts, as it could by a legitimate
claim. It does not gain reviewed/integrated or bypass independent approval.
The source of the assignee is not an authenticated historical authority proof.

Readers should be able to distinguish current assignee from evidence of who
assigned when making an attribution audit. Today the native history is evidence
to inspect, subject to the SSH attribution limit; a current assignee alone cannot
answer who assigned years ago. If a future assignment-origin record is proposed,
it needs a separate accepted scope, a legacy `unknown` case and rollout/restore
rules. This document neither restricts assignment nor treats missing assignment
provenance as invalidating historical implemented/tested facts. The corresponding
current-assignee warning is already in OPERATIONS.md:880.

## 6. Backup, restore and rollback

Use the existing complete coordination sidecar family. For VOID, the manifest
records bounded member names, hashes, bytes and the native correction generation;
validate the native target/comment bindings as well. A manifest claiming N members
with 0 present is incomplete, not an empty trusted family. Restore refuses an
omitted family when correction comments or a retained destination marker show it
is needed, including pre-VOID backups and removal of the family plus its manifest.
Incomplete restore keeps a durable marker and the old uncertainty until reconciled.
Do not clear an existing family to make a legacy backup appear complete.

The web mark family has its own bounded manifest/restore marker and exact project
and event bindings. Copying a foreign project mark grants no destination
eligibility. Host ownership and an operator-authorized supported restore are the
boundary; no undefined lineage ID or cross-installation signature is claimed.

Older serving kits may ignore a family; their backup allowlist may omit it, and
pre-family restore validators may refuse unknown members. Before downgrade, stop
new family writes, take and preserve the final complete backup with the new kit,
and explicitly decide whether the older reader's weaker reading is acceptable.
Turning issuance OFF does not retract existing corrections or marks. Retraction
is a new audited operation, not deleting a host file. Returning to the new kit
requires its complete native-plus-sidecar backup; never strip attribution to
force an old restore.

**Rollback limit:** once a new-family writer is enabled and a backup includes its
members, restore that backup only with a kit that validates every included family;
an older serving kit may ignore those decisions and its own backup can omit them,
so retain the last complete new-kit backup before going back.

## 7. Alternatives and the optional future boundary

| Alternative | Decision and properties |
| --- | --- |
| No general provenance store: D1 + write rules + audit/VOID | Chosen minimum. Protects current contributor writes and repairs judged bad history without migrating every good event or adding per-fact cost. Does not prove the dedicated route for every old consistent row. Already represented in this base. |
| Present operator list at read time | Already part of base admissibility, extended by issued web marks. Simple and bounded, but list changes alter eligibility of unmarked old integration recorders. It is not a general historical decision snapshot. |
| Host-frozen legacy inventory | If a later strict policy needs a grandfather boundary, freeze exact event IDs plus payload/native hashes once under operator control. Backdated timestamps alone cannot enter it. It can also freeze a forgery, so requires audit/operator judgment and a separately accepted bounded storage/restore contract. Not implemented here. |
| Host-owned per-write records | Retain as an optional later design, only for an identified need not met by web marks. Host ownership binds an exact issued operation; it does not prove authorization if permissive rules allowed the write. No default writer. |
| In-payload mark or unkeyed hash | Closed payload validators and rollback reject a new field; a copied hash is not authority. Reject on those properties, not on the now-closed contributor raw-write hole. |
| Existing HTTP operation journal | Bounded retry receipts are not permanent fact authority and are not opened for every lifecycle operation. Extending retention/coverage is a new storage contract, not free reuse. |
| Sign every event | Adds key custody/rotation/restore history without confining the trusted host shell. Defer unless independently authenticated offline exports become an actual requirement. |
| Attest every good legacy fact or trust a date cutoff | Reject: too costly for years of history, does not find forgeries, and a date alone is not a host-frozen set. Use negative corrections for bad facts. |

If strict positive provenance is ever justified, propose a separate
`lifecycle_provenance_reads` switch (provisional name), OFF by default, and a
read-only `lifecycle-provenance-preview PROJECT` (proposed, not a current command).
Its bounded output must report `current`/`proposed` reading and reason per named
event/task/scope; project-wide `would_become_unknown_events`,
`would_become_unknown_tasks`, affected integration/follow-on/rollback decisions,
unclassifiable counts and omitted/page totals. Count every affected legacy fact
without writing receipts or enabling anything. No live preview count is claimed.
The three existing write switches cause **zero automatic strict-read migration**.
If the future separate switch were enabled without a grandfather policy, missing
positive proof could make real integrations unknown, move approved work back to
awaiting-integration, refuse follow-ons and refuse real deployment rollbacks.
That consequence needs explicit operator review of the preview, and must be
reversible by turning only the new read switch OFF while retaining write rules.
No such strict feature is authorized by accepting this note alone.

## 8. Independently releasable steps and acceptance

| Step | Scope, default and compatibility gate | One-sentence rollback limit |
| --- | --- | --- |
| A. Raw namespace/write rules | Existing D1 guard and independently configured write rules; legacy reads stay as recorded. | An older kit may reopen the contributor raw-write hole; preserve protected history and keep the new kit for guarded writes. |
| B. Read-only audit | Existing bounded audit; no project store or switch enablement from a read. | Reverting the audit removes diagnostics, not native facts or authority. |
| C. Negative corrections | Reader/backup/restore support first; `lifecycle_void_writes` OFF by default; first actual correction creates its family. | An older reader may ignore corrections and an older backup may omit them; preserve a complete new-kit backup, and retract intentionally rather than delete files. |
| D. Web integration marks | Existing verified descriptor and bounded family; independent `web_lifecycle_mark_writes` OFF by default; all holding/restoring installations compatible first. | Older kits may lose historical web eligibility and omit marks from backup; turn future issuance OFF and retain the final complete new-kit backup. |
| E. Optional general positive provenance | Separate need/accepted design, independently gated writer OFF, exact event binding, bounded format and compatible restore before any write; no implementation in this revision. | The first included new-family backup requires a supporting restore reader; older serving/backup behavior must be measured before enabling. |
| F. Optional strict reader | Separate accepted policy, separate OFF switch and complete preview/grandfather decision; never triggered by the three write rules. | Turn only the strict-read switch OFF to regain legacy reading; keep stored evidence and write protections. |

Each step can be reviewed and released without the later ones. Current A-D are
source observations, not new claims of rollout state. Remaining document review
must judge this reconciliation and the assignment question. Any new E/F or
assignment-origin implementation requires its own accepted plan, compatibility
proofs, mutation tests, actual-reader/backup tests and rollout authorization.

Do not build a prepare/complete protocol, decision snapshots, cryptographic
lineage/restore identities, per-good-fact attestations or a new strict offline
export system without a separately stated and accepted need. Existing bounded
operational offline correction disclosure is not such a positive-proof system.
This document changes no rule, store, fact or deployment.

## Appendix: direct lifecycle reader call inventory

The following call sites are pinned to base15. Repeated calls in one function
are shown individually because different request paths can lose context.

| Source call site | Containing function | Lifecycle reader |
| --- | --- | --- |
| `admin.py:857` | `lifecycle_authors_command` | `read_event_rows` |
| `admin.py:861` | `lifecycle_authors_command` | `lifecycle_authors_report` |
| `admin.py:880` | `lifecycle_audit_command` | `read_event_rows` |
| `briefing.py:722` | `brief` | `project_facts` |
| `briefing.py:1633` | `exported_rows` | `read_event_rows` |
| `capability_verification.py:235` | `integrated_commits` | `integration_evidence` |
| `coordination.py:528` | `apply_native` | `read_event_rows` |
| `coordination.py:545` | `apply_native` | `read_event_rows` |
| `endpoint.py:1524` | `execute.lifecycle_identity` | `read_event_rows` |
| `endpoint.py:1607` | `execute.slot_identity` | `read_event_rows` |
| `endpoint.py:1718` | `execute` | `host_snapshot` |
| `lanes.py:287` | `read` | `integration_evidence` |
| `lifecycle.py:274` | `lifecycle_authors_report` | `events_by_dimension` |
| `lifecycle.py:275` | `lifecycle_authors_report` | `trusted_payloads` |
| `lifecycle.py:282` | `lifecycle_authors_report` | `latest_event` |
| `lifecycle.py:432` | `classified_events` | `native_event` |
| `lifecycle.py:462` | `events_by_dimension` | `classified_events` |
| `lifecycle.py:482` | `trusted_payloads` | `events_by_dimension` |
| `lifecycle.py:489` | `trusted_payloads` | `latest_event` |
| `lifecycle.py:498` | `trusted_payloads` | `latest_event` |
| `lifecycle.py:501` | `trusted_payloads` | `scoped_event` |
| `lifecycle.py:520` | `project_facts` | `events_by_dimension` |
| `lifecycle.py:521` | `project_facts` | `trusted_payloads` |
| `lifecycle.py:522` | `project_facts` | `scoped_evidence` |
| `lifecycle.py:561` | `enabled_states` | `trusted_payloads` |
| `lifecycle.py:621` | `scoped_evidence` | `events_by_dimension` |
| `lifecycle.py:626` | `scoped_evidence` | `latest_event` |
| `lifecycle.py:766` | `integration_evidence` | `scoped_evidence` |
| `lifecycle.py:788` | `evidence_owed` | `scoped_evidence` |
| `lifecycle.py:1121` | `release_selection` | `scoped_evidence` |
| `lifecycle.py:1204` | `require_rollback_target_deployed` | `scoped_evidence` |
| `lifecycle.py:1239` | `rollback_selection` | `scoped_evidence` |
| `lifecycle.py:1363` | `_operation_index` | `classified_events` |
| `lifecycle.py:1381` | `read_event_rows` | `host_snapshot` |
| `lifecycle.py:1458` | `_release_plan` | `project_facts` |
| `lifecycle.py:1460` | `_release_plan` | `scoped_evidence` |
| `lifecycle.py:1564` | `_supersede_plan` | `scoped_evidence` |
| `lifecycle.py:1620` | `_supersede_scope_plan` | `scoped_evidence` |
| `lifecycle.py:1718` | `apply_release` | `read_event_rows` |
| `lifecycle.py:1742` | `apply_release` | `_operation_index` |
| `lifecycle.py:1749` | `apply_release` | `recorded_scope_tokens` |
| `lifecycle.py:1841` | `release_query` | `read_event_rows` |
| `lifecycle.py:1846` | `release_query` | `scoped_evidence` |
| `lifecycle.py:1897` | `apply_native` | `release_query` |
| `lifecycle.py:1903` | `apply_native` | `read_event_rows` |
| `lifecycle.py:1905` | `apply_native` | `project_facts` |
| `lifecycle.py:1935` | `apply_native` | `_operation_index` |
| `lifecycle.py:1945` | `recorded_scope_tokens` | `scoped_evidence` |
| `lifecycle.py:2248` | `main` | `offline_snapshot` |
| `lifecycle.py:2454` | `main` | `_operation_index` |
| `lifecycle.py:2502` | `main` | `offline_snapshot` |
| `lifecycle.py:2504` | `main` | `evidence_owed` |
| `lifecycle.py:2508` | `main` | `evidence_owed` |
| `lifecycle.py:2519` | `main` | `offline_snapshot` |
| `lifecycle.py:2521` | `main` | `project_facts` |
| `lifecycle_voids.py:72` | `event_binding` | `native_event` |
| `lifecycle_voids.py:735` | `context` | `native_event` |
| `lifecycle_voids.py:1012` | `audit` | `classified_events` |
| `render.py:82` | `render` | `project_facts` |
| `review_state.py:538` | `scopes_for` | `integration_evidence` |
| `review_workflow.py:2422` | `recorded_integrations` | `host_snapshot` |
| `review_workflow.py:2441` | `recorded_integrations` | `integration_evidence` |
| `web_lifecycle.py:80` | `task_state` | `project_facts` |
| `web_lifecycle.py:151` | `read` | `read_event_rows` |
| `web_lifecycle.py:158` | `read` | `native_event` |
| `web_lifecycle.py:219` | `apply` | `read_event_rows` |
| `web_lifecycle.py:221` | `apply` | `_operation_index` |
| `web_lifecycle.py:269` | `apply` | `read_event_rows` |
| `work.py:669` | `queue` | `project_facts` |
| `work.py:669` | `queue` | `integration_evidence` |
| `work.py:1078` | `execute` | `host_snapshot` |
| `work.py:1096` | `execute` | `read_event_rows` |
| `work.py:1098` | `execute` | `host_snapshot` |
| `work.py:1118` | `execute` | `read_event_rows` |
| `work.py:1120` | `execute` | `host_snapshot` |
