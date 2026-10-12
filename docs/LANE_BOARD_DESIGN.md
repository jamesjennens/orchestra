# Lane board: a machine-readable wait state, and one read that flags a stale wait

Design note for kittrial-5bb.263. One page. Delivered before code, for a quick read.
Base: current main `08ae77f5`. Owner: `kittrial-5bb` (epic). No implementation in this delivery.

## Why (from the task, and from the coordinator's own helper)

On 2026-10-11 a lane waited four hours for the coordinator's plan verification on three tasks. Its
checkpoints said so in prose and nobody was shown it. Two other lanes reported `awaiting-review` from
stale state, and one held a task it was not working on. The information existed; nothing pushed it to
the party who had to act, and nothing flagged its age. Periodic prose status updates are NOT wanted.

The coordinator's read-only intake helper, `show-waiting-on-coordinator.py` (38 lines), is
today's answer and shows exactly what is missing:

- it lists every `in_progress` task and then reads **all comments on each one** — unbounded, per task;
- it finds the newest `task-checkpoint-v1` comment and keeps only open items whose kind is
  `blocker`/`question`/`dependency` — **or, as a fallback, greps `'oordinator' in next_action`**;
- "who is being waited on" is therefore **prose substring matching on a sentence**;
- "has the coordinator answered" is `last_coordinator_comment > checkpoint_time`;
- nothing measures **how long** the wait has run, and nothing reaches the web.

The board replaces the prose inference with declared fields and the per-task scan with one bounded read.

## (1) Three optional checkpoint fields (additive)

Added to the existing checkpoint object; a checkpoint that omits them reads exactly as today.

| field | type | rule |
| --- | --- | --- |
| `state` | string | one of `working`, `waiting-on-coordinator`, `waiting-on-review`, `waiting-on-user`, `idle` |
| `waiting_on` | string | an **actor id or a role** (`coordinator`, `reviewer`, `user`). Required when `state` starts with `waiting-`; refused otherwise |
| `expect_by` | time | when the lane expects its next delivery or next check; ISO-8601 with an offset, in the future at write time |

Validation, all at checkpoint write, all refusing rather than coercing:

- `state` outside the five values is refused, naming the accepted set.
- `waiting_on` present without a waiting state, or absent with one, is refused.
- `expect_by` without an offset is refused (the existing `parse_moment` rule); a past `expect_by` is
  accepted but flagged on read, because a lane may legitimately write down that a deadline has passed.
- No person is inferred and no actor is derived: `waiting_on` is written by the lane or not at all.
  This is attribution, not authority.
- Bounded: `waiting_on` ≤ 96 characters, matching the existing actor-field bound.

They sit beside the fields that already exist and must not duplicate them: `state` is not
`next_action`, `waiting_on` is not `owner` (the assignee), and `expect_by` is not `activity_cursor`.
The prompt guidance is one sentence: set them at claim, at every checkpoint, and when a wait begins.

The field set in `briefing.validate_checkpoint` is closed, so this is a one-line addition there plus the
three validators; `docs/BRIEFINGS.md` and `templates/CHECKPOINT.json` carry the worked example. Note
that the `docs` catalogue the client serves (`onboarding.DOCUMENTS`) does not index the `*_DESIGN.md`
files today and this note does not add an entry, so it is a repository document reached through the
branch, not through `docs NAME`.

## (2) One bounded read: `lanes`

**One export read**, one pass, no per-task comment reads: the reader takes a single native export of the
project and derives everything from it. That is the whole point — the helper it replaces is O(tasks)
comment reads.

One row per **lane**, keyed by the session's registered name (`ds-sub-5bb.253`, `sol-1`), taken from the
sessions registry; a task with no lane reads as its assignee actor, so no lane is invisible.

Each row carries: the lane's name and actor; its held tasks (id, title, `review_state`, `whose_move`,
`next_actor`, `work_state`); the `state` of each task from its newest checkpoint; **how long it has been
in that state** (age of that checkpoint); the age of the lane's newest checkpoint anywhere; and two
flags:

- `waiting_too_long` — `state` is `waiting-on-coordinator` or `waiting-on-review` and the checkpoint age
  exceeds a configurable threshold, **default 15 minutes**;
- `expect_by_passed` — a declared `expect_by` is in the past.

It must also show what the helper shows today, made cheap by the declared fields:

- a checkpoint that names the coordinator as next actor with **nothing written by the coordinator since**
  — now a field comparison, not a substring match, when `state` is declared; the helper's prose fallback
  is retained only for checkpoints too old to carry `state`, and rows reached that way are marked
  `inferred: true` so a reader can tell declared from guessed;
- a task **held with no checkpoint** for longer than a threshold (same default), which a checkpoint-derived
  read would otherwise miss entirely.

Bounded output: a row cap with `next_offset`, the same shape `work` already uses.

**Route.** Read-only, so it is safe on the contributor route, and it must be, because a lane needs to see
its own row. But the *cross-lane* view is an operator/coordinator concern, so it is exposed on the host
route and the bound-key `coordinator` route; on the contributor route a caller sees only its own lane's
row. A member of another project sees nothing — the existing `key_project_refusal` already enforces that
before any read, and the board adds no new data path around it.

## (3) The web: a Lanes panel

A panel for owners and coordinators showing the same rows and the same two flags, reading the same
export-derived data through the service — no second implementation of the aging or the flags. It follows
the existing office panel conventions (`docs/OFFICE_SERVICE.md`) and disappears with them.

## (4) What an older kit and an older checkpoint do

- **Older checkpoint** (no `state`): reads as today; `state` is `unknown`, not `working`. The board shows
  the row with age only, and marks the coordinator-wait case `inferred: true` when prose is all it has.
  Nothing is back-filled and no prose is rewritten into fields.
- **Older kit** reading a new checkpoint: the field set is closed, so an older kit must ignore the three
  unknown keys rather than refuse the checkpoint. That is the compatibility requirement this design places
  on the change, and it is why the fields are optional rather than required.
- **Older kit** reading the board: the read does not exist, so it falls back to
  `show-waiting-on-coordinator.py` unchanged. Nothing is written to the runtime that an older kit cannot
  read.

## (5) Relationship to `kittrial-5bb.207` (not here)

`state` is deliberately the same vocabulary `kittrial-5bb.207` needs when it pushes a change to a waiting
agent, so that work adds the push and does not redefine the state.

## Open questions for the owner (2)

1. **Threshold default.** 15 minutes is the task's default. Lanes here have legitimately sat
   `waiting-on-coordinator` far longer overnight, so an owner may want a different default or a per-lane
   override. Recommended: keep 15 minutes, make it configurable, and do not silence a flag by aging it out.
2. **`waiting_on` as an actor id or a role.** The board's flags key off the *role* (`coordinator`,
   `reviewer`); an actor id is more precise but cannot be aged uniformly. Recommended: accept either,
   flag on the role, and show the actor id when given.

## What this note does not do

No implementation. No new record kind — these are three optional fields on a checkpoint that already
exists, and a read over data the kit already exports. Small note first, then the work in two
contributions (fields + read; then the web panel) if that reviews better, as the task allows.
