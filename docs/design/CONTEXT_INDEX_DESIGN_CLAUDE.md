# Orchestra as a durable, indexed context store: design note (Claude lane)

Design note for `kittrial-5bb.392`, written by the Claude lane (orc-coord-capability). Base:
main `7fe1a469685e7ce4a21589878636e55c2d33eb61` (release 1023e). No code in this delivery.

This task is built twice, independently. **I did not read or open the twin task
(`kittrial-5bb.393`), its branch or its comments before delivering round 1.**

Plan checkpoint `01a16b21`, verified by the coordinator on 2026-10-24, with these answers and asks:
- Q1: the file is named for the Claude lane.
- Q2: a standing lane task plus reference drafts; say what happens when a lane is retired or moves
  host.
- Q3: search computed at read time, bounded, with its bounds and its cost stated on a project the
  size of kittrial.
- Two asks:
  - keep the pointer grammar backward-compatible both ways;
  - treat every resolved pointer as untrusted text on every surface.

**Revision 2** answers review `01a16b5e` (request changes on round 1, contribution `01a16b55`):
- **Five corrections:**
  - escaping is `briefing.plain_text`, not `clip` (§2.1.3, R1);
  - the standing-task label is new, not existing (§2.3, §2.7);
  - `transition()` matches open items by id (§2.1.1, R7);
  - the command is `work lanes`;
  - the rounds view uses the review chain's real operations, fields and item states (§2.4).
- **One design problem both notes missed:** a held standing task counts against the claim limit
  and reads as the holder's move. §2.7 designs the exemption, who may grant it, and its tests
  (R11).
- **Four ideas taken from the twin note**, read only after round 1 was delivered, from the branch
  the coordinator published, and only for the items the review named:
  - permission before ranking, counts and cursors, with cursors bound to the caller's scope
    (§4.4);
  - a coverage and staleness envelope on every answer (§4.5);
  - an `archive:` pointer, a file digest plus a line range, for items migrated from the
    coordinator's log (§2.1, §6.1);
  - a memo keyed by the read-cache generation that never becomes a second store (§4.6).

## 0. What I read before writing (about 1,950 lines)

The task allowed about 2,500 lines of reading. I read, in this order:

| what | lines |
| --- | --- |
| `docs/BRIEFINGS.md` 1-120: `brief`, `--full` and its bounds, `history`, `checkpoint_wait`, the selection facts | 120 |
| `docs/LANE_BOARD_DESIGN.md` 1-172: `lane-state-v1`, why it is not inside the checkpoint, the lanes read | 172 |
| `docs/OPEN_ITEMS_DECISIONS_DESIGN.md` 51-235 and 534-656: today's `open_items`, anchors, reuse, the item/question/decision reads, brief attention | 308 |
| `docs/REFERENCE_CATALOG_DESIGN.md` 51-101 and 708-791: the catalog at a glance, `ref get` and `ref list` | 135 |
| `docs/CAPABILITY_INDEX_DESIGN.md` 454-525: `capability find`, the hidden-surface list, attention | 72 |
| `docs/PROJECT_ROOMS_DESIGN.md` 5-44, `docs/TASK_FILES_DESIGN.md` 5-25: the summaries | 61 |
| `docs/SESSIONS.md` (all): the session registry, resume, handoff | 97 |
| `templates/CHECKPOINT.md` (all): field limits and the `lane_state` sibling | 90 |
| section headings of those seven designs and of `docs/CLI_CONTRACT.md` | about 370 |
| the first lines of `open_items.py`, `rooms.py`, `task_files.py`, `lanes.py`, `lane_state.py`, `read_cache.py`, plus the function lists of `native.py` and `activity.py` | about 170 |
| the brief of `kittrial-5bb.391` (title and description) | 3 |

For revision 2 I also read the code the review cited:
- `briefing.py` 355-380 (`transition`) and 505-522 (`clip`, `plain_text`);
- `claim_limit.py` 1-190;
- `review_workflow.py` 85-110 (the operations) and 2290-2330 (how items are recorded and
  answered);
- `reserved_comments.py` 785-905 (reserved labels, `gt:slot`);
- `work.py` 343-373 (`move_decision`);
- twin note sections 2.3, 3.1, 3.4, 4.3, 5.2-5.3 and 6.1 (about 120 lines).

What I did **not** read, and where the note therefore names something to confirm when it is built:
- `docs/REVIEWS.md` and `review_state.py`. The rounds view (§2.4) now uses the operation and field
  names of `review_workflow.py`, and slice 3 confirms the derived item states against
  `review_state.py`;
- `capabilities.py` beyond the code-index hits, so the reuse of its scoring (§4) is confirmed in
  slice 4;
- any coordinator handover file or lane notes file on any host. Those are private, and §6 is
  written from the brief's description of them.

**Built today** (from the module headers): checkpoints with `open_items` and `resolved`,
`lane-state-v1`, `work lanes`, the reference catalog, capability records, task files and rooms
slice 1, and the read cache. **Not built:** open items and decisions as records (`open_items.py` is
slice 0), the rooms scribe and room items, and any search across record kinds.

## 1. The problem, and the one principle

**The problem.** No session should have to hold everything in its context at once. For one task
Orchestra already manages: the newest checkpoint is its summary, `brief` reads it within bounds, and
`history` fetches detail when asked. The brief names five places where that breaks:
- **(a)** a coordinator's own state lives in a 5,300-line append-only handover file outside Orchestra;
- **(b)** headless lanes keep `notes.md` on their own host, invisible to others and lost when a lane
  moves;
- **(c)** a task with several review rounds has no compact list of what each round settled;
- **(d)** there is no query for "what do we know about X" or "which task decided Y";
- **(e)** lane facts (host, model, identity, how to nudge) live in coordinator files.

**The principle: store each fact once, where its writer already writes, and make everything else a
pointer to it.** A small, fixed set of existing records holds the live state (checkpoints,
reference entries, review records, decision issues). One grammar lets any of them point at any
other. Three reads walk the pointers: `brief`, the new `review --rounds` and the new `find`.

Nothing in this design is a second log. No record grows without bound, and no status is written on
a timer.

The task's seven asks are answered in sections 2 (store), 3 (read paths), 4 (search), 5 (currency),
6 (migration), 7 (risks and tests) and 8 (slices).

## 2. What to store where

### 2.0 The answer in one table

| gap | stored in (existing record) | new? |
| --- | --- | --- |
| (a) coordinator state | the newest checkpoint of the standing coordinator task (`kittrial-5bb.391`), each open item's `source` holding pointers (§2.1) | the pointer grammar only; no field, no record kind |
| (b) lane notes | facts become reference drafts; task state goes into that task's checkpoint; the lane's own cross-task list goes into a standing **lane task** (§2.3) | a new, guarded `standing-task` label with a host journal (§2.7), and one field on the `work lanes` row |
| (c) what each round settled | the review records that already exist, projected by `review TASK --rounds` (§2.4) | a read only |
| (d) "what do we know about X" | the records above, searched by `find` (§4) | a read only |
| (e) lane facts | reference entries under `lane.NAME.*` (§2.5) | a key convention |

**The design adds no record kind, no checkpoint field and no reserved prefix.** That is deliberate:
- The checkpoint's field set is closed. An older kit refuses a checkpoint carrying an unknown key,
  and then can never publish another checkpoint on that task (LANE_BOARD_DESIGN §2).
- Every addition here therefore lives *inside* a string field an older kit already accepts, or in a
  read.

### 2.1 The pointer grammar

**Today.** An open item's `source` (at most 240 characters) and a resolved item's `evidence` (at
most 240) are free text. Coordinators already write pointers into them in prose: "comment of today",
"evidence folder", "see .379". A reader has to guess what is meant and then search for it.

**The grammar.** A **pointer** is one whitespace-separated token of the form `KIND:VALUE`.

| kind | value | follows with |
| --- | --- | --- |
| `task` | a native task id, e.g. `example-12` | `brief example-12` |
| `comment` | a native comment id (a UUID) | `history TASK --cursor ...` via the comment's task (resolved, §2.1.3) |
| `ref` | a reference key, e.g. `host.build.load-rule` | `ref get KEY` |
| `cap` | a capability key | `capability get KEY` |
| `item` | an open-item record id (when those exist; `open_items.py` slice 0 names them) | `items get ID` |
| `file` | `TASK/SHA256` of a task file | `files get TASK SHA256` |
| `room` | `ROOM-ID#SEQUENCE` | the room read at that sequence |
| `commit` | a 7 to 40 hex commit id | not resolved by the kit (no repository on the server); shown as `unchecked` |
| `doc` | `PATH#ANCHOR` of a served document | `docs PATH` |
| `archive` | `SHA256#LSTART-LEND`: the digest of a frozen private file and a line range in it, e.g. `archive:3f06...c5#L120-L135` | not resolved by the kit (the file is outside Orchestra); shown as `unchecked`, with the range; whoever holds the file checks it by its digest (§6.1) |

Rules:
- `KIND` is one of the ten above, lowercase.
- `VALUE` is 1 to 200 characters from `[A-Za-z0-9._/#:-]`. An empty value is not a pointer, so the
  prose "see task: the review" contains no pointer.
- A field may hold several pointers and prose around them, all within its existing 240 characters.
  Example: `source: "comment:01a16b21-... ref:host.build.load-rule (asked by James)"`.
- A token that does not match is prose. A field with no matching token is pure prose and is treated
  exactly as it is today.

#### 2.1.1 Backward compatible both ways (the coordinator's first ask)

- **An older kit reading a new checkpoint.** The pointer lives inside a string field the older kit
  already validates by length alone. The older kit shows the field as text and can publish the next
  checkpoint. `transition()` matches open items by **id**: an item open before and absent now must
  be resolved, so a carried item keeps its id. Its `source` may be copied unchanged, which is what
  `carry_open_items` and the checkpoint template do, and that keeps its pointers. Nothing is
  refused and nothing is lost.
- **A newer kit reading an older checkpoint, or any prose.**
  - A field with no pointer gets `pointers: []`. There is no warning, because prose is legal
    forever.
  - Prose that happens to match, such as `commit:abc1234` in a sentence, resolves like any pointer.
    A pointer that does not resolve is reported as `missing`, as *information*: it is never an
    error, never a refusal and never changes whether the checkpoint is valid.
- **Writes.** No write gains a rule. A checkpoint with a malformed pointer is accepted exactly as
  the same text is accepted today, so there is no path by which this design makes an existing
  checkpoint unpublishable.
- **Rollback.** Nothing is written that an older kit cannot read, so there is no rollback boundary.

#### 2.1.2 Resolution in `brief`

`brief TASK` adds a `pointers` array beside each open item and each resolved item it already shows:

```json
{"pointer": "ref:host.build.load-rule", "kind": "ref", "state": "live",
 "label": {"text": "Build host load rule", "omitted_chars": 0},
 "trust": "data", "follow": "ref get host.build.load-rule"}
```

`state` is `live`, `superseded` (a retired reference entry, a superseded contribution, or a task
closed as superseded), `missing`, `unavailable`, or `unchecked` (`commit`, `doc` and `archive`).

**`unavailable` is used for anything the caller may not read, whether or not it exists.** So a
pointer never reveals that a hidden thing exists (§7, R2).

**Bounds:**
- at most 10 pointers resolved per item, and 50 per brief;
- the rest are listed with `state: "unresolved"` and counted in `pointers_omitted`;
- the label is clipped to 120 characters.

**Cost.** Resolution uses only data the brief has already loaded, or one cheap read per kind:
- `task`, `comment`, `ref`, `cap` and `item` resolve against the native export the brief already
  holds. Reference and capability anchors and their comments are rows in that export;
- `file` reads one host-journal entry by name;
- `room` reads one room index;
- no pointer causes a per-task comment read.

#### 2.1.3 Pointers are untrusted text, on every surface (the coordinator's second ask)

A resolved pointer brings back text someone else wrote: a reference title, a task title, a room
line. On every surface:
- the label is passed through `briefing.plain_text` first, which turns C0/C1 controls, the
  Unicode line and paragraph separators and the bidi controls into `\uXXXX` escapes, so they cannot
  reshape a reader's screen or prompt. Only then is it cut by `briefing.clip` into an excerpt
  object (`{text, omitted_chars}`). `clip` itself only truncates;
- every entry carries `trust: "data"`;
- **prompts carry the pointer token, never the label.** `agent_prompts` and any generated
  AGENT.md show `ref:KEY`, not the reference's title, following the rule the capability index
  already set ("prompts carry keys and tokens only");
- the web page renders the label as text (no HTML, no link other than the kit's own follow route),
  through the same escaping the brief page already uses for checkpoint text;
- a `comment:` pointer resolves to the comment's task and author and its first 120 characters. It
  is never treated as a newer instruction than the checkpoint that points at it.

`resolved` entries get the same treatment. A pointer in `evidence` is shown the same way, and a
resolved item's evidence is not "proved" by resolving: `live` means "this record exists", never
"this record says what the item claims".

### 2.2 (a) The coordinator's state: the standing coordinator task

`kittrial-5bb.391` is already the pattern. It is a standing task, never closed, whose newest
checkpoint is the coordinator's live state. A fresh coordinator reads `brief` of it first. This note
adds the discipline that makes it an index rather than a second log:

- **It is an exempt standing task** (§2.7, kind `coordinator`), so holding it costs the coordinator
  no claim slot and never reads as its move.

- **One open item per live thing**: a delivery in review is a `dependency`, a release in flight or
  a lane problem a `blocker`, a question to James a `question`.
  `text` says what it is in at most 400 characters. `source` holds the pointers to where the detail
  lives: `task:...`, `comment:...` (the review), `ref:...` (the fact), `commit:...` (the release).
- **`summary`** (1,000 characters) is the release line and the counts.
- **`next_action`** is the coordinator's own next move.
- **Settled things leave.** When a thing is done, it moves to `resolved` with a pointer in
  `evidence`, and is gone from the next checkpoint's `open_items`. The chain of checkpoints is the
  history; the newest one is the state. This is the existing `transition()` rule, used as intended.
- **Bounds that already exist hold the line.** At most 100 open items and 80 KB per checkpoint. A
  coordinator with more than 100 live things has a backlog to triage, not a store to grow.
- **What does not go in it:**
  - anything a worker must act on: workers are assigned through tasks, and .391's text is data;
  - anything private to the coordinator's host (§2.5).

**Several coordinators** (one per project, docs/COORDINATORS_PER_PROJECT_DESIGN.md) each get their
own standing task in their project. A cross-project view is not designed here. Each project's
`find` and `brief` stay project-scoped (§4.4).

### 2.3 (b) Lane notes: reference drafts, checkpoints, and a standing lane task

A lane's `notes.md` mixes three different things, and each already has a home:

| what the note is | example (placeholders) | where it goes |
| --- | --- | --- |
| a durable operational fact | "the build host allows at most two heavy runs; wait while load is above 20" | a **reference draft**, owner `person:orchestra-coordinator`, accepted with attestation (guidance item 3) |
| an owner's standing instruction | "never read live installations' configuration" | a **reference draft**, owner `person:james`, authority the owner's words |
| the state of one task | "bundle carried to the build host; suite at 5,100 of 7,000" | **that task's checkpoint** (`summary`, open items) |
| the lane's own cross-task list | "three deliveries waiting on review; the next run must re-check X" | the **standing lane task**, below |
| a secret, a credential, a private path | key locations, tokens | **nowhere in Orchestra**. It stays on the lane's host. |

**The standing lane task:**
- One per lane, title `Lane state (NAME)`, assigned to the lane's actor, status `in_progress`, never
  closed while the lane exists.
- It carries the **new** label `standing-task`. No such label exists in the kit today. Only the
  coordinator route can grant it, and §2.7 says how and why.
- Its newest checkpoint is the lane's current cross-task list, in the same discipline as §2.2: one
  open item per live thing, pointers in `source`, settled things move to `resolved`.
- **No new item kind.** A note that fits none of `blocker`, `question`, `decision`, `correction` or
  `dependency` is either a fact (a reference draft) or noise. That is the line that stops the lane
  task becoming `notes.md` again.
- The `work lanes` row gains one field, `lane_task`: the id of the row actor's exempt standing task
  (§2.7), or `null`. It is derived from the export and the standing-task journal `work lanes` can
  read, with no per-task read. When a holder has more than one, only the exempt one is named, with
  a warning.
- **The lane writes it** at the points it already checkpoints: when it starts or finishes a task,
  and when it starts waiting. It is not written on every run.
- **Visibility:** the lane task is an ordinary task, readable by project members. It holds nothing
  a member may not read; secrets stay off it by the table above.

**When a lane is retired** (the coordinator's ask under Q2):
1. The lane writes a final checkpoint on its lane task. Every open item is resolved, or carried with
   a pointer to where it now lives (another lane's task, .391, or a reference draft).
2. The coordinator closes the lane task with reason `lane retired`. It stays readable in history.
3. The `lane.NAME.*` reference entries are retired through the catalog's own retirement, naming a
   successor where a new lane takes the work.
4. The `work lanes` board stops listing the lane once it holds nothing and has written nothing for 24
   hours (its existing rule), so no flag is raised by retirement itself.
5. If the lane cannot write the final checkpoint (it is gone), the coordinator writes it. The
   checkpoint's author records who did, and nothing is inferred.

**When a lane moves host:**
- **Same actor.** The lane resumes with its saved actor (SESSIONS.md: a moved worker keeps its
  actor). The lane task, its checkpoints and its open items are unchanged, and only
  `lane.NAME.host` is revised in the catalog. **This is the case the design exists for:** nothing
  on the old host is needed, because nothing that matters lived only there. Only secrets have to
  be re-provisioned, and they never left the host.
- **New actor** (a new registration): this is a handoff under the existing explicit handoff rules.
  The coordinator reassigns the lane task to the new actor, and the new actor's first checkpoint on
  it carries every open item forward. The old actor's checkpoints stay as history. The `lane_task`
  field follows the assignee, so the board shows the new row at once.

### 2.4 (c) What each review round settled: `review TASK --rounds`

A task with several rounds already holds every fact in its review chain (`Kind:
contribution-review-v1`, `review_workflow.py`). It lacks one bounded page of those facts.

**The operations the view reads.** The chain's operations and their fields
(`review_workflow.EXTRA`):

| operation | fields |
| --- | --- |
| `contribute` | `repository`, `commit`, `base_commit`, `delivery`, `summary`, `supersedes` |
| `request-changes` | `contribution` and `items` of `{id, text, severity}`; `severity` is `blocking` or `note` |
| `respond` | `contribution` and `resolutions` of `{request, item, reason, evidence}` |
| `resolve-item` | the requester's own `{request, item, reason}` |
| `withdraw` | `contribution`, `reason` (the disposition `withdrawn` or `superseded`) |
| `approve` | `contribution`, `summary` |
| `request-review`, `decline-review` | as today |

**The view.** **`review TASK --rounds`** is a read-only projection of those records, newest round
first. A **round** is one contribution and every review operation that names it.

```json
{"task": "example-12", "rounds_total": 3, "rounds_omitted": 0,
 "rounds": [
  {"round": 3, "contribution": "01a1...", "commit": "abc1234...", "base_commit": "def5678...",
   "delivery": {"kind": "remote", "branch": "contrib/example-12"}, "supersedes": "01a0...",
   "at": "2026-10-20T10:00:00Z", "disposition": null,
   "outcome": {"operation": "approve", "record": "01a2...", "at": "2026-10-21T09:00:00Z"},
   "items": [], "next_contribution": null},
  {"round": 2, "contribution": "01a0...", "commit": "...", "disposition": "superseded",
   "outcome": {"operation": "request-changes", "record": "01a1..."},
   "summary": {"text": "...", "omitted_chars": 0},
   "items": [{"request": "01a1...", "id": "B1", "severity": "blocking",
              "text": {"text": "...", "omitted_chars": 0}, "state": "answered",
              "reason": {"text": "...", "omitted_chars": 0}, "evidence": "comment:01a1...",
              "by": "respond", "record": "01a1..."}],
   "next_contribution": "01a1..."}],
 "coverage": {"records": 9, "malformed": 0, "proved": true}}
```

**Each item's `state`** is derived from the chain, never stored:
- `open`: requested, with no resolution yet;
- `answered`: the task's side resolved it with `respond`, giving a `reason` and `evidence`;
- `settled`: the requester closed it with `resolve-item`;
- `withdrawn` or `superseded`: the round's contribution carries that disposition before the item
  was answered.

`settled` means the chain proves the item no longer blocks that round. It does **not** mean
integrated or deployed. A new contribution alone settles nothing. A `note` item is shown with its
`severity` and never counted as blocking.

**`coverage.proved`** is false when an old or malformed chain cannot prove a state. Those items read
`unknown`, with a warning, rather than being guessed.

**Bounds:** at most 20 rounds (the rest counted in `rounds_omitted`), at most 50 items per round,
and every text escaped (`plain_text`) and clipped to 200 characters. `evidence` is kept whole up to
its existing 240-character limit, because it is a pointer field (§2.1).

**Source.** The view uses the same chain walk that `review TASK`, `work` and `brief` share, so it
cannot disagree with them about what is approved. Slice 3 confirms the derived states against
`review_state.py`, which I have not read.

**What it answers.** A new lane taking over a task reads one page and sees:
- what was asked in each round;
- how each item was answered or settled;
- what is still open.

It then follows a pointer only for the item it needs. Nothing is written: no summary record, no
"round digest", because those would be a second copy that could drift from the records.

### 2.5 (e) Lane facts: `lane.NAME.*` reference entries

Host, model, identity and "how to nudge" are operational facts with an authority (the coordinator
observed them), a date and a review-by. That is what the reference catalog is for:

| key | statement (placeholder) | authority |
| --- | --- | --- |
| `lane.NAME.actor` | the lane's actor id | attested by the coordinator, from the session registry |
| `lane.NAME.host` | "runs on the office build machine" | attested |
| `lane.NAME.model` | "model family and version" | attested, review-by 90 days |
| `lane.NAME.nudge` | "how the coordinator wakes it: kit wait / scheduler / a message route" | attested |
| `lane.NAME.task` | `task:` pointer to its standing lane task | attested |

These rules follow from the catalog itself:
- Owner `person:orchestra-coordinator`, as guidance item 3 says for facts about hosts.
- Accepted only by the coordinator; a lane may propose its own as drafts.
- The session registry is **not** extended. It stays attribution, not a fact store (SESSIONS.md),
  and the `actor` entry joins the two.

**Privacy.** The catalog is readable by project members. A lane fact that must not be visible to
every member, such as a private address, **is not stored in the catalog**. It stays in the
coordinator's private notes, and §6 says so in the migration checklist. A visibility class on
reference entries is a real need, but it is outside this design (§9).

### 2.6 What this reuses, and what it deliberately does not

- **Reused unchanged:** the checkpoint and `transition()`, `lane-state-v1` and `work lanes`, the
  reference catalog, capability records, the review chain and its shared projection,
  `briefing.plain_text` and `clip`, and the hidden-surface filter list. Task files and rooms are
  pointer targets only.
- **Not used:**
  - the open-item records of OPEN_ITEMS_DECISIONS_DESIGN, which are not built (slice 0). The
    `item:` pointer is ready for them;
  - room items and the scribe (not built);
  - `lane-state-v1` as a note carrier: it holds a wait state and stays so.
- **Not invented:** a "context" record kind, a notes record, a digest, a summary written by a timer,
  or a field on the session registry. The one new piece of state is the standing-task journal
  (§2.7), and it is an authority for an exemption, not a store of context.

### 2.7 Standing tasks: exempt from the claim limit and from "your move"

**The problem** (found by the review; both notes missed it). A standing task is `in_progress` and
assigned, with review state `none`, so today it is held work twice over:
- **Claim limit.** `claim_limit.counted` counts it, and the default limit is 1. A lane holding its
  lane task is refused every real claim.
  - If its checkpoint leaves a `dependency`, `claim_limit.blocked` moves it out of the active slot.
    It still counts toward `hard_cap`.
  - That relief depends on what the lane last wrote, so it is no exemption.
- **Whose move.** `work.move_decision` reads a claimed, undelivered task as the holder's move. So
  the task:
  - wakes `wait` for ever;
  - counts in `your_move`;
  - raises `your_move_idle` on the lane board as soon as the lane is quiet for the threshold.

**The exemption.** A row is an **exempt standing task** when all four of these hold:
1. it carries the label `standing-task`;
2. a host journal entry exists for it, `.standing-tasks/<sha256 of the task id>.json`, holding
   `{task, holder, kind: "coordinator"|"lane", granted_by, granted_at, operation_id}`;
3. its assignee equals the entry's `holder`;
4. it is the holder's only exempt row. If the journal names two tasks for one holder in one
   project, the older grant is exempt, the newer counts normally, and `work lanes` warns.

**Why a journal and not only the label.** A label is a raw tracker field. `bd history` shows
previous values but no author, and an older kit does not reserve the label. So the label alone
cannot say who granted the exemption. That would make it a way round the claim limit that any
contributor could take. The host journal is the authority, as it is for task files
(`task_files.py`: "the reserved comment is the history record, not its authority"). The label is
the visible mirror that every reader, an older kit included, can see.

**Who may grant it.** Only the coordinator, through two new endpoint operations: `standing grant
TASK --holder ACTOR --kind lane|coordinator` and `standing revoke TASK`.
- They run on the host route and on the bound-key `coordinator` route, under the project's
  coordination lock, after `keyed_records.require_configured_operator` or the coordinator-grant
  check.
- They write the journal entry and set or remove the label in one operation, retried by
  `operation_id`.
- **Guard.** `standing-task` joins `reserved_comments.RESERVED_EXACT_LABELS`, the way `gt:slot` is
  protected: no contributor adds, removes or replaces it on any row, on any route. Before the slice
  ships, an `admin.py standing-label-check` lists any project already using the label.

**What the exemption changes, and nothing else:**
- **Claim limit.** `claim_limit.counted` returns false for an exempt row, so it is neither active
  held work nor counted toward `hard_cap`. Every other rule is unchanged.
- **Moves.**
  - `work.move_decision` returns `whose_move: "none"` and `next_actor: "none"`, with the sentence
    "Standing task: its checkpoint is the holder's live list, not a move."
  - Exempt rows are left out of `your_move`, of `wait` items and of the `work lanes` held set.
  - The lane board's `your_move_idle` never fires on them. Their own freshness is
    `lane_task_stale` (§5.2), a different flag with its own threshold.
- **No delivery.** `review TASK --file` with `contribute` on an exempt row is refused ("a standing
  task is never delivered; deliver on the task it points at"), so it never enters a review state.
  It is never `claimable`: the claimability rule gains the exempt predicate.
- **Revoke and close.**
  - `standing revoke` removes the journal entry and the label, and the row becomes ordinary held
    work at once.
  - Closing the task, as when a lane is retired (§2.3), ends the exemption with it.
  - Reassigning the task to an actor other than the entry's `holder` ends it until the coordinator
    grants again for the new holder. That is the new-actor move of §2.3.

**Rollback.** An older kit knows neither the journal nor the label. There the standing task counts
again, which is today's behaviour: no leak, but the lane's claims may be refused. The coordinator
can either unassign standing tasks before rolling back or raise those lanes' limits, and the
release notes of slice 2 say so. A label added through an older kit during a rollback grants
nothing after the upgrade, because it has no journal entry.

## 3. Read paths, and their size bounds

### 3.1 What a fresh session reads first

**A fresh coordinator session**, in order:

| step | read | bound |
| --- | --- | --- |
| 1 | `guidance get` | the guidance text (a few KB) |
| 2 | `brief kittrial-5bb.391` | a compact brief (about 6 KB in practice), first 10 open items plus `carry_open_items`, pointers resolved within §2.1.2's bounds |
| 3 | `work lanes` | a row cap with `next_offset` (LANE_BOARD_DESIGN §3) |
| 4 | `work` | the existing queue, paged |

**A fresh worker session:**

| step | read | bound |
| --- | --- | --- |
| 1 | `guidance get` | as above |
| 2 | `work --mine` | the existing owned queue |
| 3 | `brief` of its lane task | compact brief |
| 4 | `brief TASK` for the task it acts on | compact brief |
| 5 | `ref find` / `capability lookup` / `find` when it needs a fact | one page each |

**Total.** Together, a fresh session reads four or five bounded pages, about 20 to 30 KB, before it
knows where everything is.

### 3.2 Following a pointer

Each pointer kind is followed with one read of its own kind (the table in §2.1). Each of those reads
is already bounded:

| read | bound |
| --- | --- |
| `brief` | compact, `--full` at most 20,000 bytes |
| `ref get` | one entry |
| `capability get` | one record |
| `history --recent N` | a page |
| `files get` | one file, behind task-file authority |
| room read | a page |

A session follows only the pointers it needs for the move it is about to make.

### 3.3 What is never loaded unless asked

Never loaded unless the session asks, and returned by default by no read in this design:
- `history` beyond a page, and `show` (which can exceed a tool's output limit);
- superseded checkpoints, and review rounds before the newest;
- room messages, task-file bytes, and comment bodies other than the record a pointer names;
- the archived flat files.

## 4. Search: `find`

### 4.1 The command

```
find PHRASE [--kind ref|cap|decision|checkpoint|review|task]... [--task TASK] [--state open|all]
            [--limit N] [--cursor C]
```

It is an endpoint action, read-only, safe on the contributor route. It is also exposed later at
`GET /v1/projects/{pid}/find?q=` at the same read scope as `brief`.

- `PHRASE`: at most 200 characters, normalised as `capability find` normalises (§4.3).
- `--kind` narrows the corpus.
- `--task` restricts results to one task's checkpoint, review records and the records that point at
  it.
- `--state open` (the default) leaves out documents from closed tasks. `all` includes them.
- `--limit` 1..20 (default 10), and `--cursor` from the previous page.

### 4.2 The corpus: what is searched, and what is not

| kind | document | text indexed |
| --- | --- | --- |
| `ref` | each key's newest accepted revision, and its newest draft when there is no accepted one | key, title, statement, tags |
| `cap` | each capability's newest accepted or draft revision | key, name, summary, accepted aliases |
| `decision` | each decision issue (`issue_type == 'decision'` or the `decision` label, the kit's existing predicate) | title, the first 2,000 characters of the body |
| `checkpoint` | **the newest valid checkpoint of each task only** | intent, summary, next_action, open item text and source, resolved reason and evidence |
| `review` | each review record's items and each response | item titles, response text |
| `task` | each task that is not a record anchor | title |

**Not searched, and named in every answer's `coverage`:**
- superseded checkpoints;
- ordinary comments;
- `lane-state-v1` records;
- room messages;
- task-file contents;
- the raw comments of record anchors other than the kind's own documents above;
- anything outside the project.

The newest-checkpoint rule is what keeps search an index and not a history grep:
- a finding that matters is in some newest checkpoint, as an open item, or as a resolved item whose
  evidence points at it;
- or it is a reference entry or a decision;
- or it has stopped mattering.

### 4.3 Ranking

1. **Exact match first.** A phrase equal to a reference key, a capability key or a task id returns
   that record at rank 1.
2. **Score.** Rank by BM25 over the normalised words, reusing the capability lookup's normalisation
   and scoring (`capabilities.py`: `words`, `stem`, `content_stems`, `bm25_document`). This gives
   one notion of "matches" across the kit. Slice 4 confirms the exact functions; I did not read
   them for this note.
3. **Tie-break by trust class, then recency:**
   1. an accepted reference or capability;
   2. a decision issue;
   3. the newest checkpoint of an open task;
   4. a review record;
   5. a draft or unattested entry;
   6. a closed task's checkpoint (only with `--state all`).

   Trust never lifts a weak match above a strong one. It orders equals.

### 4.4 Who may read what

`find` adds no data path. It runs inside the endpoint under the caller's key, after
`key_project_refusal`, so a member of another project gets nothing. For each kind, **a document is
in the caller's corpus only if the kind's own read would show it to that caller:**

| kind | the rule |
| --- | --- |
| `ref` and `cap` | readable by any member through `ref get` and `capability get`, so included for members |
| `checkpoint`, `review`, `task` | readable through `brief` and `review`, so included on the same rule |
| hidden surfaces | record anchors are never `task` documents (the slice-0 filter list in CAPABILITY_INDEX_DESIGN §8) |
| anything a later visibility rule hides from a caller | hidden from `find` by the same predicate, never by a second one |

**Rooms and task files are excluded from v1.**
- Rooms have their own member rule.
- Task files have a download rule that depends on the caller's relation to the task.

Indexing either would mean re-implementing that rule inside search, and a second implementation of
an access rule is the classic leak. A later slice may add them only by calling their own predicate.

**Permission comes first** (from the twin note). The caller's corpus is filtered *before* anything
is scored, excerpted, counted or paged:
- a filtered-out document never reaches ranking, `total`, `coverage.documents`, a warning or an
  error sentence;
- so neither a count nor a page boundary can reveal that a hidden document matched.

**Cursors are bound to the caller.** The page position is returned only inside an opaque `cursor` binding:
- the project, the phrase and the filters;
- the read-cache generation;
- a digest of the caller's **visibility scope**: the route, the key's principal and its scopes.

A cursor presented under another scope, or after the generation changed, is refused with "start
the search again", so a cursor cannot be handed to a caller with less access.

### 4.5 The answer, and its bounds

```json
{"schema": "find-v1", "phrase": "load rule", "total": 4, "cursor": null,
 "watermark": {"generation": "<read-cache key>", "computed_at": "2026-10-24T01:00:00Z"},
 "stale": "current", "warnings": [],
 "items": [
  {"kind": "ref", "pointer": "ref:host.build.load-rule", "trust": "accepted",
   "title": {"text": "Build host load rule", "omitted_chars": 0},
   "excerpt": {"text": "at most two heavy runs ...", "omitted_chars": 112},
   "at": "2026-10-10T08:00:00Z", "rank": 1}],
 "coverage": {"kinds": ["ref", "cap", "decision", "checkpoint", "review", "task"],
              "documents": 5321, "skipped_malformed": 2, "truncated": false,
              "not_searched": ["superseded checkpoints", "comments", "rooms", "task files"]}}
```

Every text in the answer is untrusted data, carried the way §2.1.3 says.

**The envelope** (from the twin note) is the same on every new read: `find`, `review --rounds` and
the `pointers` block of `brief`:
- `coverage`: what was searched and what was not, malformed records skipped, and whether a cap
  truncated the answer;
- `watermark`: the read-cache generation the answer was computed from, and when;
- `stale`, one of:
  - `current`: computed from the generation that is current now;
  - `lagging`: a write landed while the answer was being built;
  - `partial`: some records could not be read;
  - `unknown`.

A reader can therefore always tell "nothing matched" from "not everything was looked at".

**Bounds:**
- the phrase is at most 200 characters;
- at most 20 items a page;
- every text is an excerpt object of at most 200 characters, escaped;
- the whole answer is at most 16 KB, and `truncated` says when the cap cut it;
- the opaque `cursor` pages the rest (bound to the caller, §4.4).

The answer carries **pointers**, so the session follows one with one read (§3.2) rather than
receiving the record.

### 4.6 Computed at read time (Q3): bounds and cost on a project the size of kittrial

**v1 builds the corpus at read time** from data the endpoint already loads. There is no index file
and nothing to keep in step.

**Input.** The input is the project's native export, which carries rows and their comments (the
activity feed and `work lanes` are built from it). Since kittrial-5bb.339, `read_cache.py` serves that
export from one cache file keyed by the tracker's Dolt HEAD:
- a `find` after any other read with no write in between pays no `bd` export;
- a `find` after a write pays the one re-export that the next `work` or `brief` would pay anyway.

**Work per call, on kittrial's size (about 3,400 issues and 7,400 comments):**
- one pass over the rows: about 3,400 rows to classify, the same pass `work` makes;
- one pass over the comments: about 7,400 prefix checks (a string comparison each), to find the
  checkpoint, review, reference, capability and decision records;
- parse only what is indexed:
  - the newest valid checkpoint per task: at most one per task, so at most 3,400, and fewer in
    practice because many tasks have none;
  - the review records;
  - the anchors' newest revisions;
  - the decision issues;
- tokenise and score the documents: a few thousand short documents, from a few hundred to a few
  thousand characters each.

**Estimate (not a measurement).** This is the same order as one `work` call plus one tokenising
pass over a few MB of text: on the order of one to three seconds of CPU in one Python process. It is
an estimate from the sizes above, not a measurement. The kit's real costs have surprised before, so
the bound below is what the slice must prove.

**Bounds the slice must enforce and test:**
- at most 10,000 documents per call (the rest are skipped, and `coverage.truncated` says so);
- at most 8 KB of text indexed per document;
- the page and size bounds of §4.5;
- **acceptance:** on a copy of a kittrial-sized export with a warm read cache, `find` answers within
  3 seconds at the 95th percentile, measured on the build host the suite already uses.

**If that bound fails,** the next step is a **memo** of the built corpus, and it must not become a
second store:
- **Keyed** by the read-cache generation plus the caller's scope digest, so a write (a new
  generation) or another scope never reuses it.
- **In memory only**, in the endpoint process. It is never written to disk, never backed up, never
  restored and never read by another process. Losing it costs one rebuild and loses no fact.
- **Bounded** to the newest two generations and a fixed byte budget, evicted least-recently-used.
- **Never a source of truth.** An answer served from it carries the same watermark it was built
  under, and a record it holds is never newer than the export it was built from.

Only if the memo also fails the bound is a rebuildable cache file beside the project considered,
which reopens Q3 with the measurement in hand.

## 5. Staying current without becoming a second log

### 5.1 Who writes, and when

| record | writer | when | never |
| --- | --- | --- | --- |
| coordinator state (.391's checkpoint) | the coordinator | when a live thing changes: a delivery arrives, a review ends, a release ships, a question is asked or answered | on a timer, or "no change" passes |
| lane task checkpoint | the lane | at task start and finish, and on starting to wait | on every run |
| task checkpoint | the task's assignee | as today | |
| reference entries | lane or coordinator proposes; coordinator accepts | when a fact is first established or changes | for task state |
| review records | as today | as today | |
| decision issues | coordinator | when a decision is made | |

**Each fact has one writer and one home.** Everything else points at it. The difference from a log:
a log appends what happened, while these records are **replaced**:
- a new checkpoint supersedes the old;
- a reference revision supersedes the old revision.

So what a reader reads first is the state now, not the sum of everything.

### 5.2 What makes an entry stale, and how it is flagged

| stale when | flagged where | how |
| --- | --- | --- |
| a reference entry is past `review_by` | `ref list --due expired`, `work` attention (existing) | existing |
| a pointer no longer resolves, or names a superseded record | `brief`, beside the item (`state: missing` or `superseded`) | §2.1.2 |
| a standing task (coordinator or lane) has newer activity than its newest checkpoint | `brief` (`newer_activity`, existing) | existing |
| a lane task's newest checkpoint is older than the `work lanes` threshold while the lane holds work | the `work lanes` row: one new flag, `lane_task_stale`, computed like `waiting_too_long` | slice 2 |
| an open item in a standing task points only at closed or integrated things | `brief`: a warning "every pointer of item X is settled" | slice 5 |
| a lane fact's actor no longer matches the registry | `ref check` (the catalog's own check) | slice 5 |

**Rules for flags:**
- A stale entry is **flagged on read, never deleted or rewritten by the kit.** The writer settles it
  at their next write: resolve the item, revise the entry, or retire it.
- No flag is silenced by age, following the lane board's rule ("age only ever adds a flag").

## 6. Migration

### 6.1 The coordinator's flat files

The handover log is about 5,300 lines, append-only, and private. It is migrated by hand, with a
checklist. **There is no importer in v1.** An importer would have to parse prose and would turn
guesses into records with authority. The checklist:

1. **Freeze the log first, and take its digest.** It is no longer appended to, and its SHA-256 is
   recorded in .391's summary.
2. **Live state.** Write .391's checkpoint from what is in flight today: one open item per
   delivery, release, question and lane problem.
   - An item that came from the log keeps `archive:SHA256#LSTART-LEND` (from the twin note) in
     its `source` until it is linked to a native record (`task:`, `comment:`, `ref:`).
   - So a reader holding the frozen file can check the exact lines, and a changed file (another
     digest) is visibly not the one cited.
3. **Walk the log's distinct facts once.** Each one is one of these:
   - a host or procedure fact: a reference draft, owner `person:orchestra-coordinator`, attested by
     the coordinator;
   - an owner decision or instruction: a reference draft owned by `person:james` with the owner's
     words as authority, or a native decision issue when it decides tasks;
   - a lane fact: a `lane.NAME.*` draft;
   - a private value (an address, a credential path): **stays in the private file**, and the
     reference entry, if any, says "private, ask the coordinator";
   - history, meaning what happened when: **not migrated**. It is already in the tasks' histories,
     and the log stays as a read-only archive.
4. **Point away from it.** The log's first line names .391 as the live state, and it is never read
   first again.
5. **Acceptance:** a fresh coordinator session runs a working day from `brief kittrial-5bb.391` and
   the pointers, without opening the log, and records in .391 every time it had to. Each such time
   is a missing pointer or reference entry, to be added.

### 6.2 Lane notes

On its next run after slice 2, each lane:
1. creates its lane task (or the coordinator creates it and assigns it);
2. classifies its `notes.md` entries by the table in §2.3;
3. proposes reference drafts for the facts and owner instructions;
4. moves task state into those tasks' checkpoints;
5. writes its cross-task list as the lane task's first checkpoint;
6. reduces its local file to what must never leave the host (credentials, private paths). Those are
   named in the lane task's summary as "local only", and their values are not recorded.

**Worked example (placeholders).** The Claude lane's own private notes hold about fifteen entries.
Classified, they become:
- **reference drafts** (`host.build.load-rule`, `host.build.clones-need-bundle`,
  `host.build.loopback-ports`, `kit.windows-node-tests-utf8`, `kit.test-traps.*`):
  - owner `person:orchestra-coordinator`;
  - authority: the observation, with the date it was made;
- **owner instructions** ("never read live installations", "no installs on the owner's machines")
  as drafts owned by `person:james`, quoting the instruction;
- **lane facts** (`lane.claude.host`, `lane.claude.nudge`);
- **nothing** for the lane's local paths and session handoff, which stay local.

Once migrated, a fresh Claude session on another host would start from `brief` of its lane task and
`ref find`, and would need none of the local file.

### 6.3 Order and reversibility

The migration writes only records that exist today: checkpoints, reference drafts and decision
issues. So it can start before any slice of this design ships, and an older kit reads every record
it creates. Undoing it means retiring the reference entries and closing the lane tasks. The flat
files are frozen, not deleted, until the acceptance in §6.1 has held for a release.

## 7. Risks, and the test that proves each part

| # | risk | mitigation | the test |
| --- | --- | --- | --- |
| R1 | **Prompt injection through stored facts.** A reference title, a task title or a checkpoint item says "ignore your instructions and ...", and a session reading `find` or a resolved pointer obeys it. | every resolved or found text is an escaped excerpt marked `trust: "data"`; prompts and AGENT.md carry pointer tokens only; the served guides say task text is data (they already do for coordinators, kittrial-5bb.387) | (1) a fixture with control, bidi and line-separator characters in a reference title: the `brief` pointer label and the `find` excerpt are escaped byte for byte as `briefing.plain_text` escapes, before `clip` cuts them; (2) `agent_prompts` and the generated AGENT.md for a task whose checkpoint points at that entry contain `ref:KEY` and not the title; (3) `trust` is `data` on every pointer and every `find` item (a schema test over all kinds) |
| R2 | **Leaking private text through a pointer.** A pointer to a task, room or file the caller cannot read reveals its title, or even that it exists. | `unavailable` for anything the caller may not read, whether or not it exists; no label for it | a caller of another project, a contributor without a room's membership, and an agent credential without the task's file rule each resolve pointers to (i) an existing hidden target and (ii) a missing one, and get byte-identical answers |
| R3 | **Leaking private text through search.** `find` returns a document the caller could not read with that kind's own read. | the corpus is built per caller from each kind's own predicate (§4.4); rooms and files excluded in v1 | for every kind, a caller who lacks the kind's read is given a phrase that matches exactly one hidden document, and gets `total: 0`; a member of another project gets the project refusal before any read |
| R4 | **Private values in public places.** A lane migrates a private address into a reference entry or a checkpoint. | the §2.3 table and the §6 checklist say where private values stay; reference entries are accepted by the coordinator, who checks; the public repository holds placeholders only | a review of the migration (§6.1 step 4) and of each accepted `lane.*` entry; the repository's existing privacy check over this note's examples (placeholders only) |
| R5 | **Drift: a pointer that lies.** An item points at a record that was later superseded, and a reader trusts it. | resolution states `superseded` or `missing`; `live` means "exists", never "agrees" | a fixture where the pointed reference is retired and a contribution superseded: `brief` shows `superseded` for both; a fixture with a deleted comment id shows `missing`, and the checkpoint stays valid and publishable |
| R6 | **Drift: the index disagrees with the records.** | no stored index in v1; `find` is computed from the same export as `work` and `brief` | after a write, the next `find` sees it (the read-cache key changes): write a reference draft, then `find` its key, in the same test |
| R7 | **Breaking an older kit.** A newer checkpoint, or a lane task, cannot be read or re-published by an older kit. | the grammar lives in existing string fields; to an older kit a standing task is an ordinary task with an unknown label (it then counts as held work again, §2.7); no record kind or checkpoint field is added | run the older release's `checkpoint_problems` and `transition` on a checkpoint whose items carry pointers: valid, every item matched by id and its source kept; publish the next checkpoint with the older kit |
| R8 | **Second log by the back door.** Standing tasks grow without bound, or are written every run. | the existing caps (100 items, 80 KB); the "one item per live thing, settled things leave" rule; no timer writes | the existing checkpoint limit tests; slice 5's warning "every pointer of item X is settled" has a fixture |
| R9 | **Cost.** `find` is too slow on a large project and becomes a timeout. | the bounds of §4.6, the read cache, the document and size caps | the 95th-percentile bound of §4.6 on a kittrial-sized fixture, plus a test that a corpus over 10,000 documents reports `truncated` |
| R10 | **Pointer misparse.** Prose such as "commit:abc" in a sentence is read as a pointer and shown as missing, confusing a reader. | missing is information only; prose is never refused | a table test of the grammar: matching tokens, near-misses (`task:` with no value, upper-case kinds, characters outside the set) and long fields, each with its expected `pointers` output |
| R11 | **The standing exemption as a way round the claim limit.** A lane labels ordinary work `standing-task` to hold more than its limit, or to hide a stalled task from the board. | the label is reserved (no contributor writes it on any route); the exemption needs the coordinator-written host journal and a matching holder; one exempt row per holder; an exempt row can never be delivered or claimed | (1) a contributor's raw label add or remove is refused on the SSH and HTTP routes; (2) a row with the label and no journal entry counts against the limit and reads as the holder's move; (3) with an entry, a lane at limit 1 holding its lane task can claim one real task, and the claim is refused at the second; (4) a second grant for the same holder leaves the newer row counted, with the `work lanes` warning; (5) reassigning the row ends the exemption; (6) `wait` and `your_move` leave out exactly the exempt row; (7) `contribute` on it is refused |

## 8. Slices (five, each deliverable alone)

Each slice is deliverable alone, adds a read or a convention, writes no new record kind, and can be
rolled back by redeploying the previous release with nothing to undo.

**Slice 1: the pointer grammar, and its resolution in `brief`.**
- `briefing` gains a pure parser of `KIND:VALUE` tokens and a resolver over the brief's loaded data.
- `brief` (JSON and text) and the web brief page show `pointers` beside items.
- The checkpoint template page gains a section "pointers in source and evidence".
- Tests: R1 (1) and (3) for the brief surface, R2, R5, R7, R10.
- Depends on nothing.

**Slice 2: standing tasks and lane tasks.**
- The exemption of §2.7:
  - the `standing grant` and `standing revoke` operations on the coordinator and host routes;
  - the `.standing-tasks/` journal, carried in the coordination backup;
  - `standing-task` in `RESERVED_EXACT_LABELS`, after `admin.py standing-label-check`;
  - the exempt predicate in `claim_limit.counted`, `work.move_decision`, `wait`, the claimability
    rule and `contribute`.
- `work lanes` gains `lane_task` and `lane_task_stale`.
- Docs: the lane-task discipline and the retire and move procedures of §2.3 (WORKER_GUIDE,
  OPERATIONS), and the `lane.NAME.*` key convention (reference catalog page).
- Tests:
  - R11 (1) to (7);
  - the `work lanes` row with zero, one and two standing tasks;
  - the contributor route sees only its own row's `lane_task`;
  - an older kit's restore of a backup carrying the journal.
- Depends on nothing. It uses slice 1's pointers when present, but does not need them.

**Slice 3: `review TASK --rounds`.**
- A projection of the existing review records through the shared review-state projection, with the
  bounds of §2.4.
- Tests: a three-round fixture (changes requested, revision, approval, and an item carried across
  rounds); bounds at 21 rounds and 51 items; the rounds view and `review TASK` agree on the outcome
  of every round.
- Depends on nothing.

**Slice 4: `find`.**
- The endpoint action and client command; the corpus, ranking and bounds of §4; HTTP afterwards, in
  the same slice or the next release.
- Tests: R1 (find surface), R3 (with counts and a cursor replayed under a narrower scope), R6, R9,
  the envelope's `stale` values, and ranking (exact key first, then score, then trust).
- Depends on nothing. It returns pointers whether or not slice 1 is deployed.

**Slice 5: stale flags, and the migration.**
- The two remaining flags of §5.2: settled items in a standing task, and lane facts that disagree
  with the registry.
- The §6 checklist, run for kittrial's coordinator and one lane, with the acceptance of §6.1 step 4
  recorded on .391.
- Tests: the flag fixtures. The migration itself is operational evidence, not code.
- Depends on slice 1 for the settled-items warning (it reads resolved pointers). The registry check
  depends on nothing.

**Order.** Recommended: 1, 2, 3, 4, 5. Slice 1 makes .391 an index at once. Slice 2 removes the lane
notes that are lost when a lane moves. Slice 3 is the cheapest help to a lane taking over a task.
Slice 4 is the largest and benefits from the others' records being in place.

## 9. Left open, deliberately

- **Visibility classes on reference entries.** Some lane and host facts are fit for the coordinator
  only, and the catalog is member-readable. This design keeps such values out of Orchestra (§2.5,
  §6.1) rather than inventing a visibility rule inside it. A per-entry visibility class belongs to
  the reference catalog's own design.
- **Cross-project search.** Not designed. `find` is project-scoped, like every read here.
- **Open-item and decision records.** When OPEN_ITEMS_DECISIONS_DESIGN's later slices ship, `item:`
  pointers resolve to them, `find` gains the `item` kind, and a standing task's question items may
  move to owner-question records. Nothing here needs to change for that.
- **Rooms and task files in search.** Excluded from v1 (§4.4). They come only by calling their own
  access predicates.
- **An importer for flat files.** Not in v1 (§6.1). The twin note proposes a one-shot importer
  with a dry run and a mapping file. If the checklist proves too slow, an importer
  that writes **drafts only** is the next step, as `ref import` already proposes.
