# Coordinator open items, owner questions and decisions as first-class records

Status: draft design note for owner acceptance (kittrial-5bb.96), **revision 2**. **Nothing in this
document is built.** It adds no module, no client or endpoint operation, no reserved `Kind:` prefix,
no label, no migration and no test. It proposes four new record kinds, three read commands, three
write command groups, and a staged write switch, so that a coordinator's working state stops living
as a JSON array rewritten inside a `task-checkpoint-v1` comment and starts living as addressable
records. The owner accepts, amends or rejects it; the owner's questions are §18 and what is left
undecided is §19.

**Base:** main `632ae9ce023a61b1f05fbc2d7fe32416ce6c5524`.

**What revision 2 changes, and where.** This revision answers the six blocking items of request
`01a10895-2509-70e3-b954-3cc0f5c78470`:

1. `storage-model` - the false `RecordSpec` reuse claim is gone, the model is **one item per anchor**
   as the proposal ledger does, a question is an item carrying `for` and `options`, the answer and
   the resolution live on that item's anchor, and the decision record lives on the native decision
   issue with no copied body. See §3, §4.1, §4.3, §4.4, §5.
2. `drop-the-decision-label` - the `decision:<id>` label is deleted; a decision shows on a decided
   task by read-time projection only. See §4.4, §7.4, §17.
3. `trust-table` - one route -> authority -> what-closes table; only an owner-authority answer closes
   an owner question; the host operator must map to the addressee; the web answer rule is stated; a
   relayed answer leaves the question open; `submitter_for` has exactly one shape with a route field.
   See §9.
4. `state-machine-and-joins` - the state machine is defined once, stored vs derived, per word; the
   `kittrial-5bb.18` join is FUTURE and not designed; the one-prefix-tuple argument is corrected. See
   §4.5, §5, §14.1.
5. `slices-and-rollback` - slice 0 is a true slice 0 (names only); the owner's need lands in slice 2;
   the planted-record hole is closed by a host-issued owner-answer journal; the migration is one-way
   after step 2; the 2,000-record cap refuses; the read cost is stated. See §11, §12, §13.
6. `cut-and-owner-questions` - the five trigger kinds, tags, `--create-issue`, the work queue and
   owner voids are cut; §18 is exactly the owner's six questions. See §17, §18.

## 1. What exists today, and what this design replaces

A coordinator's working state is one array inside one comment. `briefing.py:14` defines
`PREFIX='Kind: task-checkpoint-v1\n'`; `validate_checkpoint` (`briefing.py:95-130`) requires exactly
twelve top-level fields (`briefing.py:96`), of which `open_items` is a list of at most 100 entries
(`briefing.py:16`) whose item shape is exactly four keys, `{id, kind, text, source}`
(`briefing.py:115`), with `kind` drawn from the closed set
`{'blocker','question','decision','correction','dependency'}` (`briefing.py:15`), `text` at most 400
characters and `source` at most 240 (`briefing.py:17,21`).

Four consequences follow directly, and they are the whole reason for this design:

* **No owner, no status, no resolving record.** An item is `{id, kind, text, source}`. Nothing in
  the record says who is responsible or whether it is still real. `work.py:359` reduces the whole
  list to `len(latest_checkpoint[0]['open_items'])`.
* **Resolution is a set equation, not a record.** `transition()` (`briefing.py:132-138`) requires
  `old - now == resolved` and requires every carried item to be byte-identical, refusing a changed
  one with `'Carry unresolved items unchanged; explicitly resolve/supersede changed items with new
  IDs'`. There is no per-item resolution record to cite.
* **The array is re-serialised every pass.** `save_checkpoint` appends a new comment and never edits
  one (`briefing.py:262`), and only the head of the `previous` chain counts, so about 39 items are
  rewritten on every coordinator pass and no item has a stable, citable identity.
* **A question is not a question.** `question` is one of the five item kinds, so an owner question
  carries no options, no addressee separate from `source` and no answer record.

Decisions are in the same position. A decision today is prose: a comment that names tasks, plus a
hand-written pointer comment on each task it decides. The kit does have a native `decision` issue
type and a `templates/DECISION.md` body, and `proposal_records._check_decision`
(`proposal_records.py:1533-1538`) already requires an owner's escalation answer to name such an
issue. What no record carries is the link set: which tasks the decision decides, in a form a reader
can project onto each of those tasks without anyone writing a pointer.

## 2. Goals and non-goals

Goals:

* an open item is a record with an **owner**, a **state**, and a **resolving record id**, addressable
  by a stable id and citable in another record;
* an item of kind `question` carries the addressee (`for`) and the `options` offered, and
  `questions --for OWNER` answers the owner's actual question ("what do I need to decide?") from the
  records;
* `questions answer` stores the owner's words, the options that were offered and the option chosen;
* a decision is a record that links the tasks it decides and shows on each of them by read-time
  projection, with no comment and no label written on anyone's task;
* the design states exactly which core pieces it reuses and which mechanisms are new (§3.3), trust by
  route (§9), rollback and backup behaviour (§11), migration (§12) and its joins (§14).

Non-goals, stated so the owner can hold the design to them:

* **This is not a replacement for the checkpoint.** `open_items` keeps working exactly as it does
  now, a project that does not adopt the ledger is unaffected, and §12 defines an explicit, opt-in
  migration. §18 question 5 asks the owner when, if ever, the checkpoint stops carrying them.
* **This is not a general task tracker, a notification system, or an identity system.** Items do not
  create, close, assign or reopen native tasks; nothing here changes task status, ownership or
  lifecycle facts. There is no notification: every appearance of a record on a task is a read-time
  projection, and reading changes nothing. Actors are self-declared on the SSH/endpoint path
  throughout this kit, and §9 says so wherever it matters.
* **This is not a second review ledger.** §14.1 records why no join to `kittrial-5bb.18` is designed.

## 3. Model

### 3.1 One item per anchor, exactly as the proposal ledger does

Records are native comments whose body is a reserved `Kind:` prefix line followed by canonical JSON,
exactly as every existing kind (`briefing.py:262`). A comment needs a task to live on, and the kit's
convention for a record family is an **anchor** row: a closed native task, not a work item, that
carries the family's type label and the family's record comments (`is_record_anchor`,
`reserved_comments.py:860-893`, listing the families beside `reference`, `proposal`,
`contribution-settings` and `capability` at `reserved_comments.py:835-842`).

**Every existing family holds one entity per anchor.** A proposal is one anchor row: `submit` creates
the row, gives it the type label, the state label, a key label and this operation's `request:` /
`request-content:` labels, posts revision 1 on it, and closes it
(`proposal_records.py:1270-1281`, close reason `'requirement proposal anchor (not a work item)'`). A
reference key is one anchor; a capability is one anchor.

This design follows that shape exactly. **One open item is one anchor row**, and a read of the item
ledger is a labelled read of the item anchors plus their comments, not a read of one shared row:

| family label | record prefixes | anchor title | close reason |
| --- | --- | --- | --- |
| `open-item` | `open-item-v1`, `item-resolution-v1`, `owner-answer-v1` | the item's own `text`, first 60 characters | `'open item anchor (not a work item)'` |

The item's `id` is the anchor's native issue id, not a minted value, exactly as a proposal record's
`id` is its anchor id (`proposal_records.py:294`, `'id must be the native anchor id'`). A question is
not a second family: it is an item whose `kind` is `question`, carrying `for` and `options` (§4.1).
The answer (§4.3) and the resolution (§4.2) are written on that item's anchor. There is therefore no
`owner-question-v1` kind and no second anchor.

The **decision record is not a family anchor at all**. It is one comment on the **native decision
issue** the kit already requires an escalation answer to name (`proposal_records.py:1533-1538`). That
issue is a real native work item, so it is deliberately **not** hidden, and the record copies no
decision body: the issue holds the `Decision`, `Rationale`, `Alternatives Considered` and
`Consequences` words, and the record holds only the link set, the authority and the route (§4.4).

### 3.2 Where a record is *about* a task, no comment and no label are written on that task

An item may be about `example-task`; a decision decides a set of tasks. Neither writes anything on
the task it is about. The link lives in the record (`open-item-v1.task`,
`coordinator-decision-v1.decides`) and surfaces on the task by **read-time projection only**: the
`brief` attention block and the `decisions list --task` filter (§7.4). This is a deliberate rejection
of the hand-written pointer comment this task reports, and it is also the kit's existing position: no
cross-task pointer comment exists anywhere in the proposal or review ledgers today, and every native
write in `proposal_records.py` is on the record's own anchor.

Revision 1 proposed a derived `decision:<id>` label on each decided task. **That label is dropped.**
`decision:` is not in `RESERVED_LABEL_PREFIXES` (`reserved_comments.py:722-725`), and
`apply_controlled_labels` (`keyed_records.py:213-224`) only moves the labels of a record's *own*
anchor (`spec.controlled`, `keyed_records.py:88`); it is not a mechanism for writing a derived label
onto sixteen other people's tasks. A reserved `decision:` prefix would be worse, not better: the
read-before-write guard (`first_reserved_label`, `reserved_comments.py:975-980`) would then refuse
`create --parent` and label edits on **every** task a decision decides. Read-time projection has
neither problem and no write cost on the decided task at all.

### 3.3 What is reused, and what is new

The revision-1 claim that the design "reuses the shared keyed-record core" was wrong, and this
section states the reuse exactly.

`keyed_records.RecordSpec` (`keyed_records.py:54-88`) is **not** a field-set declaration. It is the
full declaration that binds a one-revision-plus-acceptance record family to its write pipeline:
fourteen required values (`VALUES`, `keyed_records.py:70-73`) and twenty-three required hooks
(`HOOKS`, `:74-79`), all of which must be supplied, and `apply_native` (`keyed_records.py:389`) is
written against them: key uniqueness, one revision of one key, an optional acceptance record bound to
the revision hash, a `spec.reconcile_command`, and `spec.state_labels`. This design does **not**
build a `RecordSpec` for its kinds, and the note says so rather than implying reuse. Each kind's
closed field set is declared and checked by its own small validator, as `capability_records` and
`reference_records` already do for their own kinds.

What **is** reused, unchanged and exactly:

* `requirements.canonical_bytes` (`requirements.py:50-62`) and `requirements.content_hash`
  (`:65-70`) for every byte that is written, compared or hashed, plus `SHA256_TEXT` (`:17`);
* `record_json.loads` / `record_json.check` and its `NESTING_MAX = 64` for parsing untrusted bodies,
  where a non-canonical body returns `None` rather than raising;
* `field_limits.check_text` for every bounded text field, so a breach names the field, the length it
  had and the limit;
* the **receipt model**: `journal_dir` (`keyed_records.py:304`), `receipt_path` (`:317`),
  `validate_receipt` (`:325-359`), `prior_state` (`:362-378`), `RECEIPT_STATUSES`/
  `RECONCILE_RELEASABLE` (`:45-46`) and the uncertain-outcome vocabulary (`:381-384`), with the
  operation's own reconcile command in place of `spec.reconcile_command`;
* `keyed_records.require_configured_operator` (`:120-142`) as the one operator guard on every host
  route;
* `keyed_records.now()` (`:47-51`) as the server stamp;
* `keyed_records.apply_controlled_labels` (`:213-224`) to move the **item anchor's own** label set
  (`{open-item} ∪ state labels`), and the `request:` / `request-content:` label pair
  (`:431-444`) for a lost-response recovery;
* the reserved-prefix guard and the hidden-surface predicates (`reserved_comments.py:195-216`,
  `:835-906`), and the void machinery (`recovery.py:46`, `:65-82`) with readers that drop a voided
  target;
* `briefing.clip` (`briefing.py:163-165`) for every excerpt object and the one attention contract
  (`attention`/`attention_total`/`attention_more`).

Mechanisms that are **new**, stated rather than hidden, with the revision-2 verdict:

| mechanism | needed? | why |
| --- | --- | --- |
| many entities on one anchor | **NO** | one item per anchor removes the need entirely; no shared row holds a ledger |
| a resumable multi-record write under the project lock | **YES** | `questions answer` writes more than one comment (at most 3, §9.3) and a crash between them must be completed by a retry, not repeated |
| writes on a row the writer did not create | **YES** | the item anchor may be created by another coordinator, and `decisions record` writes a comment on an existing native decision issue |

The 41-comment figure in revision 1 is gone: it came from a `resolves` list of up to 20 items
resolving two records each, and `resolves` is cut with the multi-item answer (§17). One answer now
answers one item on one anchor.

### 3.4 Why the item is not a keyed catalog entry

`keyed_entries.AnchoredKind` (`keyed_entries.py:138`) is the machinery behind `reference-entry-v1`
and `capability-entry-v1`: one anchor per dotted key, revisions of that key on that anchor, and a
catalog read bounded by `CATALOG_SHOW_MAX`. An open item has no human-chosen dotted key and is not a
catalog entry, so the item ledger reuses the anchor *convention* and the shared primitives above, and
not `AnchoredKind`.

## 4. The four record kinds

Every record is one native comment whose body is exactly
`'Kind: <kind>-v1\n' + canonical_bytes(record).decode('utf-8')`, with no trailing newline, so a
reader verifies bytes rather than trusting a parse: a parser returns `None` unless
`canonical_bytes(record).decode('utf-8')` equals the rest of the body. `schema_version` is the integer
`1`; versioning lives in the `Kind:` line, never in the JSON. Every record's last field is
`sha256 = content_hash(record)` over the other fields. No record carries a caller-supplied author or
timestamp: both come from the native comment, and `at` is server-stamped.

### 4.1 `open-item-v1`

```
Kind: open-item-v1
```

The item record, written on the item's own anchor. Its closed field set is exactly these nineteen
fields:

| Field | Rule |
| --- | --- |
| `schema_version` | integer `1` |
| `id` | the anchor's native issue id; verified to equal the host row id (never minted) |
| `revision` | positive integer, not boolean (`keyed_records.positive_int`); revision 1 on create |
| `kind` | one of `blocker`, `correction`, `decision`, `dependency`, `question` - the checkpoint's set verbatim (`briefing.py:15`), kept so migration is lossless |
| `text` | nonempty text, at most `ITEM_TEXT_MAX = 4000` |
| `source` | nonempty text, at most `SOURCE_MAX = 240` - the checkpoint's own limit (`briefing.py:21`), kept for migration fidelity |
| `owner` | durable identity, `account:<uid>` or `person:<name>` (`capability_records.valid_owner`, `capability_records.py:132-142`) |
| `task` | an existing native issue id, or `null`; the task the item is about, for the projection only |
| `for` | durable identity, **non-null exactly when `kind` is `question`**, otherwise `null` - the addressee |
| `options` | **non-null exactly when `kind` is `question`**: a list of 1..`OPTIONS_MAX = 8` closed objects `{id, text}`, `id` matching `[a-z][a-z0-9-]{0,31}`, unique, `text` nonempty and at most `OPTION_TEXT_MAX = 300`; otherwise `null` |
| `recommended` | an option id from `options`, or `null`; only meaningful for a question |
| `due_by` | `YYYY-MM-DD`, or `null` |
| `state` | one of `open`, `blocked`, `answered`, `resolved`, `superseded`; **written by the operation, never caller-supplied** (§4.5) |
| `state_note` | nonempty text at most `STATE_NOTE_MAX = 1000` **exactly when `state` is `blocked`**, otherwise `null`; written by the operation |
| `resolved_by` | the comment id of the `item-resolution-v1` that closed this revision, or `null`; written by the operation |
| `provenance` | `{"kind":"new"}` or `{"kind":"imported","checkpoint":<comment id>,"item":<old checkpoint id>}`; written by the operation |
| `submitted_by` | the one attribution block, `{actor, route, identity, person}` (§9.1); written by the operation |
| `at` | server stamp, `'%Y-%m-%dT%H:%M:%SZ'` |
| `sha256` | `content_hash` of the other eighteen fields |

There is deliberately no `tags` field and no `trigger` object: both were cut (§17). `owner` is the
responsible identity, `due_by` is the only date, and `state` is the only lifecycle word. `task` is a
stored link used by the read-time projection; it is never a write on that task.

### 4.2 `item-resolution-v1`

```
Kind: item-resolution-v1
```

Written on the item's own anchor. Its closed field set is exactly these eleven fields:

| Field | Rule |
| --- | --- |
| `schema_version` | integer `1` |
| `resolution` | `RESOLUTION_ID = r'r-[0-9a-f]{12}'`, minted from the operation id (§10) |
| `item` | the item's `id` (its anchor id) |
| `revision` | the item revision this resolution applies to; the compare-and-swap target |
| `disposition` | `resolved` or `superseded` |
| `reason` | nonempty text, at most `REASON_MAX = 2000` |
| `evidence` | nonempty text, at most `EVIDENCE_MAX = 1000`, in pointer form: `commit:<sha>`, `comment:<id>`, `answer:<id>`, `decision:<id>` or `task:<id>` |
| `answer` | the `owner-answer-v1` comment id that decided it, or `null` |
| `by` | the one attribution block `{actor, route, identity, person}` |
| `at` | server stamp |
| `sha256` | `content_hash` of the other ten fields |

`superseded` means a later revision or a different item replaces this one, and its reason must name
the replacement in a `comment:<id>` or `item:<id>` evidence pointer. There is no `voided`
disposition: a false record is repaired with the existing operator void (§11.3), not by this kind.

### 4.3 `owner-answer-v1`

```
Kind: owner-answer-v1
```

Written on the **item's own anchor**, not on a question record of its own. Its closed field set is
exactly these fourteen fields:

| Field | Rule |
| --- | --- |
| `schema_version` | integer `1` |
| `answer` | `ANSWER_ID = r'a-[0-9a-f]{12}'`, minted from the operation id |
| `item` | the item's `id`; the item's `kind` must be `question` |
| `question_revision` | the item revision that was answered |
| `question_sha256` | that revision record's `sha256`, binding the answer to the exact text and options the owner was shown |
| `owner` | the durable identity the answer is for; must equal the item's `for` |
| `option` | the chosen option id, or `null` for a free-form answer |
| `options_offered` | the options **as offered**: a list of 1..8 `{id, text}` objects that must repeat the item's `options` exactly |
| `words` | the owner's words, verbatim, nonempty, at most `ANSWER_WORDS_MAX = 4000` |
| `authority` | `owner` or `relayed` (§9) |
| `relayed_by` | `null` when `authority` is `owner`, otherwise the one attribution block `{actor, route, identity, person}` of the coordinator who relayed the answer |
| `by` | the one attribution block `{actor, route, identity, person}` of the writer |
| `at` | server stamp |
| `sha256` | `content_hash` of the other thirteen fields |

There is no `resolves` list: one answer answers one item, whose anchor is the row the answer is
written on. That is why the multi-record write is now bounded at three comments (§8.3). The exact
option-fidelity refusal is `'options_offered must repeat the options the question offered'`.

### 4.4 `coordinator-decision-v1`

```
Kind: coordinator-decision-v1
```

Written on the **native decision issue** named by `issue`, where `issue` must equal the host row id.
Its closed field set is exactly these twelve fields:

| Field | Rule |
| --- | --- |
| `schema_version` | integer `1` |
| `decision` | `DECISION_ID = r'd-[0-9a-f]{12}'`, minted from the operation id |
| `revision` | positive integer |
| `issue` | the native decision issue id, validated by the same rule `proposal_records._check_decision` applies (`proposal_records.py:1533-1538`); must equal the row the comment is written on |
| `title` | nonempty text, at most `DECISION_TITLE_MAX = 200`; a display title, not the decision body |
| `decides` | a list of 1..`DECIDES_MAX = 16` existing native issue ids: the tasks this decision decides |
| `authority` | `owner` or `coordinator` (§9.4) |
| `supersedes` | a prior `coordinator-decision-v1` comment id, or `null` |
| `decided_by` | the one attribution block `{actor, route, identity, person}` |
| `at` | server stamp |
| `sha256` | `content_hash` of the other eleven fields |

**No copied body.** Revision 1 carried `statement`, `rationale`, `alternatives`, `consequences` and
`prior_decisions` and rendered them into the issue. All five are cut: the native decision issue
already holds those words under the headings the kit checks, and copying them created two sources of
truth that could drift. The record adds what the issue cannot hold - the structured `decides` link
set, the authority and the route - and a canonical, hashable body.

**Showing on each decided task, by projection only.** `decisions record` writes **nothing** on a
decided task: no comment, no label, no field. A decision shows on each task in `decides` because every
read that has the record computes the link at read time: `brief TASK` adds the `decision` attention
kind when `TASK` is in a readable record's `decides`, and `decisions list --task TASK` filters the
same way. `issue` is required, and this is still the design's most consequential single choice: it
makes the record's `issue` directly usable as the `decision_id` an escalated proposal disposition
must carry (`proposal_records.py:435-439`), so the join is a value and not a code change to
`_check_decision`, the reference-catalog decision link check and the acceptance evidence field set.
The issue is created before the record with the existing native child-creation path; `--create-issue`
is **cut** (§17), so the record never creates an issue itself.

### 4.5 The state machine, defined once

`state` is the newest readable `open-item-v1` revision's word, and this table is the single
definition of every state word in the design. There is no state that is both stored and derived.

| word | STORED or DERIVED | set by | evidence it must have | how a read treats it |
| --- | --- | --- | --- | --- |
| `open` | **STORED** | `items add` / `questions ask` (revision 1); `items unblock` | the revision itself | normal, actionable |
| `blocked` | **STORED** | `items block` | `state_note` on the same revision (the reason) | normal, shown with its note |
| `answered` | **STORED** | `questions answer` with `authority: relayed` | the `owner-answer-v1` comment on the same anchor | an answer is recorded; the question is **not closed** and stays in the owner's open list until resolved (§9.3) |
| `resolved` | **STORED** | `items resolve --disposition resolved`, or `questions answer` with `authority: owner` | the `item-resolution-v1` named by `resolved_by` | closed |
| `superseded` | **STORED** | `items resolve --disposition superseded` | the `item-resolution-v1` named by `resolved_by`, whose reason names the replacement | closed |
| `void` | **DERIVED** | an operator `admin.py void-record` on the newest revision | the `record-void-v1` comment | the item is dropped from every view and named in `warnings`; it is **not** a `state` value and there is no owner-initiated void (§17) |

Read-time words that are **not** states, so nothing can contradict the table: `due` (derived from
`due_by`), `trust` (derived from the route, the native author and, for the host route, the operator
allowlist - §9.1), `answer` (the `owner-answer-v1` on the anchor, or `null`), and `conflicted`
(reported when the newest revision says `resolved`/`superseded` and no matching resolution record
exists, or when two readable revisions of one item contradict each other). A `conflicted` item is
reported and requires operator reconciliation; it is never silently repaired.

The move rules are exact: a state change is written only by the operation named above; `items revise`
writes content fields and **cannot** move `state`; a question item's `state` is moved only by
`questions answer` (to `answered` or, for an owner-authority answer, straight to `resolved`), by
`items block`/`items unblock`, or by the operator void path, and a plain `items resolve` on a
`question` item is refused (§6, §9.3).

## 5. Canonical serialisation, anchoring and hiding

Nothing here is a new mechanism. The exact functions a contributor must call, and the exact tables to
extend, are:

* **bytes**: `requirements.canonical_bytes(value)` (`requirements.py:50-62`), which is
  `json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
  .encode("utf-8")`, and `requirements.content_hash(mapping)` (`:65-70`), the SHA-256 of those bytes
  with only a top-level `sha256` removed. `SHA256_TEXT` (`:17`) is the hash shape check.
* **parse**: `record_json.loads` with `record_json.check` and `NESTING_MAX = 64`; a body that is not
  canonical returns `None`, so one malformed record never fails a whole ledger read.
* **prefixes**: add four constants to the reserved-prefix block (`reserved_comments.py:173-193`) -
  `OPEN_ITEM_PREFIX = 'Kind: open-item-v1\n'`, `ITEM_RESOLUTION_PREFIX =
  'Kind: item-resolution-v1\n'`, `OWNER_ANSWER_PREFIX = 'Kind: owner-answer-v1\n'`,
  `COORDINATOR_DECISION_PREFIX = 'Kind: coordinator-decision-v1\n'` - one `RESERVED` entry each
  (`:195-216`) naming the operation that may write it, and the four kind names in
  `_RECORD_KIND_RESERVATIONS` (`:223-234`) so every version of each kind is raw-write reserved. The
  guard then refuses a raw `bd comments add` of one of these bodies with the existing text.
* **label namespace**: add `'open-item:'` to `RESERVED_LABEL_PREFIXES` (`reserved_comments.py:722-725`)
  so the item anchor's state labels (`open-item:open`, `open-item:blocked`, ...) cannot be forged or
  moved by a raw path. `decision:` is deliberately **not** added: no decision label exists any more
  (§3.2).
* **hide**: add `RECORD_ANCHOR_FAMILIES['open-item'] = (OPEN_ITEM_PREFIX, ITEM_RESOLUTION_PREFIX,
  OWNER_ANSWER_PREFIX)` and extend `RECORD_COMMENT_FAMILIES` (`reserved_comments.py:835-846`) with
  `'Kind: open-item-'`, `'Kind: item-resolution-'`, `'Kind: owner-answer-'` and
  `'Kind: coordinator-decision-'`. With the tables extended, `is_record_comment` /
  `is_record_anchor` (`:860-906`) hide the item anchors, the item records and the decision record
  comment from every surface of the shared ten-surface list, with no per-surface change. There is
  **no** `RECORD_ANCHOR_FAMILIES` entry for `coordinator-decision`: the native decision issue is a
  real work item and must stay visible; only its machine comment is hidden.
* **void**: add the four kinds to `recovery.KEYED_KIND_PREFIXES` (`recovery.py:65-71`) so
  `admin.py void-record` can repair a malformed or foreign record. `recovery.py:72-77` states the rule
  that a void is offered only for a kind whose reader honours voids, so the item and decision readers
  must drop a voided target from the view and report it in `warnings`. That is a stated obligation on
  the new readers, not a new mechanism.

A family label mapping to one prefix tuple is **not** a reason to reject an anchor that carries
several kinds: `capability` already maps `capability` to four prefixes - `capability-entry-v1`,
`capability-acceptance-v1`, `capability-verification-v1` and `capability-alias-v1`
(`reserved_comments.py:840-841`) - and `reference` to four more (`:836-837`). `open-item` with three
is the same shape. (Revision 1 used the opposite argument to justify a second family; it was wrong,
and the second family is gone.)

## 6. Validation and refusal behaviour

Validation is per kind, closed, and happens before any journal write or native read, following
`require_configured_operator`'s documented ordering (`keyed_records.py:126-128`). The rules, in the
order they run:

1. **Shape.** The payload is a JSON object with the kind's exact caller-supplied field set; any
   unknown field, any missing field and any wrong type is refused naming the field, with
   `checked_fields`' wording (`keyed_records.py:114-117`) and
   `field_limits.check_text`'s `'%s: %d characters, the limit is %d'` for each bounded text.
2. **Operation-written fields.** `id`, `revision`, `state`, `state_note`, `resolved_by`, `provenance`,
   `submitted_by`, `by`, `relayed_by`, `decided_by`, `at` and `sha256` are written by the operation.
   A payload that supplies one is refused with `keyed_records.refuse_injected_labels`' form
   (`:93-99`) adapted per field:
   `'%s is written by the operation, never caller-supplied; use items resolve.'`
3. **Cross-record rules**, each refused before any write:
   * `for` and `options` must be non-null exactly when `kind` is `question`:
     `'open item: for and options are set exactly on a question'`;
   * `owner-answer-v1.item` must name an item whose `kind` is `question`:
     `'a question item is answered by its owner's answer; item %s is kind %s'`;
   * `owner-answer-v1.owner` must equal the item's `for`:
     `'the answer names %s, but the question is for %s'`;
   * `options_offered` must repeat the item's `options` exactly;
   * `question_revision` must be the newest readable revision and `question_sha256` its hash, or the
     write is refused as stale;
   * `coordinator-decision-v1.issue` must satisfy the `_check_decision` rule, with the same refusal
     text the proposal ledger already uses (`proposal_records.py:1537-1538`);
   * `decides` ids must exist: `'Unknown decided task(s) %s: each must be an existing issue'`.
4. **Bounds**, all numeric and all in help output through the existing `help_limits`/`describe`
   pattern: `ITEM_TEXT_MAX = 4000`, `SOURCE_MAX = 240`, `STATE_NOTE_MAX = 1000`, `REASON_MAX = 2000`,
   `EVIDENCE_MAX = 1000`, `ANSWER_WORDS_MAX = 4000`, `OPTION_TEXT_MAX = 300`, `OPTIONS_MAX = 8`,
   `DECISION_TITLE_MAX = 200`, `DECIDES_MAX = 16`, `LIST_LIMIT_MAX = 100`,
   `RECORDS_PER_ANCHOR_MAX = 2000`, `ITEM_ANCHORS_MAX = 2000`, `RECORD_MAX_BYTES = 24000`.
5. **Size.** A record whose canonical bytes exceed `RECORD_MAX_BYTES = 24000` is refused with the
   existing form `'%s: %d canonical bytes, the limit is %d (%d KB)'` (`briefing.py:130`). 24000 is
   `review_workflow.PAYLOAD_MAX_BYTES`, so a record that fits in a review payload fits here and no new
   number is introduced.
6. **Caps.** Two named caps, both refusing rather than silently dropping (§11.4): a write that would
   push an anchor past `RECORDS_PER_ANCHOR_MAX = 2000` readable record comments is refused with
   `'anchor %s already holds 2000 records; reconcile or void before adding another'`, and an
   `items add`/`questions ask` that would create the 2001st item anchor in a project is refused with
   `'this project already holds 2000 open-item anchors; resolve or void items before adding another'`.

A refused write changes nothing and returns the refusal on stderr with exit status 2, the CLI
contract for a structured refusal already.

## 7. Reads

Reads need no authority beyond endpoint access, on the kit's existing rule that reading changes
nothing. Every read is one labelled read of the project's rows plus their comments.

### 7.1 `items list` and `items get`

```
items list [--owner IDENTITY] [--task TASK] [--kind KIND]... [--state open|blocked|answered|resolved|superseded|void|all]
           [--due expired|due-soon|unset] [--limit N] [--offset N]
items get ITEM
```

`items list` returns the kit's list shape. Each item carries
`{id, revision, kind, state, state_note, owner, task, due_by, due, for, options, recommended, answer,
resolved_by, trust, text:{text,omitted_chars}, source:{...}, record_comment_id, warnings, provenance}`.
`trust` is `attested` or `unattested` by the rule in §9.1; `warnings` names a malformed record, an
orphan resolution, a voided revision, an unresolvable `task`, and a cap being reached.

Refusals follow the existing filter wording: `'items list: --state must be open, blocked, answered,
resolved, superseded, void or all'`, `'items list: --due must be expired, due-soon or unset'`,
`'items list: --limit must be 1..100'`, `'items list: --offset must be >= 0'`.

`items get ITEM` returns `{schema_version, id, state, owner, revision, record, record_comment_id,
revisions, resolution, answer, submitted_by, trust, warnings, coverage}`; `record` is a clipped view
(`briefing.clip`), `revisions` lists every readable revision's comment id and `sha256`, and
`resolution` is the resolving record or `null`. Unknown id:
`'items get takes exactly one ITEM that exists; %s is unknown'`.

### 7.2 `questions --for OWNER` and `questions get`

This is the read the task names.

```
questions --for OWNER [--state open|answered|resolved|all] [--limit N] [--offset N]
questions get QUESTION
```

It selects the readable items whose `kind` is `question` and whose `for` normalises to `OWNER`, and
returns `{schema_version, for, for_kind, total, open, answered, resolved, items:[...], next_offset,
coverage}`. Each entry carries the item's `text`, `options`, `recommended`, `due_by`, `due`, `state`,
`asked_by` (`submitted_by`) and, when an answer exists, its `authority`, `words`, `option` and
`comment_id`. `--for` is required: `'questions needs --for OWNER; list every question with items list
--kind question'`. `for` accepts the durable identity forms §9.1 defines and is matched after
normalisation.

`questions get QUESTION` adds the full answer view and the resolution, so one read answers "what did
the owner say, and what did it close?" without a second call. A question whose newest revision is
`answered` with `authority: relayed` is counted under `answered`, **not** under `resolved`, and is
reported with the warning `'Relayed answer: the owner's words as reported by <actor>; not proof the
owner said them. The question stays open until the owner confirms.'`

### 7.3 `decisions list` and `decisions get`

```
decisions list [--task TASK] [--decided-by IDENTITY] [--limit N] [--offset N]
decisions get DECISION
```

`decisions list --task example-task` returns every readable decision whose `decides` contains that
task, each carrying `{decision, revision, issue, title, decides, decided_by, authority, trust,
supersedes, record_comment_id, source}`. `state` is `recorded` or `superseded`, derived from
`supersedes` links; a decision is never "approved", because an approval is a proposal disposition and
this record is not a second state machine for the same question. `decisions get DECISION` adds the
native issue view (`id`, `status`, `labels`) so the reader can see whether the issue still exists.

### 7.4 `brief` and `work`: the same attention contract

`brief TASK` already aggregates attention items of several kinds into one list with `attention`,
`attention_total` and `attention_more`. This design adds three kinds and changes no existing kind:

* `open-item` - items whose `task` is the briefed task and whose state is `open`, `blocked` or
  `answered`, rendered `'Open item [%s, %s]: %s (%s)' % (state, trust, text, 'items get ' + id)`;
* `owner-question` - questions on those items that are not `resolved`, rendered
  `'Owner question [%s, %s]: %s (%s)' % (due, for, text, 'questions get ' + id)`;
* `decision` - decisions whose `decides` contains the briefed task, rendered
  `'Decision [%s, %s]: %s (%s)' % (authority, trust, title, 'decisions get ' + id)`.

Each kind contributes at most `BRIEF_MAX = 3` items, after the existing kinds, with
`attention_total` and `attention_more` counting every kind. Item text is server-derived from the
record and clipped (`briefing.clip`), never echoed raw. The checkpoint's `unresolved` block is **not**
changed and **not** merged with the ledger, so nothing is counted twice: `unresolved` keeps meaning
"explicit checkpoint items only" and the ledger appears in `attention`.

**Read-time projection mechanism and its cost.** A decision appears on a decided task only because
`brief`/`decisions list` scan the `coordinator-decision-v1` comments of the project's rows for
`TASK in record['decides']`. There is no comment and no label on the decided task, so the cost is
CPU-only and lands on the reader: no extra `bd` read, and no write at all on the decided task.

`work` is **not** extended: the coordinator work queue proposed in revision 1 is cut (§17).

**Read cost, stated exactly.** The item anchors and the decision host rows are already in the row set
`brief` and `work` fetch, so **`brief` and `work` add no `bd` read but parse up to 2,000 records per
anchor on every call** (`RECORDS_PER_ANCHOR_MAX = 2000`, §6). A standalone `items list` /
`questions --for` / `decisions list` costs one labelled `bd list` of the item anchors plus one
`bd show` of their comments; the decision records are found by scanning the same rows' comments for
the reserved prefix, because there is no decision anchor and no decision label.

## 8. Writes

Every write is one journaled operation: `operation_id` in, one receipt out, the records appended on
one anchor, and the anchor's `request:` / `request-content:` labels written by the same recovery
model as `keyed_records.apply_native` (`keyed_records.py:431-444`). The write result is a small
single-shape object:

```json
{"id":"example-anchor","revision":1,"native_id":"example-anchor","record_comment_id":"<comment id>",
 "state":"open","created":true,"reconciled":false}
```

### 8.1 `items add`, `items revise`, `items block`, `items unblock`, `items resolve`

```
items add --file item.json
items revise --file item.json
items block --file item.json
items unblock --file item.json
items resolve --file resolution.json
```

`add` payload: `{schema_version, kind, text, source, owner, task, for, options, recommended, due_by,
operation_id}`. The writer creates the item's anchor row through the existing child-creation path
with `--no-inherit-labels`, labels it `open-item`, `open-item:open`, `request:<identity>` and
`request-content:<digest>`, posts revision 1, and closes it with `'open item anchor (not a work
item)'` - the proposal `create_args`/`close` shape (`proposal_records.py:1270-1281`).

`revise` payload: `{schema_version, task(anchor), expected_sha256, ...the caller-supplied content
fields...}`. `expected_sha256` is the `sha256` of the newest revision being replaced. `revise` writes
revision 2 or later and refuses a state change; `add` writes revision 1 only.

`block` payload: `{schema_version, task, expected_sha256, reason, operation_id}`; it writes the next
revision with `state: blocked` and `state_note: reason`. `unblock` returns it to `open` with
`state_note: null`. These two are the **only** operations added for a state word; they replace the
five trigger kinds cut in §17.

`resolve` payload: `{schema_version, task, expected_sha256, disposition, reason, evidence,
operation_id}`. It writes the `item-resolution-v1` **first**, then the revision carrying
`state`/`resolved_by`. A `question` item is refused here (§6, §9.3).

### 8.2 `questions ask`

```
questions ask --file question.json
```

Payload: `{schema_version, text, source, owner, task, for, options, recommended, due_by,
operation_id}`. It is `items add` with `kind: question` and is allowed on the contributor endpoint,
because asking is the coordinator's job and a question that cannot be asked cannot be answered. The
record's `submitted_by` carries the route, so a reader always knows how the question arrived. When the
project configures owner deciders, `for` must be one of them, with the refusal shape
`proposal_records.py:1687-1690` uses; when none are configured, `for` accepts any durable identity and
the read view says so (`owner_deciders_configured: false`) rather than implying governance that is
not there.

### 8.3 `questions answer`

```
questions answer --file answer.json
```

Payload: `{schema_version, task(anchor), item, question_revision, option, options_offered, words,
authority, operation_id}`.

**How many comments one answer writes, and why it is resumable.** It writes at most **three** native
comments on the item's anchor, in this order, in one recovery unit under the project's
`.coordination.lock`:

1. the `owner-answer-v1` record;
2. when (and only when) the answer is accepted as `authority: owner`, the `item-resolution-v1` that
   closes the item;
3. the revised `open-item-v1` carrying the new `state` (and `resolved_by` in the owner case).

A relayed answer writes two comments (the answer and the revision) and leaves `state: answered`. A
crash between the writes leaves the answer visible and the item unchanged, which fails closed, and an
exact retry with the same operation id finds the answer receipt `complete` and finishes only the
missing comments, reporting `reconciled: true`; it never writes a second answer. A lost response is
recoverable from the anchor's `request:` / `request-content:` labels.

**Writes can land on a row the writer did not create.** The item anchor was created by whoever ran
`items add` / `questions ask`; the answering coordinator may be a different actor and session. That is
one of the three new mechanisms named in §3.3, and it is why the multi-record write is journaled and
reconciled rather than assumed atomic.

Result:

```json
{"answer":"a-77aa11bb22cc","item":"example-anchor","comment_id":"<comment id>",
 "state":"resolved","resolution_comment_id":"<comment id>","created":true,"reconciled":false}
```

Refusals, all before any write:

```
an owner answer is refused on the contributor endpoint; record it on the host route
(admin.py questions-answer) or in the web interface, or record it as a relayed answer with
authority relayed and relayed_by naming yourself
the answer names person:alex, but the question is for person:sam
authority owner requires the writing actor to be mapped to person:alex; record this as a relayed answer instead
the asker cannot answer their own question
options_offered must repeat the options the question offered
owner answer: 4001 characters, the limit is 4000
```

### 8.4 `decisions record`

```
decisions record --file decision.json
```

Payload: `{schema_version, issue, title, decides, authority, supersedes, operation_id}`. `issue` is
required and must be an existing native decision issue; there is no `--create-issue`. The writer
validates `issue` with the `_check_decision` rule, validates that every id in `decides` exists, and
writes **one** comment on the issue. It writes nothing on any decided task. Route: host or web only;
the contributor endpoint is refused with `'a decision record is an owner or coordinator action; use
admin.py or the web interface'`.

### 8.5 Host routes and the staged write switch

New host routes, all following `admin.py`'s existing shape (operator allowlist read first, under
`.coordination.lock`, `--actor OPERATOR --file F.json`):

```
admin.py questions-answer PROJECT --actor OPERATOR --file answer.json
admin.py open-item-reconcile PROJECT --operation-id ID --actor OPERATOR --reason TEXT
          --disposition complete|failed|released [--issue-id ID]
admin.py open-item-writes status|on|off --actor OPERATOR
admin.py open-items-import PROJECT --source-task TASK --owner IDENTITY --actor OPERATOR
          [--file checkpoint.json] [--trigger-default manual|date] [--due-by YYYY-MM-DD]
```

There is no anchor-creation route: `items add` / `questions ask` create the item's own anchor, exactly
as `proposal submit` creates the proposal's.

Writing the four new kinds is refused unless `open_item_writes` is true in `deployment.private.json`
at the runtime root. **Absent or false means OFF; a non-boolean value is refused rather than coerced;
there is deliberately no `ORCHESTRA_*` environment fallback.** This is `review_workflow_writes`
reused deliberately, and the refusal is `'Open-item writes are off for this installation; the
coordinator turns them on once the rollback target reads the new kinds (admin.py open-item-writes
on)'`. Readers understand every kind unconditionally, so a kit can read a ledger it may not write.

## 9. Trust by route: who may answer for the owner

The kit has no identity system on the contributor path. `docs reviews` states the position for the
review ledger: "Native comment authors are attribution, not authentication, on the SSH/endpoint
path ... a client can label itself freely". The kit's real boundary is the operator allowlist plus
host shell access, and the HTTP path's capabilities.

### 9.1 One attribution shape, with its route

Revision 1 used two shapes and `capability_records.submitter_for` has no route field at all
(`SUBMITTER_FIELDS = ('actor', 'person', 'identity', 'submitted_by_agent')`,
`capability_records.py:94`; `submitter_for` `:398-402`). This design uses **exactly one** shape for
every attribution field (`submitted_by`, `by`, `relayed_by`, `decided_by`):

```
{actor, route, identity, person}
```

* `actor` - the self-declared actor string the caller supplied (or the authenticated principal's
  bound actor on the web route);
* `route` - one of `endpoint`, `host`, `web`; **set by the writer from the transport, never from the
  payload**;
* `identity` - `verified` when `route` is `host` or `web`, else `unverified`; set by the writer;
* `person` - the durable identity the route binds: on `host`, the actor's mapped durable identity from
  the proposal-settings actor map (`admin.py proposal-settings --map-actor`,
  `proposal_records.py:1525-1529`), or `operator:<actor>` when no mapping exists; on `web`, the
  authenticated principal's durable identity; on `endpoint`, `null`.

Owner identities are durable identities, not session actors: `account:<uid>` or `person:<name>`, the
vocabulary `capability_records.valid_owner` (`capability_records.py:132-142`) already enforces.

### 9.2 Trust: what `attested` means, by route

A record is `attested` only when the record's `identity` is `verified`, **and** the stored native
comment author equals the record's `actor`, **and** either (a) `route` is `host` and that author is on
the deployment operator allowlist, or (b) `route` is `web`, where the web service binds the comment
author to the authenticated principal. A record with `route: endpoint` is **never** attested, because
`identity` is always `unverified` there. The web rule deliberately does **not** require the account id
to be on the operator allowlist: account ids cannot be on it, and requiring that would make every
signed-in web record read unattested. A record that fails the check is inert: it is shown with
`trust: "unattested"`, it moves nothing, and it is never treated as an owner answer.

### 9.3 The route -> authority -> what-closes table

This is the one table that decides who may answer for the owner and what an answer closes.

| route | who may write it | `authority` allowed | closes the owner question? |
| --- | --- | --- | --- |
| **contributor endpoint** (SSH) | any endpoint actor | `relayed` only; a payload asking for `owner` is refused | **never.** The answer is recorded and shown, the item moves to `answered`, and the question stays open until confirmed |
| **host** (`admin.py questions-answer`) | an actor on the deployment operator allowlist | `owner` **iff the writing operator's mapped durable identity equals the item's `for`**; otherwise `relayed` | **yes, iff `owner`.** Otherwise it is relayed and stays open |
| **web** (authenticated, `reviews.approve`) | a signed-in member holding `reviews.approve` | `owner` **iff the authenticated principal's durable identity equals the item's `for`**; otherwise `relayed` | **yes, iff `owner`.** Otherwise it is relayed and stays open |

The rules that make the table hold, all checked before any write:

1. `authority: owner` is refused on the endpoint outright.
2. `owner` must equal the item's `for` after normalisation.
3. On host and web, the route's bound durable identity (`person`) must equal the item's `for`; an
   operator who is not the addressee cannot record `authority: owner` for someone else. The refusal
   is `'authority owner requires the writing actor to be mapped to person:alex; record this as a
   relayed answer instead'`.
4. The writing actor's bound identity must not be the actor who asked the question.
5. A relayed answer is recorded and shown with `authority: relayed`, `relayed_by` naming the relayer,
   and the warning `'Relayed answer: the owner's words as reported by <actor>; not proof the owner
   said them.'`; it never closes the item. This is subject to the owner's answer to question 2 (§18).
6. **Only an owner-authority answer closes an owner question.** A plain `items resolve` on a
   `question` item is refused with `'a question item is closed by its owner's answer, not by items
   resolve; record the answer with questions answer or void it as an operator'`, so no endpoint actor
   can close a question with no answer.
7. The read side re-applies rules 1-5 on an `owner-answer-v1` that lacks a host-issued journal entry
   (§11.2); such an answer is inert and does not close the item.

### 9.4 Decisions

`decisions record` is a host or web action. `authority: owner` requires the route's bound durable
identity to be one of the configured owner deciders (`proposal_records.py:1687-1690`); otherwise the
record must say `authority: coordinator`. A decision record has no closing power over an item, so an
unattested one is shown with its true trust and changes nothing.

## 10. Idempotency, retry and reconciliation

The shared receipt model, reused with no new mechanism:

* `identity = content_hash({'operation_id': operation_id})` and
  `digest = content_hash({'actor': actor, 'payload': payload})`; one receipt file per operation under
  the ledger's journal directory, written by `journal_dir` (`keyed_records.py:304`) and
  `receipt_path` (`:317`) and validated by `validate_receipt` (`:325-359`);
* an exact retry - same operation id, same payload, same actor - is `reconciled: true` with no second
  native write, which is why deriving record ids from the operation id is safe;
* a reused operation id with different content is refused with `prior_state`'s existing wording
  (`keyed_records.py:377-378`);
* an uncertain native outcome keeps the receipt `pending` and raises with the existing uncertain
  message (`:381-384`) naming `admin.py open-item-reconcile` as the completion path;
* the `request:` and `request-content:` labels on the anchor make a lost response recoverable from
  native state (`:431-444`), with the existing "Reserved request has no visible record; outcome
  uncertain" refusal.

Two rules are specific to this design and a contributor must implement both:

* **A partial multi-record write is repaired, not repeated.** Because §8.3 writes up to three
  comments, a crash can leave an answer with no resolution and no revision. A retry with the same
  operation id finds the answer receipt `complete` and finishes the missing comments under the same
  operation, reporting `reconciled: true`. It never writes a second answer.
* **Duplicate ids are a conflict, not a merge.** If two readable revisions of one item carry the same
  `revision` but different `sha256`, or if revision numbers skip, the item reads `conflicted`, the
  ledger still reads, and `warnings` names the comment ids. That is the shape `capability_records`
  uses for a conflicted key.

**No `previous` chain.** The review ledger chains every record with `previous` and refuses a stale
one. This design deliberately does **not** chain the item ledger: a project-wide chain would
serialize every coordinator write behind the newest comment, so two coordinators adding two unrelated
items would conflict. Compare-and-swap is per item instead, through `expected_sha256` on `revise` and
`revision` on `resolve`. Ordering is native comment order.

## 11. Rollback, void and backup/restore

### 11.1 What a backup carries, and the journal names in slice 0

The records are native comments, so the tracker backup carries them with no change. What is new is
the coordination sidecar paths, and their **names are added to the backup whitelist in slice 0**
(§13), before any writer exists:

* `.open-item-requests/` - the receipt journal of every item, question and answer write, one
  `<64 hex>.json` per operation;
* `.decision-requests/` - the receipt journal of `decisions record`;
* `.owner-answers/` - the **host-issued** journal binding each `authority: owner` answer comment to
  the exact canonical payload it was written for (§11.2).

All three are added to `admin.RECORD_JOURNALS` (`admin.py:1551`), to the path regex in
`validate_coordination_files` (`admin.py:1574`) and to the receipt validator
(`validate_record_receipt`, `admin.py:1553-1568`), so `admin.py backup` collects them,
`validate_coordination_files` checks the `<64 hex>.json` path/hash binding, and `restore-new`
restores them. The design states the consequence the integration-revert journal already documents: a
kit built before these paths exist refuses a whole restore whose sidecar names them, with
`ValueError: Invalid coordination backup path`, so the restore after a rollback must use the new
kit's `admin.py restore-new`.

### 11.2 The rollback limit, and the planted-record hole

Three honest limits, in the order a reviewer should check them:

* **Writes.** With `open_item_writes` off - the default - this kit writes no new kind, so a
  deployment may be rolled back to the previous kit safely. With it on, a rollback leaves the records
  readable only by a kit that knows the kinds.
* **Reads.** A rolled-back kit does not know the four prefixes, so its `is_record_comment` returns
  false. The item anchors are then not hidden: the old kit's `history ANCHOR` shows the record bodies
  as ordinary comments and its `list` shows the item anchors as ordinary closed tasks. Nothing is
  lost and nothing is misread - the records are inert JSON to it - but the ledger is not hidden and
  not summarised.
* **The planted-record hole, and how it is closed.** Because the old kit does not reserve the
  prefixes, an SSH caller during a rollback window can raw-write an `owner-answer-v1` body under an
  operator's name. After roll-forward the new kit reads a well-formed record claiming
  `route: host, identity: verified`, and the naive trust rule of §9.2 would read it `attested` and let
  it close a question. **It is closed by the host-issued journal**, exactly as
  `docs reviews` closes the forged-revert hole with `.integration-reverts/`: an `authority: owner`
  answer is honoured **only** when a matching entry exists at `.owner-answers/<sha256>.json`, where
  the name and the entry's `sha256` field are the SHA-256 of the exact canonical payload the native
  comment carries, and the entry binds the answer's identity - item, revision, owner, option, words,
  comment id - to the native comment. The entry is written inside the same `.coordination.lock`
  critical section as the native comment, by the host CLI route or by the web service on the
  coordination host, so only the host can produce one. A raw-written comment from a pre-reserve kit
  has no entry; the reader is fail-closed about the journal (a missing, unreadable, symlinked or
  malformed `.owner-answers/` means no owner answer is trusted), the planted answer is displayed with
  `trust: "untrusted"` and named in `warnings`, and it **does not close the question**. A crash
  between the two writes leaves the native comment inert; re-running the same `questions answer`
  operation id persists the missing entry and reconciles instead of writing a second comment.
* **Restore.** The safe direction: take the backup with the new kit, or keep the pre-rollback backup,
  and run `restore-new` with the new kit's `admin.py`. A backup taken by an older kit during a
  rollback omits the three journals, so its restore leaves pending receipts missing; every resulting
  uncertainty reads as `pending` and is reconciled with `admin.py open-item-reconcile` rather than
  being trusted.

The staged release this implies: ship the **tolerant reader** first (slice 1 reads the kinds, nothing
writes them, `open_item_writes` absent), deploy it, then turn writing on only once the rollback target
reads the kinds. That is two integration cycles, and this document does not pretend one is enough.

### 11.3 Void, supersede and rollback of a record

* **A bad record is repaired with `admin.py void-record`**, not by editing history. Registering the
  four kinds in `recovery.KEYED_KIND_PREFIXES` (`recovery.py:65-71`) puts them under the existing void
  machinery, whose record is `Kind: record-void-v1` (`recovery.py:46`). A voided record is dropped
  from the view and reported in `warnings`. There is **no** owner void (§17): only the operator
  allowlist may void, exactly as for every other record kind.
* **A superseded item is a record field, not a deletion**: `state: "superseded"` plus a `resolved_by`
  naming an `item-resolution-v1` whose evidence names the replacement. The old revision stays readable
  in `revisions`, which is how an audit answers "what did the coordinator believe last week?".
* **A superseded question is a new revision.** The answer binds `question_sha256`, so an answer to
  revision 1 keeps naming revision 1 and cannot be re-pointed at revision 2; a reader shows both and
  reports `'Answer <a-...> names question revision 1; the newest revision is 2'` as a warning.
* **A decision is superseded by a new decision whose `supersedes` names it**, and the old record
  stays readable; `decisions list --task` reports both, newest first.

### 11.4 The 2,000-record cap

`RECORDS_PER_ANCHOR_MAX = 2000` readable record comments per anchor and `ITEM_ANCHORS_MAX = 2000`
item anchors per project. At either cap the **write is refused** with the named message in §6; reads
are unaffected and page with `--limit`/`--offset`. The cap is a refusal rather than a silent drop or
an unbounded page, so a project that reaches it must resolve or void items before adding more, and
the read view reports the cap in `warnings` before it is reached.

## 12. Migrating the existing checkpoint `open_items`

Migration is explicit, opt-in and two-step, because `transition()` (`briefing.py:132-138`) refuses a
checkpoint that drops an item without listing it in `resolved`.

**Step 1 - import.** `admin.py open-items-import PROJECT --source-task TASK --owner IDENTITY --actor
OPERATOR [--file checkpoint.json]` reads the newest valid checkpoint of `--source-task` (or a
checkpoint supplied as an attachment), creates **one item anchor per `open_items` entry**, and writes
revision 1 on each, preserving `kind`, `text` and `source` verbatim and setting:

* `owner` from `--owner IDENTITY` (required, because the checkpoint has no owner and inventing one
  would be worse than asking);
* `state: "open"`, `state_note: null`, `resolved_by: null`;
* `due_by` from `--due-by` or `null`, and `for`/`options`/`recommended` null for a non-question item;
* `provenance` as `{"kind":"imported","checkpoint":<comment id>,"item":<old id>}`.

The import does **not** touch the checkpoint.

**Step 2 - retire the checkpoint copies.** The import returns the exact `resolved` array the next
checkpoint must carry, so the coordinator does not rebuild it by hand:

```json
{"schema_version":1,"imported":39,"items":[{"item":"example-anchor","old_id":"blocked-on-keys",
 "comment_id":"<comment id>"}],
 "checkpoint_resolved":[{"id":"blocked-on-keys","reason":"migrated to open-item record <comment id>","evidence":"item:example-anchor"}]}
```

The next checkpoint publishes `open_items: []` with that array verbatim, `previous` naming the
current head and a fresh `activity_cursor`. The array is accepted by `transition()` because
`old - now == resolved` and no carried item changed.

**The migration is one-way after step 2, and the design says so before it is run.** Once
`open_items: []` is published, a rollback to a kit that predates the records shows **zero open
items**: the old kit reads only the checkpoint, the records are inert JSON to it, and the
coordinator loses the list until roll-forward. It cannot be reversed by re-importing into a
checkpoint either, because an item's `text` may be up to `ITEM_TEXT_MAX = 4000` characters while the
checkpoint's `text` limit is 400 (`briefing.py:17`), so **every item over 400 characters cannot go
back**. The only safe reverse is to restore the pre-step-2 checkpoint, so step 2 must run only after
the rollback target is a kit that reads the kinds - that is slice 4 in §13, after the reader slice
has been integrated and deployed.

**What the checkpoint keeps.** `open_items` is not deprecated by this design. A project that does not
adopt the ledger is unaffected; a project mid-migration has both, and §7.4 keeps them apart so nothing
is double-counted. §18 question 5 asks the owner whether to keep both for one release or move in one
step, and the design invents no answer.

**The one behaviour that must not be lost.** The checkpoint's carry-forward rule exists so an item
cannot quietly disappear. The ledger's equivalent is that a state changes only through the operation
named in §4.5 with a reason and evidence, so nothing goes missing silently; the failure mode moves
from "an item was dropped from a list" to "an item is still `open` and nobody looked at it", which
§7.2 and §7.4 make visible rather than fix by inference.

## 13. Slices and build order

0. **Names only - a true slice 0.** The four reserved prefixes (`RESERVED`,
   `_RECORD_KIND_RESERVATIONS`), the `open-item:` reserved label prefix, the hidden-surface tables
   (`RECORD_ANCHOR_FAMILIES['open-item']`, `RECORD_COMMENT_FAMILIES`), and the three backup journal
   names (`.open-item-requests`, `.decision-requests`, `.owner-answers`) in the backup whitelist,
   validator and `restore-new`. **Nothing else**: no reader, no writer, no command, no test of the
   ledger. This slice is what makes a rollback safe, because the prefixes are reserved before any
   record can be written.
1. **Reader slice (tolerant reader).** The parsers and the `items` / `questions` / `decisions` reads,
   and the `brief` attention kinds, exercised against records written by fixtures;
   `open_item_writes` **off**. This is the rollback target slice 0 protects.
2. **Item and question writes - the owner's need arrives here.** `items add|revise|block|unblock|
   resolve`, `questions ask`, `questions answer` (relayed on the endpoint, owner on the host/web
   route), the receipts and `open-item-reconcile`. This is ask-and-answer, and it is deliberately
   before decisions and migration.
3. **Decisions.** `decisions record` on the native decision issue and the read-time projection.
4. **Migration.** `admin.py open-items-import` and the returned `checkpoint_resolved` array, run
   only after slice 1 is the rollback target (§12).
5. **Optional, not filed:** an HTTP mirror of the three read commands, and the deprecation of
   checkpoint `open_items` if the owner answers §18 question 5 that way.

## 14. Joins

### 14.1 `kittrial-5bb.18` - FUTURE, not designed

Revision 1 designed a `review-request` trigger that would read the review ledger at read time. **That
trigger is cut, and no join to `kittrial-5bb.18` is designed here, because the task it would join is
not ready.** `kittrial-5bb.18` is **open**, its review ledger is **not built**, and decision requests
are **never addressed**. A read-time join to a ledger that does not exist and a request path that is
never exercised would be a design against an imagined system. The join is therefore recorded as
**FUTURE work**: when `kittrial-5bb.18` has a built review ledger and an addressed decision-request
path, a later design may add a pointer field (a review-record comment id) on an item. Nothing in this
design reads `review_workflow.py`, and this design writes nothing there.

### 14.2 Requirement proposals and escalation decisions

The proposal ledger's escalation path is unchanged: a coordinator moves a proposal to
`escalated-to-owner` with an `escalation = {question, owner_identity, due_by}` object
(`proposal_records.py:427-433`); the owner answers through `admin.py proposal-decide` (or the web
route with `reviews.approve`) with a mandatory `decision: {decision_id}` (`:435-439`); and
`decision_id` must name an existing native decision issue (`:1533-1538`). This design joins that path
without changing it:

1. **The escalation question is a question item.** `questions ask` writes an `open-item-v1` of kind
   `question` whose `for` is the escalation's `owner_identity` and whose `due_by` is the escalation's
   `due_by`, carrying the escalation's `question` text and its options. The proposal ledger keeps its
   own copy in `escalation` (untouched), and the item is what makes the question queryable by
   `questions --for OWNER`.
2. **The owner's decision is a decision record.** `decisions record` requires `issue`, a native
   decision issue, so the record's `issue` is directly usable as the `decision_id` the proposal
   disposition must carry. That is the whole reason `issue` is required: it makes the join a value,
   not a code change in three existing ledgers.
3. **The answer is an answer record.** `questions answer` with `authority: owner` is written on the
   same host route family as `admin.py proposal-decide`, by the same allowlist, and it closes the
   item the escalation created.
4. **Nothing is written twice.** The proposal disposition stays the proposal ledger's record; the
   decision record does not restate the proposal's state, and `decisions get` reports the proposal
   only through the native `issue`.

### 14.3 The guidance channel and the feedback feed

Neither is touched. `guidance` stays a project-level instruction channel with its own file pair,
version hash and acknowledgement table, and this design does not put coordinator records into it or
read instruction text out of them. `feedback` stays an append-only JSONL feed.

## 15. Worked examples (placeholders only)

All names below are placeholders: `example-task`, `example-anchor`, `alex/session1`,
`coordinator/session1`, `person:alex`, `account:u-0001`, `example.invalid`, and repeated-digit
commits. No example uses a real project, host or person.

**Ask the owner what he must decide.**

```sh
b questions ask --file question.json
b questions --for person:alex
```

```json
{"schema_version":1,"operation_id":"question-2f9c1d7a04b3e856",
 "kind":"question","text":"Which release carries the change: the next minor or the next major?",
 "source":"coordinator pass 2026-10-04","owner":"person:alex","task":"example-task",
 "for":"person:alex","due_by":"2026-11-03",
 "options":[{"id":"a","text":"The next minor release."},{"id":"b","text":"The next major release."}]}
```

The owner's read returns the open questions with their options already in the payload (§7.2), which
is the read that replaces a hand-rebuilt list.

**Record the owner's answer.**

```sh
b questions answer --file answer.json     # refused on this route for authority "owner"
admin.py questions-answer PROJECT --actor OPERATOR --file answer.json
```

```json
{"schema_version":1,"task":"example-anchor","operation_id":"answer-77aa11bb22cc33dd",
 "item":"example-anchor","question_revision":1,"question_sha256":"<64 hex>","owner":"person:alex",
 "option":"a","options_offered":[{"id":"a","text":"The next minor release."},
                                {"id":"b","text":"The next major release."}],
 "words":"Minor. Do not hold the major for this.","authority":"owner"}
```

The answer stores the owner's words verbatim and the options as offered, and the writer then writes
the resolution and the item revision, so `items get example-anchor` reads `state: "resolved"` with
`resolved_by` naming the resolution.

**Record a decision and see it on each task it decides.**

```sh
admin.py ... # b decisions record --file decision.json on the web route
```

```json
{"schema_version":1,"operation_id":"decision-1c2d3e4f5a6b7c8d",
 "issue":"example-task-9001","title":"Ship the change in the next minor release",
 "decides":["example-task","example-task-2"],"authority":"owner","supersedes":null}
```

After the write, **nothing** has changed on `example-task` or `example-task-2`: no comment, no label,
no field. `b brief example-task` shows
`Decision [owner, attested]: Ship the change in the next minor release (decisions get d-...)` because
the read projected the record onto the task, and `b decisions list --task example-task` shows the
same record.

## 16. Test strategy and acceptance criteria

This contribution adds no test, because it adds no behaviour. A `docs/*_DESIGN.md` addition is inert,
and the delivery evidence for it is the regression suite run at the delivered commit, unchanged and
green. The design's own acceptance criteria belong to the slices that implement it, and a second
contributor must be able to prove every one of these with disposable fixtures:

1. **Bytes.** Every record the writer produces re-reads as canonical; a body whose JSON differs from
   `canonical_bytes(record)` is ignored, not trusted.
2. **Closed field sets.** For each of the four kinds, a payload with an unknown field, a missing
   field and an operation-written field each refuses with the specific message, before any write.
3. **One item per anchor.** `items add` creates exactly one closed anchor row carrying
   `open-item` and revision 1; a second item on the same anchor is impossible; the anchor is hidden
   from every surface of the ten-surface list.
4. **The owner route.** An `authority: "owner"` answer on the plain endpoint is refused and writes
   nothing; the same payload on the host route with an operator mapped to the addressee is written
   and closes the item; the same payload with an operator **not** mapped to the addressee is refused
   or forced to `relayed`; an operator not on the allowlist is refused and writes nothing.
5. **The web rule.** A signed-in principal equal to `for` writes an attested, closing owner answer
   without appearing on the operator allowlist; a principal who is not `for` is relayed.
6. **No-self-answer.** A coordinator who asked the question cannot answer it, on either route.
7. **Options fidelity.** An answer whose `options_offered` differs from the item's `options` is
   refused; an answer whose `question_sha256` is stale is refused.
8. **The journal gate.** An `authority: owner` answer with no `.owner-answers` entry reads `untrusted`
   and does not close the item; the same record with its host entry closes it.
9. **Crash ordering and retry.** With the resolution write failed after the answer write, the item
   reads unchanged with an orphan warning; a retry with the same operation id completes the missing
   comments and writes no second answer.
10. **Idempotency.** The same operation id and payload twice yields one comment set and
    `reconciled: true`; a changed payload under the same id is refused.
11. **Projection.** A decision writes nothing on any decided task and still appears in that task's
    `brief` attention and in `decisions list --task`.
12. **State machine.** Each row of §4.5 is exercised, including the refusal of `items resolve` on a
    `question` item and the derived `void`.
13. **Caps.** The 2,001st record on an anchor and the 2,001st item anchor are refused with the named
    message; reads page.
14. **Migration.** A 39-item checkpoint imports to 39 anchors with `text`, `kind` and `source`
    preserved; the returned `checkpoint_resolved` array is accepted verbatim by the next checkpoint;
    an item over 400 characters is shown to be un-reversible into a checkpoint.
15. **Backup.** A backup carries all three journals, `validate_coordination_files` validates the
    path/hash binding, `restore-new` restores them, and a pending receipt after restore is reconciled
    rather than trusted.

## 17. What was cut

The following were in revision 1 and are **removed** because they were not asked for:

* **the five `trigger` kinds with read-time joins** - `manual`, `date`, `task`, `record` and
  `review-request`. The `task`/`record`/`review-request` kinds were joins; the `review-request` join
  was to a ledger that is not built (§14.1). `due_by` remains as the only date;
* **`tags`** - no tag field, no `--tag` filter, no second ordering authority;
* **`--create-issue`** on `decisions record` - the native decision issue is created before the record
  with the existing child-creation path;
* **the work queue** - `work` gains no `attention.coordinator_queue` block;
* **owner voids** - only the operator allowlist may void a record; there is no owner-initiated void
  and no `voided` state;
* **the `decision:<id>` label** and every claim that it was safe (§3.2);
* **the `owner-question-v1` kind and the second anchor** (§3.1);
* **the copied decision body** (`statement`, `rationale`, `alternatives`, `consequences`,
  `prior_decisions`) (§4.4);
* **the `resolves` list** on an answer (§4.3).

Rejected alternatives that survive from revision 1, re-checked:

* **Open items as keyed catalog entries (`AnchoredKind`).** Rejected: it requires one anchor per
  dotted key and a catalog read; items have no human key.
* **One shared anchor holding a ledger of items** (revision 1's model). Rejected on review: it needs
  "many entities on one anchor", which no existing family has, and it forced one `questions answer`
  to write up to 41 comments. One item per anchor removes both.
* **A project-wide `previous` chain.** Rejected: it serializes every coordinator write behind the
  newest comment.
* **Writing a pointer comment on each decided task.** Rejected: it is the reported problem, and it
  writes into another actor's history.
* **A derived `decision:<id>` label on each decided task.** Rejected: unreserved it is editable and
  inherited; reserved it breaks `create --parent` and label edits on every decided task; and it is up
  to 16 label writes on other people's tasks per decision (§3.2).
* **A comment-only decision record.** Rejected: it would require widening
  `proposal_records._check_decision`, the reference-catalog decision link check and the acceptance
  evidence field set, a behaviour change to three existing ledgers.
* **Storing `status` as a caller-supplied field.** Rejected: `state` is written by the operation that
  moved it and refused when a caller supplies it, exactly as `acceptance` is.
* **Building a `RecordSpec` for the new kinds.** Rejected: `RecordSpec` binds a one-revision-plus-
  acceptance family to its write pipeline, not a field-set declaration (§3.3); the kinds declare
  their own closed sets and share the receipt model instead.
* **A separate "free text question" kind.** Rejected: an answer with `option: null` covers it.
* **Putting the ledger in a sidecar file pair, like guidance.** Rejected: records need native
  attribution and native ordering, and a sidecar adds a rollback gap for every record rather than
  only for receipts.

## 18. Owner questions (awaiting the owner)

These are the questions that are the owner's to decide. They are put to the owner now and the
coordinator will record the answers on this task. Each is answerable without reading the rest of this
note, and this design does **not** answer them.

1. Is 4,000 characters enough for an item?
2. Does an answer the coordinator records from conversation close the question, or stay open until
   the owner confirms?
3. Who may enter the owner's answer as the owner's?
4. Who may read questions and answers? (Over SSH this cannot be restricted.)
5. Keep open items in both the checkpoint and the records for one release, or move in one step?
6. Is one tracker issue per decision acceptable?

## 19. What is left undecided

* The six owner questions above.
* Who turns on `open_item_writes`, and when: recommended off by default and turned on by the
  coordinator once the rollback target reads the kinds, exactly as `review_workflow_writes` works.
* Whether to file an HTTP mirror of the three read commands for the web surface (slice 5, optional).
* Whether `RECORDS_PER_ANCHOR_MAX = 2000` and `ITEM_ANCHORS_MAX = 2000` are the right ceilings.
* Whether the read-only `owner-question` attention kind should appear on a task that is not the
  question's `task` link.
