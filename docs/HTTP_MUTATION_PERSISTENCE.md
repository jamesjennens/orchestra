# HTTP mutation persistence

The following inventory counts call sites, including wrapper forwarding paths; one handler can cover several operations. It is a source inventory, not a promise of a transaction across JSON, SQLite and the canonical backend.

## Project registration and confirmation

The registration callback passes its `registered_by` mark into `create_project`, so one save writes the project record and its mark together and a kill or a failed save cannot leave a project on disk without the mark; a failed create save also takes the record and its membership back out of memory, so no later save can make an unaudited registration durable. The confirmation callback saves `confirmed_by`, `confirmed_at` and any backfilled `registered_by` before its success receipt, and puts them back — the earlier values, present or absent, exactly — if that save raises, so a failed save leaves no unaudited confirmation in memory for the next save of anything to make durable. A process killed after a committed receipt therefore replays only a durable complete effect. A kill before the effect save cannot replay success; a kill after the effect save but before the receipt leaves an in-progress identity that refuses the identical key for `idempotency_ttl` (24 hours; it is the module constant `IDEMPOTENCY_TTL_SECONDS`, not a setting). The caller (a superuser on both routes) gets out with a new key (or none), which is answered from the true state: a registration of a record already written answers 409 "already registered", and a confirmation of a record already confirmed answers 409 "This project needs no confirmation"; an exact retry of a committed request replays its own answer. Known and not closed: one path can raise after the state file has already been renamed into place — the warning print in `Store._write` just after the rename (`http_auth.py`, about line 888) — so a confirmation save that fails there leaves the confirmation on disk while memory is put back, and the next save writes memory and erases it. Ordinary route retry behavior is otherwise unchanged. The final route audit save follows the receipt, so a crash can still omit that audit entry; these fixes do not claim cross-store atomic audit persistence.

## The coordinator grant

`grant_coordinator` and `revoke_coordinator` save the state inside the service method, before the route writes its audit entry and its receipt. If the process dies between that save and the receipt, the grant (or its removal) is on disk with **no audit entry**, and the same idempotency key answers `409` "in progress" until its reservation expires. Every route of this inventory whose contract reads "Service method has an inner save" has that window; it is not changed here. A grant that ends because its agent left the project (`agents_update`, `project_agent_revoke`) is deleted and audited in the same save as the change that ended it.

## Call-site inventory

### Web merge-slot writes

| Handler | Operation | Effect and receipt locations |
| --- | --- | --- |
| `merge_slot_acquire` | `merge-slot.acquire` | Native project slot plus `.merge-context.json`; existing project guarded operation journal, service canonical result and HTTP receipt. |
| `merge_slot_release` | `merge-slot.release` | Native project slot; saved context retained; existing project guarded operation journal, service canonical result and HTTP receipt. |
| `merge_slot_force_release` | `merge-slot.force-release` | Native holder comparison and release under the project lock; existing guarded journal, service canonical result and HTTP receipt. |
| `merge_slot_create` | `merge-slot.create` | Native slot creation/shape repair, preserving a recorded holder; existing guarded journal, service canonical result and HTTP receipt. |

Each confirmed operation saves one committed web audit entry in the existing service JSON document before its HTTP receipt, with project, task (null when no context exists), authenticated actor, operation id, and holder before/after; force release also records its bounded reason. An exact retry deduplicates that entry by project/action/operation id and uses the original canonical result. A failure before any native write releases the reservation and is rejected; an interrupted native write keeps the guarded identity uncertain and requires inspection/reconciliation. No transaction across native data, the guarded journal, service JSON and receipt SQLite is claimed. The native result may precede its web audit after a crash; the stable identity permits a confirmed canonical replay to finish the audit without repeating the effect. No new store or backup family is added.

| Handler | Operation | Persistence contract |
| --- | --- | --- |
| `accounts_create` | `accounts.create` | Service method has an inner save; conditional paths need route-specific proof. |
| `account_password` | `accounts.password` | Service method has an inner save; conditional paths need route-specific proof. |
| `account_reset` | `accounts.reset` | Service method has an inner save; conditional paths need route-specific proof. |
| `account_disable` | `accounts.disable` | Service method has an inner save; conditional paths need route-specific proof. |
| `projects_create` | `projects.create` | Service method has an inner save; conditional paths need route-specific proof. |
| `_project_host_create` | `projects.host-create` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `_project_register` | `projects.create` | Complete JSON effect saved in callback before receipt; subprocess crash/restart covered. |
| `_refuse_unless_replay` | `projects.create`, `projects.confirm` or `projects.host-create` (the route the caller named) | Refusal or receipt replay; no new effect. |
| `project_confirm` | `projects.confirm` | Complete JSON effect saved in callback before receipt; subprocess crash/restart covered. |
| `project_update` | `projects.update` | Service method has an inner save; conditional paths need route-specific proof. |
| `_onboarding_write` | `projects.onboarding` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `project_archive` | `projects.archive` | Service method has an inner save; conditional paths need route-specific proof. |
| `member_set` | `members.set` | Service method has an inner save; conditional paths need route-specific proof. |
| `member_remove` | `members.remove` | Service method has an inner save; conditional paths need route-specific proof. |
| `credential_issue` | `credentials.issue` | Service method has an inner save; conditional paths need route-specific proof. |
| `credential_revoke` | `credentials.revoke` | Service method has an inner save; conditional paths need route-specific proof. |
| `agents_create` | `agents.create` | Service method has an inner save; conditional paths need route-specific proof. |
| `agents_update` | `agents.update` | Service method has an inner save; conditional paths need route-specific proof. |
| `agents_disable` | `agents.disable` | Service method has an inner save; conditional paths need route-specific proof. |
| `agents_enable` | `agents.enable` | Service method has an inner save; conditional paths need route-specific proof. |
| `agents_credential_issue` | `agents.credentials.issue` | Credential transaction: JSON save before receipt; absent-agent path refuses. |
| `agents_credential_revoke` | `agents.credentials.revoke` | Credential transaction: JSON save before receipt; absent-agent path refuses. |
| `_mutate_agent_credential` | `agents.credentials.issue` | Credential transaction: JSON save before receipt; absent-agent path refuses. |
| `_mutate_agent_credential` | `agents.credentials.revoke` | Credential transaction: JSON save before receipt; absent-agent path refuses. |
| `project_agent_revoke` | `agents.project.revoke` | Service method has an inner save; conditional paths need route-specific proof. |
| `project_agent_coordinator_grant` | `agents.coordinator.grant` | Service method has an inner save (`grant_coordinator`); a grant that is already there writes nothing. See "The coordinator grant" below. |
| `project_agent_coordinator_revoke` | `agents.coordinator.revoke` | Service method has an inner save (`revoke_coordinator`). See "The coordinator grant" below. |
| `tasks_create` | `tasks.create` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `tasks_update` | `tasks.update` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `_requirements_write` | the `requirements.*` operations (create, revise, accept, withdraw, supersede, finish, abandon, release, governance) | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `proposals_submit` | `proposals.submit` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `proposals_dispose` | `proposals.dispose` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `tasks_claim` | `tasks.claim` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `checkpoints_add` | `checkpoints.add` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `reviews_add` | `reviews.add` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |
| `feedback_add` | `feedback.add` | Canonical backend operation/journal; local receipt alone does not prove external atomicity. |

There are 32 direct `_mutate` call sites and two credential-wrapper call sites. Backend operations require their own journal and recovery evidence; an inner-save witness does not prove every conditional path.

## Regression evidence

`tests/test_project_receipt_durability.py` uses actual loopback HTTP, SQLite receipts, subprocess termination and a fresh service restart. Its canonical existence check uses the synthetic endpoint stub, not a live database. It covers both project effects before their JSON save, before receipt commit and after receipt commit, as well as failed confirmation persistence and conflicting retries. No private review evidence is required to run these probes.
