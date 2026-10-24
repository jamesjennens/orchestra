# Lane board: a declared wait state in a separate reserved record, and one read that flags a stale wait

Design note for kittrial-5bb.263, revision 2, answering review request
`01a12e0f-e60c-7684-a0a6-f38bb652c488`. Base: current main `2c5c3117`
(`2c5c31176e9ab8637b7a3e44b446754710bc8833`), rebased from `9afd062` when main moved.
Owner: `kittrial-5bb` (epic). No implementation in this delivery.

## Why (unchanged from revision 1)

On 2026-10-11 a lane waited four hours for the coordinator's plan verification on three tasks. Its
checkpoints said so in prose and nobody was shown it. Two other lanes reported `awaiting-review` from
stale state, and one held a task it was not working on — four cases, and two of them were the holder
sitting on its own move while its task read `changes-requested`. The information existed; nothing
pushed it to the party who had to act, and nothing flagged its age. Periodic prose status updates are
NOT wanted.

The coordinator's read-only intake helper, `koopa:/home/james/show-waiting-on-coordinator.py` (38
lines), is today's answer and shows exactly what is missing:

- it lists every `in_progress` task and then reads **all comments on each one** — unbounded, per task;
- it finds the newest `task-checkpoint-v1` comment and keeps only open items whose kind is
  `blocker`/`question`/`dependency` — **or, as a fallback, greps `'oordinator' in next_action`**;
- "who is being waited on" is therefore **prose substring matching on a sentence**;
- "has the coordinator answered" is `last_coordinator_comment > checkpoint_time`;
- nothing measures **how long** the wait has run, and nothing reaches the web.

The board replaces the prose inference with declared fields and the per-task scan with one bounded
read.

## 1. The form: a separate reserved record, `Kind: lane-state-v1`

The wait state is **not** three new keys inside the checkpoint object. It is one small record of its
own, written beside the checkpoint in the same act:

```
Kind: lane-state-v1
{"schema_version":1,"task":"kittrial-5bb.263","checkpoint":"<checkpoint comment id>",
 "state":"waiting-on-coordinator","waiting_on":"coordinator","expect_by":"2026-10-12T04:00:00+00:00"}
```

| field | type | rule |
| --- | --- | --- |
| `schema_version` | int | `1` |
| `task` | string | the task the checkpoint was written on; refused when it names another |
| `checkpoint` | string | the comment id of the checkpoint written with this record; refused when it names a checkpoint that is not the task's newest at write time |
| `state` | string | one of `working`, `waiting-on-coordinator`, `waiting-on-review`, `waiting-on-user`, `idle` |
| `waiting_on` | string | a **role** (`coordinator`, `reviewer`, `user`) or an **actor id**. Required when `state` starts with `waiting-`; refused otherwise |
| `expect_by` | time | ISO-8601 with an offset. Optional. **Any parseable value is accepted**, a past one included; an unparseable one is refused |

Rules, all enforced at write, all refusing rather than coercing:

- `state` outside the five values is refused, naming the accepted set.
- `waiting_on` present without a waiting state, or absent with one, is refused.
- `expect_by` is refused only when it cannot be parsed. A past `expect_by` is accepted and flagged on
  read (`expect_by_passed`), because a lane may legitimately write down that a deadline has passed.
  There is no "must be in the future at write time" rule.
- No person is inferred and no actor is derived: `waiting_on` is written by the lane or not at all.
  This is attribution, not authority. The flag keys off the **role**; the actor id, when given, is
  shown beside it and never converted into a role by guessing.
- Bounded: `waiting_on` ≤ 96 characters, matching the existing actor-field bound.
- Written with the checkpoint and never on its own: the checkpoint write is the only writer, so a
  lane cannot leave a `lane-state-v1` record that lies about a checkpoint it did not just write.
- **The checkpoint object itself is not touched.** Its field set in `briefing.validate_checkpoint`
  stays closed, and nothing existing grows a key. That is the whole point of this form.

The prefix is **reserved** in `reserved_comments.RESERVED`, so the raw `comments add` path can never
write one (the same guard that protects `Kind: task-checkpoint-v1`): the record is written only
through `checkpoint TASK --file payload.json`. It is deliberately **not** added to
`RECORD_ANCHOR_FAMILIES`: it anchors no row and hides no task. Until a later slice decides otherwise
it stays visible in `history` as an ordinary comment — which is exactly what makes an older kit
tolerant of it (§5).

## 2. Why not the three keys inside the checkpoint: what the readers actually do

The earlier draft claimed "an older kit must ignore the three unknown keys rather than refuse the
checkpoint". That claim is false, and it was load-bearing. On current main (and in the previous
release, which is byte-identical here — §6) `briefing.checkpoint_problems` builds a **closed** field
set (briefing.py:202-203) and refuses anything outside it: briefing.py:209-212 appends
`Invalid checkpoint: unknown fields: <names>`, so a checkpoint carrying `state`, `waiting_on` and
`expect_by` is **INVALID**, not ignored. Reader by reader, on main `2c5c3117`:

- **`brief`** — `checkpoint_state` (briefing.py:339-369) parses every `Kind: task-checkpoint-v1`
  comment; a failing one does not raise. Its id goes into `state['invalid']` (briefing.py:350-354)
  and the comment is **dropped from the linked chain**, so the newest *valid* checkpoint stays
  `current`. `brief` (briefing.py:682) then reports the warning
  `Malformed checkpoint comments ignored: <ids>` (briefing.py:829). **The row is not an error.** If
  an older valid checkpoint exists, the brief silently describes the OLDER record as the current
  position and next action (probe C2); if the dropped checkpoint was the only one, the brief reads
  "No checkpoint yet; current position and unresolved items have not been summarized." (probe B3).
- **`work --mine`'s `checkpoint_wait`** — work.py:246 returns the unknown shape when
  `state['invalid']`: `status: "unknown"`, `active: null`, `items: []`, `total: null`,
  `next_action: null`. That is not an error row and not a dropped row, but it is worse than dropping
  it: the lane's own recorded wait stops being visible and `active` is `null`, not `false`. The
  per-row attention fields are computed from `current` (work.py:744-745); a chain-level failure (a
  valid checkpoint whose `previous` is an invalid one) is caught at work.py:756 and sets
  `open_items`, `blocking_items`, `checkpoint_at` and `newer_activity` all to `null`. The displayed
  page's `checkpoint_queue_fields` returns all-`null` for an invalid state (briefing.py:1077).
- **`resume`** — `worker.py:105-119` sends `session resume`, and `sessions.py` reads only the session
  registry, never a checkpoint; `worker.py:140` then runs `work --mine`. The resume path therefore
  inherits exactly the `work` reading above, with no separate behaviour of its own.
- **the web brief** — `http_service.py:2365` maps `data['checkpoint']` from the canonical brief, so
  the task page shows the newest VALID checkpoint or none; the canonical warning is carried into the
  page through `http_service.py:2470` (`'warnings': data.get('warnings')`). The agent attention read
  raises a row only when the checkpoint history is unreadable at the CHAIN level, not merely when a
  comment is invalid: `counts['checkpoint_errors']` (http_service.py:5597) and the `checkpoint-error`
  action with `who='operator'` (http_service.py:5640).
- **writes stop** — `save_checkpoint` refuses any further checkpoint while an invalid entry exists:
  `Malformed checkpoint entries require correction before publishing another checkpoint`
  (briefing.py:932). `checkpoint --verify` (briefing.py:1028) and `checkpoint --directions`
  (briefing.py:1561) refuse the same way, and `checkpoint --latest` reports
  `invalid_checkpoint_comments` (briefing.py:1585, 1600) with the note "unresolved items are unknown,
  not zero".

So the in-checkpoint form has a rollback limit that cannot be designed away: after a rollback, every
checkpoint a newer kit wrote reads invalid, the task's wait state disappears, and **the older kit can
never publish another checkpoint on that task** until a human edits the tracker. The write-switch
alternative keeps the keys inside the closed field set behind a switch that is off by default, which
is the other form the review offered; it buys the same safety at the cost of an installation setting
and of a task that cannot be re-checkpointed while the switch is off. **The separate record is
chosen: no switch, no rollback limit.**

## 3. One bounded read: `lanes`

**One export plus the session registry**, one pass, no per-task comment reads: the reader takes a
single native export of the project and the sessions registry and derives everything from them. That
is the whole point — the helper it replaces is O(tasks) comment reads.

- **Threshold is an argument of the read** (`--idle-minutes`, default **15**), and an argument of the
  page. It is **not** a new installation setting. A flag is never silenced by age: age only ever adds
  a flag, and no default or threshold removes one.
- One row per **lane**, keyed by the session's registered name (`ds-sub-5bb.253`, `sol-1`), taken
  from the sessions registry; a task with no lane reads as its assignee actor, so no lane is
  invisible.
- Each **holding** row carries: the lane's name and actor; its held tasks (id, title, `review_state`,
  `whose_move`, `next_actor`, `work_state`); the task's declared `state`, who it waits on and its
  `expect_by` from the newest valid `lane-state-v1` record; **how long it has been in that state**
  (age of the checkpoint that the record names); the age of the lane's newest write anywhere; and
  three flags:
  - `waiting_too_long` — `state` is `waiting-on-coordinator`, `waiting-on-review` or
    `waiting-on-user` and the checkpoint age exceeds the threshold;
  - `expect_by_passed` — a declared `expect_by` is in the past;
  - `your_move_idle` — the row's `whose_move` is the HOLDER (the row is `changes-requested`, or a
    claimed task with nothing delivered) and the holder has written nothing on it for longer than
    the threshold. This is the flag for two of the four cases on 2026-10-11: lanes that said
    "awaiting review" while their own task was `changes-requested`.
- **Lanes that hold nothing** are rows too: every registered session with a checkpoint or a claim in
  the last 24 hours and no held task is listed as `idle` with the age of its last write, so "who is
  free" is answered by the same read.
- The `state` shown beside `whose_move` comes from **the same decision that already computes
  `whose_move`** (`work.move_decision`, work.py:146), not from a second rule. The board adds the
  declared wait state *beside* that decision; it never re-derives the move.
- It must also show what the helper shows today:
  - a checkpoint that names the coordinator as next actor with nothing written by the coordinator
    since — now a field comparison on the declared record when there is one;
  - the helper's prose fallback is retained only for checkpoints that carry no `lane-state-v1`
    record, and rows reached that way are marked **`inferred: true`**, so a reader can tell declared
    from guessed;
  - a task **held with no checkpoint** for longer than the threshold, which a checkpoint-derived read
    would otherwise miss entirely.
- **`show-waiting-on-coordinator.py` becomes unnecessary.** The acceptance evidence for the read
  slice is: on a **copy of a real export**, every row the helper prints is on the board, with
  `inferred: true` wherever the checkpoint carries no `state`.
- **Bounded output**: a row cap with `next_offset`, the same shape `work` already uses.

**Route.** Read-only, so it is safe on the contributor route, and it must be, because a lane needs to
see its own row. But the *cross-lane* view is an operator/coordinator concern, so it is exposed on
the host route and the bound-key `coordinator` route; on the **contributor route a caller sees only
its own lane's row**, and a **member of another project sees nothing** — the existing
`key_project_refusal` already enforces that before any read, and the board adds no new data path
around it. **Both are tested.**

### 3.1 Empty wakes (kittrial-5bb.397)

A worker `wait` can wake because the fingerprint of its move rows changed while none of the changed
rows is its move any more (the integrated-but-open case of kittrial-5bb.390 is the example): the
worker is woken to find nothing. That is not hidden and not a second wake rule; the same read records
it. Each lane row carries four additive fields outside the move fields and the three flags:

- `empty_wakes_24h` — how many DIFFERENT wake fingerprints in the last 24 hours changed a row but
  left no row that was the lane's move;
- `empty_wake_task`, `empty_wake_trigger`, `empty_wake_at` — the newest one: its task, the field class
  that changed (`new-comment`, `review-state`, `assignment`, `delivery`, `review-items`, `status`,
  `guidance` or `edit`) and when.

The record is one append-only JSONL per lane under the project's `.wait-wakes/` (a short hash of the
actor names the file), capped at 4096 bytes / 64 entries / 24 hours, with the last-seen rows in a
bounded `.last.json` beside it; it is written by the worker read itself, never by a tracker write,
and a log error is swallowed into an answer `warnings` entry. The coordinator form gains an
`empty-wake` trigger: one row per lane whose `empty_wakes_24h` is 3 or more. That group is part of the
coordinator fingerprint only when it has a row, so an unchanged quiet answer keeps its old fingerprint.

## 4. The web: the Lanes panel

The panel belongs on the **project page of the web service, for owners and coordinators**
(`kittrial-5bb.209`'s role). It reads the same export-derived data through the service — no second
implementation of the aging or the flags — and it takes the same threshold **as an argument of the
page**. The reference to office panel conventions is dropped: this is a project-page read, not an
office surface.

**How it scopes the read.** The web service calls the endpoint without a bound key, so
`lanes_scope_for` answers `all` today; the panel must never rely on that default. It passes its own
scope: owners and coordinators — the same `review_workflow.listed_operator_actor` definition the
endpoint itself uses — read every lane, and everybody else is scoped to their own agents' lanes,
exactly the contributor route's rule. The transport default is a property of the route, not an
authority the page may inherit. The backend sends the authenticated caller's principal with a
`read` authority descriptor and requires live authority on the endpoint; only `work lanes` has
that table entry. The descriptor authorizes the member read, while `lanes_scope` still only
narrows the route-selected scope.

## 5. What an older checkpoint, an older kit and a rollback mean

- **Older checkpoint** (no `lane-state-v1` record): reads as today; `state` is `unknown`, not
  `working`. The board shows the row with age only and marks the coordinator-wait case
  `inferred: true` when prose is all it has. Nothing is back-filled and no prose is rewritten into
  fields.
- **Older kit** reading the new record: an unknown `Kind:` is an **ordinary comment** to it. The
  previous release's `reserved_comments.is_record_comment` matches only its own known families
  (reserved_comments.py:881), `hide_records` therefore keeps the comment, and no reader parses a
  `Kind:` prefix it does not know. The record is not a checkpoint and does not touch the checkpoint
  object at all, so an older kit reads, writes and re-checkpoints the task exactly as before. This is
  proved in §6, not asserted.
- **One reading is deliberately wrong on an older kit.** Because it cannot know the record was
  written with the checkpoint, it counts the record as activity the checkpoint did not incorporate
  and reports the checkpoint **stale** (`checkpoint.newer_activity: true`, and the matching wake flag)
  until the next checkpoint supersedes it. The tip kit treats a `Kind: lane-state-v1` record that
  names checkpoint C and was written by C's author as part of C — in the brief, in `work`/next-action
  and in the board's checkpoint age — so only a kit at or after this change reads a declared wait
  state as current. This is the one rollback cost of the separate record: no tracker repair, no switch
  and no unwritable task, but a rolled-back reader sees one spurious stale/wake reading per declared
  state. The capability record says the same.
- **Older kit** reading the board: the read does not exist, so it falls back to
  `show-waiting-on-coordinator.py` unchanged. Nothing is written to the tracker that an older kit
  cannot read.
- **Going back to an older kit** therefore means: the wait state stops being *read* (the board read
  and the panel do not exist there, and the helper is unchanged), while every checkpoint, every task
  and every write path keeps working — the older kit sees one extra ordinary comment and nothing
  else. No tracker repair, no switch, no task that cannot be checkpointed again.

## 6. The older-kit probe (evidence for §1 and §5)

Kit under test: the previous release
`/home/james/beads-team-pilot/releases/orchestra-9afd062-20261012zzzd`. Its `briefing.py`,
`reserved_comments.py` and `endpoint.py` are **byte-identical** to current main `2c5c3117`
(`briefing.py 50cc7ce6c7e8498c8c8e8585831ec4ce89ffb21f5a24cd8c8487e3c2e0436813`,
`reserved_comments.py f89d829a50800c31d7c598ed5079946c52f7f6b2909abf72b6869d36e76deb4d`,
`endpoint.py 4e140e1db852551fe414b47aaf4278dc1f4dd7fab6675c3670ba4a7a43415e15`); `work.py`,
`http_service.py` and `worker.py` differ only by the `kittrial-5bb.253` next-action integration,
which touches no invalid-checkpoint path.

Disposable tracker: `bd init` (embedded, pinned bd 1.2.2) under `/tmp/run99-probe/proj` with `HOME`,
`XDG_CONFIG_HOME` and `DOLT_ROOT_PATH` scoped inside `/tmp/run99-probe`. No live runtime is touched.

```
cd /home/james/beads-team-pilot/releases/orchestra-9afd062-20261012zzzd
python3 /tmp/run99-probe-src/probe.py \
    /home/james/beads-team-pilot/releases/orchestra-9afd062-20261012zzzd \
    /tmp/run99-probe /home/james/rv39r2-2GR7/rt/bin/bd
```

Observed, on a task carrying a valid checkpoint **and** a `Kind: lane-state-v1` record
(task `proj-oci`):

```
A1 brief        -> checkpoint comment 01a12e20-d322-... found; warnings are the ordinary
                   direction/activity lines only; no "Malformed checkpoint" line
A2 work --mine  -> checkpoint_wait.status "recorded", is_current true, active true;
                   open_items 1, blocking_items 1
A3 reserved     -> is_record_comment(lane-state body) false; record_comment_kind null
A4 hide_records -> the kind line "Kind: lane-state-v1" is still present after hiding
A6 writer       -> the older kit writes a SECOND checkpoint on the same task
                   (comment 01a12e20-dd48-...): the task stays fully writable
A7 resume       -> sessions resume returns normally (register + resume, disposable session root)
```

Observed, on a task whose checkpoint carries `state`, `waiting_on` and `expect_by` (task `proj-p2u`):

```
B1 validate_checkpoint -> REFUSED: Invalid checkpoint: unknown fields: expect_by, state, waiting_on
B2 checkpoint_state    -> current null; invalid ["01a12e20-e142-..."]
B3 brief               -> checkpoint null; warning
                          "Malformed checkpoint comments ignored: 01a12e20-e142-...";
                          current_position "No checkpoint yet; ..."
B4 work --mine         -> open_items 0, blocking_items 0, checkpoint_at null,
                          checkpoint_wait {status "unknown", active null, items [], total null}
B5 checkpoint --verify -> REFUSED: Malformed checkpoint entries require correction before verification
B6 checkpoint --latest -> checkpoint null; invalid_checkpoint_comments ["01a12e20-e142-..."]
B7 save_checkpoint     -> REFUSED: Malformed checkpoint entries require correction before
                          publishing another checkpoint
```

Observed, on a task with a valid checkpoint and then an invalid newer one chained to it
(task `proj-cbc`):

```
C1 checkpoint_state -> current is the older VALID checkpoint 01a12e23-1def-...;
                       invalid ["01a12e23-2087-..."]
C2 brief            -> checkpoint_comment_id 01a12e23-1def-... (the OLDER record),
                       current_position "valid checkpoint before the invalid one",
                       warning "Malformed checkpoint comments ignored: 01a12e23-2087-..."
C3 work --mine      -> open_items 0, blocking_items 0, checkpoint_at set,
                       checkpoint_wait {status "unknown", active null, items [], total null}
```

C3 is the silent failure: even though a valid checkpoint exists, the lane's own wait reads
`unknown` and `active: null` — the wait is not flagged, it is not false, it is *unknown*.

B7 is the rollback limit. A and B together are why the separate record is chosen and why the
in-checkpoint keys are rejected.

## 7. Implementation contract (from the review, recorded here for the code)

- The read is **ONE export plus the session registry, no per-task comment read**, bounded with
  `next_offset`.
- The state shown beside `whose_move` comes from the same decision (`work.move_decision`), never from
  a second rule.
- A lane sees **only its own row** on the contributor route and a **member of another project sees
  nothing**; **both are tested**.
- `koopa:/home/james/show-waiting-on-coordinator.py` must become unnecessary: **show, on a copy of a
  real export, that every row it prints is on the board**, marked `inferred: true` where the
  checkpoint has no state.
- Worker prompts and guides: **one sentence** — set the fields at claim, at every checkpoint and when
  a wait begins. The start document must stay under its 8000-byte bound
  (`onboarding.START_LIMIT`, onboarding.py:41).
- Windows: **no POSIX-only calls in new tests without a skip guard**, and the whole suite is run on
  the tip.
- Capability payload: **propose a new key for the board**, `work.lanes-board` (the
  `capability_records.KEY` shape `[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+`,
  capability_records.py:73).
- Two contributions, as proposed: **fields and the read first, the page second.**

## 8. The prose-direction case, found on this task itself

Recorded as the design's own evidence, not as a new design element. On this task the coordinator
directed a merge-forward as a plain comment (`01a12f2e`, 2026-10-12T08:16Z: main has moved, merge it,
keep both sides, deliver as a superseding contribution). While that direction was outstanding,
`work --mine` still returned the task as `awaiting-review` with `pending_review_items` 0, so the
documented read path said there was nothing to do while the coordinator was in fact waiting on the
lane. The direction surfaced only through the brief's `next_action`, which had to be read as prose,
and only because a worker went looking. A `Kind: lane-state-v1` record with
`state: waiting-on-coordinator` and an `expect_by` would have made that row self-describing, and the
board's `waiting_too_long` flag on a stale checkpoint would have pushed it to the party who had to
act instead of leaving it to be discovered.

## 9. Relationship to `kittrial-5bb.207` (not here)

`state` is deliberately the same vocabulary `kittrial-5bb.207` needs when it pushes a change to a
waiting agent, so that work adds the push and does not redefine the state. The record lives beside
the checkpoint precisely so that the push work can extend it without reopening the checkpoint schema.

## 9a. Needs investigation (kittrial-5bb.398)

A lane may raise a fault flag that is its own channel (`lane_state.newest()` and so the held task's
declared state skip needs-investigation records, so a raise never replaces `waiting_on`, `expect_by` or the
`checkpoint-to-coordinator` trigger, and a clear gives nothing back because nothing was taken). It is independent of its working/waiting state: a `lane-state-v1`
record with `state: needs-investigation`, a required `reason` (at most 300 characters, one printable
line) and an optional `about_task`. The reason is untrusted everywhere: escaped, clipped to 300 and
folded onto one line in text views. The board's lane row carries `needs_investigation` (reason, task,
record, since, age) on every lane, faulted lanes sort first, and the flags summary counts them. The
flag is the lane's newest raise across all its tasks, closed ones included; a later state record never
clears it. Only `coordinator fault-clear` (or the host `admin.py fault-clear`) clears it, writing a
`lane-fault-clear-v1` comment plus a journal entry that records who and when. A newer raise does not hide an older open one: the
row's `needs_investigation.earlier` lists up to five older open raises and `earlier_omitted` counts the rest (a clear
of the newest ends them all), and the wait row carries `task` (the `about_task`, else the raised-on task),
`raised_on`, the older reasons and `earlier_omitted`. The web Lanes panel renders the reason as text, with the
omitted count. The coordinator wait gains a `needs-investigation` trigger, ordered before any empty-wake trigger.
An EMPTY group never enters the coordinator fingerprint, so every fingerprint is unchanged while nothing is
flagged; a non-empty group is fingerprinted by stable fields only (lane, actor, task, raised-on task, raise
record and time), never by the reason text or the age. An older kit drops the unknown state and so does not
show the flag; the clear reads there as an ordinary comment. Use it for process faults, not task
blockers, which stay open items.

## 10. Decisions taken (the two former owner questions are closed)

1. **Threshold** — 15 minutes by default, given as an argument of the read and of the page, **not** a
   new installation setting. A flag is never silenced by age.
2. **`waiting_on`** — accept a role (`coordinator`, `reviewer`, `user`) or an actor id; flag on the
   role; show the id when given.
3. **`expect_by`** — a past value is accepted and flagged; refuse only what cannot be parsed.

## 11. What this note does not do

No implementation. No change to `briefing.py`, `endpoint.py`, `http_service.py`, `work.py`,
`worker.py`, `capability_records.py`, `reserved_comments.py` or any test. This is the note the task
asks for before the work; on acceptance the work follows in two contributions (the record and its
read first, then the project-page panel).
