# Capability index - design proposal

Status: **proposal, revision 1. Not implemented, not accepted.** This document
changes no code. It proposes a `capability` record kind on the shared keyed-record
core, an authority split, a recorded drift check, aliases, lookup and the slices to
build them. The project owner accepts it together with the reference catalog
(kittrial-5bb.41) and requirements gathering (kittrial-5bb.58).

- **Base:** `main` `8e1f9ec6f4d818de8983f59f80aa896111a99712`.
- **Written against:**
  - the reference catalog design, `docs/REFERENCE_CATALOG_DESIGN.md` at `d32d400` ("revision 3"), cited as **.41 §x**;
  - the requirements-gathering design, `docs/REQUIREMENTS_GATHERING_DESIGN.md` at `057c7fa` (rev 2), cited as **.58 §x**.
- **Builds on:** the read-only lookup delivered separately as kittrial-5bb.61
  (`capabilities.py`, cited as **.61**). That lookup ships on its own and does not
  wait for this design.

## 1. The ask

The owner asked for:

1. **A capability record** with these fields: id, name, aliases, summary, requirement
   IDs, design anchors, code pointers (`file::symbol`), tests, owner, status and the
   `verified_at` commit. It is to be stored in Beads "the way requirement records are
   stored".
2. **`lookup "<phrase>"`**, matching names and aliases and returning JSON. A miss
   returns the nearest candidates and a hint to propose an alias.
3. **A `propose-alias` command.**
4. **A drift check** that confirms code pointers still resolve, using a stdlib ast
   indexer, or graphify's `graph.json` when one exists.

The owner's stated intent is: "leverage graphify to help map out the code but also
have this lookup to allow agents to find things easily and navigate the code base.
If they don't find what they want they should update the index … an efficient way for
agents to get context … helps people understand the project and ideally is a self
documenting system."

.61 already answers "where is this code?" from the checkout alone. This design adds
what code alone cannot say:
- what a capability is for;
- which requirements and design text it serves;
- who owns it;
- whether its pointers were still true at a known commit.

It also adds a cheap way for agents to improve the index when a lookup misses.

## 2. Design at a glance

- **A record kind, not a module.** `capability` is one more `RecordSpec` on the
  keyed-record core that .41 slice 1 extracts from `requirement_records.py`
  (.41 §12.1). It uses the same closed anchor, reserved labels, append-only revision
  ledger, receipts, reconcile and F3 acceptance.
- **Meaning is accepted; location is verified.**
  - An operator accepts the name, summary, requirement links, design anchors and
    owner, with F3 evidence.
  - Code pointers and tests are checked by machine against a checkout. The result is
    a separate recorded **verification**, which needs no human acceptance.
- **The check runs where the code is.** The server never reads a repository
  (.41 §3.6). `capability check --repo` runs client-side on the .61 resolver.
  - With `--record`, it posts a reserved verification record through the endpoint.
  - `refresh` only renders.
  - This also closes the gap .41 leaves open: .41 §6.6 never says how a
    client-side result reaches a read on the server.
- **Aliases are cheap and attributed.**
  - Any contributor or agent can propose one, and it helps lookup at once, marked
    `proposed`.
  - The coordinator folds aliases into the next accepted revision, or rejects them.
  - Once .58's queue exists, alias proposals become one of its kinds.
- **One lookup.** The client merges matching records from the endpoint with the .61
  code index. It resolves each record's pointers live against the caller's own
  checkout.

## 3. The record

### 3.1 Storage: the `capability` RecordSpec

These values follow the .41 §3.2–§3.7 pattern exactly:

| RecordSpec item | Value |
| --- | --- |
| anchor | one `task` issue per capability, closed by `capability propose` before its first revision (.41 §3.2) |
| type label | `capability` |
| state labels | `capability:draft`, `capability:accepted`, `capability:superseded` |
| lookup label | `capability-key:<key with . as ->` |
| idempotency labels | `request:<identity>`, `request-content:<digest>` |
| revision prefix | `Kind: capability-entry-v1\n` |
| acceptance prefix | `Kind: capability-acceptance-v1\n` (F3 evidence, .41 §4) |
| verification prefix | `Kind: capability-verification-v1\n` (section 5; new) |
| alias prefix | `Kind: capability-alias-v1\n` (section 6; new) |
| journal | `.capability-requests/`, with the frozen receipt schema (.41 §10.3) |
| key | the .41 §3.3 regex, e.g. `review.structured-contribution`; immutable and unique; a slug collision is refused at propose time |
| retire | `capability retire KEY --successor KEY2`, operator-only (.41 §3.5) |

### 3.2 Revision fields: a closed set, `-v1`

| Owner's field | Record field | Truth |
| --- | --- | --- |
| id | native id plus `key` | fixed at propose |
| name | `name`, at most 120 characters | accepted |
| aliases | `aliases`, the accepted list; proposed aliases live in alias records (section 6) | accepted / proposed |
| summary | `summary`, untrusted text, at most 1200 characters | accepted |
| requirement IDs | `requirements`: `[{key, revision?}]`, checked at write against a requirement record, like .58's `target.requirement-key` | accepted |
| design anchors | `anchors`: `file.md#anchor` pointers | accepted; resolvability is checked |
| code pointers | `code`: `file::Qualified.name` pointers | machine-verified |
| tests | `tests`: code pointers or test files | machine-verified |
| owner | `owner`, as `account:<uid>` or `person:<name>`; `session-<uuid>` is refused (.41 §4) | accepted |
| status | **not stored**: derived as `state` plus `verification` (below) | derived |
| verified_at commit | **not stored in the revision**: taken from the newest passing verification record | recorded check |

The remaining fields are:
- `schema_version`, `key` and `revision`;
- `tags`, at most 8, used for brief selection as in .41 §7;
- `acceptance_state`, written by the operation and never by the caller;
- `successor` and `sha256`.

Pointers use .61's syntax and safety rules: repo-relative POSIX paths, no `..`, no
absolute path, drive or backslash, and at most 400 characters. There are at most 32
`code`, 32 `tests`, 16 `anchors` and 16 `requirements` entries.

`status` is derived rather than stored, because a stored `status` would collide with
the native closed status and with `acceptance_state`, and would go stale the moment
the code moved. Reads therefore report two things:
- **`state`:** `draft`, `accepted` or `superseded`, plus `malformed` or `unsupported`
  for a bad record (.41 §3.7).
- **`verification`:**
  - `verified`: the newest verification for this revision passed;
  - `drifted`: it failed;
  - `unverified`: there is none;
  - `superseded-revision`: a verification exists only for an older revision.

## 4. Authority: meaning is accepted, location is verified

| Action | Who | Route |
| --- | --- | --- |
| propose or revise a draft | any contributor, including agents | `capability propose` / `capability revise --file` (endpoint, under the project lock) |
| accept or demote | allowlisted operator only, with F3 `decision_id` and `evidence` | `admin.py capability-apply`, as .41's `reference-apply` (.41 §4) |
| record a verification | any contributor, attributed | `capability check --record` → endpoint `capability verify` |
| propose an alias | any contributor, including agents | `capability propose-alias` |
| fold or reject an alias | allowlisted operator | the next accepted revision, or `capability alias-reject` |
| retire | allowlisted operator | `capability retire` |

- **Revisions:** a revision carries the exact next `revision` and `expected_sha256`
  (compare-and-swap, .41 §3.5). This is a new core option. It must leave requirement
  bytes unchanged (.41 §12.1 byte test).
- **Batch acceptance:** `capability-apply` accepts a list of `{key, revision,
  record_sha256}` under **one** owner decision. It writes one acceptance record per
  capability, each citing the same `decision_id`. This keeps F3 per record without
  asking the owner for a decision issue per capability. The acceptance record shape
  is unchanged.
- **Verification is never acceptance:**
  - A passing check does not accept a draft.
  - A failing check does not demote an accepted revision. It makes that revision read
    `drifted` and raises attention for its owner and the operators (section 8).
- **Agents never accept.** Session actors are never owners or approvers (.41 §4).

## 5. Drift check: runs where the code is, recorded where people read

```sh
b capability check --repo . [--key KEY ...] [--record]
```

1. **Fetch records.** The client pages through `capability list --json` through the
   endpoint: accepted and draft records.
2. **Resolve pointers.** It resolves every `code`, `tests` and `anchors` pointer with
   .61's `resolve`:
   - `ast` for Python files;
   - Markdown headings for anchors;
   - graphify's `graph.json` for other languages when present, reporting a stale
     graph as .61 does.
3. **Report.** The output is `capability-check-v1` JSON: per capability, per pointer
   `resolved`, `basis` and `reason`, plus a summary.
   - Nothing is written without `--record`.
   - A missing pointer is a result, not an error (`cli-contract-v1`).
4. **Record, with `--record`.** The client first refuses a checkout with uncommitted
   changes, or one without a full `HEAD` commit. It then posts one verification per
   capability through the endpoint action `capability verify`.
   - **Validation:** the endpoint validates the closed shape and binds the
     verification to the revision's exact `record_sha256`.
   - **Idempotency:** a verification is idempotent on `(key, revision, commit)`.

`Kind: capability-verification-v1` fields: `schema_version`, `key`, `revision`,
`record_sha256`, `commit` (40 or 64 hex), `checked_at`, `source` (`ast` or
`graphify`), `graph_built_at_commit` (or null), `tool` (`{name, version}`), `results`
(`[{pointer, resolved, reason}]`), `passed` and `sha256`. The native comment author
is the verifier. In this trusted-team model that is attribution, not authentication
(README "Scope of the pilot"). Any reader can re-run the check, and client lookups resolve live
anyway (section 7).

`verified_at` for a revision is `{commit, checked_at, verifier}` from its newest
passing verification. The server cannot judge commit ancestry, so reads show the
commit and its age and leave that judgement to the reader.

`refresh` renders a people-facing `views/CAPABILITIES.md`. It lists each capability's
name, summary excerpt, owner, requirement links, anchors, pointers, state and
`verified_at`. It is a projection like `CURRENT.md`: it never runs a check. It also
does not appear in any task view (section 8).

.41's `ref check --repo` has the same unrecorded-result gap. It can adopt this
verification shape through the core (a `verification_prefix` on `RecordSpec`),
optionally, without changing .41's slices.

## 6. Aliases and the self-documenting loop

```sh
b capability propose-alias KEY "reserved label guard" [--evidence POINTER]
```

- **Record:** this writes a `Kind: capability-alias-v1` record on the capability's
  anchor. Its fields are `key`, `alias`, `normalized` (.61's normalisation),
  `action: "propose"`, `evidence` (an optional pointer where the proposer found the
  code), `at` and `sha256`.
- **Effect:** lookup uses it immediately, marked `alias_state: "proposed"`.
- **Collisions** are refused at write. A normalised alias may not equal any other
  capability's key, name or accepted alias.
  - A repeated identical proposal is idempotent.
  - The same phrase proposed for two capabilities makes both **candidates**, never an
    exact match.
- **Folding:** an operator puts the alias into the next accepted revision's `aliases`.
- **Rejecting:** an operator writes `action: "reject"` with a reason, using
  `capability alias-reject`. Lookup then ignores the proposal.
- **After .58 slice 3:** the interim path above becomes a queue kind. Alias and
  capability proposals appear as `kind: alias` and `kind: capability` items, where
  "incorporate" means folding into an accepted revision.
  - The records stay the same; .58 treats the queue as "a read view" (.58 §5.1).
  - This design adds no second disposition vocabulary.

**The loop** that makes the index self-documenting:

1. **Look up.** An agent runs `capability lookup`.
2. **On a miss,** it follows .61's candidates or searches by hand.
3. **Record what it found:**
   - **If a capability exists:** `propose-alias` with the phrase that missed.
   - **If none exists:** `capability propose` a draft with the pointers it found.
4. **Verify.** `capability check --record` machine-verifies the new pointers.
5. **Accept.** The coordinator accepts the meaning in batches.

The worker guide and prompt text that .61 adds ("note misses in your checkpoint")
become these commands in slice 1a.

## 7. Lookup

The endpoint gains two actions: `capability find PHRASE` and `capability get KEY`.
- **`find`** is bounded, reads records only, and uses .61's normalisation and
  scoring over key, name, accepted aliases and proposed aliases.
- **`get`** returns a single record by key.

The client's `capability lookup`:
- **Without config:** keeps its .61 behaviour exactly.
- **With `--config` and `--project`:**
  1. calls `find` once;
  2. resolves each returned record's pointers **live** against the caller's checkout
     (`live: resolved | missing | unknown`);
  3. falls back to the .61 code-index candidates.

The output stays `capability-lookup-v1`, with an additive `records` array; additive
fields keep `cli-contract-v1`.

Record text is returned only as excerpt objects with a `trust` marker:
- `accepted` or `draft`, using .41's vocabulary;
- `proposed` for aliases.

Prompts carry keys and tokens only, never summaries (.41 §8, .58 §8.7).
`COORDINATOR_PROMPT.md` gets .58's standing rule, extended to capability summaries
and aliases.

## 8. Hidden surfaces, attention, HTTP

- **Hidden surfaces:** the `capability` label and `Kind: capability-` comments join
  .41's filter on the nine surfaces (.41 §6.0). A capability anchor is never a task
  in `work`, the queue, My work, prompts, `render.py` task pages, `/tasks/{id}`,
  `/history` or `/brief`.
- **`work`:** gains `attention.capability_index`, in the `_agent_attention` shape
  (`state, summary, counts, actions, truncated, computed_at`) plus `items` and
  `next_offset`. It deliberately does not use .41's flat block (.58 §5.1).
  - Counts: `drifted`, `alias_pending`, `draft_pending`.
  - Items go only to the capability owner and to operators.
- **`brief`:** shows at most 3 accepted capabilities whose `tags` match the task's
  labels, trust-marked, with the same deterministic selection as .41 §7.
- **HTTP (slice 2):** `GET /v1/projects/{pid}/capabilities` and `/capabilities/{key}`
  at `CAP_READ`, with no HTTP writes in v1 and the .41 §8 text rules.

## 9. Backup, restore, rollback

- **Backups:** `.capability-requests/` receipts travel in the coordination sidecar.
  Their frozen schema is `requirement_records.validate_receipt`'s (.41 §10.3).
- **Records:** the records themselves are native comments on closed anchors, so
  native backups cover them.
- **Rollback floor:** slice 0 is the oldest kit a deployment may roll back to once
  any capability record exists (.41 §10.3).

## 10. Slices

0. **Tolerant reader.** It writes nothing, and a test asserts that. It ships in **the
   same release** as .41's and .58's slice 0.
   - It reserves the four prefixes above and the labels `capability`, `capability:`
     and `capability-key:`.
   - It whitelists `.capability-requests/`, with the frozen receipt validator.
   - It extends the nine-surface filter to `capability`.
   - It marks unknown `vN` records `unsupported`.
1a. **SSH records.** This slice depends on .41 slice 1, which extracts the core. It adds:
   - the `capability_records.py` spec;
   - `propose`, `revise`, `get`, `list` and `find`;
   - batch `capability-apply`, `retire`, `propose-alias` and `alias-reject`;
   - the client lookup merge;
   - updated worker guide and prompt.
1b. **Drift and view.** `capability check --repo [--record]`, the `capability verify`
   endpoint action, `views/CAPABILITIES.md` in `refresh`, and `work`/`brief` attention.
2. **HTTP and queue.** HTTP GET routes and a web panel; .58 slice 3 queue kinds
   `alias` and `capability`.

**Later, optional:** `capability suggest` drafts proposals from the .61 index or
graphify communities, to bootstrap a project's index. It never auto-accepts.

## 11. Rejected alternatives

- **Fold into .41 as another reference type.** The verification models differ. A
  reference is an authority statement with a review-by date, whereas a capability's
  location is machine-checkable. Folding it in would muddle .41, which is ready now.
- **A standalone module.** It would be a third copy of labels, receipts, reconcile,
  guards and rollback staging, which is what the .41 review asked to stop
  (.41 §12.1).
- **Drift check inside `refresh`.** The server has no checkout; see section 5.
- **Auto-accept on a passing check.** A resolving pointer proves a location, not a
  meaning.
- **A capabilities file in each repository.** A file versioned with the code would
  make drift trivial and put acceptance into pull-request review.
  - Rejected as the primary store, because the owner asked for Beads, the web and
    non-repository readers need it, and alias proposals would each need a pull
    request.
  - An opt-in export, like .41 §9.1, stays possible later.

## 12. Owner questions, with recommended answers

1. **May aliases work before acceptance?** **Yes,** marked `proposed`. They never
   shadow another capability's accepted names.
2. **May one owner decision accept a batch of capabilities?** **Yes,** with one
   acceptance record per capability, all citing that decision.
3. **May any contributor record a verification?** **Yes.** The verifier is attributed,
   uncommitted checkouts are refused, and readers re-check live.
4. **Must an accepted capability have a person or account owner?** **Yes.** Drafts
   need none.
5. **Should `refresh` render `views/CAPABILITIES.md`?** **Yes.** A repository export
   stays off by default.
6. **Build order?** **.60 slice 0 in the shared slice-0 release, then .41 slice 1,
   then .60 slices 1a and 1b.** .61 is independent and ships now.
7. **Should `capability suggest` exist?** **Later,** and it never auto-accepts.
