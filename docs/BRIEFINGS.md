# Task briefings and checkpoints

Start with `brief TASK`, then use `history` for the evidence you need. Both use the canonical server and require an explicit session actor. In these examples `b` means `python client.py --config client.local.json --project example --actor alex/session1 --`.

```sh
b brief example-task
b brief example-task --json
b brief example-task --full
b history example-task --limit 5 --since 2026-09-15T12:00:00-04:00
b history example-task --recent 10
b history example-task --since-checkpoint
b history example-task --since-my-checkpoint
b history example-task --kind comment --recent 20
b history example-task --cursor RETURNED_CURSOR
b checkpoint example-task --latest
b checkpoint example-task --latest --provenance
b show example-task --json
```

The default briefing answers who owns the task, its current position, unresolved items and next action. It also includes intent, acceptance, checkpoint author/time/branch/source commit and incorporated activity cursor, freshness, native dependencies, and six independent lifecycle facts with evidence pointers. A checkpoint does not set lifecycle facts; completed code never implies deployment. The native assignee remains authoritative.

`brief TASK --full` is the explicit fuller-context read: the compact brief stays compact, and `--full` adds an additive `full_context` block with the title, owner, intent, acceptance, current position and next action, plus `omission_counts` naming exactly how many characters the compact read clipped from each field. The block is **bounded** like a history page: at most 20,000 encoded bytes in total and 8,000 for any one field, with `full_omitted_chars` naming what that cap removed, `truncated` listing the clipped fields, `complete` saying whether anything was capped, and control characters escaped exactly as the JSON form escapes them. `show TASK` keeps the complete text. It changes no existing field.

The `review` object comes from the same projection used by `review TASK`, `work` and the review write receipt, so those surfaces cannot disagree about whether a contribution is integrated. It carries the raw `workflow_state` plus an additive `integration` block (`fact`, `scope`, `scope_token`, `source_commit`, `integration_commit`, `matches_contribution`, `newest_fact`, `newest_scope_token`); see [REVIEWS.md](REVIEWS.md). The top-level `lifecycle`/`lifecycle_scope` fields remain the newest recorded scope, and top-level `lifecycle_matches_contribution` keeps its original meaning — True only when that newest scope belongs to the current contribution's FULL commit. The ANY-scope answer is `review.integration.matches_contribution`, and integration is any-pass-wins with `newest_fact` exposing the newest matching value.

An old task without a structured checkpoint reports its current position and unresolved coverage as **unknown**, not empty or complete. It shows bounded excerpts and directs the reader to reconcile history. Do not manufacture a checkpoint from only the last few comments. `show` retains the full native output; it can still exceed a calling tool's output limit.

## Publish a checkpoint

`work --mine` also returns `items[].checkpoint_wait`, so the owned queue printed
after host worker `resume` carries the worker's authored wait and next action.
This is read-only context from that exact actor's latest validated checkpoint,
not inferred recipient information or proof of authority. `status` is `recorded`,
`none` (no own checkpoint), `unknown` (unreadable or invalid history), or
`not-owned` (a row shown to another reader). `checkpoint` and `author` identify
the source. `is_current` is false when a newer checkpoint by another actor exists;
`active` is false when no wait remains, or for historical, closed or delivered review work. Review state
still takes precedence over the authored next action.

The projection includes only blocker/dependency items, at most three, with exact
`total` and `omitted` counts. Text/source/next action are objects with `text` and
`omitted_chars`: controls (C0 and C1, DEL included), the Unicode line/paragraph
separators and the bidi controls are escaped before clipping to 400/240/600 characters.
Questions, decisions and corrections do not become waits. Missing person wording
stays missing; do not guess it from task titles or actor names. Use `brief TASK`
for the current full unresolved-item page and reconcile before acting. Reads
acknowledge nothing and add no native query to the owned work read.

Each `work --mine` item also carries `whose_move` (`you`, `reviewer`, `coordinator` or
`unknown`, relative to the calling actor) and `next`, one plain sentence saying whose
move it is in words, both derived only from the validated review state the item already
shows. A task with changes requested is the caller's work until a revision is delivered;
one waiting for review or integration is the reviewer's or the coordinator's move. Where
the caller's own current checkpoint records a blocker or dependency, `next` quotes that
blocker or dependency entry itself, escaped; no person is inferred, and the move and the
count stay the caller's. Items where the move is the caller's sort first, and top-level
`your_move` carries their count in the answer.

### The selection facts: `next_actor`, `work_state`, reconciliation and `claimable`

`work` items, the brief's `review` block and the `review TASK` read carry the five additive
selection facts below, all computed once in the shared review-state projection, so no two
surfaces can disagree (kittrial-5bb.6). They are not carried by every view: each view
carries the facts it needs, and this is the table per view:

| View | `next_actor` | `work_state` | `reconciliation_needed` | `reconciliation_reasons` | `claimable` |
| --- | --- | --- | --- | --- | --- |
| `work` item | yes | yes | yes (boolean) | yes (empty list when there are none) | yes |
| the brief's `review` block | yes | yes | no (`work_state == 'reconciliation-needed'` is the same fact) | only when there are any | no |
| the `review TASK` read | yes | yes | yes (boolean) | only when there are any | no |

- **`next_actor`** is the next-action actor as a caller-neutral ROLE: `assignee`,
  `reviewer`, `coordinator`, `operator`, `anyone`, `none` or `unknown`. It is deliberately
  NOT the assignee field and never a person. It comes from the SAME decision that gives
  `whose_move`: `whose_move` is the caller-relative answer (`you` for the caller), and
  `next_actor` is that decision's role, so the two can never name different movers. Where
  the decision cannot be told, both say so — `next_actor` is `unknown` on integrated work,
  on a withdrawn or superseded final contribution with no open item, and on an unassigned
  row nobody could claim. It is never null: a malformed row (a broken review chain, a
  malformed handoff journal or an unparseable native row) reads `operator`, the party who
  must repair it. A closed approved-but-unintegrated task reads `none` and leaves
  active work/agent queues. Its raw approval and lifecycle evidence remain readable;
  closure is not approval or integration. Closed blocking feedback and malformed
  review history still name the assignee or operator who needs to act.
- **`work_state`** is the review state the row SHOWS, or `reconciliation-needed` when the
  shared projection found recorded evidence that must be reconciled first. It never
  replaces `review_state`.
- **`reconciliation_needed`** (boolean) is true exactly for those rows, and
  **`reconciliation_reasons`** names the mismatching facts: `integration-facts-disagree`
  (the shared projection's integration disagreement, with both scopes and both values) or
  `lifecycle-scope-lags` (the newest recorded lifecycle scope names a PRIOR revision of
  the current contribution while the current one records no pass).
- **`redelivered`** (boolean) is true when the current contribution supersedes the one
  every open review item was filed on: a revision is already delivered and only the
  responses are owed. It is the one rule from `work.is_redelivery`, used by the
  `work` projection and by the in-process backend alike (kittrial-5bb.264 item 2/4).
  A malformed row carries it as `false`.
- **`claimable`** is the one bounded claimability rule: open, unassigned, no contribution,
  and neither a record anchor nor an epic, decision, gate or job — by issue TYPE or by the
  `decision` LABEL (`reference_records.py` treats that label as a decision whatever the
  type). Advisory only: claimability is not a reservation and an atomic claim can still be
  refused.

`work --claimable` lists exactly the rows that rule marks claimable. `work --state` keeps
its meaning on main for every value that existed there — it filters on the review state main
computed, which is the state BEFORE a malformed handoff journal turns a row's shown state
into `error`, so a row is always found under the state main showed, including a
lagging-scope follow-on whose work state is `reconciliation-needed`. The closed-row rule is
decided on that same state, so a malformed journal file never puts the project's closed
tasks into a queue. Two additive cases: `--state reconciliation-needed` selects the rows
whose recorded evidence must be reconciled, whatever review state they show, and
`--state error` additionally finds the rows the journal override turned into a shown
`error`.

The brief carries `next_actor`, `work_state` and `reconciliation_reasons` in its bounded
`review` block and never twice. It does not repeat the `reconciliation_needed` boolean:
`work_state == 'reconciliation-needed'` is the same fact, and a second name for it would
spend brief bytes on nothing new. 6000 bytes is NOT an enforced bound of the brief: it is
the size `tests/test_follow_on_contributions.py` holds for its long-chain fixture (5930
bytes on main, 5989 here), and a one-contribution brief with disagreeing integration facts
measures 6088 bytes here (5468 on main) and 6368 (5736 on main) — the test never builds
that fixture. Nothing refuses or clips a brief at 6000 bytes; the number is one test's
assertion, not a contract.

What is NOT delivered, stated plainly rather than claimed:

- **Stale prose is not detected.** A checkpoint whose summary or `source_commit`
  contradicts the evidence raises no reconciliation signal. Reconciliation is raised only
  by the two evidence-backed signals above.
- **A short or unrelated scope is not detected.** A scope naming a 7-character commit, or a
  different full commit sharing seven characters with the contribution's, reads its normal
  review state with no reconciliation prompt. Only a scope naming a PRIOR revision of the
  current contribution raises `lifecycle-scope-lags`.
- **No project owner is shown**, and the reconciliation reasons carry the scope token and
  the two commits but **no evidence times and no record ids beyond that token**.
- **Open dependencies and parent tasks are not considered by `claimable`**, and a
  re-contribution of the SAME commit reads `integrated` after its own approval.

Clearing nothing is a property, not a caveat: `work_state == 'reconciliation-needed'` is a
selection signal. It never overwrites `review_state`, never clears an open item or a
blocking checkpoint, and infers neither abandonment nor deployment. `workers.checkpoint-wait`
therefore stays exactly as it is: reconciliation clears no blocker.

The `Reconciliation needed: ...` line is added to the EXISTING warning surfaces — the
`work` item's `integration_warnings` and a read's top-level `warnings` — not to a new
field, so a reader that already reads warnings sees it.


Read `brief --json`, reconcile relevant history and repository state, and save a UTF-8 JSON file using [the template](../templates/CHECKPOINT.json). The served `docs checkpoint-template` states every field limit the validator enforces (open items included), the `lane_state` shape with an example, and the same payload. Set `previous` to the current checkpoint's `comment_id` (or null for the first checkpoint) and copy the briefing's top-level `activity_cursor`. Fill in the real source commit and branch, or leave them empty when unknown.

When new writes are enabled (see compatibility below), provenance is server-bound and round-trips: do not hand-craft incorporated digests. Omit them and the server computes and binds the exact snapshot provenance, or fetch the supported map with `checkpoint TASK --provenance` (returns the current `activity_cursor` and `provenance`) and embed the returned `provenance` verbatim — a missing, extra or altered map is rejected, so fabricated maps cannot be asserted. The first enabled record keeps an exact digest window, a bounded truncated-digest map for older entries and a chain hash. Subsequent enabled records store only new or changed evidence from those bounded maps (`provenance.delta: true`); unchanged evidence remains in the linked history. Each candidate map contains up to 200 exact and 300 truncated digests before the size fit, and the server shrinks the window as needed so the final serialized comment (including its Kind prefix) always fits the 80 KB cap; the same cap is enforced on read, so an accepted write always reads back.

`lane_state` optionally declares the lane's wait state BESIDE the checkpoint, in the same payload file (`docs/LANE_BOARD_DESIGN.md`, kittrial-5bb.263): `{"schema_version": 1, "task": TASK, "state": "working|waiting-on-coordinator|waiting-on-review|waiting-on-user|idle", "waiting_on": "coordinator|reviewer|user|ACTOR_ID" (required with a waiting state and refused without one), "expect_by": "2026-10-12T04:00:00+00:00" (optional; a past value is accepted and flagged on read — only an unparseable one is refused)}`. It is **not** a checkpoint field: the checkpoint object above is unchanged and its field set stays closed, `lane_state` is popped out before the checkpoint is validated, and the checkpoint write is the only writer. Do not send `checkpoint` in the request: the writer sets it to the comment id the same act just published, so a record always names the task's newest checkpoint at write time. The record is posted as a reserved `Kind: lane-state-v1` comment beside the checkpoint; it anchors no row, is not hidden from `history`, and every older kit reads it as an ordinary comment. Read the board back with `work lanes [--idle-minutes N] [--limit N] [--offset N]` (kittrial-5bb.263): one row per lane keyed by the session's registered name, its held tasks with `whose_move`/`next_actor`/`work_state`, the declared `state`/`waiting_on`/`expect_by` and the age of the checkpoint it names, and the `waiting_too_long`, `expect_by_passed` and `your_move_idle` flags; `--idle-minutes` is an argument of the read (default 15), never an installation setting.

 `checkpoint TASK --verify` uses the newest retained evidence for each entry in the **linked checkpoint chain**, independent of native export order. Newer truncated digests replace older exact digests, and newer truncated digests replace older truncated ones. It reports `coverage: verified` when the newest checkpoint has provenance and every current entry can be classified (unchanged, changed or genuinely new). A newest legacy checkpoint with no provenance reports `coverage: unknown`, zero verified/fresh entries and every entry unverified, even if older checkpoints recorded evidence: that evidence cannot establish what the legacy checkpoint incorporated. With bounded provenance, entries outside all retained maps remain `unverified` and coverage is `bounded`; they are never called fresh or unchanged. There is no shortcut that verifies never-recorded entries. Publishing a checkpoint records evidence for future verification; reconcile history manually through `history`/`show`. `brief` uses the same retained chain evidence (`unverified_count`, `coverage: windowed` when truncated evidence is used). A legacy checkpoint resets retained evidence: later enabled writes re-anchor it.

`directions` optionally records explicit dispositions for other actors' comment directions: `{"id": "<task>-c<comment_id>", "state": "acknowledged|resolved|superseded", "digest": "<full digest from --directions>", "note": ..., "evidence": ...}`. Acknowledged means seen and taken into account — it is not completion. Resolved/superseded require note and evidence, and the digest must match the incorporated entry, so an edited direction re-surfaces until re-dispositioned. Only the current task assignee may record dispositions, including acknowledgements; unassigned tasks cannot receive dispositions until claimed. Entry IDs must belong to the task snapshot and full digests must match. Own comments, checkpoint/review records and non-comment activity cannot receive direction dispositions; they are refused before mutation. Fetch any outstanding direction, regardless of age, with `checkpoint TASK --directions --offset 0 --limit 50` (limit 1–100). It returns full digests, total and next_offset. Compare activity_cursor across fresh pages and restart if it changes; use a current brief cursor when writing. Dispositions remain effective across the linked history:
an ordinary checkpoint that omits `directions` keeps the existing dispositions, and an edit to the direction comment invalidates them via digest mismatch. A direction absent from `directions` stays outstanding on `brief` (and on `work --mine`, as `unresolved_directions`) even when checkpoint activity is current — receipt and cursor advancement are never resolution. Submit:

```sh
b checkpoint example-task --file checkpoint.json
```

The final record contains at most 100 effective dispositions. When carrying them would exceed that cap, the server retires older resolved/superseded entries by the age of their last **explicit** disposition in the linked chain. Carrying one forward does not refresh its age. Every disposition in the current request is protected from retirement, including an edited direction whose prior resolution was retired. Retired resolutions remain effective through their original checkpoint records; unresolved acknowledgements are never silently retired. If no eligible completed entries can make room, the write is refused before mutation. `carried` is server-derived and caller values are refused. Completed inherited dispositions are not repeated in new bodies; unresolved acknowledgements are carried. Each record holds at most 100 effective dispositions plus the bounded provenance delta and the existing checkpoint fields; the final 80 KB comment cap is enforced before writing. A repeated unchanged 300-comment snapshot adds only its preceding checkpoint entry to evidence, rather than repeating all 300 digests.

Compatibility uses two releases. This release always reads optional provenance/directions/carried/direction_owner and delta evidence, but **new writes are OFF by default**, including an existing configuration file with no switch key. With `checkpoint_provenance_writes` absent or false, checkpoints keep the previous kit's body fields and compatible activity cursor. Explicit requests for optional fields, acknowledgement or resolution are refused before mutation. The template is the compatible shape. Once this reader is the oldest supported rollback target, the installation operator runs `admin.py --root RUNTIME checkpoint-provenance-writes on --actor OPERATOR`. `status` reads the effective setting and audit count; `off` disables new writes. The actor must be on the deployment operator allowlist. Each on/off action records actor, UTC time, previous and new value in `checkpoint_provenance_audit`, atomically with the switch in the private deployment configuration, under the deployment lock that every change to that file takes (it refuses after 10 seconds if another change still holds it). An on that changes nothing writes nothing. Other settings are preserved. A hand-edited audit that is not a list of entries of the written shape reads as an empty history with a warning, and the next on/off keeps it aside under a dated key and starts a fresh list. The operator procedure (checks, order across installations, telling workers) is in OPERATIONS.md, *Checkpoint provenance write switch*. A malformed non-boolean switch reads OFF with a warning; no client/environment fallback enables it. This is an installation action, not a contributor command.

The rollout is **one-way per task** once it has a provenance record. Turning the switch off does not convert that history or make it readable by a pre-reader rollback kit. New legacy checkpoints on that task are refused before mutation rather than hiding outstanding directions; re-enable the switch to continue. Exact retries of previously committed requests still reconcile under their original actor even with writes off. Stored cursors are compared in both the released shape and the first provenance kit's shape, preventing an unchanged old checkpoint from becoming stale merely on upgrade. New cursor output omits the redundant derived entry-digest map from its hash, so switch-off writes also stay current on previous readers. Existing immutable history cache continuations remain usable.

**Switch off provides surfacing only, and reports no outstanding directions (kittrial-5bb.284).** With `checkpoint_provenance_writes` off no disposition can be recorded, so the readers do not count other actors' ordinary comments as outstanding directions: `brief` reports `directions.total` 0 and carries a `directions_check` block (`state` `off`, `reason` `switch-off`, and the note that dispositions need the installation switch), and `work` rows report `unresolved_directions` 0. `next_action` is unchanged: the block already states the check, so no prefix is added and a checkpointed task with nothing to acknowledge reads exactly as it does with the switch on. A closed task reports none whatever the switch says (`state` and `reason` `closed`), because a closed task takes no new disposition. Comments later than the newest legacy checkpoint are still shown as activity, including ambiguous timestamp ties, so nothing is hidden: the next checkpoint by anyone advances that baseline, even a non-assignee's checkpoint, and an edited earlier direction before the baseline does not return. There is no durable acknowledgement/resolution while off. Enable new writes for persistent direction dispositions and the reassignment example below. A direction is another actor's ordinary comment. Two things never count: the task owner's own comments (`author` equal to the assignee), and the structured records, whose `Kind: task-checkpoint-v1` and `Kind: contribution-review-v1` prefixes mark checkpoints and review records - every `request-changes` item included, so the coordinator's `review-summary` is not a direction. A coordinator's plain comment IS an ordinary comment by another actor and does count while the check applies. A coordinator note that needs no acknowledgement therefore belongs inside a record rather than beside one: a `request-changes` summary (the send-back helper already does this), or a checkpoint or review record. The switch is read once per request on every `work` and `brief`, as `review_workflow_writes` already was, so a `deployment.private.json` that is not a JSON object now fails those reads as well as checkpoint writes.

The first enabled checkpoint, or the first after any legacy record in an existing chain, stores a full bounded evidence anchor rather than a delta: a 300-comment task is about 30 KB with short checkpoint fields. Subsequent unchanged snapshots add about 0.8 KB per checkpoint. These are measurements for that fixture, not universal sizes; maps are bounded to 200 exact and 300 truncated digests, and the complete comment is capped at 80 KB. A legacy interruption requires another anchor of similar size.

Migration direction baseline: comments strictly before the newest legacy checkpoint are historical with **unknown coverage**, not outstanding directions; brief says this once. Comments after it or tied with its timestamp enter the direction set; uncertain ties may retain an extra older comment rather than hiding an instruction. On an unassigned task the newest checkpoint author is the own actor, so their own comments are excluded. New records bind the observed assignee in server-derived `direction_owner`; older records use the native checkpoint author. After reassignment, the previous owner's comments through that owner's **own last checkpoint** are excluded for the new owner. Later comments remain possible directions, as do other actors' comments under the legacy baseline rule. A checkpoint written by an operator or reviewer who merely observes the assignee does not advance that owner's cutoff. Without an own checkpoint there is no owner-specific cutoff. Native timestamps may have only second precision: previous-owner comments tied with their checkpoint timestamp remain possible directions unless retained checkpoint provenance proves their IDs were already present before that checkpoint. This can retain an extra historical comment, but avoids hiding a later instruction; opaque IDs are not treated as authoritative time. No assignment timestamp is invented, and a newer checkpoint does not resolve later instructions by itself.

Worked unassigned example: Alex writes the latest checkpoint, then the task is unassigned. Alex's subsequent progress comment is own activity and is not a direction; Blair's subsequent instruction is an outstanding direction until explicitly resolved or superseded. The native checkpoint author is Alex even if the record captured a different assignee.

Worked reassignment example (new writes enabled): Alex's own last checkpoint is at 12:00. Alex comments at 11:00 and 13:00, and an operator checkpoints the task at 14:00 while Alex is still assigned. The task is then assigned to Blair, who checkpoints at 15:00. With no later legacy baseline, Alex's 11:00 comment is excluded and the 13:00 comment remains a possible direction for Blair. The operator's 14:00 checkpoint cannot move Alex's cutoff; Blair's 15:00 checkpoint cannot resolve the 13:00 instruction merely by incorporating its digest. If the newest pre-feature checkpoint were at 12:30, comments strictly before 12:30 would instead have unknown historical coverage and be excluded by that shared migration baseline.

The server supplies author and timestamp through the native append-only comment. It rejects a stale previous checkpoint or changed activity under the same project lock used by other client mutations. Re-read and reconcile before retrying with a revised payload. After an uncertain response, retrying the exact payload under the same actor reconciles an already stored checkpoint. Direct operator writes bypass this lock.

Carry every open item forward **unchanged**, regardless of its age. To resolve or supersede one, omit its old ID from `open_items` and add that ID to `resolved` with a reason and evidence pointer. A changed or replacement item needs a new ID. Supported kinds are blocker, question, decision, correction and dependency. Historical resolutions remain in their original checkpoints. Native dependency edges are shown separately and are not cleared by checkpoint prose.

**Naming an item in `resolved` while still carrying it in `open_items` does not resolve it**, and neither does naming an id the previous checkpoint does not hold open. Both are refused, and since kittrial-5bb.287 the refusal names the ids it compared and says which of the three things went wrong: an id open in the previous checkpoint that this record neither carries nor resolves; an id in both `resolved` and `open_items`; or an id in `resolved` that is not an open item of the previous or the current record. The comparison is always the previous checkpoint's `open_items`, this record's `open_items` and this record's `resolved`.

**A direction is NEVER resolved by `resolved`.** A direction is another actor's ordinary comment with an id of the form `<task>-c<comment_id>`; it is dispositioned with the `directions` field (acknowledged, resolved or superseded) and its digest from `checkpoint TASK --directions`. An installation with `checkpoint_provenance_writes` off cannot record a disposition at all, and while it is off no direction is reported as outstanding (`directions_check` says so, kittrial-5bb.284), so there is nothing to disposition and the item-level rules above are the whole of it. **No client update is required to write a checkpoint**: the record shape and the carry rule are unchanged, and the payload is built by the caller's own tooling.

**The minimal payload that disposes of ONE outstanding direction while carrying the open items** is [templates/example-direction-resolution.json](../templates/example-direction-resolution.json). Take `previous`, `activity_cursor` and every `open_items` entry from `brief TASK --json` (or `checkpoint TASK --latest`), take the direction's id and full `digest` from `checkpoint TASK --directions --offset 0 --limit 50`, and write it:

```sh
b checkpoint TASK --file example-direction-resolution.json
```

The three things it does, in one line each: `open_items` repeats every unresolved id with its text and source byte for byte; `resolved` disposes of **open items** only and stays empty here; `directions` disposes of the **direction** with its own id, its full digest and a note and evidence. An ordinary checkpoint that omits `directions` leaves the recorded dispositions in force - it does not resolve them.

A refusal from the **chain walk** is a different sentence on purpose: `Checkpoint history is unreadable: stored pair A -> B ...` names the two stored records and says the task needs reconciliation by an operator (a correction record plus a re-anchoring checkpoint). No payload fixes a stored pair, so the refusal never sends the caller back to the payload for one.

The freshness flag detects comments, events and changes to task fields, including edits/deletions. It excludes the checkpoint's own comment. It means the checkpoint needs reconciliation; it does not prove the newer activity invalidates its conclusions. This digest is an observation of exported state, not a complete database audit watermark.

When the flag is set, `brief` also returns a bounded `newer` summary: counts split between the owner's own entries and other actors' entries, separated into `fresh_count` and `changed_or_late_count`, a bounded `other_authors` list (with an `omitted` count), up to five oldest unincorporated entry references carrying stable entry IDs, per-entry clipped authors and a `changed` flag, plus `history` and `history_new` (`--since` checkpoint timestamp) read paths. The summary also carries `newer_activity_marker`, defined once in "Newer activity: one definition" below. In that state the checkpoint's recorded `next_action` is prefixed with a `STALE CHECKPOINT:` qualification instead of being presented as unconditional. A project restored under another name (`restore-new`) is not by itself newer activity: the cursor names the project it was taken in, and an unchanged task reads current (kits before kittrial-5bb.131 report it stale until a fresh checkpoint). It still says so: `checkpoint.taken_in_project` names the project the checkpoint was taken in whenever that is not the current one, and the next action is prefixed `CHECKPOINT FROM PROJECT NAME:` so the restore is reconciled before the checkpoint's next action is trusted. The prefix precedes the checkpoint's next action and is left off when the review state supplies its own next action. NAME is shown only when it is a project name; a cursor naming anything else (a newline, instruction text, a terminal escape, an over-long name) shows `(unrecognised project name)` instead (kittrial-5bb.142).

Coverage is per-entry within bounded maps: checkpoints retain exact digests for up to 200 entries, truncated digests for up to 300 older entries, and a chain hash for the rest. Edits and late arrivals can be classified within those maps; entries beyond them remain unverified. A legacy checkpoint without either provenance or incorporated digests still shows the freshness flag and reports `coverage: 'timestamp'`: the released timestamp rule selects the entries, and a backdated arrival or an edit of an older entry can be missed. Reconcile with `history` before trusting its counts. Counts are never asserted as complete when coverage is timestamp, unknown or windowed. Reading clears nothing: acknowledgement, reconciliation and completion remain distinct. The work queue mirrors counts as `newer_activity_by_others` / `newer_activity_own` plus `newer_activity_coverage` (`current`/`snapshot`/`windowed`/`timestamp`/`unknown`, null without a valid checkpoint), and carries the same `newer_activity_marker` and `newer_activity_fields`, so `work --mine` surfaces directions arriving after a checkpoint and a bridge can wake on each one. `session resume` returns session, resume and guidance metadata, without task IDs or rows. A worker's `wait` fingerprints `wait_newer_activity_marker` rather than the checkpoint-relative fields, so the agent's own plan checkpoint does not wake its own wait; everything by others and every edit of the title, description or acceptance still does. The coordinator form and its fingerprint are unchanged. Read `work --mine` for these fields; `worker.py resume` also runs onboarding and prints the queue.

### Newer activity: one definition (kittrial-5bb.354)

These eight fields have the same names and the same meaning on every route that carries them: a `work` row, `brief`'s `activity` block, a held action of `GET /v1/agents/me/next`, a row of `GET /v1/agents/me/wait` and the web task read (`GET /v1/projects/{pid}/tasks/{tid}/brief`, its `activity` block). One function computes them (`briefing.checkpoint_queue_fields`), and every route carries its answer unchanged.

- **"Since the checkpoint"** means what the task's newest valid checkpoint did not incorporate. The checkpoint's own comment, and the lane-state record written with it, never count. With per-entry digests (a checkpoint written by this kit) the selection is exact: a new entry, an **edited** entry (same id, new content) and a **late** arrival backdated before the checkpoint. A legacy checkpoint has no digests, so entries are selected by time and coverage is `timestamp`.
- **Own and other.** An entry is *own* when its author is the task's direction owner: the assignee, or the checkpoint's author when there is none. Every other author is *other*. This is the same rule the outstanding directions use.
- **`newer_activity_by_others`** and **`newer_activity_own`** count those entries. **`newer_activity_coverage`** is `current` (the checkpoint's cursor matches the task as it is now), `snapshot` (exact), `windowed` (some older entries cannot be re-verified), `timestamp` (a legacy checkpoint read by time) or `unknown` (a legacy checkpoint with no timestamp at all). **`unresolved_directions`** counts other-party comments not yet resolved or superseded. All are null without a valid checkpoint.
- **`newer_activity`** keeps its released rule exactly, for older clients: true when a comment by anyone other than the assignee was written after the checkpoint comment. One function computes it (`briefing.newer_comment_by_other`). It does not see an edit, a late arrival or a field change; the counts and the marker do.
- **`newer_activity_marker`** is what a bridge wakes on. It is `<entry_id>:<16 hex>`, naming the newest other-party entry the checkpoint did not incorporate. When there is none, it is `fields:<16 hex>`. The hex digests that entry's current content AND the current title, description and acceptance. So it moves:
  - for each new other-party entry;
  - for an edit to the newest one;
  - for every edit of the title, description or acceptance.
  It never moves for a claim, a status, assignee, priority, notes or label change, or the owner's own comment. It is never a timestamp. It stays non-null while there is a valid checkpoint, including when the read is current, so a later claim reads the same marker; a legacy checkpoint read by the timestamp rule gets the same marker from the newest other-party entry at/after it, and its `timestamp` coverage says a backdated arrival or an older edit can be missed. It is `null` only without a valid checkpoint (or for a legacy checkpoint with no timestamp at all). **A client wakes on a CHANGE of the marker** (any new value, compared with the one it last saw), never on its presence, and falls back to the `newer_activity` boolean only when the marker is null. The agent's own checkpoint changes it at most once: from the newest other-party entry it incorporated to `fields:<16 hex>`.
  - **Who edited a field is not recorded** (the tracker's version history names no author; see below). An agent's own edit of one of those three fields therefore moves the marker too, and wakes the agent once, until its next checkpoint incorporates it. Kits before kittrial-5bb.354 wrote the marker as `null` when the read was current; no released client relied on that.
- **`newer_activity_fields`** is the current title/description/acceptance digest alone, as 16 hex characters (kittrial-5bb.367 F2b). It moves exactly when one of those three fields is edited, never for a claim, a status/assignee/priority/notes/label change or the owner's own comment, and it is `null` without a valid checkpoint. Because the marker's hex covers the newest other-party entry and these fields together, this field lets a bridge tell a field edit from the agent's own checkpoint without unpicking the marker: the marker changes in both cases, the fields digest only in the first.
- **`wait_newer_activity_marker`** is the checkpoint-INDEPENDENT marker a worker's `wait` fingerprints (kittrial-5bb.369). `newer_activity_marker` is relative to the newest checkpoint, so the agent's own plan checkpoint both resets it and moves `checkpoint_at`; the next wait then returns once with nothing new. This marker is instead the newest entry by anybody other than the task's ASSIGNEE across the whole task history, folded with the current title/description/acceptance digest by the same `activity_marker` rule. It is `<entry_id>:<16 hex>`, or `fields:<16 hex>` when nobody else has written, and it moves for each new or edited other-party entry, for another actor's checkpoint, and for every edit of the three fields. The agent's own checkpoint, lane-state record and comments are its own entries (its actor is the assignee), so they never move it. It is computed even when the task has no checkpoint yet, so writing the FIRST one does not move the marker either; it is `null` only when the snapshot cannot be read. It rides beside the seven checkpoint-relative fields, and a worker's wait compares it INSTEAD OF `checkpoint_at`, `open_items`, `blocking_items`, `newer_activity` and `newer_activity_marker`; `review_state`, `whose_move`, `next_actor`, `owner` (assignment), `status` (claim/release), `pending_review_items`, `contribution_id` and `redelivered` are still compared directly. **The caller's own delivery or review response** takes the row out of its move, so the wait does not need to wake on it: the write's answer prints the post-write fingerprint to pass as `--since`.
- `checkpoint.newer_activity` in `brief` is a different, older fact with its own name: the checkpoint is stale (its cursor no longer matches), own entries and task-state changes included.

### New comments return an open task to its assignee (kittrial-5bb.390)

A coordinator or owner can reach an assignee by commenting on the task even while
its contribution awaits review or integration. The queue reports the task as the
assignee's move until that assignee records a checkpoint or a valid structured
review response after the comment. This also applies to a partly approved or
integrated task kept open for its remainder. A closed task gains no comment
attention. Existing unresolved review requests remain discoverable independently.

`assignee_comment_pending` is the shared attention decision, alongside the review
and lifecycle evidence. An `awaiting-review` contribution can therefore have
`whose_move: you` and `next_actor: assignee`: the assignee must read the new comment;
no approval, integration or other lifecycle fact has changed. A named reviewer
still sees its own review assignment; assignee attention does not cancel it. An operator error
still needs its operator. Only plain comments count: review, integration, scope,
hand-back, receipt, lane-state, checkpoint and every other reserved record kind
remain evidence and never add comment attention.

The selection uses the existing `newer_selection` and `same_activity_actor` rules,
including parent/sub-label ownership. The assignee's own comments do not add
attention. A checkpoint by someone else does not acknowledge for the assignee.
The author must currently be a project owner or coordinator, an agent with an
effective coordinator grant, or a configured host operator (including its adopted
session actor through the existing principal map). This is the current view on
each read: granting a role makes an earlier unacknowledged plain comment count;
removing the role makes it stop counting. No historical role is inferred or stamped.
A response clears attention only when it comes from the validated linked review
chain and its author is the assignee. Its boundary is its entry in the same ordered
history; it does not claim new checkpoint digest coverage. Write a checkpoint when
recording exact incorporation of an edited or backdated history entry is needed.
Reading or posting an ordinary own progress comment acknowledges nothing.

The `.351/.354` marker, count and coverage meanings remain unchanged. Worker wait
continues to fingerprint the `.369` checkpoint-independent
`wait_newer_activity_marker`; no second marker or polling clock is introduced.
Two successive eligible plain comments each move it, while own comments and an
unchanged reread keep it identical. Recording a checkpoint can clear the attention
row; it does not move that wait marker by itself.

`GET /v1/agents/me/next` offers `new-comments` for a delivered task needing this
attention, with the same activity fields and a link to its brief/history. Its
attention state is `working` while its persisted review state stays visible.
`changes-requested` and undelivered work keep their existing action kinds. CLI
`work --mine`, worker wait and HTTP worker wait use the same move decision. The
`.379` coordinator wait retains its four trigger groups and shared CLI/HTTP
fingerprint: a lane acknowledging a comment does not review or integrate a delivery.

### Changes since your checkpoint (kittrial-5bb.354)

`brief` and the web task read carry `changes_since_checkpoint`, the section an agent reads first after a wake. The worker prompts say so.

- **`checkpoint`**: the checkpoint compared with (comment id, author, time). A task with no checkpoint carries neither `activity` nor this section in the JSON brief, which keeps its compact size; the text form prints `Changes since your checkpoint: This task has no checkpoint yet ...`, and the web task read carries the section with `checkpoint: null` and that `note`.
- **`entries`**: the same selection the counts read, newest first. Each has `entry_id`, `kind`, `author`, `timestamp`, `own`, `state` (`new`, `edited` or `late`) and `body`. The body is untrusted text: terminal-safe (controls escaped) and clipped to 300 characters with `omitted_chars`. At most 10 entries are listed, within 3,000 bytes of body; `total` counts all of them and `omitted` says how many are not listed. `history` is the read for the whole entries (`history TASK --since-checkpoint`).
- **`fields`**: each changed title, description or acceptance, with `changed_at`, `edits`, `current` and `previous` (both clipped and terminal-safe) and `author: null` ("who made the change is not recorded"). The previous value comes from the tracker's own version history (`bd history TASK --json --limit 50 --readonly`). bd keeps one Dolt commit per write with the whole row, so the value at the checkpoint is the one in the newest commit at or before it. That history names no author: every commit's committer is `beads`.
  - The history is read only when the checkpoint's cursor is stale, only on these two reads (never on a wait or next poll), and through the read cache, keyed on the tracker's HEAD commit like the export.
  - The read runs inside `brief`, which holds the project lock SHARED. `history` is a read verb for the endpoint's write classifier, and bd itself is passed `--readonly`.
  - When the fields cannot be determined (the history cannot be read, is not in the expected form, or does not reach back to the checkpoint within 50 commits, or the route has no tracker history, as on the disposable in-process backend), `fields` is empty and `fields_note` says why. Nothing is guessed.
  - **A byte cap (kittrial-5bb.362).** The read stops at 256 KiB of bd's answer: it stops reading the pipe there and ends the process, so a larger history is never held in memory, parsed or cached. One smaller read, of the newest 5 commits, then follows. If that is over the cap too, `fields_note` says the history is larger than this read takes and that the previous values are not shown. If it fits but does not reach back to the checkpoint, the "does not reach back" sentence says so.
  - The per-task history cache is pruned on every history write: files older than 24 hours, and any beyond the newest 256, are removed (history files only, never the export cache).
  - **Previous values are shown to every reader of the project.** Up to 300 characters of a field's PREVIOUS value appear in this section, on brief and the web task page, for anyone who can read the task. So text edited out of a title, description or acceptance (a pasted secret, for instance) stays visible here until the task's next checkpoint, and it stays in the tracker's own version history for good. Secrets never belong in task text: rotate a leaked one, do not just edit it away.
- **`coverage`**, `unverified_count` and `note` state what the section can and cannot see, as `history --since-checkpoint` does. Reading clears nothing: a new checkpoint incorporates the entries and the section reads `current`.

The text form of `brief` prints the section as `Changes since your checkpoint (READ THESE FIRST): ...`, one line per entry and per field, after the `newer` lines.

Only use `checkpoint` to write the structured format. Malformed structured comments are flagged; conflicting branches or missing revisions fail clearly. Such corruption requires an operator to preserve the original evidence in a correction record and reconcile the invalid native record before publishing again. The system will not choose a conflicting conclusion or silently erase evidence.

## Bounded history

History returns JSON, with up to 5 entries and 4,000 encoded body bytes by default. `--limit` accepts 1–20; `--body-budget` accepts 256–8,000. Oversized comments are split into fragments retaining `entry_id`, character offset, total characters and `continued`. Concatenate fragments by entry ID and offset to recover the entire body. Metadata is additional to the body budget.

`--since` is inclusive and requires `Z` or an explicit UTC offset. Each continuation cursor binds project, task, cutoff, snapshot and position. Continue with the exact returned token; omit `--since` or repeat the same instant. Ordering is timestamp followed by stable entry ID, including timestamp ties. An immutable server snapshot prevents gaps or duplicates when new activity arrives or old comments are edited during pagination. Start a new history request without a cursor to see that activity. The separate project activity feed supports novelty tracking across exports.

`--recent N` (the same option as `--last N`) pages the same immutable snapshot newest-first: the newest N entries, then older pages through the returned `next_cursor`. It is a direction and page size in one, so it replaces `--limit` rather than combining with it; a fragmented body is still delivered in body order (offset and `continued` unchanged), so the newest-first read reconstructs exactly with no gaps or duplicates. The ordinary recent-review retrieval is `b history example-task --recent 10`. `--recent`/`--last` accept N in 1–20 (the same cap as `--limit`), which is the newest 20 entries per page; a larger N is refused and the refusal names `--recent`. A selection with nothing in it — no entries at all, no entries of the requested `--kind`, a `--since` instant in the future, or `--since-checkpoint` with nothing new — answers `total_entries: 0` with a null `next_cursor`, exactly like the same read without `--recent`.

`--kind comment|event` filters entries server-side. `--since-checkpoint` selects the activity the baseline checkpoint did not incorporate. The baseline is the **newest valid checkpoint, by anyone** — the kit lets any project actor write a real checkpoint on a task it does not hold, so the newest record need not be the reader's own. `--since-my-checkpoint` uses this actor's own newest valid checkpoint instead. The answer names the baseline (`since_checkpoint`, `checkpoint_author`, `checkpoint_timestamp`) and states its evidence and its blind spots (`checkpoint_coverage`, `unverified_entries`, `checkpoint_note`), so a reader can tell from the answer alone what it was not shown:

- `snapshot`: retained evidence from the **linked checkpoint chain** re-verifies every entry the checkpoint incorporated, so the read hides nothing it can prove.
- `windowed`: some entries the checkpoint recorded have no per-entry evidence. They are returned only when they order after the checkpoint, and `unverified_entries` counts the current entries that may therefore be hiding an edit. `checkpoint_note` says so; run `checkpoint TASK --verify` or read the whole history first.
- `timestamp`: the baseline is a legacy record with no provenance. The released timestamp rule applies — every entry at or after the checkpoint is shown — so an entry edited since it, or an arrival backdated to before it, **cannot** be seen in this read.
- `unknown`: the baseline is a legacy record with no timestamp at all, so there is no selection rule to state.

Recorded provenance makes the selection exact in both directions and is taken from the whole chain, not the newest record's own delta: a late backdated arrival or an entry edited since the checkpoint is included even though it sorts before the checkpoint entry, an entry edited after an **earlier** checkpoint is still surfaced after a later one, and an entry the chain provably already had is dropped so a timestamp tie is not reported twice. A tie that cannot be proven is retained rather than hidden. With no valid checkpoint it returns the whole snapshot and reports `since_checkpoint: null`. The result also reports `order`, `kind`, `snapshot_entries`, `total_entries` and `omitted_entries`, so a bounded read states its omissions.

A cursor binds the direction, the kind filter and the checkpoint boundary as well as the snapshot and `--since`. Continue with the exact returned token alone, or repeat the same values; a mismatched direction, kind or boundary is refused. A cursor written by an earlier kit (no direction key) still reads as oldest-first, and unknown cursor keys are still refused.

`checkpoint TASK --latest` is the bounded latest-checkpoint read: one record with its `comment_id`, author and timestamp, source commit and branch, activity cursor, intent, acceptance, summary, next action, open items and resolutions, plus `coverage` (`provenance` or `legacy`). Its retained provenance is returned as **counts** (`covered`, `digests`, `older`, `verifiable`, `complete`, `chain`), so a long task's answer stays small; `checkpoint TASK --latest --provenance` returns the digest map itself. It reads nothing else of the history and reports a missing checkpoint rather than inventing one.

More unresolved items are available through `brief TASK --items-offset N`; the default is five per page, maximum ten. This is a fresh current-state query, so re-read after a checkpoint change. Dependency counts and omitted counts are explicit; use `show` for all native edges.

History snapshots live in each private runtime project's `.history-snapshots` directory. They contain private task evidence, are disposable, and are not part of the durable backup pair. There is no automatic retention policy yet. Operators may remove old snapshot files under the project's coordination lock; affected cursors fail explicitly and readers restart. Checkpoints and their provenance remain in native backups.

## Opaque cursors are complete tokens

`brief`, in both text and `--json` form, and `history` print `activity_cursor` and `next_cursor` as **complete** base64url tokens. They are never display-elided, wrapped or shortened: the string you see is exactly the string to send back. Copy the whole token, without retyping it, inserting line breaks or dropping a character.

**A refused checkpoint names every problem at once.** With one problem the refusal is that problem's sentence, as before (`summary: 1001 characters, the limit is 1000`). With two or more it is one numbered list of the same sentences: `Invalid checkpoint (3 problems): [1] summary: 1001 characters, the limit is 1000 [2] Invalid cursor [3] open_items[0].kind: expected one of blocker, correction, decision, dependency, question`. At most 20 are listed; the rest are counted (`(+4 more)`). Every item of `open_items` and `resolved` is checked, not only the first. Two refusals are about the task's state and not about the record, so they stay separate and come after the record is valid: a stale `previous` and a changed `activity_cursor`.

**The merge slot takes no checkpoint and no review record.** `PROJECT-merge-slot` is the project's merge slot: an internal record, not a task. `work` does not list it. A checkpoint, a review record, a claim or a status change on it is refused with "PROJECT-merge-slot is the project's merge slot, an internal record, not a task"; its holder changes only through `coordinate`. So is every other write that names it (an edit, a label, a comment, a dependency, a child), and the label `gt:slot` is reserved on every row. The slot is the row with the exact id, with or without its label. See OPERATIONAL_WORKFLOW.md, "Integrate through one project-wide merge slot".

**A field name in a refusal is escaped when it is not an ordinary name.** `unknown fields: extra` is unchanged. A name with anything but letters, digits, `_`, `.` and `-` is shown as a quoted ASCII string (`"line\nbreak"`, `"z \u005b2\u005d ..."`), so it cannot break the line or look like the next numbered problem.

`checkpoint` requires the current `activity_cursor` for the same task. History continuation instead reads the immutable snapshot bound to its cursor, even after live activity changes. It fails explicitly if that saved snapshot is unavailable or its scope is invalid; start without a cursor to read newer activity. Checkpoint freshness and history continuation are separate contracts.

Copy tokens exactly. Malformed or altered tokens can fail decoding, schema, scope or snapshot validation; the error text depends on the failure. Do not rely on one universal error string or reconstruct a token from a displayed abbreviation.


## Pointers in checkpoint source and evidence

Open-item `source` and resolved-item `evidence` may mix prose and whitespace-separated
`KIND:VALUE` tokens. The twelve lowercase kinds are `task`, `comment`, `ref`, `cap`, `item`,
`file`, `room`, `commit`, `doc`, `archive`, `req` and `design`; values have 1–200 characters from
`[A-Za-z0-9._/#:-]`. The two traceability kinds also fix their value's shape
(docs/design/TRACEABILITY_DESIGN.md §2.4): `req:KEY` or `req:KEY@REV` (REV a positive
revision number; `@` is legal only there), and `design:SCOPE` or `design:SCOPE#SECTION`
(`design.SCOPE` is a reference key). A nonmatching token is ordinary prose, including a
malformed `req:` or `design:` value such as `req:KEY@x`. Punctuation is literal:
`task:x.` names `x.`, not `x`; separate sentence punctuation with whitespace.
No checkpoint field, validation rule or stored byte changes, and an older kit can carry
the strings unchanged.

`unresolved.items` retains its original checkpoint item shape, so a complete list can
be copied verbatim into the next checkpoint. Pointer metadata is separate under
`unresolved.pointer_items`, keyed by item id; each value has `pointers` and
`pointers_omitted`. The new resolved view likewise keeps raw `resolved.items` separate
from `resolved.pointer_items`. HTTP keeps `checkpoint.open_items` raw, with metadata in
`checkpoint.pointer_items` and `checkpoint.resolved_pointer_items`. The complete
`carry_open_items` and template body remain write-ready. Respect the existing item
paging; a first page is not the complete carry list. Web and text views join the maps
for display without changing raw items.

Resolution uses only loaded, caller-authorized project data, with no new I/O.
The five states are:

- `live`: a task, comment, reference or capability target exists. This proves existence,
  not the checkpoint's claim, and grants no authority to pointed text.
- `superseded`: an explicitly retired entry or superseded contribution. Additive
  `follows` deliveries remain live historical evidence.
- `unavailable`: absent, inaccessible, malformed, conflicted or ambiguous targets,
  including task/comment requests for record anchors and comments hidden by the existing
  brief/history record-comment filter. No target label or follow command distinguishes them.
- `unchecked`: uniform for every target of a kind the brief does not load: file, room,
  item, commit, doc and archive. No file, room, repository or document is fetched.
- `unresolved`: beyond 10 resolved pointers per item or 50 per brief. Tokens remain
  listed and contribute to `pointers_omitted`.

`req:` and `design:` entries also carry `flags`, a list (empty when nothing is wrong):

- `req:` resolves against the project's requirement records. `req:KEY` means the newest
  accepted revision, or the newest revision when none is accepted. A resolved entry carries
  `reference: {id, revision, sha256}` and follows with `requirements get ID`. Flags:
  `unknown-key` (no single readable requirement has that key; hidden, absent and another
  project's read the same), `unknown-revision`, `draft` (the revision is not accepted),
  `superseded` (a newer accepted revision exists, or the owner superseded the requirement)
  and `withdrawn` (the owner withdrew it). A superseded or withdrawn requirement reads
  state `superseded`, never `live`. Acceptance is the requirements read's own check
  (`resolved_acceptance` with the operator list); owner decisions are checked against
  the project's requirements governance, which the brief reads only when a `req:` token
  is present. A ledger, acceptance or owner state that cannot be verified reads
  `unavailable` with no flags.
- `design:` resolves against the reference entry `design.SCOPE`. Flags: `unknown-scope`
  (no such readable entry) and `not-selected` (only a draft revision). A retired entry
  reads `superseded`. A `#SECTION` is shown as `section: {anchor, state: "unchecked"}`:
  headings are checked once designs are indexed on the server.

Text output appends `flags=...` and `section=ANCHOR unchecked` to these lines.

Every pointer carries `trust: data`. Labels pass through `plain_text` before clipping
at 120 characters, without leaving a partial Unicode escape. Text display folds line
breaks and Unicode separators in labels, source, reason and evidence; direction marks
(including LRM, RLM and ALM) are escaped. Web rendering uses text nodes. Follow commands
are kit reads, never arbitrary URLs. Pointed comments do not become new directions.
The index is lazy: pure prose and unchecked kinds never scan the export for targets.
Production supplies already-authorized project rows; the optional `can_read` library
predicate can only restrict them further for direct embedders and tests.

The latest checkpoint's resolved list is shown within the requested item limit, with
its total and omitted count, independently of the open-item page.


## Read checkpoint summaries for many tasks

`b work summary TASK [TASK ...] [--json]` reads a batch of 1..100 distinct task IDs
under the shared project coordination lock. IDs must have the existing task-ID syntax;
invalid, repeated, empty or oversized requests are refused before the native read.
The batch shares one export snapshot instead of calling brief/history once per task.
The existing generation cache can avoid that export on a warm read; generation and
classification metadata queries are separate from the export.

The HTTP client provides the same read:

```sh
python http_client.py --base-url https://office.example --token=TOKEN work PROJECT summary TASK TASK2
```

Its `Client.task_summaries(PROJECT, [TASK, TASK2])` method posts
`{"tasks":["TASK","TASK2"]}` to `/v1/projects/PROJECT/tasks/summary`. This is a read,
without an idempotency reservation. Current project read authority is required,
including current membership and credential scope. Record anchors (including malformed
anchors), the merge slot and rows of issue type `event` or `gate` are hidden just as
on the task surface. A hidden task ID and an unknown ID return
exactly the same missing item; the read never probes again to explain absence.

Both forms return `schema_version: 1` and `items` in requested order. Each item has
`task`, `found`, native `status`, `checkpoint`, `last_post` and `coverage`.
The newest valid linked checkpoint contributes only its `comment_id`, `timestamp`,
`state`, `next_action`, `next_action_omitted_chars` and `open_items_count`.
Its `state` is the declared lane state only when the existing lane-state reader binds
that record to the selected checkpoint; otherwise it is `unknown`. No prose or age
implies a state. A missing checkpoint is null: its open-item count is unknown, never
an invented zero. Conflicting/unreadable checkpoint history gives unknown coverage;
invalid skipped checkpoint records give partial coverage. Neither means completion. Coverage describes the
summary fields, not provenance completeness or incorporation of all history.

`last_post` is the latest ordinary nonblank comment, ordered by timestamp then native
comment-ID text, with `comment_id`, `timestamp`, `author` and
`author_omitted_chars`. Record-family comments of any version and reserved record
comments, including review, integration,
checkpoint and lane-state records, never become ordinary posts. No checkpoint body,
provenance map, item text or task description is returned. Next actions are clipped
to 500 characters and authors to 96, with exact omitted-character counts. Unusable
comment identities/timestamps are reported with unknown coverage instead of an
unbounded or fabricated answer. The read changes no native task/comment record.

`work --owner` shares a matching checkpoint's activity selection between attention
and displayed fields within one request. An owner's earlier checkpoint remains a
separate selection from another author's current checkpoint. This reuse is neither
persistent nor shared between callers; current comment authority is still evaluated
by the read. Performance depends on the project and host; synthetic timings are
qualification evidence, not a guaranteed latency for another deployment.
