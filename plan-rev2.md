# Plan rev2: kittrial-5bb.107 - separate "what is live" from "where evidence is recorded"

Owner lane actor: `session-1f183156-fda2-4edc-b788-ab26fdf7945d` (ds-sub-5bb.107).
Task: `kittrial-5bb.107`, review request `01a1060c-5fe2-7160-896a-2b8f2fef9eb6`
(changes-requested), superseded contribution `01a105f5-54c0-76cb-aa7f-866060c2cfbe`.

## Preflight (STEP A/B)

- `review kittrial-5bb.107`: `changes-requested`; six pending items (4 x P2, 1 direction, 1 x P3);
  contribution `01a105f5-54c0-76cb-aa7f-866060c2cfbe`; latest comment `01a1060c-5fe2-7160-896a-2b8f2fef9eb6`.
- `brief --json`: status `in_progress`, owner the lane actor; previous checkpoint
  `01a105f5-fb89-77f0-84e3-168fb75b0a2b`; 2 unresolved checkpoint items (`k107-open-1`,
  `k107-open-2`); activity cursor captured at read time.
- Branch: `contrib/kittrial-5bb.107-release-deploy-followups`.
- Main re-confirmed on koopa: `ssh koopa git ls-remote https://github.com/jamesjennens/orchestra.git refs/heads/main`
  -> `c8b94d3585682dd3c9a6b2d628ac2c4b2fdc330b`.
- Fetched the koopa bundle `/home/james/int-1004aa.bundle` (`git bundle list-heads` names
  `c8b94d3585682dd3c9a6b2d628ac2c4b2fdc330b refs/heads/main`), fetched it to `run240-main`
  and rebased. New commit after rebase: `52f973fb1e74417c95a18ddeb6faffcb5ea137e7`.
  `git merge-base --is-ancestor c8b94d3 HEAD` exits 0. Worktree keeps `*.bundle` and
  `client.lane.json` untracked.

## The smallest model that separates the two concerns

Root cause (review item 4): one per-task *current scope* is used both as **what is live in an
environment** and as **where evidence is recorded**. Rev1 then made "already deployed" per
`(environment, release_id)`, which broke the incremental default (item 1), and moved a task's
current scope when an older release was rolled back (item 2) or late-verified (item 3).

I propose the smallest separation that needs **no new task and no host-side journal**: add one
additive, per-task lifecycle dimension that records liveness, and keep the evidence scope exactly
as the place evidence is written.

1. **Evidence scope (unchanged place):** `lifecycle-scope` plus the fact dimensions
   (`deployed`, `live-verified`, `enabled`, ...). Evidence for a release is written under that
   release's scope. `scoped_evidence` already reads every recorded scope, so an older release's
   evidence stays readable.

2. **Liveness fact (new, additive dimension `live`, values `live` / `superseded`):** written under
   the release scope it refers to. It says "this recorded release scope is (or is no longer) the
   live release for the task's environment". It is written ONLY by the explicit deploy and
   rollback operations; recording evidence never writes it.

3. **Plain deploy is incremental by default.** Restore the pre-.107 containment rule: a task is
   already deployed when it has a trusted `deployed=passed` scope for this environment in a
   release that **contains** the chosen integration commit and is itself **contained in** R. That
   rule alone makes R2 after R1 select only the tasks new in R2, without
   `--previous-release-commit`; the flag keeps working and is still validated. A task whose newest
   trusted `live` fact is `superseded` is NOT already deployed, so a roll-forward re-lives it.

4. **Rollback is explicit** (`lifecycle.py release --rollback`, or `"rollback": true` in the
   payload). It moves the environment's live release back to R:
   - every task whose chosen trusted integrated commit is an ancestor of R (R's membership, not
     reverted) gets the R scope re-recorded as its current scope, `deployed=passed` under R and
     `live=live` under R;
   - every task deployed in this environment whose chosen integrated commit is NOT an ancestor of
     R gets `live=superseded` under its current live scope; its evidence events are untouched.

5. **Verifying an older release never moves a task's current scope.** When `--live-verified`
   targets a task already deployed at that release/environment, the write plan contains only the
   `live-verified=passed` evidence fact under that release's **already recorded** scope - no scope
   event. `_apply_fact` accepts a non-scope fact whose scope is any scope recorded for the task,
   not only the newest, so the outcome no longer depends on whether the operator reuses the deploy
   operation id.

6. **The default release command learns the reverted set from the endpoint.** The client sends a
   read-only `release-query` payload (new, no writes) and selects using the endpoint's authoritative
   reverted-integration set and, for reporting, the environment's live release; `--journal` stays a
   local fallback. This closes item 5 without an `ORCHESTRA_OPERATORS` dependency. What only the
   client checks stays documented: Git ancestry/membership is decided in the caller's checkout; the
   endpoint re-verifies that each named target is a trusted `integrated=passed` scope and is not
   host-reverted, in its own export.

## What each reader says in the three sequences

`project_facts` (which feeds `brief`, `work` and the `review`/`brief` `lifecycle` block) applies the
liveness fact to `deployed`: newest trusted `live=superseded` -> `deployed` reads `unknown` (not
live); `live=live` with a matching `deployed=passed` under the same scope -> `passed` and that scope
is the task's live delivery. No `live` fact at all (older data) -> exactly today's behaviour, so
the change is additive and backward tolerant.

**Sequence 1 - plain deploy R1 (t1), then R2 (t1, t3), default incremental.**
- selection for R2: only t3 (t1 is already deployed in R1, which is contained in R2).
- `brief`/`work`: t1 `deployed=passed`, `deployed_delivery` = R1, `live=live` (t1 is still live
  because R2 contains R1); t3 `deployed=passed`, `deployed_delivery` = R2, `live=live`.
- `review`/`brief` `lifecycle`: `deployed=passed` for both; t3's current scope is R2, t1's R1.
- `evidence-owed`: R1 row for t1 (plus whatever it still owes) and R2 row for t3, each `live=live`.

**Sequence 2 - R1 (t1), R3 (t1, t3), then an explicit rollback to R1.**
- rollback writes for t1: scope R1 (current), `deployed=passed` under R1, `live=live` under R1;
  for t3: `live=superseded` under R3, evidence untouched.
- `brief`/`work`: t1 `deployed=passed` at R1, `deployed_delivery` = R1, `live=live`;
  t3 `deployed=unknown`, `deployed_delivery=null`, `live=superseded` (not live).
- `review`: t1's lifecycle reads `deployed=passed` at R1; t3's reads `deployed=unknown`; the
  contribution/review chain is untouched by a release write.
- `evidence-owed`: t1's R1 row `live=live`; t3's R3 row is still listed with `live=superseded`
  (its debt is not silently dropped, and it is named as no longer live).

**Sequence 3 - deploy R1, deploy R2, then `--live-verified` for R1.**
- selection selects t1 as a verify-only target; the plan is only `live-verified=passed` under R1.
- the task's current scope stays R2, whatever operation id the operator uses.
- `brief`/`work`: `deployed=passed`, `deployed_delivery` = R2, `live=live` (R2 is live), and no
  scope rewrite; `live-verified` is evidence for R1 and is read per scope.
- `review`: unchanged chain; `lifecycle` shows R2.
- `evidence-owed`: the R1 row no longer owes `live-verified`; the R2 row is unchanged.

**What an older kit reads.** The `live` dimension and the `release-query` payload are additive: a
kit that predates them ignores both. It reads every evidence scope exactly as today, so it is
fail-open for liveness - after a rollback it still shows t3 `deployed=passed` at R3, and it never
sees `live`. Nothing fails to read, no chain is refused, and no operator reconciliation is needed;
the same limit is documented for the new kit reading pre-change data (no `live` fact -> today's
behaviour).

## Item 6 (small)

1. `deployed_delivery` values go back to **clipped strings** (160 chars), not excerpt objects, so a
   field released one version ago keeps its shape; documented in `docs/CLI_CONTRACT.md` and
   `docs/OPERATIONAL_WORKFLOW.md`.
2. A planted operation-id refusal prints the same JSON failure report as a group failure (not just
   the plain error line).
3. The between-group failure report and the docs say a fresh export is needed before retrying.
4. Measure `evidence-owed` on koopa on the same synthetic export at base `460857a` and at the new
   commit, quote the numbers, and fix what the measurement shows (memoise the per-scope content
   hash, which is recomputed per event today).
5. State plainly that Windows was NOT run for the full suite (Linux/koopa is authoritative).

## Out of scope / limits carried forward

- `k107-open-1` (per-release already-deployed semantics) is **resolved by this revision**: the
  default is the incremental containment rule and rollback is explicit, so the old
  "per-(environment, release_id) reselects everything" behaviour is gone.
- `k107-open-2` (a remote client without the host journal cannot trust a revert) is **resolved by
  this revision**: the client asks the endpoint (`release-query`) and only falls back to a local
  journal when explicitly given one.
- No new task, no new sidecar file, no JJBP, no project-specific data.
