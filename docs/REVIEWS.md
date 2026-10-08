# Resume, contribution revisions and actionable reviews

These commands use `b` for your configured client prefix, including project and actor. Read `docs sessions` and `docs worker-guide` too. These records add workflow state; they do not set any of the six lifecycle facts.

## Returning worker: resume before choosing unrelated work

Keep the actor returned by your original registration. Reuse it on a later run of the same worker:

```sh
ssh HOST python3 /PATH/kit/worker.py --root /PATH/runtime --project PROJECT --actor SAVED_ACTOR resume
```

This records a durable resume event without allocating another actor or changing task ownership, then prints onboarding and your work queue with revision requests first. The emitted request ID can be reused after an uncertain response. With a local client, `b session resume` records the event and `b work --mine` retrieves the queue.

Do not call `start` again merely because the harness started a fresh process. Retain the actor in private worker configuration or handoff notes. Registered resume requires a registered actor. Existing legacy actors can still use `onboard` and `work --mine` under their original actor; do not invent a registration to replace them implicitly. Matching names and elapsed time never authorize takeover.

## Replacement worker: explicit handoff

For a new actor taking over an existing task, the current owner supplies this JSON (use an exact saved target actor, not a display name):

```json
{
  "schema_version": 1,
  "operation_id": "handoff-001",
  "task": "example-task",
  "from_actor": "OLD_ACTOR",
  "to_actor": "NEW_ACTOR",
  "reason": "Continue the recorded contribution in response to review.",
  "approval": "Pointer to the agreed handoff or owner authorization."
}
```

The **old owner** runs `b handoff example-task --file handoff.json`. The new worker cannot invoke this operation under its own actor to acquire someone else's work.

If the old worker has ended and cannot perform the handoff, an operator acting on explicit coordinator/project-owner authorization runs:

```sh
python3 admin.py --root /PATH/runtime handoff PROJECT --actor COORDINATOR_ACTOR --file handoff.json
```

This operator-only path records the initiator, approval evidence, old/new actor and reason. It is not a new permission system: service-account shell access remains trusted, and approval evidence must reflect actual authorization. The contributor endpoint does not accept an operator override.

Handoff checks the expected current owner, journals the operation, records native intent/completion comments and changes only the assignee. It does not reopen/close tasks, change review/lifecycle facts or transfer a merge slot. If interrupted, inspect and retry the exact payload under the same initiating actor/authority. A completed retry never reassigns a task that has subsequently moved again. Pending journals are included in backups and must be reconciled after restore. Reopen a closed task explicitly under existing authority before requesting revisions.

When a handoff request exists, its task, original owner and destination are stored in the request journal. A completed direct transfer settles only the matching pending request. If the native owner update succeeded but its response was lost, an authorized retry reconciles the durable receipt before applying the current-owner check; conflicting request or receipt identities fail closed. Queue reads validate the full request journal before state or owner filtering so malformed records remain visible, including empty filtered pages.

### Claim, handoff and the coordinator's take-over

**The one who has a task changes in three ways, and they are not the same.**

- A **claim** takes a task that is free and open. It never takes a task from somebody:
  bd's own claim refuses a held task ("issue already claimed by NAME") and one that is
  not open, on the host route (`update TASK --claim`) and over the web alike.
- A **handoff** moves a task from its owner to another worker and leaves a trail: the
  owner (or the receiver's request the owner accepts) runs `handoff TASK --file`, or an
  operator with the owner's or coordinator's authorization runs `admin.py handoff`; the
  operation is journalled and the task carries the intent and completion records.
- A **take-over by the coordinator** is the plain update on the host route,
  `update TASK --assignee ACTOR --status in_progress`. It only changes the row: no
  record of who gave what to whom, no check that the old holder agreed, nothing in the
  review chain. It is what a coordinator uses when a task must move and its holder is
  gone or the task was never properly held; it is not refused, and there is no web
  route for it. Prefer the handoff whenever the old holder or an operator can make one,
  and say in a comment on the task why the row was changed when it was not.

**A task that is in progress and has nobody.** A host update can leave a task
`in_progress` with no assignee. It cannot be claimed, by anybody, on either route: bd
answers "issue not claimable: status in_progress" (over the web: 409 "Task is not open
(it is in progress)"). The coordinator settles it on the host route: gives it to
somebody (`update TASK --assignee ACTOR`), or sets it open again (`update TASK --status
open`), after which the first claim takes it. (Measured on bd 1.2.2.)

## Structured contribution delivery

Read `b review example-task`. It returns `latest_comment_id`, the current contribution and unresolved review requests. Copy `latest_comment_id` into `previous` for every new operation; use null only for the first operation. Each new assertion has a new operation ID; uncertain retries retain the exact original payload/actor.

The assigned owner submits `b review example-task --file contribution.json`:

```json
{
  "schema_version": 1,
  "operation": "contribute",
  "operation_id": "contribution-001",
  "task": "example-task",
  "previous": null,
  "supersedes": null,
  "follows": null,
  "repository": "https://example.org/team/repository.git",
  "commit": "1111111111111111111111111111111111111111",
  "base_commit": "2222222222222222222222222222222222222222",
  "delivery": {
    "kind": "bundle",
    "path": "HOST:/absolute/review/contribution-v1.bundle",
    "sha256": "3333333333333333333333333333333333333333333333333333333333333333"
  },
  "summary": "Delivered change and verification pointers."
}
```

For remote branches, use `delivery: {"kind":"remote","remote":"git@example.org:team/repository.git","branch":"worker/task"}`. Replace synthetic hashes with exact full commit/base IDs and the actual bundle SHA-256. Remote access and bundle contents are not verified automatically: the reviewer must retrieve, verify and test them. The protocol rejects incomplete delivery fields, not false assertions.

Evidence is part of the delivery, not decoration. The `summary` and any linked evidence notes must state the platform the suite ran on and the exact commit tested, give the real pass/fail/skip counts, and enumerate every skipped test with the reason it skipped. A platform-specific behaviour whose test was skipped is unverified: `N passed, 1 skipped` is not evidence that the skipped behaviour works, so the contribution must say so rather than implying a full pass. If the suite could not be run on the deployment/target platform, state that explicitly and state why.

**Evidence checklist for a reviewable contribution:**

- Which platform (name and version/architecture) and which exact commit the suite ran on; if it was not the deployment/target platform, why not.
- The real pass/fail/skip counts from that run, not a summarized "all tests passed".
- Every skipped test, with the reason it was skipped (platform-specific, missing optional dependency, unavailable service, and so on).
- Whether any claimed behaviour rests on a skipped test; if it does, the claim is unverified until the test runs on the platform that exercises it.
- The exact commands and environment a reviewer needs to reproduce the run on the target platform.

For a revision that replaces a rejected or unintegrated contribution, set `previous` to the latest workflow comment and `supersedes` to the current contribution's `comment_id`.

For an **additive follow-on** to work that is already integrated, set `previous` to the latest workflow comment and `follows` to the current contribution's `comment_id`, with `supersedes: null` and `base_commit` set to the prior integration commit, or to an integration commit the project recorded after it (below). Before any native write, the protocol reads the **shared review-state projection** (the same projection `review TASK`, `brief` and `work` use; see below) and requires three independent things of the prior revision:

1. it must be **approved with no unresolved requests** — the raw append-only chain must project `awaiting-integration`;
2. the approving record's **normalised native author** must differ from **both** the prior contribution's author and the **assignee recorded when the approval was written** (`assignee_at_approval`, stamped server-side; a record with no usable snapshot — absent, or an explicit null written while the task was unassigned — falls back to the current assignee), so the contributor's own approval — or the assignee's, before or after a handoff — cannot open the gate; and
3. `integration.fact` must be `passed` with an `integration_commit` for the prior contribution's FULL commit, matched in **any** recorded lifecycle scope rather than only the task's current `lifecycle` scope.

`base_commit` must be that `integration_commit`, **or an integration commit this project recorded after it** (kittrial-5bb.148). Main usually moves before a follow-on is ready, so the later commit is the honest base. The endpoint decides this from the tracker alone:

* **recorded**: the `integration_commit` of a scope whose `integrated` fact is `passed`, on any task of the project, **recorded by a listed operator of the installation** (kittrial-5bb.155; see the limits below), read through the same trusted lifecycle reading as every other reader (a task whose labels and events disagree contributes nothing; a `source_commit` is not an integration commit);
* **after**: the event that recorded that fact was created later than the event that recorded the prior revision's integration. Both times are the tracker's own stamps, compared as times (a fraction of any length and an offset are read; a stamp with no zone is UTC). The tracker stamps whole seconds, and two events in the same second cannot be ordered (the export lists rows by id, not by time), so the same second is not "after": a later base recorded in the very second of the prior integration is refused, and an earlier one is never accepted. A time that is missing or cannot be read is not "after" either;
* **not reverted**: no operator revert record that the host journal confirms names that commit, on any task.

Anything else is refused with zero native writes, in one sentence that names the acceptable bases: `Contribution base_commit must be the prior integration commit <commit>, or an integration commit this project recorded after it (a passed integrated fact on any task, recorded by a listed operator, not reverted); the newest such commit is <commit>. <base> is not accepted: <why>` (or `; none is recorded yet`). The four reasons are that the project has no passed integrated fact naming it; that it was recorded before the prior integration or in the same second; that an operator revert names it; and that it was recorded, later, by somebody who is not a listed operator: `it was recorded as an integration commit by "NAME", who is not a listed operator of this installation. Integrations must be recorded by a listed operator for a later base to count: an operator adds the recorder (admin.py operators add) or records the integration`. A contributor cannot declare a base to be a descendant: there is no field for it.

**Who may record an integration that counts, and the two limits of that (kittrial-5bb.155).** Any actor can record an `integrated` fact on a task, including a task it made for the purpose. So that a contributor cannot make a commit of her choosing an acceptable base, a later base counts only when its fact was recorded under the name of a listed operator (`admin.py operators`). This is the interim rule until kittrial-5bb.106 settles who may write lifecycle facts, and it has two limits, stated plainly:

1. **The exact prior commit is only as strong as who may record an integrated fact.** A follow-on based on exactly the prior revision's integration commit is accepted whoever recorded that integration, as it has been since `follows` exists. A contributor whose contribution was approved by somebody else can record `integrated=passed` for it herself, at a commit of her choosing, and base the follow-on there. The approval by a different person cannot be forged this way; the reviewer of the follow-on still checks its base.
2. **Over SSH an actor name is a declared label.** "Recorded by a listed operator" means "recorded under an operator's name": a caller who reaches the endpoint and names itself as an operator passes. Over HTTP the actor is the authenticated account or agent, which a contributor cannot choose.

What a coordinator needs to know about the operator list here (kittrial-5bb.158):

* **The list is read when the follow-on is checked, not when the integration was recorded.** Listing a recorder makes everything already recorded under that name count as a later base from that moment; removing one makes all of it stop counting. Nothing is stamped on the fact itself.
* **Names match exactly: the whole name, with case.** `Ops` is not `ops`; `ops-2` is not `ops`; an entry that is the beginning of a name (a truncated entry) matches nobody. A recorder that is refused is named in the sentence as it was recorded, so the entry to add can be copied from it.
* **Being on the operator list grants the other operator powers too**: writing void and revert records, and whatever else `admin.py` checks the list for. Listing the coordinator so that its integrations count is a decision about those as well; the other way is to have a listed operator record the integrations.
* **With no list configured, no later base counts.** A follow-on based on exactly the prior integration commit is still accepted.
* **When a commit was recorded more than once**, a revert wins, then a listed operator's later recording, then an unlisted later one, then an earlier one. So a commit recorded before the prior integration and again later by somebody not listed is refused with the reason that names that recorder: the one that can be acted on.

**Over HTTP the refusal arrives whole** (kittrial-5bb.158). The web service hands a canonical refusal on up to 200 characters; cut there, this sentence lost its reason, the recorder's name and what an operator does. For this sentence, and only when the line is nothing but the kit's own words around hexadecimal commit ids and a recorder name of the plain shape (`review_workflow.BASE_REFUSAL`), the limit is 1500, and the sentence is the `message` of the 422 as well as its `detail`. A recorder's name is constrained twice: the lifecycle action accepts only letters, digits and `_.@/-` for an actor, and the sentence repeats a name only if it is such a name of at most 80 characters (otherwise it says "an actor"). A scope may name any string as its integration commit; a refusal that would repeat such a string is not this sentence and is cut at 200 as before.

**If `deployment.private.json` cannot be read**, the review action fails like every other action. Over SSH the endpoint's line names the file, as its busy line names the lock file it waited for; the caller there is somebody who was given the endpoint. Over the web service see docs/HTTP_DEPLOYMENT.md.

What an installation should do meanwhile: put its coordinator on the operator list (`admin.py operators add NAME --actor OPERATOR --reason "why the coordinator is listed"`) and have the coordinator record the integrations. Where integrations were recorded by somebody who is not listed, follow-ons based on exactly the prior integration commit keep working, and a later base is refused with the sentence above until an operator lists the recorder or records the integration.

**What this rule cannot know.** The endpoint has no Git repository. It cannot tell that a recorded integration commit really descends from the prior one, only that the project recorded it, later, as an integration. Two release lines in one project, or a fact recorded with a wrong commit, would pass. A commit of main that no lifecycle fact names (a fix-up made directly on main) is never accepted: base the follow-on on the nearest recorded integration commit. The reviewer verifies the base, as for any contribution.

Native comment authors are **attribution, not authentication**, on the SSH/endpoint path: a client can label itself freely, so rule 2 is a provenance check that refuses the obvious self-approval, not an identity system — an arbitrary third-party label still counts as a distinct author there. Attribution keys are compared case-folded with any `/`-namespace suffix dropped, so `Worker` and `worker/sub` fold to `worker`, and `team/alice` and `team/bob` both fold to `team` (an extra refusal); `worker@host`, `worker-2` and `worker.` stay distinct. The same normalisation applies on the HTTP path. The real approval authority is the HTTP `CAP_APPROVE` capability: on that path the native author is bound to the authenticated principal, and a worker credential can never approve at all. An awaiting-review, changes-requested or approved-but-unintegrated prior is refused with zero native writes, and so is a self-recorded `integrated=passed` lifecycle fact on its own: the lifecycle action only requires `payload.actor == request actor`, so the task owner can write that fact alone. The protocol cannot verify Git ancestry; `integration_commit` remains an assertion the reviewer verifies, exactly like `commit` and `base_commit`.

Integration is decided per scope (ANY-scope, any-pass-wins), so recording a newer lifecycle scope for other work does not make a genuinely integrated prior un-followable. That rule fixes base selection when one commit has several passing scopes: the accepted `base_commit` is the **newest passing** scope's `integration_commit`, so once a second scope records `integrated=passed` for the same commit the older scope's `integration_commit` is **refused** even though that pass is not retracted; conversely a pass recorded earlier under one scope is not undone by a later `integrated=failed` under another scope, and that earlier pass's `integration_commit` is still **accepted**, unless that exact integration commit has been reverted by an explicit revert record (see "Reverting integrated work" below). `integration.newest_fact` reports the newest matching value so this is auditable. With no trusted scoped evidence at all, `integration.fact` is `unknown` and the gate fails closed: there is deliberately no separate "no scoped evidence" fallback branch.

`follows` asserts that the new change builds on the prior revision rather than retracting it: what stays visible for the prior contribution is its **record, commit and relation** in `review TASK`/`brief` under `prior_contributions`, where each replaced revision carries the `relation` (`follows` or `supersedes`) that replaced it as current. Every prior entry also carries the same additive `integration` block as the current contribution, computed from that entry's own FULL commit over the same scopes, so the read says whether the replaced revision is integrated. Its scoped lifecycle facts are not re-scoped: they are valid only under their own lifecycle scope, stay recorded in lifecycle history and `show`/`history`, and disappear from the top-level `lifecycle`/`lifecycle_scope` of `brief`/`work` once the follow-on records its own scope (the per-scope `review.integration` view still sees them). `brief` embeds only a bounded recent slice of `prior_contributions` (a total count plus the last few entries carrying comment_id, commit, relation, timestamp and that `integration` block); read `review TASK` for the complete list. `follows` and `supersedes` are mutually exclusive, and the field is optional so payloads and chains written before it existed keep validating; a first contribution sets both to null. Record the exact new branch/bundle path, commit, base and checksum in every case, even when only the bundle filename changed. Old delivery records remain in history. A new contribution does not resolve feedback automatically.

## The reviewer's part in a capability proposal (kittrial-5bb.179)

A contribution that adds or changes something a user or an agent can rely on carries a
capability **proposal payload file** in its delivery, named in the contribution summary
(conventionally `capability-proposals/<key>.json`, one file per record). The worker does
**not** run `capability propose`: a record written from a lane is in every release's check
set while its work is still in review, so it is checked against main, its pointers are
missing, and it reads `drifted`. The reviewer reads the payload with the change, and the
coordinator writes and accepts it at release, after integration, with the release as
evidence. The reviewer's part is:

1. **Read the named payload files with the diff.** A delivery that claims a new or changed
   capability and names no payload file is incomplete; ask for it rather than approving.
2. **Judge the claim, not the pointers.** The check proves **location only**: that the
   `code`, `tests` and `anchors` pointers resolve at the commit the check runs at. It never
   proves that the sentence is true. The reviewer judges the meaning — whether the summary
   says what the change actually does, and whether the capability is worth claiming at all.
3. **Check the pointers are the right ones.** They must exist at the delivered commit, never
   on a branch still under review, and `tests` must include a test that fails when the
   capability breaks. A regression fails that test; the check still passes, because the check
   proves location only. Approval rests on the change plus
   that test, not on a green check.
4. **Check the payload's field set.** It is exactly `key`, `name`, `aliases`, `summary`,
   `requirements`, `anchors`, `code`, `tests`, `owner`, `tags`, plus `schema_version`,
   `operation_id` and (for a revision) `revision` and `expected_sha256`. Any other field is
   refused, so a payload carrying a "check" or a commit is malformed: the check records the
   commit it was run at, and the coordinator writes the record only after integration.
5. **A removal or weakening is not a contributor's write.** Retiring is
   `admin.py capability-retire`, operator-only, and it needs a successor key. The endpoint
   accepts a contributor's `capability propose` or `capability revise` payload, but the process
   does not use a live write from a lane: the worker carries the payload as a file in the
   delivery, the reviewer judges it, and the coordinator writes it at release. This kit has no
   demotion. The delivery must
   therefore *state* that the entry is to be revised or retired and carry the revised
   payload file or the successor key, for the coordinator to carry out. A delivery that
   silently drops a capability is a change to the index's promise and belongs in the review.
6. **A failing draft does not hold up a release; a failing accepted entry does.** When the
   release is cut, the release is unfinished until `capability-verify` passes for every
   accepted entry; a failing draft is listed in the release record with its key, the missing
   pointers and an owner. Reviewers should not demand that a draft's pointers resolve before
   its work is integrated, and should not accept a draft as if it settled the claim.

## Rollback compatibility of the approve snapshot

An `approve` record may carry an additive `assignee_at_approval` field: the task assignee as it was when the approval was written, stamped server-side so a caller-supplied value is overwritten and the field cannot be forged to open the follow-on gate. It fixes two follow-on-gate defects: an approval that was an assignee self-approval stays refused after the task is handed off, and a reviewer who genuinely approved before later becoming the assignee can still follow on. A record with no usable snapshot — absent, because it was written before the field existed, or an explicit null written while the task was unassigned — falls back to the current assignee, which is the pre-snapshot, fail-closed reading.

**The field is additive for readers, not for writers, and it is not rollback-safe.** The reader in this kit accepts approve records with or without the field, but a kit built before the field existed validates the approve field set *exactly*, so reading a task whose approve record carries `assignee_at_approval` fails the whole chain with:

```text
Malformed contribution-review history; operator reconciliation required
```

Once any approve record with the snapshot has been written, rolling the deployment back to a kit that predates the field therefore leaves every affected task unreadable by `review`/`brief`/`work`. To recover, an operator voids the affected approve comment on the coordination host (`python3 admin.py --root /PATH/runtime void-record ...`) or restores the tolerant reader; the rest of the chain is then readable again. Decide the rollback position before deploying: if a rollback must stay possible, do not write the field yet.

The fully rollback-safe sequence is a staged release: ship a **tolerant reader** first (accept and ignore the optional field, write nothing new), deploy it, and only then ship the **writer** and the gate that reads the snapshot; rolling the writer back to the tolerant reader is then safe. That staging needs two integration cycles, so this change does not deliver it. Deriving the approval-time assignee from native tracker events instead of the payload was also rejected: assignment changes are not present in `bd export --all`, and the Dolt commit timestamps in `bd history` are batched and can be rewritten by a backup restore (kittrial-5bb.44's own 2026-09-27 edits carry 2026-09-29 commit timestamps), so they cannot be ordered reliably against comment timestamps. Until the staged release lands, the snapshot is a deliberate forward-only schema addition documented here.

## Reverting integrated work

A revert of already-integrated work is an **explicit, audited record**, not a newer `integrated=failed` lifecycle fact. Only a coordinator/operator may record one, and only on the coordination host. The record is its own reserved machine comment (`Kind: integration-revert-v1`), so it is never part of the `contribution-review-v1` chain and never moves that chain's `previous` pointer:

```json
{
  "schema_version": 1,
  "operation": "revert-record",
  "operation_id": "revert-6f1c9b2ad4e84730",
  "task": "example-task",
  "contribution": "CONTRIBUTION_COMMENT_ID",
  "integration_commit": "INTEGRATION_COMMIT_BEING_REVERTED",
  "revert_commit": "REVERT_COMMIT",
  "reason": "Why the integration is being reverted.",
  "evidence": ["commit:REVERT_COMMIT", "ci:job-1234"],
  "operator": "OPERATOR_ACTOR"
}
```

The `operation_id` must be **unpredictable**: a fresh random value per revert (a UUID or 16 random bytes in hex), never a counter such as `revert-001`. Retrying the *exact* same operation id with the same actor and payload is how an interrupted write is reconciled — the earlier native comment is **adopted** and journaled instead of a second record being written — so a predictable id lets a pre-planted same-author, same-payload comment be adopted by the real write; the intended record is then never written and the planted comment is what the host journals. `evidence` is optional (`revert_commit` is the other allowed form of evidence); every other field is required, `contribution` must name the current or a prior contribution of the task, and `integration_commit` must be the integration commit the shared projection currently reports as that contribution's passing integration — a revert cannot be a silent no-op and cannot be recorded for work that is not integrated. Write one with the operator CLI, which binds the deployment allowlist and the native comment author to the issuing operator:

```text
python3 admin.py --root /PATH/runtime revert-record PROJECT --actor OPERATOR --file revert.json
```

Authorization follows the void-record rule — the record is refused over the contributor review transport (including `review TASK --file`), and a record whose native author is not on the server-side operator allowlist is ignored — but on the SSH/endpoint path the stored native author is the caller's **self-declared** actor, so the author binding alone is not proof that the host issued the record. `revert-record` therefore also writes a **host-issued journal entry**, one JSON per revert, and the reader honours a native revert comment **only** when that entry is present (kittrial-5bb.52 review item `forged-pre-deploy-reverts`):

* the entry lives at `<project>/.integration-reverts/<sha256>.json`, where the name and the entry's `sha256` field are the SHA-256 of the exact canonical payload the native comment carries. It binds the revert identity — task, contribution, integration commit, revert commit, operation id, operator and issuing actor — to the native comment id it was written for;
* the write happens inside the same `.coordination.lock` critical section as the native comment, so only a process on the coordination host can produce it. A contributor who can post a comment (through the endpoint, or through a kit that predates the reserved-prefix guard during a rollback window) cannot write the journal, so such a comment is **ignored**: no revert, no closed follows gate;
* the reader is fail-closed about the journal. A missing, unreadable, symlinked or malformed journal directory means **no revert is trusted at all**, and `apply_revert` refuses before its native write when it cannot persist the entry. A crash between the two writes leaves the native comment inert; re-running the same `revert-record` operation id persists the missing entry and reconciles instead of writing a second comment;
* the journal is part of the coordination sidecar: `admin.py backup` collects it with symlink refusal, `validate_coordination_files` checks the record shape and the `<64 hex>.json` path/hash binding, and `restore-new` validates it before the first write and restores it under `.integration-reverts/`. The pre-fix exposure the P1 probe showed — a forged operator-authored comment becoming a real revert after a roll-forward, closable only by a new commit — is fixed by that entry, not by the reserved-prefix guard alone.

**Deployment order.** The journal path must be whitelisted, backed up and restorable on a host **before** the first revert is recorded there: a host that cannot write and restore `<project>/.integration-reverts/` must not record one, and `revert-record` refuses before its native write when the path is unusable or not writable. **Two-release staging is not required**: the revert record has its own reserved `Kind:` prefix and the journal is a new sidecar path, so an older reader ignores both and this one revision can be deployed, backed up and then used to record reverts. What a rollback does *not* give you is journal protection, so read the next paragraph before rolling back.

**Rollback and restore limits (measured).** Rolling the deployment back to a kit that predates this change (`8e1f9ec`) gets no protection for the revert journal, and a backup taken while rolled back is not interchangeable with one taken forward:

* a rolled-back kit writes no journal entry and honours no revert, so it keeps reporting a reverted contribution as integrated with no warning — fail-open for reverts. Nothing is lost (the native revert comment and the journal files are still on disk), but only a kit with this reader shows the revert;
* the **old** kit's `restore-new` of a **new**-kit backup fails for the whole project with `ValueError: Invalid coordination backup path`, because the sidecar now names `.integration-reverts/<hash>.json` entries its validator refuses. A restore taken during a rollback window must therefore be run with the **new kit's `admin.py restore-new`**, which was verified to work while the deployment serves the old kit;
* a backup taken **by the old kit** while the deployment is rolled back **drops the journal** (its sidecar carries zero journal entries). Restoring that backup after roll-forward leaves every reverted task `integrated`/`passed` with `reverted=False`, surfaced only as an "Integration revert record(s) ignored (malformed, not host-issued)" warning. So keep the pre-rollback backup/sidecar and restore that, **or take a fresh backup with the new kit after roll-forward** before relying on a restore.

The roll-forward direction is safe: the new kit reads the old native records unchanged, and a revert comment with no journal entry stays inert until it is re-recorded with `revert-record` (which journals it) or retracted.

Effect on the projection (`review TASK`, `brief`, `work` and their HTTP equivalents all agree): the named integration commit stops counting as a passing scope for that contribution. `integration.fact` is `reverted` when nothing passes afterwards; `matches_contribution` stays true and `scope` reports the reverted scope, so the record is still visible. The follows gate therefore refuses that base with its existing "not integrated" refusal and writes nothing.

**A revert is never silent.** `integration.reverted` is true whenever a host-issued revert removes an integration commit this contribution recorded, and `integration.reverted_commits` (bounded, with `reverted_total`) names the commits it removed. That flag is deliberately **not** the same question as `fact`: a revert stays visible when another, older passing scope still governs (`fact='passed'` with the surviving scope's commit) and when the newest matching scope records a failure (`fact='failed'`). Every such contribution carries a `reverted` entry in `integration_disagreements` and a warning naming the reverted commit(s), the reported fact/scope and the newest scope, on `review TASK`, `brief` and `work` rows (`brief` and `work` also carry it as a warning). An empty `reverted:false` with no warning was the review's S2/S3 defect. The compact `brief` read embeds only a bounded recent slice of the `reverts` list (`reverts_total` and `reverts_more` carry the rest); read `review TASK` for the complete list, exactly as for `prior_contributions`.

**Re-integration, and the deviation from the task wording.** The task wording said "an integrated=passed fact recorded after the revert re-integrates it". That is inaccurate as written: a passing evidence scope can **predate** the revert, and there is no reliable shared ordering between a lifecycle event and a native comment (see kittrial-5bb.44), so "after" cannot be established. The rule this kit implements — deliberately, and stated here as a documented deviation — is that the revert removes exactly ONE named `integration_commit`: a later `integrated=passed` recording a **DIFFERENT** `integration_commit` re-integrates the work, while a pass under the **same** commit (including a rollback-then-redeploy of the same commit) does **not** clear the revert. Clearing a same-commit revert requires the explicit retraction path below. Reverting the reverted commit again is refused as a duplicate, and an exact retry of the same revert operation is idempotent.

**Retraction.** A mistaken revert is undone with the existing audited operator void route, which accepts a target whose `target_kind` is `integration-revert`:

```text
python3 admin.py --root /PATH/runtime void-record PROJECT --actor OPERATOR --file void.json
```

The void must preserve the revert comment's exact bytes, must follow it in native order, must be authorized by the deployment allowlist exactly like any other void, and the target must be a **host-issued** revert (the journal entry is required) — so a void cannot retract a forged revert, and there is nothing to retract in a bare comment. The retraction is host-issued too: `void-record` writes a second journal entry (`kind: integration-revert-retraction`) binding the void comment to the reverted comment id, after opening the journal directory **before** its native write. The reader excludes a revert only when that host entry exists **and** the native void it names is an applied, authorized void; an unbacked retraction attempt is surfaced in the ignored-records warning and the revert **stands**, because dropping it would fail open. A retraction does not invalidate an earlier approval (it is not a repair of the review chain). An interrupted retraction is completed by re-running the same void operation id.

**Revoking an operator.** `admin.py operators remove ACTOR --confirm-revoke` makes every record that actor authored stop applying. That already covered voids; it also affects the host-issued revert journal in **two opposite directions**, and the interactive refusal (and the warning text) now reports both, exactly as it does for voids: the integration revert records the actor issued **stop applying**, while the retractions the actor issued **also stop applying**, which **re-applies** the reverts they retracted. A revert that is already retracted by another operator is reported as neither. The report is computed as the difference between the reverts honoured under the **live deployment allowlist** and under that allowlist **without** the actor, so it uses the deployment's real authority instead of the revoked actor alone (which would hide another operator's retraction). Each list in the refusal is capped at five entries with `(+N more)`; `operators remove ACTOR --all-revoked` names every affected entry instead (kittrial-5bb.92). Re-adding the operator restores every one of them: nothing is deleted.

**Rollback compatibility of the revert record.** The revert record itself is additive: it uses its own reserved `Kind:` prefix, no existing record schema gains a field, and `review_workflow.records()` matches only `Kind: contribution-review-v1`. A kit built before the revert record existed therefore reads the task completely normally and simply does not know the revert happened — no read fails, no chain is refused, and no operator reconciliation is required. The documented limit is that the older kit is **fail-open**: it keeps reporting the contribution as integrated until it is upgraded (or the kit is pinned forward). This differs from the approve snapshot above, which is forward-only because it added a field to an existing record. With the journal, the same limit applies to the **new** kit reading records written by an old host: a revert comment with no journal entry is ignored, so a revert recorded before this change (or on a host that could not journal it) does not apply and must be re-recorded with `revert-record`, which will journal it (or retracted with a void once journaled). The backup/restore consequences of an actual rollback are in **Rollback and restore limits** above; they are part of this limit, not a separate hazard.

**Known limits (not fixed here).** `lifecycle.integrated` and `lifecycle_matches_contribution` keep their existing meaning: after a revert they can still report `integrated=passed` and `true`, because the scoped lifecycle fact is unchanged — the whole-task lifecycle read is NOT revert-aware. Read `integration.fact`/`integration.reverted` (and the revert warning) for the integration answer. The office WEB UI does not render the warnings list yet, so a reverted task looks unchanged there; that is kittrial-5bb.20 slice 2's work, recorded here so it is not mistaken for a fix. Finally, the journal is a host-side store: a restore that brings back the native comment but not its entry (or the reverse) surfaces the mismatch in the ignored-records warning instead of guessing.

The owner decision on kittrial-5bb.32 (comment 01a0eea4) is to **keep any-pass-wins**. That decision is not changed by this record: a revert subtracts exactly one named integration commit, and a newer `integrated=failed` under a different scope still does not undo an older pass. Because any-pass-wins can leave the newest matching scope disagreeing with the reported fact, that conflict is **warned about** rather than silently resolved: every read whose `integration.newest_fact` differs from `integration.fact` — for the current contribution or for any `prior_contributions` entry — carries the warning text for both facts and both scopes, plus the machine-readable `integration_disagreements` entries (`work` rows carry them as `integration_disagreements`/`integration_warnings`, and `brief` reports the warning text in its top-level `warnings`). Invalid, unauthorized, unhosted or conflicting revert records are surfaced the same way, in an "Integration revert record(s) ignored" warning, instead of being dropped silently.

## Request changes and respond

A reviewer records requests against the **current contribution comment ID**:

```json
{
  "schema_version": 1,
  "operation": "request-changes",
  "operation_id": "review-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "items": [{"id":"fix-1","text":"Describe the required correction and acceptance."}]
}
```

Submit with the same `review TASK --file` command. This removes a legacy `review-ready` label and projects `changes-requested`, independently of any older checkpoint. The task must be open. Pending items remain visible across subsequent checkpoints and contribution revisions.

After publishing a corrected contribution, its assigned owner explicitly responds:

```json
{
  "schema_version": 1,
  "operation": "respond",
  "operation_id": "response-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "resolutions": [{"request":"REVIEW_REQUEST_COMMENT_ID","item":"fix-1","reason":"What changed or why the request is superseded.","evidence":"Exact commit/test/decision evidence."}]
}
```

Responses are owner assertions, not reviewer acceptance. When all requests have explicit responses, the contribution returns to `awaiting-review`. A reviewer may accept using an `approve` operation with the same common fields plus `contribution` and a `summary`. Approval requires no unresolved requests and refers to the current contribution. It projects `awaiting-integration`; it does not set reviewed/integrated/deployed lifecycle facts. A new contribution requires review again. Review participation remains subject to project policy; actor names are attribution, not verified authority.

**Known limit: variation selectors are refused in every review text field.** Write-path text validation refuses control, bidi, zero-width and tag characters, and also the variation-selector block `U+E0100`-`U+E01EF`. That last range is what a CJK ideograph carries when it is written with an ideographic variation sequence (for example `U+845B` + `U+E0100`), so such an ideograph is refused in **every** review text field - a `summary`, a `reason`, a request item `text`, a resolution `reason`/`evidence` - and it applies to the **legacy** operations too, `contribute` and `approve` included, with `review_workflow_writes` off. This is deliberate (the range is indistinguishable from a hidden tag character at the byte level and the task asked for it), but it is a real limit: an author who needs that variation selector must write the base ideograph without it. The rule is enforced on the write path only, so a record written before it existed still reads.

**Over HTTP the review payload is exact.** `POST /v1/projects/{project}/tasks/{task}/reviews` accepts only the canonical fields for the operation, plus the transport's `task_id`/`actor`, the web page's presentation fields (`contribution_revision`, `contribution_commit`) and, on a contribution, the legacy `bundle_sha256`/`branch`. A key whose **value is null** is treated as **absent**: it is neither forwarded nor counted against the operation's field set, so a client that sends every optional field as null - the released `http_client.Client` at `cbf6d01` and `b0a4fbd` puts one fixed 16-key union in every body - gets the operation default instead of a refusal (kittrial-5bb.110 items 1 and 2). `previous` on a `recommend` is accepted and ignored: a recommendation is a record beside the chain and carries none, but the released clients send the key for every operation. An unknown **non-null** key, or a canonical field on the wrong operation (a top-level `severity`, a `disposition` on a `request-changes`), is refused with **422** instead of being dropped and answered 201; an `operation` sent as a list or object is **422**, not 500; an unsupported operation is refused with a fixed sentence that never echoes the caller's value; and the unknown-field refusal names at most five fields, echoes a name only when it is a plain identifier (anything else reads `<non-identifier name>`) and reports the rest as `(+N more)` (kittrial-5bb.110 item 4).

`request-changes` also accepts an **optional** `summary` (1..1200 characters). It used to reject the field, so a reviewer's reasoning had to sit in a separate unlinked comment. The summary is stored on the request record and returned in structured form: every `pending_requests`/`note_requests` entry carries it as `summary`, so `review TASK` and `brief` show why the changes were asked for without reading raw comments:

```json
{
  "schema_version": 1,
  "operation": "request-changes",
  "operation_id": "review-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "summary": "Why these corrections are required, kept with the request.",
  "items": [{"id":"fix-1","text":"Describe the required correction and acceptance."}]
}
```

### Item severity: blocking or note

An item may carry `severity`:

```json
{"id":"fix-1","text":"Describe the required correction.","severity":"blocking"}
{"id":"nit-1","text":"Optional readability suggestion.","severity":"note"}
```

`blocking` is the default when the field is absent, so every item written before severity existed keeps blocking approval. Only a **blocking** item holds up approval and appears in `pending_requests`; a `note` is reported separately in `note_requests` and never blocks. Both lists carry the same per-item fields (`request`, `item`, `text`, `severity`, `summary`, `contribution`, `author`, `timestamp`); `summary` is the requesting review's optional summary (`null` when the request carried none), so `review TASK` returns the reviewer's reasoning in structured form instead of leaving it only in history and raw comments. At most 20 unresolved items of either kind are allowed.

A reviewer can also **resolve or downgrade their own item** without the task owner, using `resolve-item`. The operation names the request record and the item, and requires the caller's native author to be that item's requester; anyone else is refused (the task owner uses `respond`):

```json
{
  "schema_version": 1,
  "operation": "resolve-item",
  "operation_id": "resolve-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "request": "REQUEST_CHANGES_COMMENT_ID",
  "item": "fix-1",
  "reason": "Why the request no longer applies.",
  "disposition": "resolved"
}
```

`disposition` is optional and is `resolved` (default) or `note`: `note` downgrades a blocking item to a non-blocking note instead of removing it. The record stays in the chain either way.

**The requester match is a normalised attribution key, not verified identity.** `resolve-item` (and only `resolve-item`) compares the caller's **self-declared** native author to the item's requester with the same normalisation the follow-on gate uses: case-folded with any `/`-namespace suffix dropped. `RITA`, `rita/anything` and `rita` are therefore all accepted as the requester, and the stored record then reads as the reviewer's own resolution of their own item. Nothing verifies who actually ran the command: this is the self-declared-actor limit tracked as kittrial-5bb.106, and it is a deliberate narrowing of the review workflow, not an identity system. Every other authority decision in this document (withdraw, decline, request-review, coordinator actions) compares an actor label the caller supplied just as directly.

**`approve` itself does not refuse the contribution's author.** Any actor the transport accepts may record an `approve` on the current contribution, including its author and the assignee: the projection only requires that no blocking item is unresolved. What refuses a self-approval is the **additive `follows` gate** below, which is evaluated only when a later contribution declares `follows`. A first contribution, a `supersedes` revision and integration are not gated on the approval's author, and the HTTP path's `CAP_APPROVE` capability is the only route where approval authority is authenticated.

### Withdrawing or superseding a contribution

A contribution that is no longer to be reviewed can be closed out by its **author** or by a **coordinator** (an actor on the server-side operator allowlist) with `withdraw`:

```json
{
  "schema_version": 1,
  "operation": "withdraw",
  "operation_id": "withdraw-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "reason": "Superseded by a re-scope; a different change is needed.",
  "disposition": "withdrawn"
}
```

`disposition` is optional and is `withdrawn` (default) or `superseded`. The operation closes the contribution's review requests and `review TASK` then reports `review_state` `withdrawn` or `superseded` plus a `withdrawal` block naming the record, reason, author and timestamp. Approval is refused on a withdrawn revision; the only way back into review is a new contribution, which clears the withdrawal. The record stays in history for audit. Over HTTP a null optional field is treated as absent, so a client that sends `"disposition": null` gets the `withdrawn` default; the canonical writer itself **refuses** an explicit null `disposition` (null is neither `withdrawn`/`superseded` nor `resolved`/`note`), and a record an older kit already stored with a null disposition reads as the operation default - a defined state, never `None` (kittrial-5bb.110 item 2). For `resolve-item` that default is **`resolved`** (`review_workflow.RESOLVE_DEFAULT`), whatever the item's severity: a stored null resolve disposition removes a `blocking` item from `pending_requests` and a `note` item from `note_requests`, and never downgrades or re-reads one as the other.

Three rules keep a withdraw from becoming a way around review:

* **It carries the unresolved items forward.** A withdraw does **not** clear the contribution's `pending_requests`/`note_requests`; the task's NEXT contribution carries them, exactly as a `supersede` revision does. Without that, withdrawing and re-contributing at the same commit would read `awaiting-review` with no pending items and an unrelated `approve` would be accepted - a second route around the blocking items that left no per-item answer. The items stay visible on the withdrawn revision too (`review_state` is still `withdrawn`/`superseded`, with the items listed), and after the new contribution the state is `changes-requested` until the owner responds.
* **A closed, withdrawn task with a blocking item stays in `work` for the same reason**, and its **one** way out of work is to deliver a new revision: `contribute` again on the closed task (a new revision clears the withdrawal and re-opens review). `work` keeps listing it so the unresolved item is not lost when the task is closed, but it is not an actionable reviewer entry: a closed task takes no `request-changes`, `request-review` or `decline-review` until it is explicitly reopened, and a `respond` on the withdrawn revision cannot settle an item that is already final. This is the only exit; do not read the queue entry as an invitation to review.
* **An integrated contribution cannot be withdrawn.** Once the contribution reads `integrated` (a trusted scope records `integrated=passed` for its FULL commit) the withdraw is refused before any write. Otherwise the read showed `withdrawn` with an integration block still saying `passed` and matching, the task left `work --state integrated`, and a following contribution was refused as not approved, so the task could no longer take a second delivery. Withdrawing an approved-but-unintegrated revision is still allowed.
* **A second withdraw on the same contribution is refused.** The disposition is decided once. On the read path a chain that already holds two withdraw records stays readable: the **first** disposition stands and the ignored record is named in a warning, so a later record can never flip `withdrawn` to `superseded` (or back).

**Withdrawal does not stop integration being recorded.** The integration fact is scoped lifecycle evidence, not part of the review chain, so a `integrated=passed` recorded after a withdraw is still recorded. `review TASK`, `brief` and `work` then report the withdrawal **with a warning** (`integration_disagreements` kind `withdrawn-integration-passed`, and a matching warning line naming the withdraw record, the commit and the scope), and the operator decides whether the integration stands and the task needs a new revision or the integration commit is removed with `admin.py revert-record`. A withdrawn contribution with a passing integration block is never shown without that warning.

**Closing a task clears its awaiting-review queue entry.** A closed task is no longer offered to a reviewer when its review state is `awaiting-review` (or the legacy `review-ready` label) - that was the queue behaviour the pilot reported as contributions "awaiting review for ever" on closed tasks. The record stays readable through `review TASK`, and `brief`/`work` still surface a closed task with outstanding `changes-requested`, an approved-but-unintegrated revision, or a malformed history, because closure is not acceptance.

### First-class review requests

Independent review is requested on the contribution itself instead of a hand-made review-assignment task. The current task owner (or a coordinator) records `request-review`, naming the reviewer:

```json
{
  "schema_version": 1,
  "operation": "request-review",
  "operation_id": "request-review-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "reviewer": "REVIEWER_ACTOR",
  "summary": "Optional pointer to what needs independent review."
}
```

The optional `summary` is bounded to 1200 characters. An open request appears in `review TASK` as `pending_review_requests` and puts the task in the **named reviewer's** `work --mine` queue even though they are not the assignee; it is not in any other actor's queue. A reviewer named for an already-open request is refused as a duplicate, a request for a revision a new contribution replaced is dropped with it, and the self-approval refusal is unchanged: `request-review` refuses to name the contribution's own author as the reviewer, and the additive follow-on gate below still refuses an approval by the contribution author or the assignee. The reviewer field is an actor identity, so `@` and `/` are accepted (for example `alice@host`, `team/alice`); the narrower comment-id shape used elsewhere is not applied to a person.

**A queue row that is a review request says so.** Two additive `work` item fields: `review_requests` lists the open requests naming the **calling** actor (request id, reviewer, contribution, summary, author, timestamp), and `review_request` is true when the row is in that caller's queue because they were named rather than because they own the task. `work --help` lists both under `item_fields`.

**A request closes four ways.** The named reviewer's own `approve` or `request-changes` on the same contribution closes it (as before); **any** `approve` on the current contribution closes every request for it, so a request cannot stay open after another reviewer approves; closing the native task closes them too (`projection` reads the task status, so `review TASK`, `brief` and `work` all report no open request for a closed task, and a new `request-review` on a closed task is refused with the "Reopen the closed task explicitly" rule); and the named reviewer can **decline** their own request with a new gated operation:

```json
{
  "schema_version": 1,
  "operation": "decline-review",
  "operation_id": "decline-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "request": "REQUEST_REVIEW_COMMENT_ID",
  "reason": "Out of my area; ask someone else."
}
```

Only the named reviewer may decline (normalised attribution key); anyone else is refused, and the declined request is returned additively as `declined_review_requests` (request, reviewer, reason, author, timestamp) so the closure is not silent.

**Open requests are capped per requester.** One actor may hold at most **10 open review requests at a time, counted across every task in the project**: an 11th is refused before any write, naming the requester's current count and the cap. That is the abuse path where one actor put 25 tasks into another's `work --mine` queue in 71 seconds. The count reads every task's chain and ignores a chain it cannot parse, exactly as `work` treats an unreadable row; a declined, closed or otherwise settled request frees a slot.

### A reviewer's recommendation

A reviewer who finds nothing to change, and who may not approve (a reviewing agent never may), records a **recommendation**. It is advice to whoever approves. It is never an approval.

```json
{
  "schema_version": 1,
  "operation": "recommend",
  "operation_id": "recommend-001",
  "task": "example-task",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "commit": "THE_CONTRIBUTION_COMMIT",
  "verdict": "approve",
  "summary": "What was checked and what was not.",
  "items": [{"id": "naming", "text": "An optional note for the approver."}]
}
```

Sent with `review TASK --file payload.json`, like the other review operations. It takes no `previous`.

**What it is.**

* A record of its own kind (`Kind: review-recommendation-v1`), stored as a comment on the task **beside** the review chain. It is not a link in the chain.
* It names the contribution's comment id and the contribution's commit. Both must be the task's current ones.
* `verdict` is `approve` only. A reviewer who wants changes uses `request-changes`.
* `summary` is 1 to 1200 characters. `items` is 0 to 20 notes, each with an `id` and a `text` of at most 1000 characters. The payload is at most 24 KB. `review --help` lists these.
* Text with control or format characters (a bidi override, a zero-width character) is refused.

**What it does not do.**

* It does not change the review state, so the task stays `awaiting-review`.
* It does not move `latest_comment_id`, so an owner's `approve` with the contribution as `previous` is not stale.
* It does not make approval allowed and does not resolve or create an item. Nothing in the approval rules reads it.

**Who may write one.** Anyone who may write on the task, except the contribution's author and the task's assignee (compared with the name rule of the follow-on gate, so an agent of the same owner under another session id is the same author).

* **The author rule is applied when a recommendation is written and again every time it is read.** A record by the contribution's author is never shown, however it reached the task. That is what stops a forged or natively written self-recommendation from displaying.
* **The assignee rule is applied when it is written and is not re-checked on read.** The reader sees only who is assigned now. If the task is later reassigned to someone who had already recommended it, their honest recommendation must not disappear.

**Independence by person, over HTTP only.** The rule above compares actor names. A person and their agent have different names, so over SSH the kit cannot tell that a recommender is the owner of the agent that delivered the work. The web service knows who owns each agent, and it also refuses (403) a recommendation from:

* the person who owns the agent that authored the contribution or is the task's assignee;
* another agent owned by the same person as the author or the assignee.

The web service applies the same rule when it reads, in the brief and in the queue alike: a record by the contribution author's own person is left out of the brief's `recommendations` and of a row's `recommended_by` (queue rows carry `contribution_author` for this). **A mixed installation.** A queue row that does not say who delivered comes from an endpoint older than this rule, under a newer web service, during a staged upgrade or a rollback. The service cannot tell whose recommendation is independent of the delivery, and it does not guess from the task's assignee, who may have changed since. On such a row it counts **no recommendation at all**: the queue row, My work and the agents' next actions show none (`recommended_by` empty, `recommended` false, no `review-recommended` action) until the endpoint is updated. This replaces, for such rows only, the earlier fall-back to the assignee; a row that names the author behaves exactly as described above. The brief names the contribution's author on every endpoint, so it still shows an independent recommendation, and an owner can approve from there. One thing stays visible on a mixed installation: a delivery whose task was reassigned or unassigned afterwards is offered as `to-review` to the agent that delivered it and to its owner's other agents, because the row names only the new assignee; their recommendation is refused with 403 and nothing is written. **A reviewer who has already recommended a delivery is not asked again**: the names the service did not count stay on the row as `recommended_unchecked`, and they answer the one question it can answer whoever delivered, namely whether this reviewer (for an agent's next actions) or this person (for My work) has recommended it already. They do not make the delivery read as recommended, for the owner or for anybody else. The field is absent on a row that names the author. A brief whose contribution names no author is compared with the task's assignee, and one with neither shows no recommendation. When the newest standing recommendation is left out, the brief shows the newest of the others in its place. The canonical read returns the five newest in full; if all five are left out, a sixth is named in `recommendations` and its text is in the task history. `review`, `brief` and `work` over SSH show every recommendation that passes the name rule. **SSH and HTTP readers agree on the assignee:** neither compares a standing recommendation with the task's current assignee. Over both, a recommendation by the assignee's person is refused when it is written, and one written before a reassignment stays shown after it.

Over HTTP a refused recommendation says which rule it hit: the 422 message is the canonical sentence (for example "The recommendation names a commit that is not the current contribution's commit; re-read the review before recommending"). A field the operation does not take (`approved`, anything made up) is refused by the one rule for every review operation: "Unsupported review payload field(s) for recommend: ...", naming at most five, plain identifiers only, then "(+N more)". A field sent as null is absent, and `previous` is accepted and ignored for a recommendation, because the released client sends both for every operation.

**When it stands.** A recommendation is shown only while all of these hold:

* the task is `awaiting-review` and not closed;
* it names the current contribution and that contribution's commit;
* it was written after the contribution;
* no `approve` or `request-changes` on that contribution was written after it.

A new revision, a decision or a withdrawal of the contribution makes it lapse.

**One standing recommendation for a contribution from each reviewer (kittrial-5bb.154).** While a recommendation stands, a second one for the same contribution by the same reviewer is refused and nothing is stored: `NAME has already recommended this contribution, and that recommendation stands until the contribution is revised or decided. To ask for changes instead, request changes: the recommendation then stops counting. A recommendation cannot be withdrawn.` "The same reviewer" is the same actor by the name rule in the canonical action (over SSH), and the same PERSON in the web service: the reviewer, the person who owns the agent, or another agent of that person (409, with `detail.recommended_by`). An exact retry is still answered as recorded. A recommendation by another person is a second voice and is stored.

How a reviewer changes their mind:

* **From recommending to asking for changes: `request-changes`.** Any reviewer may, not only an owner. The recommendation stops counting at once, for every reader: the brief, the queue row, My work and the agents' actions. After the contributor has responded, the contribution awaits review again and the reviewer may recommend it again; the earlier one does not come back.
* **A revised contribution is a new contribution.** Every recommendation of the earlier revision has lapsed, and the same reviewer may recommend the new one.
* **From a mistaken recommendation to none: there is no such operation.** A recommendation cannot be withdrawn or voided; it lapses at the next decision or revision. A reviewer who recommended by mistake and does not want changes either tells the owner, who decides: a recommendation is advice to the owner, never a decision. **For the owner:** treat a recommendation that is followed by a comment from the same reviewer taking it back as taken back, although every reader still counts it; `request-changes` is the only record that stops a recommendation counting.

Records written before this rule are read as before: where one actor has two standing ones, the newest replaces the earlier in every reading.

**Where it shows.**

* `review TASK` and `brief TASK`: `recommendation` (the newest standing one, in full, or `null`), `recommendations` (each standing one's comment id, author and time, newest first, at most 20; the five newest also carry their verdict, summary, items, contribution and commit) and `recommended` (true or false).
* `work`: each item carries `recommended`, `recommended_by` and `contribution_author` (who delivered the current contribution).
* An agent's next actions over HTTP (`GET /v1/agents/me/next`): `to-review` for a contribution it could itself recommend, and `review-recommended` for an agent whose owner can approve. See HTTP_DEPLOYMENT.md, "What an agent is told to do next".
* Over HTTP: the same operation on `POST /v1/projects/{id}/tasks/{task}/reviews` (the `reviews` capability; a viewer is refused). The task brief's `review` carries `recommendation` and `recommendations`; rows of `GET /v1/projects/{id}/queue` and of `/v1/me/work` carry `recommended` and `recommended_by`, and a recommended contribution comes first among those of its project that await review.
* The web interface shows it on the task page above the owner's review form, and marks the row in the Reviews list. A member who can review but is not an owner gets a form to record one.

**Compatibility.** No write switch. A kit that predates this ignores the record: its `review`, `brief`, `work` and `history` reads of a task that carries one are unchanged (`history` lists it as an ordinary comment). The older kit does not reserve the prefix, so on that kit anyone could post a raw comment that starts with it. This kit shows such a comment only if it is a fully valid record that passes every rule above, which is what this kit would have accepted. The reader applies the plain-text rule too: a record whose summary or a note holds a control, bidi, zero-width or other hidden character is never shown, whoever wrote it.

**Known limits.**

* There is no operator command to void a recommendation. A wrong one lapses at the next decision or revision, and its author can replace it.
* When an operator voids a decision (`admin.py void-record`), a recommendation written before that decision stays hidden, because the reader counts the voided decision's position. The reviewer records it again.
* A recommendation is not a review request and does not close one.

### Staged rollout: writing the new shapes is opt-in

kittrial-5bb.94's coordinator direction is a **two-step ship**, because every shape above is one an older kit fails closed on.

* The **readers in this kit understand every new operation and field unconditionally**: `review`, `brief`, `work` and `history` parse and project `withdraw`, `request-review`, `resolve-item`, `decline-review`, an item `severity` and a request-changes `summary` in both settings below.
* **Writing** them is refused unless the per-installation setting `review_workflow_writes` is on. It is a boolean in the deployment configuration `deployment.private.json` at the runtime root, **absent or false means OFF**, and a value that is not a boolean is **read as OFF with a warning** rather than refused (kittrial-5bb.110 item 2), so a hand-set `"yes"` cannot make `work` and `review` fail for every actor. With it off, `execute` refuses `withdraw`, `resolve-item`, `request-review` and `decline-review`, a `request-changes` that carries `summary`, and a `request-changes` item that carries `severity`, all **before any native write**. The refusal names the operation or the field at fault (`request-changes summary`, `request-changes item severity`, `operation withdraw`). A legacy `{id, text}` request-changes item is an old shape and is still written with the switch off, and an **exact retry** of an operation id already in the chain still reconciles, so a record written while the switch was on stays recoverable after it is turned off.
* **Every flip is audited as a short append-only history** in `review-writes.audit.json` beside the deployment file, under an exclusive deployment lock (kittrial-5bb.110 item 3). Each entry names WHO set it, WHEN and the value it replaced; the history is bounded to the last 20 flips, an `on` (or `off`) that changes nothing appends nothing rather than overwriting the record, and the pre-history single-record form is still read. `review-writes status` returns `audit`, `audit_history` and `audit_agrees`, and **warns when the switch and the last recorded value disagree** - what an older kit (which does not know the file) or a hand edit leaves behind. The audit file is parsed through the same bounded-nesting guard as every other caller-written JSON, so a deeply nested file is a refusal, never a `RecursionError` traceback.
* There is deliberately **no `ORCHESTRA_*` environment fallback**: `deployment.private.json` is the single source, exactly as for `operators` and `verifiers`, and the endpoint supplies the value to the review write path. A contributor cannot set it from a payload.
* The coordinator turns it on once the **rollback target is a kit that reads the new shapes**:

```text
python3 admin.py review-writes status --actor OPERATOR
python3 admin.py review-writes on --actor OPERATOR
python3 admin.py review-writes off --actor OPERATOR
```

The actor must be on the deployment operator allowlist. Until the switch is on, a deployment may roll back to the previous kit safely: no chain it wrote can contain a shape that kit refuses. After it is on, the rollback position is the one described under "Rollback compatibility" below.

### Rollback compatibility of the additive operations

`withdraw`, `request-review`, `resolve-item` and `decline-review` are new `operation` values in the same reserved `Kind: contribution-review-v1` chain, and `severity`, the request-changes `summary` and the `disposition` fields are optional additions to existing operations. **Records written before this change keep validating and reading unchanged** in this kit: an item without `severity` reads as `blocking`, a request-changes without `summary` is unchanged, and `note_requests`/`pending_review_requests`/`declined_review_requests`/`withdrawal` read empty or null. In the other direction an older kit meets an `operation` it does not know inside a task's chain, fails closed on that chain ("Malformed contribution-review history; operator reconciliation required") and needs an operator void or an upgraded kit - it never silently misreads the record. That is exactly why writing them is behind `review_workflow_writes`: with the switch off (the default) a deployment that may still be rolled back never writes a shape the older kit cannot read, and the coordinator turns it on once the rollback target reads them. The same limits as the `assignee_at_approval` snapshot apply: decide the rollback position before deploying, and use `admin.py void-record` to reconcile a chain an older kit cannot read.

## Target the contribution record, not the latest comment

`contribution` in a `request-changes`, `respond` or `approve` operation names the **contribution record's `comment_id`** — the `contribution.comment_id` returned by `review TASK`, or `contribution_id` on an item returned by `work --mine`. The Git SHA is a separate value: `contribution.commit` in `review TASK`, or `commit` in a work item. The contribution record ID is **not** `latest_comment_id`, which is the newest record in the chain and advances with every reviewer request, response and approval. The two coincide only while the contribution is also the most recent record.

Supplying the wrong id is refused before any native write, and the refusal names the operation, the id you supplied and the id you should have used:

```text
Review operation approve supplied 2, which is the task latest comment id;
reference the current contribution instead (the current contribution id is 1).
Review operation approve supplied not-a-real-id, but the current contribution id is 1.
Review operation approve supplied unknown, but no current contribution has been recorded yet.
```

Read the current ids from `review TASK`. A refusal changes nothing: the review state, the pending items and the comment chain stay exactly as they were, so correct the payload and resubmit with a new operation ID. An **exact** repeat of an already-recorded operation — same operation ID, same canonical payload, same actor — still reconciles to the original comment without writing a second record, including after a later handoff or later reviews.

## Stale `previous`: the refusal returns the current head

Every new operation compares `previous` with the chain's current `latest_comment_id`. A mismatch is refused before any native write, and the refusal carries the current values so the retry does not need a blind extra fetch:

```text
Stale review workflow previous; reread brief. Current latest_comment_id is record-B and review_state is changes-requested; re-read the chain content before deciding.
{"code":"stale-previous","http_status":409,"latest_comment_id":"record-B","review_state":"changes-requested","supplied_previous":"record-A","task":"example-task"}
```

The first line is the human refusal; the second is the same structured error as one canonical JSON line. The CLI prints both on stderr, writes nothing and exits with status 2. `latest_comment_id` is `null` when no review record exists yet, which is the actionable value for the first write. An HTTP transport returns the same JSON object as the `409` detail.

The returned values are an identifier and a state label, not the chain content. Re-read `review TASK` before deciding, then resubmit with a new operation ID and `previous` set to the returned `latest_comment_id`. The field saves one round trip; it never replaces reading.

## Discover current work

```sh
b work --mine
b work --state changes-requested
b work --state awaiting-review
b work --state awaiting-integration
b brief example-task
b review example-task
```

The queue sorts requested changes first and shows task status, owner, review state, exact contribution commit and separate lifecycle values/scope. `--owner ACTOR`, `--limit` and `--offset` support coordinator scans. Pages are fresh views, not immutable history snapshots. Closing a task clears its `awaiting-review`/legacy `review-ready` queue entry (see "Withdrawing or superseding a contribution"); a closed task with outstanding requested changes or an approved-but-unintegrated revision stays visible, and closure is still not acceptance. Check the scope-match field before applying historical lifecycle evidence to the current contribution.

`review TASK`, `brief` and `work` combine the workflow chain and lifecycle evidence in **one shared projection**, so they cannot disagree about whether a contribution is integrated. The review write receipt (`review TASK --file payload.json`) reports the same effective `review_state` as those reads, plus the same additive `workflow_state` and `integration` fields, so an approval whose scoped integration evidence was recorded earlier is receipted as `integrated` rather than `awaiting-integration`. Additive fields:

- `review_state` is the effective state and may now be `integrated`, or `withdrawn`/`superseded` after a `withdraw` operation.
- `workflow_state` is the raw append-only workflow state, kept clearly separate (`none`, `awaiting-review`, `changes-requested`, `awaiting-integration`, `withdrawn`, `superseded`, or `legacy-review-ready` for the legacy label).
- `integration` reports `fact`, `scope`, `scope_token`, `source_commit`, `integration_commit`, `matches_contribution`, `newest_fact`, `newest_scope_token`, `newest_scope`, `reverted`, `reverted_commits` (bounded) and `reverted_total` for the current contribution. Each `prior_contributions` entry carries the same block for its own FULL commit, so a follow-on read also answers whether the replaced revision is integrated. A top-level `integration_disagreements` list (and, in `brief`/`work`, a matching human-readable warning) names both facts and both scopes whenever `newest_fact` differs from `fact`, and carries a `reverted` entry naming the removed commit(s) whenever a host-issued revert applies to the contribution.

Exactly what each match field means:

- `lifecycle` and `lifecycle_scope` are the **newest recorded scope** only. They are a newest-wins historical view; they are not scoped to the current contribution.
- `lifecycle_matches_contribution` (top level, `brief`/`work`) keeps its **original meaning**: it is True only when the scope currently shown in `lifecycle`/`lifecycle_scope` — the newest recorded scope — belongs to the current contribution's FULL commit. It becomes False as soon as a later scope for other work is recorded. Check this field before applying the top-level `lifecycle` values to the current contribution.
- `integration.matches_contribution` is the additive **ANY-scope** answer: it is True when any recorded scope names the current contribution's FULL commit, regardless of scope order.

An approved contribution displays `integrated` when **any** lifecycle scope whose `source_commit` equals the contribution's FULL commit records `integrated=passed`; scope order does not matter, so recording a later release/live-verified scope does not return settled work to the awaiting-integration queue. That rule is deliberately **any-pass-wins**, and the owner decision on kittrial-5bb.32 (comment 01a0eea4-46e8-7c3e-8878-90914d5adcd8) is to **keep it**: an older pass for the same commit is not undone by a newer `integrated=failed` recorded under a different scope, and `integration.newest_fact` reports the newest matching value so the rule is auditable rather than accidental. The decision to keep the rule is paired with two explicit mechanisms instead of a silent newest-wins change: the disagreement between `newest_fact` and `fact` is **warned about** on every read, naming both facts and both scopes (`integration_disagreements`, `warnings`), and removing an integrated commit is an explicit **audited revert record** rather than a newer failure — see "Reverting integrated work" above. The rule also decides an additive follow-on's accepted `base_commit`: since the reported scope is the **newest passing** scope, an older passing scope's `integration_commit` is refused once a newer scope passes for the same commit, while an earlier pass is still accepted after a later failure recorded under another scope (unless that exact integration commit was reverted). `integration.scope` is deterministic — the newest scope that records a trusted pass, otherwise the newest matching scope, with ties broken on the scope token — and a scope token recorded more than once keeps its newest recording position. A newest manual (unstructured) assertion, ordering ambiguity, or a value/label disagreement — including a tampered `lifecycle-scope` label — leaves the integration fact unknown instead of reusing an older pass, and `project_facts` and `integration` apply the same trust rule to the same events. A reverted integration commit reports `fact` `reverted` with `reverted` true. All of these fields are additive to the previous JSON.

`brief` prioritizes pending review actions over an older checkpoint's next action. It shows up to five pending items with a pointer to `review TASK` for the rest. Generated CURRENT.md includes a bounded review queue; native `list` remains unchanged. Legacy `review-ready` labels remain discoverable, but structured review state takes precedence. If a task has no checkpoint, its description and acceptance are shown without calling it legacy work.

The structured protocol is an append-only native comment chain with previous-comment checks. Do not hand-edit those comments. Forked/malformed records fail clearly and need operator reconciliation. Report/label changes are separate writes; a retry repairs interrupted removal of review-ready, while structured state stays authoritative.

For example, if review returns contribution.comment_id = record-A, contribution.commit = <Git SHA>, and latest_comment_id = record-B, submit contribution = record-A and previous = record-B. Do not put the Git SHA in either comment-ID field.

Handoff acceptance binds the complete disposition (including kind and supersedes) before transfer. The `.handoff-recoveries` journal is included in coordination backups and validated on restore. Interrupted completion keeps the original disposition ID on the accepted request. Older incomplete recovery identities fail closed and require explicit operator reconciliation; they are not silently promoted to exact retry evidence.
