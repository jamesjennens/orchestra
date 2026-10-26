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

**Round 2** answers review `01a16fbd`:
- **Trace is answered by the server from records alone,** for web-only coordinators. The proposer
  uploads the design document with `design.<scope>`; the server derives and stores its index
  (`design-index-v1`: the document's sha256, front block, heading anchors, plan phases with their
  tokens), checks it at acceptance, and serves every trace view. A Coverage panel and a Designs
  list on the web show it. The client only adds `trace --verify`, the repository check (§5,
  gap G4).
- **G1's fix gets the full adversarial review,** and the agent's `decision_id` must be a decision
  issue in the same project that grants record acceptance, checked by the server (§2.3, §8
  slice 1). Slice 1 as built (kittrial-5bb.407) corrects G1 and makes that grant a server-written
  delegation record; see §2.3.
- §7 and slices 1 to 4 are updated to match.

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
- **A coordinator agent** (a credential): the principal's `user_id` is the agent's **owner
  account** (http_auth.py:1800, `Principal(user['id'], …, 'credential', …, agent_id=…)`).
  - Its coordinator grant gives it `CAP_COORDINATE` (http_auth.py:3428, `_may_coordinate`), and a
    grant can only be given while that owner account coordinates.
  - `_require_record_membership(coordinator=True)` reads the role of that owner account, so a
    granted agent **passes** it. (Round 1 of this note said it was refused; that was wrong,
    corrected by slice 1, kittrial-5bb.407.)
  - What does refuse it is the endpoint's party check (endpoint.py `party_check`): an agent may
    never accept or verify a draft proposed by its **own party** (its owner account and every
    other agent or worker credential of that account), whatever `approval_by_another_party` says.

  **Gap G1, as it really is:** where one person owns every agent, coordinators and lanes alike, a
  coordinator agent can accept nothing the lanes propose. Slice 1 (kittrial-5bb.407), following
  the owner's direction of 2026-10-25, makes **every** agent acceptance need a person owner's
  delegation, and that delegation lifts the agent-only own-party refusal (never a draft the
  agent wrote itself, and never the project's independence setting). A delegation is a
  server-written `Kind: delegation-v1` record with a host journal entry, recorded only by a person
  owner's own web session or a listed operator, and checked live at every acceptance; an issue
  body's lines are not used, because a native issue's author is only an `--actor` label
  (docs/HTTP_DEPLOYMENT.md, "Delegated record acceptance").
- **An operator on the host** (`ref accept` over the endpoint with the operator allowlist) passes,
  but that is not a web-only route.

### 2.4 Tasks cite requirements and design: two new pointer kinds (Q2 A)

These extend the context index's grammar (CONTEXT_INDEX_DESIGN_CLAUDE.md §2.1), with the same
rules: a whitespace-separated `KIND:VALUE` token; prose otherwise; never a refusal.

| kind | value | resolves to | flags |
| --- | --- | --- | --- |
| `req` | `KEY@REV`, e.g. `req:srv.backup@2`; `KEY` alone means "current" | the requirement record's revision `REV` (an exact reference once resolved: id, revision, sha256) | `unknown-key`; `unknown-revision`; `superseded` (a newer accepted revision exists, or the owner superseded the requirement); `draft` (the revision is not accepted); `withdrawn` (the owner withdrew the requirement; added by slice 2, kittrial-5bb.417) |
| `design` | `SCOPE#section`, e.g. `design:server-redesign#plan-phase-2`; `SCOPE` alone means the whole design | the selected design (`design.<scope>`'s pinned file and commit) and a heading in it | `unknown-scope` (no `design.<scope>` entry); `unknown-section` (no such heading in the design's server-held index, §5.1); `not-selected` (the entry exists but its revision is not accepted) |

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
| **G1** | a coordinator agent cannot accept a record its own party proposed (the endpoint's party check, §2.3); built by slice 1 as a person owner's delegation | it is an authorization rule, not a convention | 1 |
| **G2** | pointer kinds `req:` and `design:` | the grammar is closed by design; new kinds are a code change, small, in the context index's parser | 2 |
| **G3** | a decision's link set (which tasks and requirements a decision decides) | a decision issue's text can name tasks, but no reader can project it onto them. OPEN_ITEMS_DECISIONS_DESIGN's `coordinator-decision-v1` is exactly this; its slices are designed and not built. **Reuse it; do not add a second kind.** | after this note's slices (a dependency, not a slice here) |
| **G4** (round 2) | the server cannot read the repository, so it cannot resolve `design:` sections or plan coverage for a web-only coordinator | the design's bytes and a server-derived index must be on the server; one reserved record, `design-index-v1`, written only by the server (§5.1) | 3 |

There is **no new record kind** for designs, plans, cross-reviews or disagreements: those are
documents, reference entries, epics and comments. The only new record is the server-written index
of a design (G4), because a web-only coordinator has no checkout.

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

## 5. The trace read: answered by the server, from records alone

Round 2 (review 01a16fbd). Project coordinators now work only on the web site, with no checkout and
no shell, and they are trace's main users. So the server answers every trace view from records
alone. A client command over a checkout is a convenience and the place where the repository is
checked against the records; it is never required.

### 5.1 What the server needs, and who supplies it: the design document and its index

The server has no repository, so a design document reaches it the way a file does: **with the
proposal**.

1. **The proposer uploads the document.** `ref propose` or `ref revise` of a `design.<scope>`
   entry carries the document's bytes **exactly as they are at the pinned commit**. The web form
   takes a file; the client reads `git show <commit>:<path>`.
   - The bytes go into the content-addressed blob store task files already use
     (`<project>/.task-files/`, named by sha256), so no new storage is needed.
   - The entry's authority keeps `{type: repo-path, path, commit, anchor}` and gains the
     document's `sha256`. This is a new version of the repo-path authority, under the catalog's
     forward-compatibility rule; slice 3.
2. **The server derives the index itself.** It never takes an index from the proposer: it parses
   the uploaded bytes and writes one reserved record, `Kind: design-index-v1`, on the entry's
   anchor, holding:
   - `doc_sha256`, `path` and `commit`;
   - the front block's fields (§2.1);
   - the headings, as `{anchor, level, title}`, with anchors computed the way the web's Markdown
     renders them;
   - per heading, the `req:` and `design:` tokens found in that section;
   - the plan phases: every `#plan-phase-n` heading, with its contract, test-oracle and
     requirement lines and the epic id once named.

   The prefix is reserved, so only the server writes it, as with every record family (no raw
   comment can plant one). It is bounded: at most 400 headings, 2,000 tokens and a 1 MiB document.
3. **Acceptance checks it.** When the coordinator accepts `design.<scope>`, the server checks
   three things, or refuses the acceptance:
   - the stored bytes' sha256 equals the authority's `sha256`;
   - the index was derived from those bytes;
   - the front block's `scope` equals the key's scope.

   The acceptor sees the rendered document, as text, beside the accept button, so "what am I
   accepting" is answerable on the web.
4. **What the server cannot check, and who does.** Whether the uploaded bytes really are the file
   at that commit needs the repository.
   - That check runs **where the code is**, as the capability drift check does
     (CAPABILITY_INDEX_DESIGN §5): a client or CI step `trace --verify` reads
     `git show <commit>:<path>` and compares sha256.
   - A match from a trusted verifier (the deployment's `verifiers` list, as for capabilities)
     marks the entry `verified`.
   - A mismatch writes a report, and the entry shows **`design-drift`**: the record says one thing
     and the repository another.
   - Until verified, the entry reads `unverified`. It still counts for trace, labelled as such.
5. **When the index and the file disagree.** The record wins for every read (it is what was
   accepted), and the drift flag tells everyone. The way out is a revision: upload the file as it
   now is at a new commit, and accept again. Nothing is silently re-derived from the repository.

### 5.2 The read, and its web surface

- **On the server:**
  - `GET /v1/projects/{pid}/trace?view=...&scope=...` (CAP_READ, the existing visibility rules),
    and the endpoint action `trace` for the client.
  - Inputs: one export (warm from the read cache), the reference catalog's `design.*` entries,
    the `design-index-v1` records, and the requirement records and manifests.
  - **No repository, no blob read** except when a reader opens a document.
- **The web surface:**
  - **A Coverage panel on the project page**, one line per view with counts ("3 requirements
    have no task; 2 tokens do not resolve; 1 design section has no plan"). Each line opens its
    view.
  - **A Designs list under Records**, one row per `design.<scope>` with its state (selected,
    unverified, drift), its requirement coverage and its plan coverage. A row opens the design
    page: the stored document rendered as text (md.js), with each section's tokens resolved and
    each plan phase linked to its epic.
  - **On the task page**, the task's own tokens resolved in the summary, so a reader sees "cites
    req:srv.backup@2 (superseded by @3)" where they work.
- **In the client:** `trace` prints the same views from the same endpoint action. `trace --verify`
  is the one part that needs a checkout.

### 5.3 What it reports

| view | rows |
| --- | --- |
| `requirements` | each accepted requirement with: designs whose index cites it, tasks citing it, capability records linking it. **Flags:** `no-design` (when the project has designs), `no-task`, `no-capability` (after delivery) |
| `tasks` | each open or recently closed task with the requirements and design sections it cites. **Flags:** `cites-superseded-revision` (computed by `requirement_impact.analyze` over the work graph the server builds from the tokens: the first user of that module for tasks), `cites-nothing` (a task under a scoped epic with no token) |
| `tokens` | every `req:` and `design:` token that does not resolve, with where it is: `unknown-key`, `unknown-revision`, `superseded`, `draft`, `unknown-scope`, `unknown-section` (checked against the **index**, not a file), `not-selected`. **The Q2 condition:** a typo is a row here |
| `decisions` | decision issues with what they supersede and the designs citing them. **Flag:** `possible-decision-in-comment`: an owner's comment on a task or a round with decision words and no decision issue citing it. A heuristic, labelled as one: it lists, it never acts |
| `designs` | each `design.<scope>`: selected or not, `verified`, `unverified` or `design-drift`, its sections with no plan phase, plan phases with no epic, epics with no task, front-block status disagreeing with the entry, review-by past |
| `round` | for a design round epic: each lane task's plan time, delivery time, the publication time of each twin draft, cross-reviews posted, and the converged design and decision. **Flag:** `delivered-after-publication` (independence, §4.1 step 3) |

**Bounds and safety:**
- Every row is an excerpt object, with untrusted text escaped (the context index's rule).
- Pages of at most 50 rows, with a cursor bound to the caller's scope and the export generation.
- Permission filtering happens before counts (the context index's §4.4).
- A coverage block says what was read: for example "4 designs indexed, 1 unverified; 3,402 rows".

### 5.4 A project with no design documents (the coordinator's ask)

It stays useful and cheap:
- **The design columns** say once, at the top, "No designs in this project (no `design.*`
  entries)", and `no-design` is not flagged on every requirement.
- **What still works:** requirements with no task, and no capability once delivered; tasks citing
  superseded revisions; every unresolved `req:` token; possible decisions left in comments.
- **The cost** is one cached export plus the requirement manifests; there are no index records and
  no blobs. **The Coverage panel** shows its requirement and token lines only.

**Cost at kittrial's size** (about 3,400 issues, 7,400 comments, a few dozen requirements and
designs):
- one warm export read;
- one pass over the rows and comments for tokens, decision issues and index records;
- one `analyze` call over a graph of at most a few thousand nodes;
- **no repository read.**

The few seconds this should take is an estimate, not a measurement. Slice 4 measures it on a copy
of a kittrial-sized export, as the context index did for find.

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
| a token typo silently uncounted | the `tokens` view lists every unresolved token | fixtures with each flag (`unknown-key`, `unknown-revision`, `superseded`, `draft`, `unknown-scope`, `unknown-section`, `not-selected`) each appear exactly once |
| **the uploaded document is not the file at the commit** (a mistaken or dishonest upload) | the server stores its sha256 and derives the index from the stored bytes; `trace --verify` where the code is compares with `git show`; only a trusted verifier's match marks `verified`; a mismatch shows `design-drift` | upload bytes that differ from the commit: the entry reads `unverified`, then `design-drift` after a verifier run; a verifier outside the list cannot mark it `verified` |
| **a planted index** | `design-index-v1` is a reserved prefix written only by the server from stored bytes; a raw comment cannot write one; acceptance re-checks bytes, sha256 and index | a raw `comments add` with the prefix is refused; an index whose `doc_sha256` does not match the stored bytes blocks acceptance |
| a selected design changes under its selection | the commit pin plus the stored sha256; reads use the stored index, never a working tree | change the file after acceptance: trace still answers from the record, and `--verify` at the new commit reports drift only if the pinned commit's file changed (it cannot) |
| the stored document leaks or injects | it is project data under the existing read rule, rendered as text (md.js, no HTML), never put into prompts (pointers only, as the context index rules) | a document with HTML, script and bidi controls renders as escaped text on the design page; an agent prompt for a task citing it carries `design:SCOPE#s`, not the text |
| an agent accepts what only the owner may decide | G1's fix (slice 1) requires a server-checked delegation decision; §4.3 lists owner-only choices | slice 1's adversarial tests (below) |
| a heuristic "decision in a comment" is taken as fact | labelled `possible-`; never acts; lists only | the fixture comment "we will go with B" by an owner is listed; the same words by a contributor are not |
| independence claimed but broken | times side by side, `delivered-after-publication`, the fixed statement | a fixture round where one lane delivers after publication is flagged |
| trace leaks private text | permission before counts; the context index's escaping; a caller without access to a task gets no row and no count | a contributor and an outsider each see only what their own reads show |

## 8. Slices (five, each deliverable alone)

1. **A coordinator agent accepting records (G1), with a full adversarial review, and the
   conventions.**
   - **As built (kittrial-5bb.407, after the owner's direction of 2026-10-25).** The test-first
     step found the agent is not refused by membership but by the own-party rule (§2.3). The
     fix changes authorization, so it had the kit's **full adversarial review** and a mutant set.
   - **The rule:** every agent acceptance or verification of a reference or capability record
     needs a **delegation in force**, named as `decision_id`. That delegation lifts only the
     agent-only own-party refusal (its owner's and sibling agents' drafts): an agent never
     accepts a draft it wrote itself, and with `approval_by_another_party` or
     `independent_approval` on it faces the person rule. A delegation counts only when, checked
     live at the write:
     1. it is a server-written `Kind: delegation-v1` record with its host journal entry, on a
        decision issue in the same project, recorded by a person owner's own web session or a
        listed operator (never by an agent; an issue body's lines are not read, since a native
        author is only an `--actor` label);
     2. its recorder is still a person owner of the project (or still a listed operator);
     3. it grants `record-acceptance` to that agent or to every coordinator-grant holder, and the
        agent's grant is still in effect;
     4. its scope covers the record kind, and no later narrowing or revocation excludes it.

     Each refusal names its reason.
   - **Tests:** one per refusal, revocation and narrowing on the next write, the grant removed,
     the recorder demoted, a planted lookalike without its journal entry, a person coordinator,
     contributors and viewers unaffected (tests/test_delegations.py and the native delegation
     suite in tests/test_web_records.py).
   - **Docs:**
     - the design front block, `design.<scope>`, plan phases as epics and cross-review lines;
     - the design-round convention (§4) for coordinators and lanes (WORKER_GUIDE and the
       coordinator's AGENT.md);
     - the web Records tab naming who accepts (the .400 finding).
2. **Pointer kinds `req:` and `design:` (G2).** Parsed and resolved in the context index's grammar
   with the flags of §2.4, resolved on the server: `design:` resolves against `design-index-v1`
   records (slice 3). Until slice 3 lands, a `design:` token resolves to `unknown-scope` unless the
   entry exists, and its section reads `unchecked`.
   - **As built (kittrial-5bb.417).** The kinds are in the one parser (briefing.py,
     `parse_context_pointers`). A malformed value is prose (§2.1). A requirement withdrawn or
     superseded by its owner (`requirement-owner-state-v1`) reads state `superseded` with flag
     `withdrawn` or `superseded`, never `live`. Anything the requirements read cannot verify
     reads `unavailable`. The governance snapshot that owner decisions are checked against is
     read only for a brief holding a `req:` token. `unknown-section` waits for slice 3. See
     docs/BRIEFINGS.md "Pointers in checkpoint source and evidence".
3. **Design documents on the server: upload, index, verify.**
   - The upload on propose and revise, for the web form and the client.
   - The repo-path authority with `sha256` (a new authority version).
   - The blob in the task-files store.
   - The server-derived `design-index-v1` record and its reserved prefix.
   - Acceptance re-checking bytes, sha256 and index.
   - `trace --verify` with the verifiers list, and `design-drift` and `unverified` states.
   - The design page on the web, rendered as text.
   - Tests: the §7 rows for upload, index and drift.
   - **As built, part 1 (kittrial-5bb.418): upload, index, acceptance.** The authority is a new
     type, `repo-document`, inside a `reference-entry-v3` record (a `sha256` added to the v1
     `repo-path` type would make an older kit read the entry malformed; inside v3 it reads
     `unsupported`). New `design.<scope>` entries must use it. The document travels in the
     propose or revise itself (the HTTP body's `document`, read only after the route's authority
     checks with a 1.5 MiB cap from Content-Length; the client's `--document-from-git`, from git's
     object at the pinned commit). Its bytes are a task-file entry of direction `design` on the
     anchor, so backup, verify and the byte limit count them and purge keeps them. The index is a
     function of the bytes, key, path and commit, so revisions that pin one document share it and
     an accepted revision keeps its own. Part 2 builds `trace --verify`, the design page and
     `unknown-section`.
   - **As built, part 2 (kittrial-5bb.418): verify, page, sections.** A report is a reserved
     `Kind: design-verification-v1` record. Trust mirrors capabilities: only `admin.py design-verify`
     by an actor on the operator allowlist or the verifiers list writes a trusted record; reports
     through `ref verify`, `trace --verify --record` or the members-only web route are unverified.
     Any failing report opens `design-drift`; one opened only by untrusted reports reads
     `trusted: false` with `reported_by` (the coordinator's Q4). The design page
     (`GET /v1/projects/{id}/designs/{scope}`, web `/p/{pid}/designs/{scope}`) renders the stored
     document as text with the server's anchors and gives one 404 for everything a caller may not
     read. `design:` sections resolve against the selected index: `live`, or `unknown-section`.
4. **The trace read on the server, and the Coverage panel.**
   - `GET /trace` and the endpoint action, all six views, bounds, permission before counts, the
     no-design case.
   - The Coverage panel on the project page and the Designs list under Records.
   - The task page showing its own tokens resolved.
   - The client `trace` as a convenience.
   - The cost measured on a kittrial-sized export.
   - **As built, part 1 (kittrial-5bb.425): the server read, the endpoint action, `GET /trace`, the
     client.** trace_read.py is a library over one snapshot (`Snapshot`): the rows `brief` resolves
     pointers from (briefing.py `ContextPointers.loaded`), with an `exclude` hook for any per-caller
     rule. Requirement drafts and owner states are readable by every member today (the requirement
     reads strip only operation handles, http_service.py `_requirements_state_only`), so no role-based
     exclusion applies yet. `cites-superseded-revision` is `requirement_impact.analyze`'s first
     production use, over manifests of the cited and the current accepted revisions. Lines read from
     comments (the round's publication line, the owner-only decision heuristic) count only from a
     person owner or coordinator in current membership or a listed operator; the converged design and
     its decision are read from the `design.<scope>` entry, never from comments. **Measured** on a
     synthetic kittrial-sized export (3,461 rows, 7,620 comments, 40 requirements, 20 indexed designs;
     tests/trace_cost.py, Windows, Python 3.11): `analyze` 114 ms; each view's first answer in a fresh
     interpreter 359-930 ms (`summary`, which builds every view, is the 930), a second answer 64-587 ms;
     the generation (a digest of the filtered snapshot) 46 ms. **Bounds against planted input (review of
     part 1):** every list in a row is bounded and the 64 KiB cap holds for one row; an answer resolves at
     most 2,000 distinct tokens and validates each requirement ledger once, so 10 tasks of 1,000 distinct
     `req:` tokens read in about 0.1 s (8.4-8.9 s before); the views are built after the shared lock is
     released. Part 2 builds the web.
5. **Orchestra's system design.** docs/SYSTEM_DESIGN.md rewritten with the front block, uploaded as
   `design.orchestra` and accepted, IMPLEMENTATION_PLAN retired, and trace's first report on
   kittrial attached.

**Each slice stands alone:**
- Slice 1 helps every web-only project today.
- Slice 2 helps `brief` before any design is indexed.
- Slice 3 lets a web-only coordinator read and accept a design.
- Slice 4 answers the requirement, task, token and decision views even with no designs (§5.4).
- Slice 5 uses whatever has landed.

**G3** (the decision link set) is the OPEN_ITEMS design's later slices: a dependency for richer
decision rows, not a slice here.
