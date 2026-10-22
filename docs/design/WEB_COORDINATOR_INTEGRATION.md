# Web-only coordination: the merge check, integration and release facts

**Status: design note for review (kittrial-5bb.117). Docs only. Nothing here is
built, and this task changes no behaviour.** Base: GitHub `main` at
`d72b72358a7aab9b1ce13900182eea4313f51075`, verified with
`git ls-remote https://github.com/jamesjennens/orchestra.git refs/heads/main` on
2026-10-22. Line numbers below are that commit's.

This note deliberately does not repeat the whole 2026-10-04 plan in
`docs/WEB_COORDINATOR_DESIGN.md`. That note's inventory is dated: it was read at
`02740dd` and says the web has no merge slot and no lifecycle routes. At
`d72b723` both exist. What is still missing is narrower and is the subject
here: **the merge check, release-level writes, a web-only coordinator's own
merge and push, and what a failed multi-step integration leaves behind.**

## 1. The 2026-10-04 context, corrected

The 2026-10-04 brief said an HTTP credential could contribute and respond but
could not approve, take the merge slot or record a lifecycle fact. All three of
those limitations are gone at `d72b723`:

| Fact | Where it is now |
| --- | --- |
| A project role `coordinator` sits between contributor and owner; it holds `reviews.approve` and `coordinate`. | `http_authority.py:353`, `:369-379` |
| A personal agent can hold a per-project coordinator **grant**, which adds `reviews.approve`, `coordinate` and `records` while its account still holds the role. | `http_authority.py:381-382`, `:580-590` |
| The merge slot is over HTTP: read, acquire, release, force-release, create/repair. | `http_service.py:5996-6072`, `:6131-6145`; capability table `http_authority.py:2493-2497` |
| Lifecycle scope and the six facts are over HTTP, read and write. | `http_service.py:5921-5990`; capability table `http_authority.py:2498` |
| A granted web coordinator's `integrated=passed` can count as an operator-recorded fact, through a host-issued mark. | `web_lifecycle_marks.py:1-7`, `:192-205`; `web_lifecycle.py:265-302` |

What is still true from 2026-10-04, and is the gap this note addresses:

1. **Release-level writes are host-only.** `release-deploy` and the read-only
   `release-query` are dimensions in the canonical lifecycle library
   (`lifecycle.py:48-53`) and the host command in
   `docs/OPERATIONAL_WORKFLOW.md:163-238`, but no HTTP route exposes either.
   The only task-shaped `release` route is the claim release at
   `http_service.py:8012` (the other is the agent claim release at `:6922`);
   neither writes a lifecycle fact. A web-only coordinator therefore cannot
   record a release in one operation, cannot record `live`/`superseded` at all
   through the six-fact route (`web_lifecycle.py:48-49` limits it to
   `lifecycle.DIMENSIONS`), and cannot ask which integrations are reverted
   before selecting targets. Single-task `deployed` and `live-verified` facts
   are writable over HTTP; that is exactly the hand work `.95` was filed to
   remove.
2. **There is no merge check as a verdict.** The host `merge-check` reads the
   slot only (`coordination.py:646-648`). The approval, source-commit and
   slot preconditions exist, but they are enforced inside the lifecycle write
   (`web_lifecycle.py:94-130`, `:254-264`), not offered as a "may this merge
   proceed?" answer before the merge. The base, the tests and the pushed
   result are not checked anywhere from the server.
3. **The server has no access to the code host.** It cannot see the approved
   branch, the merge commit, the push or the test run; the same limitation the
   earlier note records at `docs/WEB_COORDINATOR_DESIGN.md:386-390`. It also
   does not hold a push credential, and this note does not propose one.
4. **There is no cross-write integration transaction.** Each canonical write
   is independently idempotent (`http_authority.py:21-48`); a sequence of
   writes that fails halfway leaves the earlier writes committed.
5. **`.106`'s host allowlist is not the web route's rule.** The host rule that
   makes `reviewed`, `integrated`, `deployed` and `live-verified` operator-only
   sits behind the per-installation `lifecycle_authors` setting
   (`lifecycle.py:147-167`, `:199-247`) and is applied in `apply_native`
   (`lifecycle.py:2151-2152`). The web lifecycle adapter calls `apply_native`
   without that flag (`web_lifecycle.py:281`); the web route's authority is
   `coordinate` (`http_authority.py:2498`), plus independent approval and a
   held slot for `reviewed`/`integrated` (`web_lifecycle.py:94-130`,
   `:254-264`).

## 2. The merge check

### What the server can already know

For one task the server holds, in its own records:

- the current contribution and its full commit, from the review chain
  (`web_lifecycle.py:101-110`);
- a standing independent approval, from a party other than the contribution's
  author and the task's current assignee, with nothing unresolved
  (`web_lifecycle.py:103-117`);
- the merge slot's holder and saved task/target, read under the project lock
  (`web_merge_slot.py:78-136`);
- an open integration hold or a failed verifier attestation, through the
  same native projection the host readers use (`web_lifecycle.py:191-206`,
  `:259-262`);
- the lifecycle scope and its token (`web_lifecycle.py:149-230`);
- whether the scope's `source_commit` equals the approved contribution commit
  (`web_lifecycle.py:106-109`);
- whether the caller holds the slot for this task (`web_lifecycle.py:122-130`).

### What it cannot know

- that a merge commit exists, contains the approved commit, or is on the
  branch the scope names;
- that the tests named in evidence were run, or what they covered;
- that a push succeeded;
- that the base the coordinator merged onto is the project's current target,
  except as the coordinator writes it into the scope.

`integrated=passed` is a record of the coordinator's word under `coordinate`,
not a proof of a code-host state. That is the existing, deliberate boundary
(`docs/WEB_COORDINATOR_DESIGN.md:386-390`), and this note keeps it.

### The proposed read

One read-only route, useful alone and with no new authority:

```
GET /v1/projects/{pid}/tasks/{tid}/integration-check?target=BRANCH
```

It answers a single object with four parts.

1. **`task` and `contribution`**: the current contribution id, commit, author,
   review state.
2. **`checks`**: one row per precondition, each `allowed` or refused with the
   canonical sentence already used by the writers:
   - `approval`: a current independent approval, with approver and party;
   - `source_commit`: the scope's source equals the approved commit;
   - `slot`: the caller holds the slot for this task, with holder and target;
   - `hold`: no open integration hold or failed attestation;
   - `reverted`: the integration is not named by a host-issued revert record.
3. **`unchecked`**: honest rows for `base_membership`, `tests` and
   `pushed_result`, each with `known: false` and the reason ("the server has
   no code-host access"). A reader must not turn these into a pass.
4. **`next`**: the exact writes still owed, in order, each with its route and
   the operation identity to reuse if it was already attempted: scope,
   reviewed, integrated, integrated mark, slot release.

The read requires project read permission. It does not take the slot, write a
fact or perform a merge. A refused row is an answer, not an error, so a person
or agent can see every reason at once rather than discovering them one 409 at
a time. The same route, called after the merge and with the recorded
`integration_commit`, is the **after** check: it reports the scope and facts
that exist, the mark's state, and whether the slot is still held.

This is the smallest useful slice because it closes the gap between "the
server has the rules" and "the coordinator can see them before merging",
without granting the server a merge or a push.

## 3. A web-only coordinator performing the merge and push

### The sequence on the coordinator's own machine

1. Read `integration-check` and the task's lifecycle. Stop on any refused row.
2. Acquire the slot over HTTP for the task and the target branch
   (`POST .../merge-slot/acquire`, `http_authority.py:2493-2497`).
3. `git fetch` in a clone with push rights; check out the target branch; merge
   the approved commit (a merge commit, not a rewrite).
4. Run the project's tests in that checkout. Record the result as `tested`
   evidence (not as `integrated`) if the project wants it on the tracker.
5. Push the target branch. The push credential is the coordinator's own; the
   server never receives it.
6. Record the scope with `source_commit` = the approved commit and
   `integration_commit` = the pushed merge commit, then `reviewed=passed` and
   `integrated=passed` (`POST .../lifecycle/scope` and
   `.../lifecycle/facts/{dimension}`), reusing one `Idempotency-Key` per step
   across retries.
7. Release the slot.

### What the kit checks before and after

**Before** (the existing writer checks, which the read reports): the caller
holds `coordinate`; the current
contribution has another party's standing approval with nothing unresolved;
the source equals that full commit; the caller holds the slot for the task;
no integration hold or failed attestation is open. These are the existing
checks in `web_lifecycle.py:94-130` and `:254-264`; the new route only exposes
them earlier.

**After** (server records): the scope names the source and integration
commits; the six facts are scoped to it; a passed integration from a web
coordinator must carry the host mark (with the mark writer off, the write is
refused before the effect; if the mark is not confirmed after a native write,
the readers do not count it — `web_lifecycle_marks.py:192-205`); the slot is
released or still held. The server still cannot check the pushed result.

### What stays a person's step

- the code-host credential and the push itself;
- the approval, unless a granted agent holds the project grant
  (`http_authority.py:381-382`);
- the decision when `unchecked` says `known: false`: whether the tests are
  enough and whether the merge base is the right one;
- a forced release of a slot whose holder is gone: a signed-in owner or
  superuser only (`http_service.py:6009`, `:6016-6017`);
- voids, reverts and reconciliation.

**Recommendation:** keep the push a person's step for this and the next
release. If a later decision lets an agent push, give it a repository-scoped
credential of its own, never the server's, and still record the integration as
the agent's word (see question 3).

## 4. Release-level lifecycle writes (`.95` and `.107`)

### What exists on the host

`release-deploy` is one canonical operation for many tasks: it writes the
release scope and `deployed` for every integrated target, optionally
`live-verified`, and `live`/`superseded` for liveness. The resolver is
`lifecycle.py:1156-1253`; the writer is `apply_release` at
`lifecycle.py:1899-2025`, with per-target derived operation ids
(`lifecycle.py:923-928`, `:1417-1437`). Membership is decided by `git
merge-base --is-ancestor` in the caller's checkout
(`lifecycle.py:1372-1390`), and the endpoint rechecks the selected targets'
integrations and recorded scopes against its own export
(`lifecycle.py:1658-1758`, `:1899-1928`). A release is
sent in groups so one request stays inside the client timeout
(`lifecycle.py:65-76`, `:141-144`). `release-query` is the read-only
pre-flight that returns the host-issued reverted set (`lifecycle.py:2028-2050`).

### The proposed routes

| Route | Body | Authority | Result |
| --- | --- | --- | --- |
| `POST /v1/projects/{pid}/releases/query` | the `release-query` field set (the release payload shape; it writes nothing) | `coordinate` | reverted integrations and recorded live releases for that environment; writes nothing |
| `POST /v1/projects/{pid}/releases` | the `release-deploy` payload for **one group**, at most 200 targets | `coordinate` plus the release authority decided below | per-target receipts, `groups_completed`/`groups_total`, the resolved `targets`/`skipped`/`flags`; no background job |

Both routes take a stable `operation_id` (or an `Idempotency-Key` that the
service maps to one), exactly as the lifecycle routes do
(`http_service.py:5940-5946`). A release that spans more than one group is the
caller's loop over the same route, with a fresh group identity and a fresh
read between groups; the route never runs a long operation on the server. The
host command already prints `groups_completed` and the failure
(`docs/OPERATIONAL_WORKFLOW.md:230`); the HTTP answer should carry the same,
plus the per-target receipts, so a failure halfway is visible rather than a
single error line.

`rollback`, `supersede` and `supersede_scopes` stay explicit body fields;
`live_verified` is accepted only with the fresh-read semantics the host command
documents (`docs/OPERATIONAL_WORKFLOW.md:217-225`). The route must apply the
same hold refusals as the host writer (`lifecycle.py:1930-1959`) and the same
host-issued revert set in target selection (`lifecycle.py:1658-1690`), and must
pass the installation's `lifecycle_authors` setting through to
`apply_release` (`lifecycle.py:1926-1928`) instead of silently skipping it as
the current single-fact web adapter does (`web_lifecycle.py:281`).

### Who may release

This is the one authority decision this note cannot make for the project.
`coordinate` is enough for `reviewed` and `integrated`, but a deployment is a
release-level act: it says what runs in an environment, for many tasks at
once. Three defensible choices:

- **owner (or superuser) only** (`project.admin`): the strictest, and needs no
  new mechanism;
- **`coordinate`**, like integration: simplest for the office, but a granted
  agent can deploy;
- **a project release grant**, given by an owner to one agent: new mechanism,
  closest to `.106`'s operator-only intent without importing the host operator
  list into the web.

**Recommendation:** a release grant, defaulting to the owner until the grant
is given. It keeps "an integration is a record, a deployment is a decision"
and does not make every coordinator a deployer.

## 5. A multi-step integration that fails halfway

### What the two idempotency layers actually promise

- **The HTTP layer** reserves an `Idempotency-Key` (8-128 URL-safe characters,
  `http_service.py:4483-4489`) before the effect. An exact retry replays the
  stored answer; the same key with a different body is refused; a failure that
  may have committed leaves the key `unknown` and answers 409/uncertain
  (`http_service.py:4675-4778`).
- **The canonical endpoint layer** binds a deterministic `operation_id` to the
  authenticated principal, the route and the request body, reserves it before
  the effect and stores the response envelope with it
  (`http_authority.py:21-48`, `:2607-2719`). A committed receipt replays for 24
  hours, an uncertain reservation stays live for 7 days, and a compact
  tombstone refuses an old identity for 30 days; an unknown outcome answers
  `124`, never a duplicate (`http_authority.py:154-167`).

Neither layer is a transaction across writes. If the sequence in section 3
fails after the scope write but before `integrated`, the scope and `reviewed`
remain; if it fails after the native integration event but before the mark,
the event may exist and the readers may not count it, and the route answers
uncertain (`web_lifecycle.py:265-302`). The facts are append-only; correcting
them is a void or a revert, which stay host-only
(`docs/WEB_COORDINATOR_DESIGN.md:299-309`).

### What a coordinator should do

1. **Read canonical state first**: `merge-slot` GET, `lifecycle` GET, and the
   task's brief. Do not infer a failed step from a timeout.
2. **Find the last committed step** in `integration-check.next`; each row names
   its route and its operation identity.
3. **Retry only that step with the same key and the identical body.** A 409
   means the state changed: read again and send a changed request only after
   the read. A 124 means the outcome is unknown: keep the identity, inspect,
   and reconcile; never mint a new id for the same intent.
4. **If the native integration exists but the mark does not**, stop: an
   operator repairs the mark family. Do not record a second integration under
   a new id to "fix" it.

### The one addition this needs

`integration-check` should expose the **step ledger**: for each owed write, its
route, its operation id, the last response state (committed / unknown / not
attempted) and the canonical fact ids. That turns "fails halfway" from a
recovery puzzle into a resume from the first row that is not committed. It is
read-only, it uses records the server already keeps, and it is useful before
any of the release work.

## 6. Phased plan

Each slice is one contribution and leaves the previous one useful alone.

- **Slice 1 — the integration check (first; useful alone).** A read-only
  `GET .../tasks/{tid}/integration-check` returning the four parts in section
  2, plus a task-page panel that shows the refused and unchecked rows and the
  exact commands a person will run. No merge by the server, no new authority.
  This closes the "merge check" gap and makes the existing lifecycle writes
  usable by a person who cannot read the code.
- **Slice 2 — release query and release deploy over HTTP.** The two routes in
  section 4, one group per request, per-target receipts and
  `groups_completed`; a release page listing targets, skipped and deployed.
  The release authority is the decision in question 1.
- **Slice 3 — the step ledger and the web-only merge recipe.** The ledger in
  section 5, an `http_client.py` command for both reads, and the documented
  fetch/merge/test/push sequence for a coordinator agent with its own
  repository credential. Still no server-side merge.
- **Slice 4 — only if James says yes: a read-only code-host check.** A
  per-repository read-only token would let the server verify that the
  integration commit exists, contains the source and is on the branch it
  claims. Until then the note records that word as the coordinator's, and the
  `unchecked` rows say so.

What stays a person's step in every slice: the push, the approval unless a
granted agent holds the grant, the release decision, and every void, revert,
forced release and reconciliation.

## 7. Questions for James

1. **Who may record a release deploy over HTTP** — owner only, any
   coordinator, or a project release grant? *Recommendation: a release grant,
   owner until granted.*
2. **May the server hold a read-only code-host token** to check an integration
   commit? *Recommendation: not now; keep the honest `unchecked` row and
   revisit after the office has run it.*
3. **Is the push forever a person's step?** *Recommendation: yes for now; if
   an agent may push later, it uses its own repository-scoped credential, never
   the server's.*
4. **Should `tested=passed` become a merge gate**, and if so which test run?
   *Recommendation: keep it a fact with evidence; a gate needs a named command
   and a CI identity the server does not have.*
5. **Should the web enforce the host `lifecycle_authors` setting**, or is
   `coordinate` the whole rule on the web? *Recommendation: keep `coordinate`
   for integration, and use the release grant for deployment; do not make the
   web read the host operator list.*

## 8. Sources read for this note

- `http_service.py:5921-6072`, `:6131-6145` (lifecycle and merge-slot routes),
  `:4483-4489`, `:4675-4778` (idempotency), `:8012` (the task claim release
  route).
- `http_authority.py:21-48`, `:154-167`, `:353-382`, `:580-590`,
  `:2493-2498`, `:2607-2719`.
- `web_lifecycle.py:94-130`, `:133-146`, `:149-230`, `:233-304`;
  `web_merge_slot.py:18-24`, `:78-136`, `:139-179`;
  `web_lifecycle_marks.py:1-7`, `:62-65`, `:192-205`.
- `lifecycle.py:20`, `:48-53`, `:65-76`, `:141-167`, `:199-247`, `:923-928`,
  `:1156-1253`, `:1372-1390`, `:1417-1437`, `:1899-2050`, `:2110-2175`.
- `coordination.py:568-671` (the host merge operations, including the
  slot-only `merge-check`).
- `docs/OPERATIONAL_WORKFLOW.md:163-238` (host release command and its
  failure reporting), `docs/HTTP_DEPLOYMENT.md:23-136` (the released HTTP
  merge-slot, lifecycle and mark behaviour), `docs/WEB_COORDINATOR_DESIGN.md`
  (the earlier inventory and the no-code-host boundary).
