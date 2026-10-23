# Orchestra as a durable, indexed context store: design note (Claude lane)

Design note for `kittrial-5bb.392`, written by the Claude lane (orc-coord-capability). Base:
main `7fe1a469685e7ce4a21589878636e55c2d33eb61` (release 1023e). No code in this delivery.

This task is built twice, independently. **I did not read or open the twin task
(`kittrial-5bb.393`), its branch or its comments before delivering this note.**

Plan checkpoint `01a16b21`, verified by the coordinator on 2026-10-24, with these answers and asks:
- Q1: the file is named for the Claude lane.
- Q2: a standing lane task plus reference drafts; say what happens when a lane is retired or moves
  host.
- Q3: search computed at read time, bounded, with its bounds and its cost stated on a project the
  size of kittrial.
- Two asks:
  - keep the pointer grammar backward-compatible both ways;
  - treat every resolved pointer as untrusted text on every surface.

## 0. What I read before writing (about 1,950 lines)

The task allowed about 2,500 lines of reading. I read, in this order:

| what | lines |
| --- | --- |
| `docs/BRIEFINGS.md` 1-120: `brief`, `--full` and its bounds, `history`, `checkpoint_wait`, the selection facts | 120 |
| `docs/LANE_BOARD_DESIGN.md` 1-172: `lane-state-v1`, why it is not inside the checkpoint, the `lanes` read | 172 |
| `docs/OPEN_ITEMS_DECISIONS_DESIGN.md` 51-235 and 534-656: today's `open_items`, anchors, reuse, the item/question/decision reads, brief attention | 308 |
| `docs/REFERENCE_CATALOG_DESIGN.md` 51-101 and 708-791: the catalog at a glance, `ref get` and `ref list` | 135 |
| `docs/CAPABILITY_INDEX_DESIGN.md` 454-525: `capability find`, the hidden-surface list, attention | 72 |
| `docs/PROJECT_ROOMS_DESIGN.md` 5-44, `docs/TASK_FILES_DESIGN.md` 5-25: the summaries | 61 |
| `docs/SESSIONS.md` (all): the session registry, resume, handoff | 97 |
| `templates/CHECKPOINT.md` (all): field limits and the `lane_state` sibling | 90 |
| section headings of those seven designs and of `docs/CLI_CONTRACT.md` | about 370 |
| the first lines of `open_items.py`, `rooms.py`, `task_files.py`, `lanes.py`, `lane_state.py`, `read_cache.py`, plus the function lists of `native.py` and `activity.py` | about 170 |
| the brief of `kittrial-5bb.391` (title and description) | 3 |

What I did **not** read, and where the note therefore names something to confirm when it is built:
- `docs/REVIEWS.md` and `review_state.py`, so the field names of the review-round view (§2.4) are
  confirmed in slice 3;
- `capabilities.py` beyond the code-index hits, so the reuse of its scoring (§4) is confirmed in
  slice 4;
- any coordinator handover file or lane notes file on any host. Those are private, and §6 is
  written from the brief's description of them.

What is built today and what is not (from the module headers):
- **Built:** checkpoints with `open_items` and `resolved`, `lane-state-v1`, the `lanes` board, the
  reference catalog (`ref`), capability records (`capability find`), task files slice 1, rooms
  slice 1, and the read cache.
- **Not built:**
  - open items and decisions as records (`open_items.py` is slice 0, names only);
  - the rooms scribe and room items;
  - any search across record kinds.

## 1. The problem, and the one principle

**The problem.** No session should have to hold everything in its context at once. Orchestra already
does half of this for a single task:
- the newest checkpoint is the task's summary;
- `brief` is a bounded read of it;
- `history` fetches detail when asked.

The brief names five places where that breaks:
- **(a)** a coordinator's own state lives in a flat file outside Orchestra, a 5,300-line
  append-only handover log;
- **(b)** headless lanes keep `notes.md` on their own host, invisible to others and lost when a lane
  moves;
- **(c)** a task with several review rounds has no compact list of what each round settled;
- **(d)** there is no query for "what do we know about X" or "which task decided Y";
- **(e)** lane facts (host, model, identity, how to nudge) live in coordinator files, not in
  reference records.

**The principle: store each fact once, where its writer already writes, and make everything else a
pointer to it.** The design has three parts:
- A small, fixed set of records holds the live state. They all exist today: checkpoints, reference
  entries, review records, decision issues.
- One grammar lets any of them point at any other.
- Three reads walk the pointers: `brief`, the new `review --rounds` and the new `find`.

Nothing in this design is a second log. No record grows without bound, and no status is written on
a timer.

The rest of the note answers the seven sections the task asks for:

| ask | section |
| --- | --- |
| 1. what to store where, for (a) to (e) | 2 |
| 2. read paths and their size bounds | 3 |
| 3. a search over decisions, findings and facts | 4 |
| 4. staying current without a second log | 5 |
| 5. migration from flat files and lane notes | 6 |
| 6. risks, and the test that proves each part | 7 |
| 7. at most five slices | 8 |

## 2. What to store where

### 2.0 The answer in one table

| gap | stored in (existing record) | new? |
| --- | --- | --- |
| (a) coordinator state | the newest checkpoint of the standing coordinator task (`kittrial-5bb.391`), each open item's `source` holding pointers (§2.1) | the pointer grammar only; no field, no record kind |
| (b) lane notes | facts become reference drafts; task state goes into that task's checkpoint; the lane's own cross-task list goes into a standing **lane task** (§2.3) | a label convention (`lane-task`) and one field on the `lanes` row |
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

Rules:
- `KIND` is one of the nine above, lowercase.
- `VALUE` is 1 to 200 characters from `[A-Za-z0-9._/#:-]`. An empty value is not a pointer, so the
  prose "see task: the review" contains no pointer.
- A field may hold several pointers and prose around them, all within its existing 240 characters.
  Example: `source: "comment:01a16b21-... ref:host.build.load-rule (asked by James)"`.
- A token that does not match is prose. A field with no matching token is pure prose and is treated
  exactly as it is today.

#### 2.1.1 Backward compatible both ways (the coordinator's first ask)

- **An older kit reading a new checkpoint.** The pointer lives inside a string field the older kit
  already validates by length alone. The older kit shows the field as text, carries the item
  forward byte for byte (as `transition()` requires) and can publish the next checkpoint. Nothing
  is refused and nothing is lost.
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

`state` takes one of five values:
- `live`;
- `superseded`: a retired reference entry, a superseded contribution, or a task closed as
  superseded;
- `missing`;
- `unavailable`;
- `unchecked`: `commit` and `doc`.

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
- the label is an excerpt object (`{text, omitted_chars}`), escaped with `briefing.clip`, so
  control, bidi and line-separator characters cannot reshape a reader's screen or prompt;
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

- **One open item per live thing**, with these kinds:
  - a delivery in review: `dependency`;
  - a release in flight: `blocker`;
  - a question to James: `question`;
  - a lane problem: `blocker`.

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
- One per lane, title `Lane state (NAME)`, the existing label `lane-task`, assigned to the lane's
  actor, status `in_progress`, never closed while the lane exists.
- Its newest checkpoint is the lane's current cross-task list, in the same discipline as §2.2: one
  open item per live thing, pointers in `source`, settled things move to `resolved`.
- **No new item kind.** A note that fits none of `blocker`, `question`, `decision`, `correction` or
  `dependency` is either a fact (a reference draft) or noise. That is the line that stops the lane
  task becoming `notes.md` again.
- The `lanes` row gains one field, `lane_task`: the id of the open task labelled `lane-task` and
  assigned to the row's actor, or `null`. It is derived from the export `lanes` already reads, with
  no new read. When there are two, the newer is shown with a warning.
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
4. The `lanes` board stops listing the lane once it holds nothing and has written nothing for 24
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

A task with several rounds already holds every fact in its review records: each contribution, each
review with its items, each response. What it lacks is one bounded page of them. **`review TASK
--rounds`** is a read-only projection of those records, newest round first:

```json
{"task": "example-12", "rounds_total": 3, "rounds_omitted": 0,
 "rounds": [
  {"round": 3, "contribution": "01a1...", "commit": "abc1234...", "base": "def5678...",
   "delivered_at": "2026-10-20T10:00:00Z", "supersedes": "01a0...",
   "review": {"id": "01a2...", "outcome": "approve", "at": "2026-10-21T09:00:00Z"},
   "settled": [], "still_open": []},
  {"round": 2, "contribution": "01a0...", "commit": "...",
   "review": {"id": "01a1...", "outcome": "changes-requested"},
   "settled": [{"item": "B1", "title": {"text": "...", "omitted_chars": 0},
                "response": "fixed", "in_round": 3, "pointer": "comment:01a1..."}],
   "still_open": []}],
 "coverage": "review records of this task; text clipped to 200 characters"}
```

**Bounds:** at most 20 rounds (the rest counted in `rounds_omitted`), at most 50 items per round,
and every text an excerpt object clipped to 200 characters.

**Source:** the same review-state projection that `review TASK`, `work` and `brief` share, so the
rounds view cannot disagree with them about what is approved or integrated. I did not read that
module for this note. Slice 3 confirms the names `outcome`, `response` and `in_round` against it, and
uses its words where they differ.

**What it answers.** A new lane taking over a task reads one page and sees:
- what was asked in each round;
- what the answer was;
- what is still open.

It then follows a `comment:` pointer only for the item it needs. Nothing is written: no summary
record, no "round digest", because those would be a second copy that could drift from the records.

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

- **Reused unchanged:**
  - the checkpoint and its `transition()` rule;
  - `lane-state-v1` and `lanes`;
  - the reference catalog;
  - capability records;
  - the review records and their shared projection;
  - task files and rooms, as pointer targets only;
  - `briefing.clip`, and the hidden-surface filter list.
- **Not used:**
  - the open-item records of OPEN_ITEMS_DECISIONS_DESIGN. They are not built (slice 0). This design
    does not depend on them, and is ready for them through the `item:` pointer: when they land, a
    checkpoint item can point at its record.
  - room items and the scribe (not built);
  - `lane-state-v1` as a note carrier. It holds a wait state, not notes, and stays so.
- **Not invented:**
  - a "context" record kind;
  - a notes record;
  - a digest;
  - a summary written by a timer;
  - a field on the session registry.

## 3. Read paths, and their size bounds

### 3.1 What a fresh session reads first

**A fresh coordinator session**, in order:

| step | read | bound |
| --- | --- | --- |
| 1 | `guidance get` | the guidance text (a few KB) |
| 2 | `brief kittrial-5bb.391` | a compact brief (about 6 KB in practice), first 10 open items plus `carry_open_items`, pointers resolved within §2.1.2's bounds |
| 3 | `lanes` | a row cap with `next_offset` (LANE_BOARD_DESIGN §3) |
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

The following are never loaded unless the session asks for them:
- `history` beyond a page, and `show`, which can exceed a tool's output limit (BRIEFINGS.md);
- superseded checkpoints;
- review rounds older than the newest (until `review --rounds` is asked);
- room messages;
- task-file bytes;
- comment bodies other than the record a pointer names;
- the coordinator's and lanes' archived flat files.

No read in this design returns any of them by default.

## 4. Search: `find`

### 4.1 The command

```
find PHRASE [--kind ref|cap|decision|checkpoint|review|task]... [--task TASK] [--state open|all]
            [--limit N] [--offset N]
```

It is an endpoint action, read-only, safe on the contributor route. It is also exposed later at
`GET /v1/projects/{pid}/find?q=` at the same read scope as `brief`.

- `PHRASE`: at most 200 characters, normalised as `capability find` normalises (§4.3).
- `--kind` narrows the corpus.
- `--task` restricts results to one task's checkpoint, review records and the records that point at
  it.
- `--state open` (the default) leaves out documents from closed tasks. `all` includes them.
- `--limit` 1..20 (default 10), and `--offset` of at least 0.

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

### 4.5 The answer, and its bounds

```json
{"schema": "find-v1", "phrase": "load rule", "total": 4, "next_offset": null,
 "items": [
  {"kind": "ref", "pointer": "ref:host.build.load-rule", "trust": "accepted",
   "title": {"text": "Build host load rule", "omitted_chars": 0},
   "excerpt": {"text": "at most two heavy runs ...", "omitted_chars": 112},
   "at": "2026-10-10T08:00:00Z", "rank": 1}],
 "coverage": {"kinds": ["ref", "cap", "decision", "checkpoint", "review", "task"],
              "documents": 5321, "skipped_malformed": 2, "truncated": false,
              "not_searched": ["superseded checkpoints", "comments", "rooms", "task files"],
              "generation": "<read-cache key>"}}
```

Every text in the answer is untrusted data, carried the way §2.1.3 says.

**Bounds:**
- the phrase is at most 200 characters;
- at most 20 items a page;
- every text is an excerpt object of at most 200 characters, escaped;
- the whole answer is at most 16 KB, and `truncated` says when the cap cut it;
- `next_offset` pages the rest.

The answer carries **pointers**, so the session follows one with one read (§3.2) rather than
receiving the record.

### 4.6 Computed at read time (Q3): bounds and cost on a project the size of kittrial

**v1 builds the corpus at read time** from data the endpoint already loads. There is no index file
and nothing to keep in step.

**Input.** The input is the project's native export, which carries rows and their comments (the
activity feed and `lanes` are built from it). Since kittrial-5bb.339, `read_cache.py` serves that
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

**If that bound fails,** the next step is a per-process memo of the built corpus, keyed by the same
read-cache generation. It is still derived, and still rebuilt on any write. Only after that is a
cache file beside the project considered, which reopens Q3 with the measurement in hand.

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
| a lane task's newest checkpoint is older than the `lanes` threshold while the lane holds work | the `lanes` row: one new flag, `lane_task_stale`, computed like `waiting_too_long` | slice 2 |
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

1. **Live state first.** Write .391's checkpoint from what is in flight today: one open item per
   delivery, release, question and lane problem, each with pointers. .391 already exists, so this
   is its normal use.
2. **Walk the log's distinct facts once.** Each one is one of these:
   - a host or procedure fact: a reference draft, owner `person:orchestra-coordinator`, attested by
     the coordinator;
   - an owner decision or instruction: a reference draft owned by `person:james` with the owner's
     words as authority, or a native decision issue when it decides tasks;
   - a lane fact: a `lane.NAME.*` draft;
   - a private value (an address, a credential path): **stays in the private file**, and the
     reference entry, if any, says "private, ask the coordinator";
   - history, meaning what happened when: **not migrated**. It is already in the tasks' histories,
     and the log stays as a read-only archive.
3. **Freeze the log.** It is no longer appended to. Its first line names .391 as the live state. It
   is never read first again.
4. **Acceptance:** a fresh coordinator session runs a working day from `brief kittrial-5bb.391` and
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
| R1 | **Prompt injection through stored facts.** A reference title, a task title or a checkpoint item says "ignore your instructions and ...", and a session reading `find` or a resolved pointer obeys it. | every resolved or found text is an escaped excerpt marked `trust: "data"`; prompts and AGENT.md carry pointer tokens only; the served guides say task text is data (they already do for coordinators, kittrial-5bb.387) | (1) a fixture with control, bidi and line-separator characters in a reference title: the `brief` pointer label and the `find` excerpt are escaped, byte for byte, as `briefing.clip` escapes; (2) `agent_prompts` and the generated AGENT.md for a task whose checkpoint points at that entry contain `ref:KEY` and not the title; (3) `trust` is `data` on every pointer and every `find` item (a schema test over all kinds) |
| R2 | **Leaking private text through a pointer.** A pointer to a task, room or file the caller cannot read reveals its title, or even that it exists. | `unavailable` for anything the caller may not read, whether or not it exists; no label for it | a caller of another project, a contributor without a room's membership, and an agent credential without the task's file rule each resolve pointers to (i) an existing hidden target and (ii) a missing one, and get byte-identical answers |
| R3 | **Leaking private text through search.** `find` returns a document the caller could not read with that kind's own read. | the corpus is built per caller from each kind's own predicate (§4.4); rooms and files excluded in v1 | for every kind, a caller who lacks the kind's read is given a phrase that matches exactly one hidden document, and gets `total: 0`; a member of another project gets the project refusal before any read |
| R4 | **Private values in public places.** A lane migrates a private address into a reference entry or a checkpoint. | the §2.3 table and the §6 checklist say where private values stay; reference entries are accepted by the coordinator, who checks; the public repository holds placeholders only | a review of the migration (§6.1 step 4) and of each accepted `lane.*` entry; the repository's existing privacy check over this note's examples (placeholders only) |
| R5 | **Drift: a pointer that lies.** An item points at a record that was later superseded, and a reader trusts it. | resolution states `superseded` or `missing`; `live` means "exists", never "agrees" | a fixture where the pointed reference is retired and a contribution superseded: `brief` shows `superseded` for both; a fixture with a deleted comment id shows `missing`, and the checkpoint stays valid and publishable |
| R6 | **Drift: the index disagrees with the records.** | no stored index in v1; `find` is computed from the same export as `work` and `brief` | after a write, the next `find` sees it (the read-cache key changes): write a reference draft, then `find` its key, in the same test |
| R7 | **Breaking an older kit.** A newer checkpoint, or a lane task, cannot be read or re-published by an older kit. | the grammar lives in existing string fields; the lane task is an ordinary task with an existing label; no record kind or field is added | run the older release's `checkpoint_problems` and `transition` on a checkpoint whose items carry pointers: valid, and carried byte for byte; publish the next checkpoint with the older kit |
| R8 | **Second log by the back door.** Standing tasks grow without bound, or are written every run. | the existing caps (100 items, 80 KB); the "one item per live thing, settled things leave" rule; no timer writes | the existing checkpoint limit tests; slice 5's warning "every pointer of item X is settled" has a fixture |
| R9 | **Cost.** `find` is too slow on a large project and becomes a timeout. | the bounds of §4.6, the read cache, the document and size caps | the 95th-percentile bound of §4.6 on a kittrial-sized fixture, plus a test that a corpus over 10,000 documents reports `truncated` |
| R10 | **Pointer misparse.** Prose such as "commit:abc" in a sentence is read as a pointer and shown as missing, confusing a reader. | missing is information only; prose is never refused | a table test of the grammar: matching tokens, near-misses (`task:` with no value, upper-case kinds, characters outside the set) and long fields, each with its expected `pointers` output |

## 8. Slices (five, each deliverable alone)

Each slice is deliverable alone, adds a read or a convention, writes no new record kind, and can be
rolled back by redeploying the previous release with nothing to undo.

**Slice 1: the pointer grammar, and its resolution in `brief`.**
- `briefing` gains a pure parser of `KIND:VALUE` tokens and a resolver over the brief's loaded data.
- `brief` (JSON and text) and the web brief page show `pointers` beside items.
- The checkpoint template page gains a section "pointers in source and evidence".
- Tests: R1 (1) and (3) for the brief surface, R2, R5, R7, R10.
- Depends on nothing.

**Slice 2: standing lane tasks.**
- Docs: the lane task convention (label `lane-task`, title, discipline) and the retire and move
  procedures of §2.3, in WORKER_GUIDE and OPERATIONS.
- `lanes` gains `lane_task` and `lane_task_stale`.
- The `lane.NAME.*` key convention is documented in the reference catalog page.
- Tests: the `lanes` row with zero, one and two lane tasks; reassignment moves the row; the stale
  flag; the contributor route sees only its own row's `lane_task` (the lanes rule).
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
- Tests: R1 (find surface), R3, R6, R9, and ranking (exact key first, then score, then trust).
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
- **Open-item and decision records.** When OPEN_ITEMS_DECISIONS_DESIGN's later slices ship:
  - `item:` pointers resolve to them;
  - `find` gains the `item` kind;
  - a standing task's question items may move to owner-question records.

  Nothing here needs to change for that.
- **Rooms and task files in search.** Excluded from v1 (§4.4). They come only by calling their own
  access predicates.
- **An importer for flat files.** Not in v1 (§6.1). If the checklist proves too slow, an importer
  that writes **drafts only** is the next step, as `ref import` already proposes.
