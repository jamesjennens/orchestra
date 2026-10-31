# Programs: several projects under one coordinator (design note, Claude lane)

Status: design note (kittrial-5bb.404), no code. It extends the traceability note
(docs/design/TRACEABILITY_DESIGN.md, kittrial-5bb.401, approved at 783120f) from one project to a
program of several. Plan VERIFIED with Q1-Q4 all A and two additions:
- the adoption of an owner-only program decision is a person owner's act (§6.2);
- nothing in `interface-v1` may block a later runtime reader (§4.6).

## 0. The problem in one paragraph

A larger effort is several Orchestra projects, one per area, each with its own coordinator and
lanes. They share infrastructure, and a program coordinator plans with the owner and delegates to
them. The motivating case is a trading platform of several areas that exchange data through
shared interfaces. Area coordinators feed suggestions
back up. The traceability note links requirement, design, plan, task and code inside one project.
A program needs that chain across projects, plus a register of the interfaces the areas share. It
must not loosen what makes the kit safe: **nothing becomes readable in another project by
accident.**

## 1. What the kit has today (cited at main b9b1c12)

- **Nothing groups projects.**
  - A project is a directory on the host (admin.py:2300-2304) and `state['projects'][pid]` on
    the web (http_auth.py:1990-2039).
  - There is no program, parent or organisation field anywhere.
  - The nearest framing is COORDINATORS_PER_PROJECT_DESIGN.md: several projects on one
    installation, each with its own coordinator.
- **Roles are per project:** viewer < contributor < coordinator < owner, with capabilities per
  role (http_authority.py:362-390).
  - A person coordinates through their role.
  - An agent needs a per-project coordinator grant, capped by what its owner holds there
    (http_authority.py:596-625; `_may_coordinate`, http_auth.py:3428-3471).
  - Operators are one installation-wide allowlist (review_workflow.py:526-557).
- **Isolation is strict.**
  - Every web read checks the caller in that project.
  - An agent credential reaches only the projects in its **live** grant list; anything else is a
    404, the same answer as a project that does not exist (http_authority.py:832-845).
  - The host route refuses projects outside the key's list (endpoint.py:949-965).
- **Reads across projects are views placed side by side.** Each is re-checked per project, and
  none shares a record:
  - My work: `GET /v1/me/work`, at most 50 projects (http_service.py:12176-12260, :125);
  - an agent's next action across its granted projects (http_service.py:8186-8209).
- **Requirements belong to one project.**
  - A manifest carries one project and one job (requirements.py:20-30).
  - Impact refuses history "from another project or job" (requirement_impact.py:27-28, :61-62).
  - A record's references stay inside its manifest (requirements.py:143-170).
- **Reference and capability records are project-scoped.**
  - Their keys carry no project (reference_records.py:102; capability_records.py:83), and their
    routes are `/v1/projects/{pid}/...` (http_service.py:10228-10415).
  - Both are kinds on one shared layer, `keyed_entries.AnchoredKind` (reference_records.py:626,
    capability_records.py:489): closed anchors, append-only revisions, accepted revisions bound to
    their content hash.
  - Both use a closed field set (`set(record) != set(ENTRY_FIELDS)`, reference_records.py:272,
    capability_records.py:208), so a new field is a new record version.
- **No interface or contract record exists.** The kit versions only its own CLI contract
  (docs/CLI_CONTRACT.md) and schema versions.

**What this note keeps:** the per-project check stays the only gate. A program adds records and
reads; it never adds a role, a grant or a path around `check_authority`.

## 2. A program (Q1 A): an ordinary project, linked on both sides

### 2.1 The program project

A program is **an ordinary Orchestra project**, the program project. It holds what belongs to the
program as a whole, each with the existing machinery:

| piece | how | from |
| --- | --- | --- |
| program requirements | requirement records under the program project's jobs | .401 §3 |
| the program's system design | `design.<scope>` in the program project, its index on the server | .401 §2.2, §5.1 |
| program decisions | decision issues in the program project | .401 §2.6, §6 here |
| the interface register | `interface-v1` entries in the program project | §4 |
| the areas | link records (§2.2) | new convention |

**The program coordinator** is the program project's coordinator: a person with the coordinator or
owner role, or an agent with a coordinator grant there. It has no rights in an area unless it is
also a member of that area, by the ordinary route.

### 2.2 The link: two records, one on each side

A link counts only when **both** sides have accepted their record:

| side | record | key | authority | who accepts |
| --- | --- | --- | --- | --- |
| program | reference entry | `program.area.<area>` | `decision`: the program owner's decision admitting the area | a person owner of the program project (or an operator) |
| area | reference entry | `program.member-of` | `decision`: the area owner's decision joining the program | a person owner of the area (or an operator) |

- **Keys:** `<area>` is the area's canonical project name (http_authority.py:349, which fits the
  key pattern). The statement names the other project's exact id.
- **Joining or leaving is an owner decision on each side.** It changes who a project answers to,
  so it is an owner-only choice (.401 §4.3). The `decision` authority must be a decision issue
  whose native author is a person owner of that project (.401's rule as approved). An agent
  coordinator may accept the record, but it cannot write the decision.
- **Link states:**
  - `linked`: both accepted;
  - `one-sided`: only one record accepted; shown, never counted as a member;
  - `left`: either record superseded by a revision whose statement says so.
- **What the program side reveals:** `program.area.<area>` is program data, readable by the
  program project's members. It names the area and nothing inside it. While the area has not
  accepted, the program view shows the link as one-sided, with no counts or contents.

**As built (kittrial-5bb.446, slice 1):**
- **Who accepts (Q1 A, amended above):** a person owner of each project, by their own web session, or an operator on
  the host. A coordinator, person or agent, proposes and revises. A native issue author is only an `--actor` label
  (TRACEABILITY_DESIGN.md:544-547), so the owner's act is the acceptance, whose evidence only the web service or an
  operator writes. It is checked live at every read: an owner who loses the role or whose account is disabled stops
  the side counting. The web accept and retire routes refuse anyone else for a `program.` key.
- **Follow-up, not in this slice:** letting an owner delegate the acceptance through a `delegation-v1` record, as
  record acceptance is delegated today (delegations.py).
- **The statement's lines:** exactly `Area: <area>` or `Program: <program>`, then `State: in` or `State: left`,
  with no second State line and nothing beyond (review N3). A
  `program.` key is written only as one of the two links, with these lines and a `decision` authority.
- **States:** `linked`, `one-sided`, `left` and `proposed`.
  - `left` is the program side saying so, or its record superseded.
  - An area that leaves makes the link `one-sided` from the program's view; an area learns one fact about the
    program, and the program one fact about an area: whether the other side counts.
  - `proposed` is a program side that does not count yet; no area is read for it.
- **The read:** `GET /v1/projects/{pid}/program-links`, 50 areas a page and so at most 50 area reads. The host's
  `ref links` lists one project's link records only.
- **The isolation harness:** tests/program_isolation.py. Later slices add their reads to it, and a test fails if
  any route whose pattern mentions "program" is missing from it.
- **Notes between coordinators (kittrial-5bb.447):** a link is also what lets the coordinators of the two
  projects (and of two areas of one program) write to each other. The link is checked live, the sender needs no
  right in the other project, and a note's text reaches only the coordinators of its two projects. The harness
  covers the note reads, and its route test now catches any route mentioning "program" or "notes".
  - **Revision 2 (review 01a18a03):** the sender, the reader and the one who marks a note noted must really
    coordinate the project: an owner or a coordinator, or their agent holding the grant. A web superuser who is
    only a viewer is refused like any viewer; "operators" means the host's operator list.
  - **The sender's own records first:** a target that A's own records do not name is refused, with the one 404,
    before B is read at all. A names B by an accepted side: as its area, as its program, or as an area its own
    program's accepted side lists.
  - **Siblings:** each area's own program is read, and they must be the same program.
  - **Wakes** name the sending project and "its coordinators", never a person or an agent.
  - **Refusals of A's own right** may stay 403 or 404 (review F1). Only the refusal of the address must be the
    one 404.

### 2.3 What being in a program grants: nothing

- **No new authority.** A link adds no role, no grant and no visibility. A program coordinator
  who is not a member of an area reads nothing in it, and an area coordinator who is not a member
  of the program project reads nothing there.
- **Membership stays ordinary.** People and agents who should work across levels are made members
  of each project they need, as today:
  - area coordinators usually join the program project as contributors, so they can file
    suggestions (§6.3) and read program requirements;
  - the program coordinator usually joins each area as a viewer, or as a coordinator where the
    owner delegates.
- **Agents.** An agent reaches a project only through its live grant list
  (http_authority.py:838-841). The grant list cannot include a project its owner is not a member
  of (http_auth.py:2926-2959).
- **Every cross-project read** (§3, §5) is the caller's own per-project read, done once per
  project, exactly as My work does. A read that spans N projects makes N checks, and each one can
  fail on its own.

**The test that keeps it honest** (slice 1): for each read this note adds, a caller who is a
member of the program project but not of an area gets the same answer as a caller with no
membership at all. That means no rows, no counts and no names beyond the program's own link
records. The same holds for an agent whose grant list omits the area, and for the host route
with a key that omits it.

## 3. Cross-project pointers (Q2 A)

### 3.1 The tokens

The traceability note's tokens gain a project qualifier, and `iface:` is new:

| token | example | resolves to |
| --- | --- | --- |
| `req:<project>/<key>@<rev>` | `req:platform/latency.order-ack@2` | a requirement revision in that project |
| `design:<project>/<scope>#<section>` | `design:platform/platform#interfaces` | a section of that project's selected design (its server-held index, .401 §5.1) |
| `decision:<project>/<id>` | `decision:platform/<id>` | a decision issue in that project |
| `iface:<project>/<key>@<rev>` | `iface:platform/iface.quotes.v2@3` | an interface entry in that project (§4) |

- **Unqualified tokens mean "this project"**, exactly as in .401.
- **Splitting is deterministic:** when the value contains `/`, the part before the first `/` is a
  project. Requirement keys are free text today (requirement_records.py:177-180). A key that
  contains `/` must therefore always be cited qualified, even in its own project. Slice 2 lints for
  it.
- **The project part** is the canonical project name. A project without one (a web-created
  `proj_…` id) is cited by its exact id.

### 3.2 Resolution under the caller's permissions

For each distinct target project in a read:
1. The server runs the same check a direct read of that project would run, for **this caller**:
   `check_authority(principal, project, CAP_READ)` on the web route, the key's project list on
   the host route.
2. **If it passes,** the token resolves as in .401, with .401's flags (`unknown-key`,
   `unknown-revision`, `superseded`, `draft`, `unknown-scope`, `unknown-section`,
   `not-selected`).
3. **If it fails,** the token reads **`not-visible`**. It carries no title, state or revision.

**`not-visible` is identical for "no such project" and "you may not read it"** (Q2 A), as the
404 already is. So a token never reveals whether a project exists.

**Counting:** trace counts `not-visible` apart from real breaks, so a coordinator sees "12
resolved, 1 broken, 3 not visible to you". A not-visible token is not called broken: someone
else may resolve it.

**Information flag `outside-program`:** the target project is readable but not linked to the
citing project's program. Citing across programs is allowed; the flag only makes it visible.

**Cost:** one warm export read per distinct target project, at most 50 per read (My work's
bound), with the same read cache. A token never triggers a read of a project the caller cannot
read: the check comes first.

**As built (kittrial-5bb.449, slice 2):**
- **The grammar:** `req:`, `design:` and a new `decision:` take `<project>/` (briefing.QUALIFIED_KINDS). The split
  is at the first `/`. `decision:` resolves to a native decision issue, with the flag `unknown-decision`.
- **Where it resolves:** in the trace read, in one endpoint call, with the caller's readable projects.
  - **On the web:** each project is checked with check_authority for this caller and passed as the service's own
    `--readable`. Any other caller passing it is refused.
  - **On the host:** the key's projects, or any project for an operator's unbound key.
  - A project outside that set is never read and reads exactly like one that does not exist.
  - At most 50 other projects are read; past that, `not-checked`.
- **`outside-program`** is set on the web route only, from the same link check that allows a note between
  coordinators.
- **The summary** has a pointers line: resolved, broken, not-visible, not-checked.
- **The lint:** the trace's `qualify-key` rows, and a warning on creating a key with `/`, never a refusal.
- **Briefs** name another project's token as `cross-project` and read nothing of it.
- **An accepted design document** containing a qualified `design:` token reads `differs` until it is accepted
  again; the release notes say so. So does one containing `req:/x`, `req:A/` or `req:A/@3`: an empty project or an
  empty key is prose now, where it was a token before. `decision:` tokens never enter a design index.

## 4. The interface register (Q3 A)

### 4.1 One entry per major version

An interface is a named, versioned contract between areas: a message feed, an API, a file format.
The register lives in the program project as entries of a new kind, **`interface-v1`**, built as
a third `keyed_entries.AnchoredKind` beside references and capabilities. It has the same anchors,
draft, accept and evidence flow, revisions and content hashes.

- **The key is `iface.<name>.v<major>`**, for example `iface.quotes.v2`. Each major version is
  its own entry, so two majors can be live at once with their own states.
- **A revision of the entry is a minor version.** Revision 3 of `iface.quotes.v2` is 2.3.
  Revisions are compatible changes only (§4.3).

**Fields** (a closed set, like the other kinds):

| field | meaning |
| --- | --- |
| `schema_version` | 1 |
| `key`, `revision` | `iface.<name>.v<major>`, the minor version |
| `name`, `meaning` | a short name; what it carries and for whom (at most 2,000 characters, untrusted text) |
| `owner_project` | the area that defines it (exact project id) |
| `owner` | the person or role answerable for it, as on reference entries |
| `schema` | `{type: repo-path, path, commit, anchor}` in the owner project's repository, or `{type: url, url, retrieved}`; plus `media_type` (e.g. `application/schema+json`, `application/vnd.oai.openapi`, `application/x-protobuf`, `text/markdown`) and `sha256` of the schema bytes |
| `compatibility` | `{rule: additive-within-major, change: additive | initial}` for this revision |
| `replaces` | for a new major: the key of the major it replaces, or null |
| `state` | `draft`, `accepted`, `deprecated`, `retired` |
| `review_by`, `tags` | as on reference entries |

### 4.2 Who provides and who consumes: on the area's capability records

Where the code is, the area declares what it implements. A capability record ("where a feature
lives") gains one field in a new record version:

```
interfaces: [ {iface: "iface:platform/iface.quotes.v2@3", role: "provides"},
              {iface: "iface:platform/iface.orders.v1@1", role: "consumes"} ]
```

- **The revision is the minimum** the area provides or needs: providing 2.3 provides 2.0 to 2.3,
  and consuming 2.1 needs a provider at 2.1 or later within v2.
- **It is a version of the capability kind, not a new kind.** The field set is closed, so it
  follows the kind's forward-compatibility rule.
- **Why on capabilities:** a capability is already the accepted record of where a feature lives,
  with code and test anchors and requirement links (capability_records.py:173-185). The
  provider's code anchor is what a reviewer checks.

### 4.3 The rules for a new version and for retiring one

- **A compatible change** is a revision of the same entry with `compatibility.change: additive`.
  - The owner project's coordinator proposes it.
  - The program project's coordinator accepts it, as any draft (.401 G1 applies to an agent
    acceptor).
  - The accept step refuses a revision that declares anything but `additive`.
- **A breaking change** is a new entry `iface.<name>.v<major+1>` with `replaces` set.
  - When it is accepted, the old major becomes `deprecated` (a revision whose only change is the
    state). It stays usable, and the program view lists its consumers.
- **Retiring** a major (`retired`) is accepted only when no area consumes it. Since the acceptor
  may not see every area, the accept step reads consumers **as the acceptor** and refuses unless
  its view is complete:
  - every linked area was readable;
  - and none of them consumes the major.

  Otherwise it answers "3 areas not visible to you; 1 still consumes". The alternative is a
  program decision (§6) that retires it anyway, cited as the revision's evidence. This never
  widens the acceptor's access: it refuses instead.
- **Undeclared breaking changes.** The kit cannot judge whether a schema change is compatible.
  Where the code is, a check can (as trace `--verify` does for designs). Such a check compares the
  pinned schema at the previous and new revision for the media types it understands (JSON Schema,
  OpenAPI) and reports the result. Until a trusted verifier reports, a revision reads
  `compatibility unverified`.

### 4.4 What the register lets the program view check

| flag | condition |
| --- | --- |
| `no-provider` | a consumed `iface@rev` has no accepted provider at that revision or later in a readable area |
| `consumes-retired` | a consumer pins a retired major |
| `consumes-deprecated` | a consumer pins a deprecated major (a warning, with the replacement named) |
| `provider-behind` | the newest accepted revision has no provider, while consumers pin it |
| `breaking-without-version` | a trusted verifier reports a breaking difference inside one major |
| `compatibility-unverified` | no verifier has reported on the newest revision |
| `orphan` | an accepted interface with neither provider nor consumer |

Each flag is computed only over the areas the caller can read, with a coverage line ("read 4 of 5
linked areas").

### 4.5 Runtime registration: out of scope

Which processes run, where, and which interface versions they serve is runtime state. This note
does not design it, and the kit does not record it.

### 4.6 Not blocking a later runtime reader (the second addition)

The owner has not decided whether the register should drive runtime registration later. Nothing in
`interface-v1` prevents it:
- **A stable identity:** the key `iface.<name>.v<major>` never changes meaning. A version is the
  pair (key, revision), and a major's replacement is explicit (`replaces`).
- **Machine-readable fields:** the state is an enum, the compatibility rule is an enum, and the
  schema pointer carries `media_type` and `sha256`, so a reader can fetch and check the schema
  without parsing prose. `meaning` is the only free text, and no rule reads it.
- **One read:** `GET /v1/projects/{pid}/interfaces` returns the register as JSON with
  `schema_version`, under CAP_READ like every record catalog. A runtime registry would read it
  with its own credential, under the same rules.
- **No runtime fields.** A runtime reader would keep its own records and point at
  `iface:<project>/<key>@<rev>`. The register never points back.

## 5. The program view and coverage

A read on the server, `GET /v1/projects/{program}/program`, needs CAP_READ in the program project.
The client's `trace --program` is a convenience over it. A **Program page** in the program
project's menu shows it on the web. For each linked area it does the per-project check of §3.2
for the caller, then reports:

| section | rows |
| --- | --- |
| areas | each `program.area.*` entry: link state (`linked`, `one-sided`, `left`); if readable, its coordinators (`coordinators_of`, http_auth.py:3390-3426), open tasks, lanes waiting long; if not, "not visible to you" |
| decisions | program decisions that bind areas (§6), each with its adoption state per area: `adopted`, `not-adopted`, `adoption-invalid`, `not-visible` |
| suggestions | open suggestions from areas (§6.3), oldest first |
| interfaces | the register with the §4.4 flags |
| requirements | program requirements with no area design section or task citing them (`req:<program>/…` tokens in readable areas), and area designs citing superseded program revisions |

- **Bounds:** pages of 50 rows; at most 50 areas per read, as My work.
- **The coverage block** says what was read: "4 of 5 linked areas readable; 3,120 rows".
- **Permission comes before counts:** an unreadable area adds one line and no numbers.

**As built (kittrial-5bb.453, slice 4):**
- **One module, one action:** `program_view.py` builds the answer; the endpoint action `program` reads it, one call a
  page. The web route `GET /v1/projects/{program}/program` and the client's `trace --program` both use it.
- **The readable set** is .449's rule: the web service's own `--readable` (each project checked for this caller),
  honoured only from its launch; on the host the key's projects, or any for an operator's unbound key. Every other
  project's path is resolved by `execute`, after the gate.
- **Areas:** each `program.area.*` entry with its link state, the same as the program-links read gives (.446's one
  fact). For a readable area: open tasks, lanes `waiting_too_long` and `expect_by_passed` (from `lanes.read`,
  `idle_minutes` passed through as on the Lanes page) and, on the web, its coordinators. Otherwise "not visible to
  you": a missing area gives the same row.
  - **Open tasks:** open, in progress or blocked, not an epic, not a requirement record (`requirement`,
    `brd-section`), and nothing the queue never lists as work (event and gate rows, the merge slot, record anchors).
  - **The program's side first** (review of .453, B1): an area whose program side is not in (proposed, left, not
    accepted by an owner) is never read, by any caller, so a proposed entry tells nothing about whether a project of
    that name exists. Only an area whose program side is in is read for the one fact, and only a readable one for
    numbers.
  - **A key bound to projects** reads nothing of a project it is not bound to (rule 1 of
    COORDINATORS_PER_PROJECT_DESIGN), so there an area outside the key reads `not-checked`, with no read at all.
  - **A right lost after the read:** the web route asks `coordinators_of`, which checks the caller again; the row
    becomes the hidden one, and the page is read again without that area, so none of its citations stay in the
    requirements section (review of .453, B2). After three reads that each lose a right, the requirements section
    is `unsettled`, with no row.
- **Interfaces** read `not-available` until slice 3 (kittrial-5bb.450) is merged forward. **Decisions** and
  **suggestions** are slice 5's (kittrial-5bb.454, as built under section 6).
- **Requirements:** each accepted program requirement that no readable area of this read cites (`no-area-cites`),
  and each area design section pinning a superseded program revision (`cites-superseded-revision`), 50 rows a page.
  The coverage says they are counted over the readable areas of this read only.
- **Coordinators** are on the web route only (Q2): the host route says "Coordinators are shown on the web Program
  page."
- **The menu:** "Program" shows only on a project with an accepted `program.area.*` side, from a flag kept in the
  web state (`program_flags`). Every program read recomputes it, and so does a web accept or retire of a
  `program.` key; no menu render reads anything. The page itself always reads the view.
- **The harness** covers the read twice: as an area read (an outsider's view of an area is a missing project's
  404) and as the program's own read (`assert_area_rows_hidden`).

## 6. Decisions across levels (Q4 A)

### 6.1 A program decision that binds areas

A program decision is a decision issue in the program project, with fixed lines in its body:
- `Binds: <area>, <area>` names the areas it applies to;
- `Owner-only: yes` or `no`. It is `yes` when the choice is the owner's under .401 §4.3:
  requirements, cost or scope bounds, or what only the owner can know.

**Who may write it** (revision 2 of .454 replaced "native author" with an attestation; see the as-built below): an
owner-only program decision counts only if its native author is a person
owner of the program project or a deployment operator. This is .401's rule as approved: an agent
never writes an owner's decision. Other program decisions may be written by the program
coordinator.

**It binds an area only when the area adopts it.** Being in the program binds nothing, just as it
grants nothing.

### 6.2 Adoption in the area, and who may adopt

The area records adoption as a decision issue in the area project whose body carries
`Adopts: decision:<program>/<id>`. The server checks it live when trace or the program view reads
it:

| program decision | a valid adoption is written by |
| --- | --- |
| `Owner-only: no` | the area's coordinator: a person with the role, or an agent with the area's coordinator grant |
| `Owner-only: yes` | **a person owner of the area** (the native author), **or** an area coordinator whose adoption also cites `Per: decision:<area>/<id>`, where that decision's native author is a person owner of the area. **Never an agent alone.** (The first addition.) |

An adoption that fails its row counts as not adopted, with the flag `adoption-invalid` and the
reason. Examples: an agent adopting an owner-only decision with no owner decision cited, or a
`Per:` decision written by an agent.

**The flags:**
- `not-adopted`: a bound area has no valid adoption;
- `adoption-invalid`: as above;
- `superseded-upstream`: the program decision was superseded (`Supersedes:`, .401 §2.6) while an
  area's adoption still cites the old one.

### 6.3 Area suggestions going up

An area coordinator raises a suggestion as a task in the program project, labelled `suggestion`.
It cites its source with a qualified token (`From: decision:<area>/<id>`, or a task in the area).
This needs the coordinator to be a contributor in the program project. That is ordinary
membership (§2.3), and it is the only way up: the program never reads an area's comments looking
for suggestions.

The program coordinator answers with a program decision, or by closing the task with a reason. The
program view lists the open suggestions.

### 6.4 Owner-only choices stay with a person owner

At every level:
- The program owner decides program owner-only choices.
- An area owner adopts owner-only decisions in their area, directly or through their own decision
  that an area coordinator cites.
- No coordinator agent, in the program or in an area, can make or adopt an owner-only decision.

This is .401's grant rule (a granting decision counts only if a person owner or operator wrote
it), applied across levels.

**As built (kittrial-5bb.454, slice 5, revision 2):**
- **Where this differs from §6.1 and §6.2:** they say "native author". That does not hold. A row's `created_by` says
  who made the row, not who wrote the text now in it: a contributor's edit keeps it, and over the host route the name
  is only an `--actor` label. A row counts only with an attestation. Wherever §6 says who "writes" a row, read who
  attests its exact text.
- **The modules:** `decision_attest.py` is the record and how a row counts; `program_decisions.py` reads the lines and
  applies §6. The program view (endpoint action `program`, web route, `trace --program`) and the trace's decisions view
  use them alike.
- **The lines:** `Binds: <area>, <area>`, `Owner-only: yes|no`, `Adopts: decision:<program>/<id>@<attestation id>`,
  `Per: decision:<area>/<id>` and `Authorises: decision:<program>/<id>@<attestation id>`, each a whole line anywhere in the description,
  at most once. A repeat or a line that does not read makes the row `malformed-lines`: it binds and adopts nothing. A
  binding decision has both a `Binds:` and an `Owner-only:` line. An event or gate row is never a decision.
- **The record:** `Kind: decision-attest-v1`, a reserved comment prefix no raw comment can write, honoured only with its
  journal entry `<project>/.decision-attests/<sha256>.json` (in backups, validated on restore). It names the project,
  the decision, `via` (`session`, `agent` or `operator`), the author, the time, a server-random nonce and a digest of the
  row's exact type, title and description. Status and labels are outside the digest.
- **Who writes it:** `POST /v1/projects/{id}/decisions/{did}/attest`, for the author's own web session or agent with
  owner or coordinator standing in that project now; a contributor, a viewer, a worker credential or an unbound key is
  refused. Or an operator with `admin.py decision-attest`. The body names the digest the author read
  (`GET .../attestation`), and a text that changed since is refused with 409. A text attested at an owner's level (see
  the gate) is attested again only by a person owner or an operator. So is a row with an attestation comment no journal
  honours: its journal entry is gone, or the project was restored. What it said cannot be known and a contributor can have
  edited the text, so a coordinator's attestation could otherwise lower an owner's row. An owner or an operator always can
  recover it, and that record keeps the owner's level. The comment stays, so a coordinator does not attest that row again:
  a coordinator's later choice is a new decision.
- **How a row reads, at every read, in this order:** `closed`; `not-attested`; `attestation-stale`, when the newest
  record's digest is not the row's now (the newest, not any: going back to replaced text stays stale);
  `attester-not-authorised`, when the author's standing has gone. Each of these binds nothing, and the row is listed so
  an owner can see what needs attesting. Otherwise it counts, as the highest live standing among the unbroken run of
  newest records over this text, so a coordinator attesting the same words cannot lower an owner's attestation.
- **Authority is decided live at every read** from the web state: a person by their role and an enabled account; an
  agent only while it is enabled, holds the project, its account is enabled and is itself owner or coordinator there (a
  superuser who is only a viewer does not count), and its grant is in force. An operator's record counts while its exact
  actor is on the allowlist. A bare name counts for nothing.
- **A program decision counts** when an owner-only one was attested by a person owner of the program or an operator
  (`owner-only-not-by-owner` otherwise), and another by a coordinator, an owner or an operator (anyone else reads
  `attester-not-authorised`). A coordinator agent's attestation counts as coordinator, never owner.
- **An adoption**, per bound area: `adopted`, `not-adopted`, `adoption-invalid` (with the reason), `adoption-stale` (below), what the row reads as
  when it does not count (`closed` when only closed adoptions remain, `not-attested`, `attestation-stale`,
  `attester-not-authorised`), `superseded-upstream` (a counting adoption of a decision this one supersedes, and none of
  this one), `not-visible` (not readable or no such project: the same entry, nothing else), `not-in-program` (the
  program's side is not in: never read) or `not-checked` (off this page of areas, or outside a bound key).
- **`Per:` has to relate:** a coordinator's or agent's adoption of an owner-only decision counts only when `Per:` names a
  decision of the area that counts as a person owner's or an operator's and whose `Authorises:` line is exactly the
  adoption's `Adopts:` pointer. An unrelated owner decision does not satisfy it. An agent never satisfies an owner-only
  rule on its own.
- **Superseded:** only a counting row marks another superseded, and a chain does not pass through a row that does not
  count. A row that supersedes owner-level text needs owner-level attestation: from a coordinator or an agent it reads
  `supersedes-owner-level`, marks nothing and binds nothing. The level is the same one the gate uses.
- **The gate on status, type and labels:** on a row with an honoured attestation, and on any decision row carrying an
  attestation comment, honoured or not, or a `Binds:`, `Owner-only:`, `Adopts: decision:`, `Per: decision:` or
  `Authorises: decision:` line, a status change (close, reopen, or any
  flag that moves status), or a type or label change that would start or stop the row being a decision, needs standing.
  An owner's level (a person owner or an operator) for a text that says `Owner-only: yes`, that carries `Authorises:`, or
  an adoption a person owner or an operator attested; otherwise an owner or a coordinator, agents by the grant rule. Where
  there is a record that level comes from the record, not from the text now. An attestation comment no journal honours
  needs an owner, beside any record that is honoured, because its text can have been edited. Prose such as `Per: the meeting of Tuesday` is not one of these
  lines, and an ordinary decision carrying it closes as it always did. The web refuses anyone else with 403. Every host route refuses
  everyone, an operator included; an operator uses `admin.py decision-change`. A row moving from `in_progress` to
  `open` through task assign or release does not reach this gate, and that changes nothing that binds. A decision row
  with none of these lines and no attestation is not gated, and a task with none of them may be retyped: it reads
  `not-attested`. A status write uses the row the status guard already read, and a label other than `decision` reads
  nothing for this gate, so an ordinary write stays one read. Only a type change or a `decision` label change costs one more.
- **Backup and restore:** the journal is in the backup. `restore-new` always makes a new name, as it refuses the same one,
  and a record binds its project's name: a new name is a new project. So its rows read `not-attested`, on purpose, their
  gate still stands, and adoptions elsewhere that pin the old name are not read as this one. A person owner of the new
  project attests them again.
- **What a contributor can still do:** edit the text. That makes the row `attestation-stale`, which binds nothing and is
  visible, so it can only remove a binding, never create one. It is not refused.
- **An adoption pins the text it saw:** the pin on an `Adopts:` or `Authorises:` line is the program decision's attestation
  id, and `GET /v1/projects/{program}/decisions/{id}/attestation` gives the line to copy. The id is the sha256 of the first
  record of the unbroken run of newest records over this text. When the program decision is attested with other text, a
  new run starts and the adoption reads `adoption-stale`, binding nothing until the area adopts again; an `Authorises:`
  line that names this decision but pinned another attestation makes the `Per:` adoption read the same. Another attester
  of the same words does not change the id.
  - **At write:** a row carrying either line is attested only by someone who can read that decision now, decided live for
    that project, and only with the id it has now while it counts. Otherwise nothing is written: 403 with the same
    words for an unreadable program, no such project and no such decision, and 409 to a reader for other text or a
    decision that does not count. An area owner who may not read the program cannot adopt; they ask for read of it. An
    operator on `admin.py` reads any project.
  - **Later:** an adopter who loses read of the program keeps the adoption. It was read at write, and it goes stale if the
    text changes.
  - **What it discloses:** nothing about the text. The id is a hash over the record's random nonce, so a reader of the area
    cannot derive it from the text or confirm a guess of it.
- **The boundary:** anyone with a shell on the host can forge bd rows with `--actor` or `BEADS_ACTOR`, and can write the
  journal. Only the attestation journal and the live checks defend, and only against the routes the kit exposes. An
  HTTP-shaped actor and its look-alikes are still refused on every host route (`reserved_comments.refuse_http_actor`), so
  no host route writes a web identity's name.
- **Suggestions:** the program's open tasks labelled `suggestion`, oldest first, 50 a page, with their `From:` line
  as written; no area is read for them.
- **The trace:** an area's decisions view marks each `Adopts:` decision with the same state when the caller may read
  the program (.449's loader), and `not-visible` otherwise.

## 7. What is reused from .401, and what is new

| need | reused | new | why new |
| --- | --- | --- | --- |
| a program, its requirements, design and decisions | an ordinary project; .401's chain unchanged | — | — |
| membership of a program | reference entries with `decision` authority, both sides | key conventions `program.area.<area>`, `program.member-of` | — |
| cross-project tokens | .401's `req:`/`design:` grammar and flags, the context index's resolver | a project qualifier; the `not-visible` and `outside-program` flags | the grammar is closed by design, so a qualifier is a parser change |
| interfaces | the `keyed_entries` layer (anchors, revisions, acceptance evidence), the record routes | **`interface-v1`**, a new record kind | reference entries have no `deprecated` or `retired` states, no compatibility rule and no schema hash, and a rule written in a statement cannot be checked mechanically (Q3) |
| providers and consumers | capability records (where a feature lives, with code anchors) | an `interfaces` field: a new capability record **version**, not a kind | the field set is closed (capability_records.py:208) |
| the interface pointer | the context index's token rules | `iface:` | a new target kind |
| program view | .401's trace read and its bounds; My work's per-project loop | `GET …/program`, a Program page | a read across linked projects |
| decisions across levels | decision issues; .401's supersession line and grant rule | the `Binds:`, `Owner-only:`, `Adopts:`, `Per:` lines | conventions in text, checked on read: no new kind |
| suggestions | tasks | a `suggestion` label | — |

**One new record kind (`interface-v1`) and one new record version (capability).** Everything else
is conventions over records that exist.

## 8. Risks, and the test for each

| risk | mitigation | test |
| --- | --- | --- |
| a program member reads an area by accident | no grant from a link; every read is the caller's per-project check | program member, non-member, agent without the grant, host key without the area: identical answers, no rows, no counts (slice 1) |
| a token reveals that a private project exists | `not-visible` for both cases, as the 404 | tokens to a missing project and to an unreadable one give byte-identical rows |
| a one-sided link is taken as membership | `linked` needs both accepted records | accept only the program side: the area is `one-sided`, absent from counts |
| an agent adopts an owner-only decision | the live attestation and `Authorises:` checks of the as-built under §6 | agent adoption alone: `adoption-invalid`; with a `Per:` decision an agent attested: invalid; with one a person owner attested that authorises another decision: invalid; with one that authorises this one: valid |
| a breaking change slips into a minor revision | accept refuses non-additive revisions; the verifier check; `compatibility-unverified` | a revision declaring `breaking` is refused; a verifier's breaking report flags `breaking-without-version` |
| a major is retired while still consumed | retirement needs a complete view and no consumer, or a program decision | an acceptor missing one area is refused with "1 area not visible to you" |
| cross-project reads cost too much | one warm export per distinct project, at most 50; check first | a trace citing 3 projects makes 3 reads; a token to an unreadable project makes none |
| untrusted text from another project | it is shown as text (the context index's escaping) and never put into prompts; pointers only | an interface `meaning` with HTML and bidi controls renders escaped |

**The program-links read's timing (review of kittrial-5bb.446, N1).** The read answers alike for an admitted
area that does not exist and for one that exists but has not joined: both read "does not count". Its time does not.
The service's read of a missing project fails at once (about 1.6 s measured for the whole read in the review), while
an existing one reads its records (about 2.5 s). So a program member can tell, from the time alone, whether a project
with an admitted area's name exists on this installation. This is accepted, not padded:
- It concerns only names that the program's own owner wrote into accepted `program.area.<area>` records, and only
  readers of the program project.
- It says nothing about what is inside the area: the answer itself carries one fact, the same for every reader.
- The same difference exists wherever a read touches a project's records only when the project exists.
- Padding every area read to the slower path would cost every reader up to 50 slow reads a page.

If an installation needs the names hidden, the read can pad each area to a fixed floor.

**Notes between coordinators: timing (review of kittrial-5bb.447, B3).** A note's address is checked from the
sender's own records first. A target the sending project does not itself name by an accepted side (as its area, as
its program, or as an area its program's accepted side lists) is refused before the target's records are read. So any project the sender has not named
costs the same as one that does not exist. Only projects the sender's own owner-accepted records already name are
read, and they are the projects it may write to. That is one change in the
web service's read (`program_links_read`), with no change to the records.

## 9. Slices (five, each deliverable alone)

1. **Program links, the "nothing granted" rule, and the conventions.**
   - The two link records and their states, read by a small program-links read in the program
     project.
   - The adversarial isolation tests of §2.3 and §8: they run on every read this note will add,
     and slice 1 adds the harness.
   - Docs: WORKER_GUIDE and the coordinator's AGENT.md (how to set a program up, who joins
     what); the owner-decision rule for joining.
   - No dependency.
2. **Project-qualified pointers.**
   - `req:`, `design:` and `decision:` with a project, `not-visible` and `outside-program`, the
     `/` lint.
   - Depends on .401 slice 2, the unqualified kinds.
3. **The interface register.**
   - The `interface-v1` kind on `keyed_entries`; the capability `interfaces` field (a new record
     version); `iface:` tokens.
   - The accept rules of §4.3, including the complete-view retirement check; `GET …/interfaces`.
   - The verifier hook for JSON Schema and OpenAPI.
   - Depends on slice 2 for qualified tokens.
4. **The program view and the Program page.**
   - `GET …/program`: areas, interfaces with the §4.4 flags, program requirements' coverage;
     bounds and the coverage block. The client `trace --program`.
   - The decision and suggestion sections are slice 5's (kittrial-5bb.454).
   - Depends on slices 1-3, and on .401 slice 4's trace read.
5. **Decisions across levels.**
   - The `Binds:`, `Owner-only:`, `Adopts:` and `Per:` lines, checked live, with their flags.
   - The `suggestion` label and its section of the view; the docs for program and area owners.
   - Depends on slice 4, and on .401 slice 1's live author check.

**Each slice stands alone:**
- Slice 1 lets a program be declared and read safely before anything resolves across it.
- Slice 2 helps any two projects that cite each other, program or not.
- Slice 3's register is useful as a catalog even before the view.
- Slice 4 shows whatever slices 1-3 recorded.
- Slice 5 adds the decision columns.
