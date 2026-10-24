# Traceability: requirements, decisions, design, plan, tasks and code (design note, Claude lane)

Design note for `kittrial-5bb.401`. Base: main `a89790a33938fd316a381043b2499b7fca62ce43` (release
1024d). No product code in this delivery.

Plan checkpoint `01a16f7a`, verified by the coordinator on 2026-10-24, with these answers and conditions:
- **Q1 = A.** The selected design is a reference entry `design.<scope>` with repo-path authority
  pinned to a commit. Condition: show who accepts it and the route a web-only coordinator uses;
  where the route is missing, name the gap and slice it.
- **Q2 = A.** Tasks cite `req:KEY@REV` and `design:SCOPE#section` tokens in the context index's
  grammar. Condition: trace flags tokens that do not resolve.
- **Q3 = A.** Design rounds are a convention the coordinator runs, with a command later. Conditions:
  say how independence is kept, N ≥ 2, what a recorded disagreement looks like, and when the
  owner is asked rather than the coordinator deciding.
- **Also:** say what trace shows for a project with no design documents. Orchestra's own system
  design stays a slice.

The repository is public. The private project that prompted this task is described only in the
brief's general terms: a server redesign treated as a sub-project, two independent proposals, a
cross-review that converged, owner choices left in task comments, requirements not revised to
match, and no single selected design.

## 0. The problem in one paragraph

The kit records each link of the chain somewhere, but nothing ties the links together:
- **requirements** are records, with revisions, baselines and an impact module;
- **owner decisions** are native decision issues, or, too often, sentences in task comments;
- **designs** are Markdown files in the repository, or task comments;
- **implementation plans** are a hand-written table (docs/IMPLEMENTATION_PLAN.md), written once
  and now stale;
- **tasks** carry free text;
- **code** is reached through contributions, and through capability records that do link
  requirements.

So a coordinator cannot answer the cheap questions:
- which requirements have no design or no task;
- which tasks were built against a requirement revision that has since changed;
- which owner decision lives only in a comment;
- which design section has no plan.

This note links the chain with records the kit already has, plus two pointer kinds and one
read, and names the few real gaps.

## 1. What the kit already has (cited at main a89790a)

| link | what exists | where | what it can carry in the chain | where the chain breaks |
| --- | --- | --- | --- | --- |
| Requirements | requirement and brd-section records with a parent (a job), revisions and acceptance states | requirement_records.py:89-93 (kinds, state labels, fields), :185-211 (parent), :601 (native create with `--parent`), :626 (the record spec) | the requirement text, its revision and its job (the sub-project scope, §3) | nothing outside the requirement points back at an exact revision except capability records |
| Exact references | `{id, revision, sha256}` references and their check | export_requirements.py:111-123 (`exact_reference`), requirement_baseline_records.py:328 | a precise, hash-bound citation of one revision | only manifests and the impact graph use it; tasks do not |
| Baselines | named, write-once baselines of a job's requirements | requirement_baselines.py:193 (`get`), :208 (`current`), :100 (`_write_once`) | "what was agreed at time T" | — |
| Impact | revision-aware reassessment over a work graph of nodes `{id, kind, requirements, depends_on, evidence}` | requirement_impact.py:23 (`analyze`), :49-75 (graph validation; manifests keyed by project and **job**, :63) | needs-reassessment and reaffirmed states, without changing records | **nothing builds the work graph from tasks**: the module is ready and unused for tasks |
| Publication | a BRD from a manifest | publish_brd.py:358 | the requirement document | — |
| Proposals | contributor proposals targeting a requirement, a requirement area or a new requirement | proposal_records.py:103-105 (fields), :239-255 (targets) | "someone asks for this change" | — |
| Decisions | native issues of type `decision` (or the `decision` label), checked where a record names one | proposal_records.py:1583-1586 (`_check_decision`), reference_records.py:456-473 (`check_decisions`, `_decision_issue`), requirement_http.py (`create_decision`, used by requirement acceptance and room decisions) | the owner's or coordinator's decision, with its rationale (templates/DECISION.md) | no supersession link between decisions; no link set from a decision to the tasks it decides (OPEN_ITEMS_DECISIONS_DESIGN designs `coordinator-decision-v1`; only slice 0, the names, is built) |
| Reference records | keyed entries with authority (`repo-path`, `url`, `attestation`, `decision`), owner, review-by, tags and a `decisions` list | reference_records.py:134 (authority kinds), :142-143 (fields), :263-267 (`valid_decisions`) | **a selected design, pinned to a commit, owned, reviewed by a date, citing the deciding decision** (§2.2) | — |
| Record acceptance over HTTP | `POST /v1/projects/{pid}/references/{key}/accept` (and capabilities), which needs `CAP_COORDINATE` **and** an owner or coordinator membership role | http_service.py:10196-10223 (`_records_write`, `coordinator=True`), :9966-9976 (`_require_record_membership`) | a person coordinator accepts on the web (the Records tab) | a coordinator **agent** may be refused: §2.3 and gap G1 |
| Capability records | keyed entries linking requirements `[{key, revision?}]`, code and test pointers, design anchors | capability_records.py:101-103 (fields), :173-185 (`valid_requirements`), :279-285 (live check) | **requirement → code**, the end of the chain | design anchors are free paths, unchecked against a selected design |
| Epics and tasks | native issues; `epic`, `decision`, `gate` and `job` are never claimable | work.py:19 (`CLAIMABLE_EXCLUDED_TYPES`) | plan phases (epics) and their tasks | tasks cite nothing machine-readable |
| Contributions and reviews | the review chain: contribute, request-changes, respond, approve | review_workflow.py:91-94 (`EXTRA`) | "this commit delivered this task" | — |
| Context index | the pointer grammar `KIND:VALUE` in checkpoint sources and evidence, resolved by `brief`; a bounded `find`; `review --rounds` | docs/design/CONTEXT_INDEX_DESIGN_CLAUDE.md §2.1 (grammar), §4 (find) (approved, not yet built) | the place the new pointer kinds go (§2.4) | — |
| `req:` pointers | **none in code at a89790a** | `grep "req:"` finds none in *.py; docs/SYSTEM_DESIGN.md states the intent ("Design/task/evidence: exact baseline or requirement revision references") | — | this note defines them (§2.4) |
| A system design and a plan | docs/SYSTEM_DESIGN.md (49 lines, a draft of the requirements phase) and docs/IMPLEMENTATION_PLAN.md (43 lines; a table mapping work rows to R01-R12, kittrial-pth tasks) | — | the only requirement → plan map the kit has | hand-written, never updated, no design in between (§6) |

**Two facts shape the design:**
- The kit already has hash-exact requirement references and an impact engine that consumes a work
  graph. What is missing is a way for tasks and designs to state their references so a read can
  build that graph.
- The reference catalog already gives "an owned, dated, pinned fact with a deciding decision".
  That is exactly what "the selected design" is.

## 2. The chain

```
requirement (job J, key K, revision r)        requirement records, baselines
   │  cited by req:K@r
   ▼
selected design  doc docs/design/<scope>.md @ commit C   reference entry design.<scope>
   │  open decisions → decision issues (deciding, superseding)
   │  sections #s
   ▼
implementation plan  the design's "Plan" section: phases → one epic each
   │  cited by design:<scope>#plan-phase-n
   ▼
tasks  under the phase epic; description cites req:K@r and design:<scope>#s
   │
   ▼
deliveries  contributions and reviews on each task (commit, base)
   │
   ▼
capability  capability record: requirements [{K, r}], code, tests, design anchors
```

Each arrow is either a record the kit has or a pointer token. The trace read (§5) walks them.

### 2.1 A design document: the front block

A design is a Markdown file in the repository, as now: docs/design/<NAME>.md, or a project's own
path. It gains one small front block that a read can parse, and stays prose below it:

```markdown
<!-- design-v1
scope: server-redesign
status: candidate            # candidate | selected | superseded | rejected
requirements: [srv.availability@3, srv.backup@2, srv.migration@1]
decisions-open: [proj-41, proj-42]
alternatives: [docs/design/SERVER_REDESIGN_B.md@1a2b3c4]
supersedes: []
-->
```

- **The front block** is an HTML comment, so it renders as nothing and older readers ignore it.
- **`status` is the author's claim.** The authority is the reference entry (§2.2): a design is
  "selected" when `design.<scope>` points at it, whatever its own block says. Trace reports any
  mismatch.
- **Section anchors** are the Markdown headings (`#plan-phase-2`), so `design:<scope>#plan-phase-2`
  names one.

### 2.2 The selected design: the reference entry `design.<scope>` (Q1 A)

| field | value |
| --- | --- |
| `key` | `design.<scope>`, e.g. `design.server-redesign` (the catalog's `<area>.<slug>` form) |
| `authority` | `{type: "repo-path", path: "docs/design/SERVER_REDESIGN.md", commit: "<40-hex>", anchor: "design-v1"}` |
| `statement` | one paragraph: what was selected and what it replaces |
| `owner` | the person or agent account that answers for the design (`person:` or `account:`) |
| `review_by` | when the selection must be looked at again |
| `decisions` | the decision issues that selected it (the owner's or the coordinator's, §4.4) |
| `tags` | `design`, the scope |

**Why this, and not a new kind:**
- **Revisions are history.** A later selection is a new revision of the same key, and `ref get`
  shows what replaced what.
- **The commit pin keeps it immutable.** The selected text cannot change under the selection.
- **review_by brings it back** to a coordinator's attention when it is due (the catalog's existing
  attention).
- **`decisions` ties it to the decision issue.**
- **Readers already have it:** the web Records tab, `ref get` and `find` (context index §4) all
  read it.

### 2.3 Who accepts `design.<scope>`, and through which route (the condition on Q1)

**Who accepts.** Per the coordinator's statement of how projects run now, records are accepted by
the project's coordinator under a recorded delegation decision. The owner (James) is not in the
loop unless the decision is his (§4.4).
- **The proposer** is the lane or coordinator that wrote the design.
- **The acceptor** is the project's coordinator. Its acceptance carries
  `decision_id` = the delegation decision issue, as the field set already allows
  (`REF_ACCEPT_FIELDS`, http_service.py:9942-9944: `decision`, `evidence`, `decision_id`, `owners`,
  `approvers`, `policy`).

**The route.** `POST /v1/projects/{pid}/references/design.<scope>/accept` (http_service.py:10196,
`_records_write(..., coordinator=True)`). The web Records tab's "Review & accept"
(web/js/views/records.js:168) opens the record, whose accept calls this route (web/js/api.js:610).
It requires:
1. `CAP_COORDINATE` in the project (`_project(ctx, needed)`, :10200); and
2. a membership role of owner or coordinator for `principal.user_id` (:9966-9972).

**Who passes today:**
- **A person who is the project's coordinator** (signed in on the web): passes both. **This route
  exists and works for a web-only person coordinator.**
- **A coordinator agent** (a credential): the principal is the agent's own account
  (http_auth.py:1800, `Principal(user['id'], …, 'credential', …, agent_id=…)`).
  - Its coordinator grant gives it `CAP_COORDINATE` (http_auth.py:3428, `_may_coordinate`).
  - An agent is a project member as a **contributor** ("It works as a contributor", the agents
    page). So `_require_record_membership(coordinator=True)` refuses it, by reading the code.

  **Gap G1:** the web route a coordinator agent would use to accept a record refuses it. A
  web-only project whose coordinator is an agent cannot accept `design.<scope>`, or any record,
  except through an owner. Slice 1 verifies this with a test first, then fixes it: an effective
  coordinator grant counts as the coordinator role for `coordinator=True` record writes, and the
  acceptance must name the delegation decision (`decision_id`) when the acceptor is an agent.
- **An operator on the host** (`ref accept` over the endpoint with the operator allowlist) passes,
  but that is not a web-only route.

### 2.4 Tasks cite requirements and design: two new pointer kinds (Q2 A)

These extend the context index's grammar (CONTEXT_INDEX_DESIGN_CLAUDE.md §2.1), with the same
rules: a whitespace-separated `KIND:VALUE` token; prose otherwise; never a refusal.

| kind | value | resolves to | flags |
| --- | --- | --- | --- |
| `req` | `KEY@REV`, e.g. `req:srv.backup@2`; `KEY` alone means "current" | the requirement record's revision `REV` (an exact reference once resolved: id, revision, sha256) | `unknown-key`; `unknown-revision`; `superseded` (a newer accepted revision exists); `draft` (the revision is not accepted) |
| `design` | `SCOPE#section`, e.g. `design:server-redesign#plan-phase-2`; `SCOPE` alone means the whole design | the selected design (`design.<scope>`'s pinned file and commit) and a heading in it | `unknown-scope` (no `design.<scope>` entry); `unknown-section` (no such heading at the pinned commit); `not-selected` (the entry exists but its revision is not accepted) |

**Where tokens go:**
- a task's **description** (the stable place), and a checkpoint's open-item `source` and resolved
  `evidence`, as the context index allows;
- a plan phase's epic description: `design:<scope>#plan-phase-n` and the requirement keys of that
  phase;
- a contribution summary may carry them too.

**Resolution:**
- `brief` resolves them like every pointer (context index §2.1.2): a label, a state, untrusted
  text, never a refusal.
- `trace` (§5) **flags every token that does not resolve**: unknown key, unknown revision or
  section, superseded revision. A typo is visible, never silently uncounted (the condition on Q2).

### 2.5 The plan: a section of the design, with epics

A selected design ends with a **Plan** section: phases, each a heading `#plan-phase-n` with:
- **a contract:** what the phase delivers, and the interfaces it fixes;
- **test oracles:** the checks that prove it, named so tests can cite them;
- **the requirements it covers**, as `req:` tokens;
- **the epic id once filed.**

Each phase becomes one epic (never claimable, work.py:19), whose tasks cite the phase and their
requirements. The plan is versioned with the design: a new plan is a new design revision and a new
`design.<scope>` revision, so tasks citing the old phase show up as citing a superseded selection.

### 2.6 Decisions: supersession, alternatives, cross-reviews

- **Owner and coordinator decisions** are native decision issues (the existing predicate,
  proposal_records.py:1586).
  - Supersession is a convention: the newer decision's description starts with
    `Supersedes: decision:<id>`, a pointer token trace resolves.
  - A decided design records the decision in `design.<scope>.decisions`.
- **Alternatives stay immutable as commits.** A rejected or superseded design stays in the
  repository, its front block `status: rejected` or `superseded`, and the selected design's front
  block lists it under `alternatives` with its commit.
- **Cross-reviews** (§4) are review comments on the other lane's design task, with a fixed first
  line (`Cross-review of <task> at <commit>`). They are not a new record kind: the review chain and
  comments carry them, and trace finds them by that line.

### 2.7 The real gaps, and why each needs something new

| gap | what is missing | why the conventions are not enough | slice |
| --- | --- | --- | --- |
| **G1** | a coordinator agent cannot accept a record over the web (§2.3) | it is an authorization rule, not a convention | 1 |
| **G2** | pointer kinds `req:` and `design:` | the grammar is closed by design; new kinds are a code change, small, in the context index's parser | 2 |
| **G3** | a decision's link set (which tasks and requirements a decision decides) | a decision issue's text can name tasks, but no reader can project it onto them. OPEN_ITEMS_DECISIONS_DESIGN's `coordinator-decision-v1` is exactly this; its slices are designed and not built. **Reuse it; do not add a second kind.** | after this note's slices (a dependency, not a slice here) |

There is **no new record kind** for designs, plans, cross-reviews or disagreements: those are
documents, reference entries, epics and comments.

## 3. Sub-projects

A sub-project is a part of a project with its own requirements, design and plan, such as a server
redesign inside a product. It is one **scope name**, used in three places that already exist or
are conventions:

| piece | what carries the scope | exists? |
| --- | --- | --- |
| its requirements | a requirement **job** (the parent of its requirement records, requirement_records.py:185-211); manifests and baselines are keyed by job (requirement_impact.py:60) | yes |
| its design | `design.<scope>`, and the design document's `scope:` | convention (§2) |
| its plan | an epic per phase, labelled `scope:<scope>`, under one sub-project epic | convention |
| its tasks | under those epics, citing `req:` keys of its job | convention |

**One rule keeps it simple: the scope name equals the job's key.** Trace filters by scope
(`trace --scope server-redesign`), so a coordinator reads one sub-project's coverage without the
rest.

A sub-project's requirements may refine a parent requirement. That refinement is a requirement
record under the sub-project's job, citing `req:<parent-key>@<rev>` in its rationale, so impact
and trace follow it.

## 4. Multi-agent design: a design round (Q3 A)

A **design round** asks two or more lanes to design the same thing independently, lets each read
the others only after all have delivered, cross-reviews, converges or records disagreement, and
ends in a decision. It runs on existing records, as a convention the coordinator follows; a command
comes later (slice 4).

### 4.1 The steps

1. **Open.** The coordinator files one **round epic** (`design-round:<scope>`) and **N ≥ 2 child
   tasks**, one per lane, with the same brief.
   - The brief names: the scope; the requirements, as `req:` tokens; the reading budget; the
     deliverable (a design document with the front block, status `candidate`); and the
     independence rule (step 2).
   - Each lane plans first, as every task does.
   - **N ≥ 2 is required.** One lane is an ordinary design task, not a round.
2. **Independence (the blind phase).** No lane reads another lane's task, branch, comments or draft
   until **every** lane has delivered.
   - Each lane delivers on **its own branch or bundle**, not on a shared one.
   - The coordinator **publishes the other drafts only after the last delivery**. In kittrial this
     was a review/ branch pushed after both lanes had delivered.
   - Each lane's evidence states, in a fixed sentence: "I did not read <the twin tasks>, their
     branches or their comments before delivering."
3. **How the coordinator checks independence.** Prevention is partial, so the check is evidence:
   - **Times.** Every lane's plan checkpoint and delivery come before the publication time of any
     twin draft. The round read in trace (§5) prints them side by side.
   - **Access.** A twin's draft is not reachable from a lane before publication when each lane
     delivers on its own branch or bundle. A lane with read access to the shared repository could
     still fetch a pushed twin branch, so the coordinator asks lanes to deliver as bundles, or
     publishes nothing before the end of the blind phase.
   - **Statements.** Each lane's fixed sentence. A lane that broke independence says so, and its
     draft is treated as not independent: the round continues, the draft is marked.

   This is attribution, not proof, like every self-declared fact in the kit. The design does not
   claim more.
4. **Cross-review.** After publication, each lane reads every other draft and posts one
   **cross-review** comment on each other lane's task (`Cross-review of <task> at <commit>`). Each
   one lists:
   - what the other draft does better (to adopt);
   - where they differ, with each side's argument;
   - the requirements either draft misreads (`req:` tokens).
5. **Converge.** One lane, chosen by the coordinator, or the coordinator itself writes the
   **converged design**. It is a new document with `status: candidate`, `alternatives` listing every
   lane draft at its commit, and an **Adopted from** list crediting each idea to its draft. Where
   the lanes still disagree, the disagreement is recorded (§4.2), not hidden.
6. **Decide.** A decision issue selects the converged design, or one lane's draft, and names the
   rejected alternatives by commit. `design.<scope>` is then proposed and accepted (§2.3).

### 4.2 What a recorded disagreement looks like

A section of the converged design, one row per point the lanes did not settle:

```markdown
## Disagreements

| point | position A (docs/design/X_A.md@1a2b3c4) | position B (docs/design/X_B.md@5d6e7f8) | what it costs either way | who decides | decision |
| --- | --- | --- | --- | --- | --- |
| Where the index lives | in memory, rebuilt per process | a file beside the project | A: no rollback concern, slower cold read; B: faster, a second store | coordinator | decision:proj-51 |
| Whether to keep the old API one release | keep it | drop it | A: one release of double maintenance; B: customers must move now | **owner** (changes req:api.compat@2) | decision:proj-52 (open) |
```

Each row names the two positions by document and commit, the cost of each, who decides (§4.3), and
the decision issue once there is one. A design is not `selected` while a row's decision is open,
unless the row is explicitly deferred to a later phase.

### 4.3 When the owner is asked, and when the coordinator decides

**The coordinator decides** (and records a decision issue) when the choice:
- stays inside accepted requirements (no requirement text or acceptance changes);
- stays inside the delegation decision's terms (cost, risk, schedule), as that decision states
  them;
- or is a technical choice between designs that both meet the requirements.

**The owner is asked** when the choice:
- would change a requirement, its acceptance criteria or its priority (the requirement must then
  be revised and accepted before the design is selected: the step the private project skipped);
- exceeds the delegation's cost, risk or scope bounds;
- concerns something only the owner can know (customers, contracts, budget);
- or is a disagreement the lanes and the coordinator could not settle and that the delegation
  does not cover.

The owner's answer is a decision issue, never only a comment. Trace flags owner sentences that
look like decisions and live only in comments (§5).

### 4.4 The worked example: kittrial-5bb.392 and kittrial-5bb.393 (public)

The kit's own context-index note was designed this way:
- **Open:** two lanes (Claude, Kimi), one brief each. The tasks were twins, with "do not read the
  twin until you deliver" in each brief.
- **Blind phase:** both planned and delivered on their own. The Kimi lane delivered as a bundle on
  its own host; the Claude lane delivered on its own branch.
- **Independence check:** the coordinator published the twin's note on a GitHub review/ branch only
  after both had delivered. The Claude lane's delivery states that it did not read .393, and it read
  the twin note only after that publication, for the four items the review named.
- **Cross-review:** done by an independent reviewer the coordinator spawned, who compared the two
  blind and scored them. The lanes did not cross-review each other. **This note's convention adds
  the lane-to-lane cross-review the example lacked.**
- **Converge:** the coordinator chose the Claude note as the base and named four ideas from the twin
  to adopt (permission before ranking, a coverage envelope, digest-plus-range pointers, a memo that
  is not a store). The next round of the chosen note adopted them with credit. There was no
  separate converged document: the adopted note became it.
- **Decide:** the coordinator approved round 3 as the design.
- **What the convention would have recorded better:**
  - a decision issue naming the selected note and the twin by commit;
  - a `design.context-index` reference entry;
  - the Disagreements table: here none survived, and the table would say so.

## 5. The trace read

### 5.1 The command

```
trace [--scope SCOPE] [--kind requirements|tasks|decisions|designs|tokens|round] [--limit N] [--cursor C]
```

- It runs in the **client**, against a checkout of the project's repository, like
  `capability lookup`. The server holds the records; the designs are files in the repository,
  which the server does not have.
- It reads:
  - one export through the existing endpoint read (the read cache serves it warm: work and wait
    already use it);
  - the reference catalog (`design.*` entries);
  - the requirement manifest of each job;
  - the design documents at their pinned commits, with `git show <commit>:<path>` in the checkout.
- **Read-only:** no record changes, no acknowledgement.

### 5.2 What it reports

| kind | rows |
| --- | --- |
| `requirements` | each accepted requirement with: designs citing it (`design.<scope>` front blocks), tasks citing it, and capability records linking it. **Flags:** `no-design` (when the project has designs), `no-task`, `no-capability` (after delivery) |
| `tasks` | each open or recently closed task with the requirements and design sections it cites. **Flags:** `cites-superseded-revision` (computed by `requirement_impact.analyze` over the work graph trace builds from the tokens: the first user of that module for tasks), `cites-nothing` (a task under a scoped epic with no token) |
| `tokens` | every `req:` and `design:` token that does not resolve, with where it is: `unknown-key`, `unknown-revision`, `superseded`, `draft`, `unknown-scope`, `unknown-section`, `not-selected`. **The Q2 condition:** a typo is a row here |
| `decisions` | decision issues with what they supersede and the designs citing them. **Flag:** `possible-decision-in-comment`: a comment by a project owner on a task or a design round that contains decision words ("I decide", "we will", "go with", "choose") and is not cited by any decision issue. A heuristic, labelled as one: it lists, it never acts |
| `designs` | each `design.<scope>`: its pinned document, the sections with no plan phase, plan phases with no epic, epics with no task. **Flags:** front-block `status` disagrees with the entry, and review-by past |
| `round` | for a design round epic: each lane task's plan time, delivery time, the publication time of each twin draft, cross-reviews posted, and the converged design and decision. **Flag:** `delivered-after-publication` (independence, §4.1 step 3) |

**Bounds and safety:**
- Every row is an excerpt object, with untrusted text escaped (the context index's rule).
- Pages of at most 50 rows, with a cursor bound to the scope and the export generation (the
  context index's §4.4 rule).
- Every answer carries a coverage block saying what was read and what was not, for example
  "3 design documents read at their pinned commits; 1 not found in this checkout".

### 5.3 A project with no design documents (the coordinator's ask)

It stays useful and cheap:
- **The design columns** say once, at the top, "No designs in this project (no `design.*`
  entries)". They are not repeated on every row, and `no-design` is not flagged on every
  requirement: a project that never writes designs is not shamed for it.
- **What still works:**
  - requirements with no task, and no capability once delivered;
  - tasks citing superseded requirement revisions;
  - every unresolved `req:` token;
  - possible decisions left in comments.
- **The cost** is the one export (cached), plus the requirement manifests. No `git show` happens,
  because there are no pinned documents.

**Cost at kittrial's size** (about 3,400 issues, 7,400 comments, a few dozen requirements and
designs): one warm export read (the read cache), one pass over the rows and comments to find tokens
and decision issues, one `analyze` call over a graph of at most a few thousand nodes, and a few
dozen `git show` calls. The few seconds this should take is an estimate, not a measurement. Slice 3
measures it on a copy of a kittrial-sized export, as the context index did for find.

## 6. A system design for Orchestra itself

**It belongs in a slice, not in this note** (slice 5). Orchestra is the first user of the chain:
1. **Requirements:** the kit's own requirement records (R01-R12 and later).
2. **A short maintained docs/SYSTEM_DESIGN.md** with the front block:
   - scope `orchestra`;
   - the components: endpoint, web service, client, records, rooms, files, lanes, context index;
   - each component's contract;
   - the requirements each serves;
   - its open decisions;
   - **at most a few hundred lines**, pointing at the design notes rather than restating them.
3. **`design.orchestra`** pinned to it, accepted by the coordinator.
4. **docs/IMPLEMENTATION_PLAN.md replaced** by the design's Plan section and live epics; the old
   table is retired with a pointer.
5. **trace run over kittrial,** its first report attached to the slice's evidence. That shows the
   real coverage of the kit's own requirements.

## 7. Risks, and the test for each

| risk | mitigation | test |
| --- | --- | --- |
| a token typo silently uncounted | trace's `tokens` view lists every unresolved token | fixtures with each flag (`unknown-key`, `unknown-revision`, `superseded`, `draft`, `unknown-scope`, `unknown-section`, `not-selected`) each appear exactly once |
| a selected design changes under its selection | the commit pin; trace reads the pinned commit, never the working tree | edit the file after selection: trace still reads the pinned text and flags `newer-unselected-text` |
| an agent accepts what only the owner may decide | G1's fix requires the delegation decision on an agent's acceptance; §4.3 lists owner-only choices | an agent's accept without `decision_id` is refused; with it, accepted and attributed |
| a heuristic "decision in a comment" is taken as fact | labelled `possible-`; never acts; lists only | the fixture comment "we will go with B" by an owner is listed; the same words by a contributor are not |
| independence claimed but broken | times side by side, `delivered-after-publication`, the fixed statement | a fixture round where one lane delivers after publication is flagged |
| trace leaks private text | the context index's visibility and escaping rules; it reads only what the caller may read | a caller without access to a task gets no row for it, and counts exclude it |

## 8. Slices (five, each deliverable alone)

1. **Records acceptance for a coordinator agent, and the design convention (G1).**
   - A test showing whether a coordinator-grant agent can accept a record through
     `POST …/references/{key}/accept`. If it is refused, as the code reads, the fix: an effective
     coordinator grant satisfies the coordinator role for `coordinator=True` record writes, and an
     agent's acceptance requires `decision_id`.
   - Docs: the design front block, `design.<scope>`, plan phases as epics, cross-review lines.
   - The web Records tab names who accepts (the .400 finding).
2. **Pointer kinds `req:` and `design:` (G2).** Parsed and resolved in the context index's grammar,
   with the flags of §2.4. This lands with or after the context index's slice 1; until then trace
   (slice 3) resolves them itself.
3. **The trace read.** The client command, its six views, bounds and coverage, the no-design case,
   and the cost measured on a kittrial-sized export.
4. **Design rounds.** The convention written up for coordinators and lanes (WORKER_GUIDE, the
   coordinator's AGENT.md), and trace's `round` view. A command to open a round (the epic and N
   tasks) is optional here and can follow.
5. **Orchestra's system design.** docs/SYSTEM_DESIGN.md rewritten with the front block,
   `design.orchestra` accepted, IMPLEMENTATION_PLAN retired, and trace's first report on kittrial.

Each slice stands alone: slice 1 helps every web-only project today; slice 2 helps `brief` without
trace; slice 3 works on tokens or none (§5.3); slice 4 is mostly documentation; slice 5 uses only
conventions if 2 and 3 are not there yet. **G3** (the decision link set) is the OPEN_ITEMS design's
later slices: a dependency for richer decision rows, not a slice here.
