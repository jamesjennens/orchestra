# Durable per-project reference catalog - design proposal

Status: **proposal, revision 1. Not implemented, not accepted.** This document
changes no code. It proposes a record kind, an authority model, client commands,
attention surfacing, repo sync, backup coverage and a migration path for review
by the coordinator and acceptance by the project owner.

Nothing here is implemented: there is no `ref` command, no `reference` record
kind and no review-by surface in the current kit. Every change named below is a
follow-up implementation slice (§13), to be filed only after acceptance.

## 1. Problem and asks

Reference facts - "what is the authority for X, where does it live, when must it
be refreshed" - live in module docstrings, repository documents and each
session's private memory. Other sessions and workers cannot see them, and they
are not durable across sessions. The motivating example is a trading-calendar
module whose yearly holiday and early-close lists expire at year end, so a chart
built on a stale assumption is wrong in a way nobody is warned about. Similar
facts: identity authorities, settlement instants, which scheduled step writes
which store, server schedules and paths, units of external feeds, and restart
mode semantics.

The asks this design answers:

1. a reference record kind: key, statement, authority (repo path + commit, or
   URL), owner, review-by date, tags, supersession chain, readable as
   `ref get calendar.trading` and `ref list --tag data`;
2. two-way links between decisions and entries;
3. review-by dates surfaced in `work` and `brief` as attention items (expired or
   due-soon);
4. optionally, briefs show entries tagged to the areas a task touches;
5. no drift with repository documents: either sync/export with a repo file, or
   entries that only point at repo paths.

## 2. Design at a glance

| Question | Decision |
| --- | --- |
| Storage | Reserved machine-record kind (`Kind: reference-entry-v1`) on one native anchor issue per entry, with controlled labels. Not a new bd issue type, not a manifest field. |
| Keying | Immutable lowercase dotted key, unique per project, mirrored in a controlled `reference-key:` label for lookup. |
| Versioning | Monotonic revision per key; same key/revision with different content is refused. Content-addressed with `sha256`. |
| Supersession | Two levels: revision chain (`supersedes` exact reference) and key retirement (`replaces` / `replaced_by`, accepted by an operator). |
| Authority | Contributor proposes and revises drafts; owner/operator acceptance is the only route to `accepted`, with F3 acceptance evidence bound to the record hash. |
| Reads | `ref get`, `ref list`, `ref decisions`, `ref check` (read-only); writes `ref propose`, `ref revise` (contributor) and the operator route for acceptance. |
| Attention | Additive project-level `attention` block in `work` and additive `attention` array in `brief`; new kind `reference-review`; reading changes nothing. |
| Repo sync | Mode `repo-export` (default): catalog is authoritative, file is a deterministic projection guarded by receipts and `ref export --check`. Mode `repo-path-only`: no file, repo-path authorities only. |
| Backup | Entries are native comments, so the native backup covers them; local receipt journals join the coordination sidecar like the requirement journals. |
| Migration | `ref import` from `REFERENCE.md` creates **draft** entries only; nothing imported is accepted. |
| Implementation now | None. This document only. |

## 3. Storage model

### 3.1 Why a reserved machine-record kind

`requirements.py:34` already has a field named `REFERENCE_FIELDS`
(`id`, `revision`, `sha256`), but that is **not** a reference catalog: it is an
in-manifest content pointer resolved against the manifest's own record index
(`requirements.py:142-171`). It answers "which exact revision of this record does
this manifest cite", not "what is the authority for this operational fact". The
two must not be conflated, and the catalog must not become a new manifest field:
changing the manifest schema would require a `REQUIREMENTS_CONTRACT` revision and
a publisher change, which is out of scope here.

A new bd issue type is also not available: `coordination.py:248` accepts only
`task`, `bug`, `feature`, `chore` and `decision` for `create-child`, and those
type names mean work items. Using `decision` for a reference fact would corrupt
the decision semantics that `templates/DECISION.md` and the briefing attention
kind `decision` (`briefing.py:13`) depend on.

The kit already has the right precedent for durable, non-work, append-only
structured facts: a **reserved machine-record comment kind** on a native issue,
written only by a dedicated locked operation, with controlled labels and an
operator-only acceptance record. `requirement_records.py` documents exactly this
pattern for `Kind: requirement-revision-v1` and
`Kind: requirement-acceptance-v1`: the record is authoritative, the labels are
derived and never caller-supplied, raw `comments add` of the prefix is refused
by the guard in `reserved_comments.py`, and operator acceptance is bound to the
revision's content hash. The catalog reuses that pattern rather than inventing a
second storage mechanism.

### 3.2 Native anchor

Each entry is one native issue, created by the dedicated `ref propose` operation:

- native issue type `task` (the requirement-record precedent uses the ordinary
  issue type; the controlled labels, not the type, identify the record);
- controlled type label `reference`;
- controlled state label, exactly one of `reference:draft`,
  `reference:accepted`, `reference:superseded`;
- controlled lookup label `reference-key:` + the key with `.` replaced by `-`;
- the usual `request:<hash>` / `request-content:<hash>` idempotency labels.

`work` must skip rows carrying the `reference` type label, exactly as it already
skips `event`, `gate` and `merge-slot` (`work.py:175`), so a catalog of hundreds
of entries never appears as claimable work. That is an additive filter in the
implementation slice, not part of this design.

> Serialization note: `work.py` is owned by the in-progress checkpoint/freshness
> claim at this base. The implementation slice that adds the filter and the
> `work` attention block must be sequenced after that claim releases, or
> delivered as a coordinated follow-on.

### 3.3 Key

A key is an immutable lowercase dotted slug, at most 80 characters:

```text
^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$
```

Examples: `calendar.trading`, `identity.authority`, `settlement.instant`. Keys are
unique per project. Key changes are refused for an existing entry: a different
key is a different entry, related by `replaces`/`replaced_by` (§3.5).

Because bd label values are not guaranteed to accept `.`, the lookup label
substitutes `-` for `.`. `ref get` resolves through the label and then verifies
that the record's canonical `key` matches the requested key exactly; a slug
collision between two distinct keys (for example `a.b` and `a-b` used together)
is therefore detected and refused at propose time rather than silently aliasing.

### 3.4 Entry revision record

A revision is one reserved comment whose text is exactly `PREFIX + canonical
JSON`, where `PREFIX` is:

```text
Kind: reference-entry-v1
```

and canonical JSON means UTF-8, sorted keys, `ensure_ascii=False`, `,`/`:`
separators, no non-finite numbers, duplicate JSON fields rejected - the existing
`requirements.canonical_bytes` / `content_hash` conventions. The record's
`sha256` is the content hash over every field except `sha256` itself.

The field set is closed:

```json
{
  "schema_version": 1,
  "key": "calendar.trading",
  "revision": 3,
  "title": "Trading calendar authority",
  "statement": "Every chart and settlement calculation derives holidays and early closes from the pinned calendar module, never from an inline list.",
  "authority": {
    "type": "repo-path",
    "path": "src/example/calendar.py",
    "commit": "0000000000000000000000000000000000000000",
    "anchor": "HOLIDAYS"
  },
  "owner": "alex/session1",
  "review_by": "2027-01-15",
  "tags": ["calendar", "data"],
  "decisions": ["example-project-42"],
  "supersedes": {
    "id": "example-project-17",
    "revision": 2,
    "sha256": "1111111111111111111111111111111111111111111111111111111111111111"
  },
  "replaces": null,
  "replaced_by": null,
  "acceptance_state": "draft",
  "origin": {"type": "authored"},
  "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
}
```

Field rules:

| Field | Rule |
| --- | --- |
| `schema_version` | integer `1`. |
| `key` | §3.3, immutable. |
| `revision` | positive integer, exactly the next revision for the key. |
| `title` | nonempty, <= 200 characters. |
| `statement` | nonempty, <= 2000 characters; state the fact, not the evidence. |
| `authority` | exactly one closed object, `repo-path` or `url` (§3.6). |
| `owner` | nonempty attribution string; the person who refreshes the entry. Attribution, not authenticated identity. |
| `review_by` | `YYYY-MM-DD`, real calendar date. Required for an accepted revision, optional for a draft. |
| `tags` | 0..12 unique lowercase slugs `^[a-z][a-z0-9-]{0,31}$`. |
| `decisions` | unique native decision issue IDs (§3.8). |
| `supersedes` | `null` for revision 1, else exact `{id, revision, sha256}` of the previous revision of the same key. |
| `replaces` | `null`, or the key this entry replaces. Set on the replacement entry. |
| `replaced_by` | `null`, or the key that replaces this entry. Set on the retired entry. |
| `acceptance_state` | `draft`, `accepted` or `superseded`; written by the operation, never caller-supplied. |
| `origin` | `{"type":"authored"}` or `{"type":"import","path":"REFERENCE.md","digest":"<64 hex>"}`. |
| `sha256` | content hash of every other field. |

### 3.5 Versioning and supersession

Revisions are append-only and never rewritten. A meaningful change - including a
change to `acceptance_state`, a `review_by` change, or an authority move - is a
new revision with a new hash. Reposting identical bytes for the same key and
revision is tolerated (idempotent); conflicting bytes for the same key and
revision are refused, which is the same rule `export_requirements.check_history`
applies to requirement revisions.

Two levels of supersession:

- **Revision chain.** Revision *N* cites revision *N-1* by exact
  `{id, revision, sha256}`. A dangling, mismatched or cyclic chain is a malformed
  record and fails closed (§3.7).
- **Key retirement.** When entry `K` is replaced by a different key `K2`, the
  operator accepts `K2` with `replaces: "K"` and then accepts `K` with
  `replaced_by: "K2"` (state `reference:superseded`). Both halves are separate
  operator decisions. `ref get K` on a superseded key follows `replaced_by` one
  hop, returns the retired record plus a `resolved` pointer to `K2`, and never
  loops: a chain longer than 8 hops or a cycle is reported as a warning and the
  walk stops at the last well-formed record.

### 3.6 Authority

`authority` is exactly one of:

```json
{"type": "repo-path", "path": "src/example/calendar.py", "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"}
```

```json
{"type": "url", "url": "https://example.invalid/authority/page", "retrieved": "2026-09-01"}
```

- `repo-path`: `path` is relative, uses `/`, and may not be absolute, contain
  `..` or start with `~`; `commit` is a 40-character lowercase hex revision and
  is required for an accepted revision; `anchor` is an optional symbol or heading
  inside the file.
- `url`: `retrieved` is the date a human last verified the page. The kit makes no
  network request: `ref check` reports a `url` authority whose `retrieved` date is
  older than the entry's `review_by` window as a warning, and never fetches it.

The server never reads a repository path. Authority resolution is a client/operator
concern (`ref check`, §6.5), which is why an entry can be accurate without the
coordination server having the project checkout.

### 3.7 Reserved-write guard and malformed records

Implementation adds two prefixes to `reserved_comments.RESERVED`:

- `Kind: reference-entry-v1` -> writer `ref propose|revise`;
- `Kind: reference-acceptance-v1` -> writer `admin.py reference-apply`.

Raw `comments add` of either prefix is refused on the contributor endpoint, in
every pflag/attachment ordering the existing guard already normalizes, so the
dedicated operations are the only writers. A comment that claims one of these
prefixes but fails schema validation makes the affected read fail closed and name
the offending comment, exactly like a malformed checkpoint
(`docs/OPERATIONS.md`, "Malformed structured history"). Repair is the existing
operator `void-record` path; nothing new is needed and nothing is deleted.

### 3.8 Relation to requirements.py and BRD/decision traceability

- **No manifest change.** The catalog is not part of `requirements-baseline.json`
  or the publication manifest. `REFERENCE_FIELDS` keeps its current meaning. If
  the owner later wants a requirement revision to cite catalog keys durably, that
  is a separate contract change with its own validator/publication slices, and it
  is deliberately not smuggled into this design.
- **Decisions.** Decisions remain native issues created through `create-child`
  with `type=decision`, validated against `templates/DECISION.md`. An entry cites
  decision issue IDs in `decisions`; the write path validates that each ID
  resolves to an existing issue whose `issue_type` is `decision` or which carries
  the `decision` label, and refuses an unknown link. The reverse direction is a
  read-time view, `ref decisions DECISION-ID`, computed over the catalog; **no**
  write ever edits a decision record, so the decision lifecycle, the render
  backlink scanner (`render.py:33`, which already understands
  `Supersedes`/`Supports`/`Contradicts`/`Comments-on` between comments) and the
  review workflow are untouched.
- **Impact views.** An impact selection's `context_ids` already names decisions
  and discussions to retain alongside selected requirements
  (`export_requirements.selection_from_manifest`); a catalog key is not added
  there in this design, because that would require touching the impact contract.

### 3.9 Relation to the agent registry and the web interface

At this base commit, the integrated personal-agent work is the **owner-bound
agent registry**, not BRD/decision traceability (the task text's phrasing does
not match the record; the discrepancy is recorded honestly here). The catalog's
`owner` field and acceptance authority deliberately reuse existing authority
sources rather than inventing one:

- acceptance authority is the deployment operator allowlist already used by
  `void-record` and `requirement-apply`
  (`admin.py`, `docs/OPERATIONS.md`), so a restore's operator policy applies
  unchanged;
- where an actor is an agent, the actor string is the agent identity the registry
  already issues; the catalog treats it as attribution, exactly as every other
  record does, and the registry's owner-bound cap is enforced by the existing
  HTTP authority layer, not re-implemented here.

The web interface (in progress) exposes the catalog as
**read-only views plus the propose write**, using the same JSON shapes as the
client (§6): a catalog list filtered by tag/state/due, an entry detail with its
acceptance evidence and links, and a due/expired badge driven by the same
`reference-review` attention projection. Implementation adds routes to the
existing `ROUTES`/`@route` registry in `http_service.py`, which that claim owns;
the follow-up slices must serialize with it and must not re-declare the
projection, so the web view and `work`/`brief` cannot disagree.

## 4. Authority model

The split mirrors `requirement_records.py`'s F3 split, because that is the kit's
reviewed answer to "contributor proposes, operator accepts":

| Action | Who | Evidence written |
| --- | --- | --- |
| Create a new entry as `reference:draft` (`ref propose`) | any contributor actor | one `reference-entry-v1` revision, `acceptance_state: draft` |
| Revise an entry's draft (`ref revise`) | any contributor actor (trusted team) | next `reference-entry-v1` revision, `draft` |
| Accept an entry (`admin.py reference-apply`) | deployment operator / project owner | next revision with `acceptance_state: accepted` **plus** `reference-acceptance-v1` acceptance evidence bound to that revision's `record_sha256` |
| Supersede or retire a key | operator only | revision with `acceptance_state: superseded` (`replaced_by`) plus acceptance evidence |
| Propose a change to an accepted entry | any contributor | a higher draft revision; the **accepted** pointer does not move until an operator accepts |
| Set or change `review_by` | any contributor proposes; operator accepts | takes effect only on acceptance; a draft's date is shown as proposed, never as the effective deadline |

Acceptance evidence reuses the existing F3 shape (`requirements.ACCEPTANCE_FIELDS`
minus `manifest_sha256`, plus `record_sha256`), the existing policies
`any-owner` / `all-owners`, and the existing subset rule
(`approvers` must be a subset of `owners`; `all-owners` requires identical sets).
`decision_id` and `evidence` are required, so an acceptance always points at a
recorded decision and a durable evidence pointer. The acceptance record is
written **before** the accepted revision and label move, so an uncertain write can
never leave an entry that reads accepted with no evidence - the ordering
`requirement_records.py` already documents.

No new authority source is introduced. A payload's own `operator`/`owner` string
is never authority; the deployment allowlist is, exactly as
`docs/OPERATIONS.md` states for void records.

## 5. What authority means to a reader

`ref get` returns both halves so no reader can mistake a proposal for authority:

- `record`: the newest revision whose chain is valid and whose
  `acceptance_state` is `accepted` or `superseded`, or `null` if the key has only
  drafts;
- `proposed`: the newest `draft` revision that has no acceptance, or `null`;
- `state`: `accepted`, `superseded`, `draft-only` or `malformed`;
- `acceptance`: the operator evidence for `record`, or `null`.

A key with `state: draft-only` is explicitly **not authoritative**: `ref list`
marks it, `ref check` reports it, and the attention block never presents a draft
as the answer to "what is the authority for X". This mirrors the brief rule that
reading clears nothing and that unsummarized prose is not classified.

## 6. Client commands and JSON shapes

`ref` becomes a first-class client action, routed like `brief`/`checkpoint`
(`client.py` pops the action; the endpoint dispatches it under the project lock).
Output follows the v1 client contract: one JSON envelope, JSON on `stdout`,
diagnostics on `stderr`, excerpt objects for long text, bounded pages, nonzero
exit with a labelled `ValueError` on refusal.

### 6.1 `ref get KEY`

```sh
b ref get calendar.trading --json
```

```json
{
  "key": "calendar.trading",
  "state": "accepted",
  "native_id": "example-project-17",
  "record": {
    "revision": 3,
    "title": "Trading calendar authority",
    "statement": "Every chart and settlement calculation derives holidays and early closes from the pinned calendar module, never from an inline list.",
    "authority": {"type": "repo-path", "path": "src/example/calendar.py", "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"},
    "owner": "alex/session1",
    "review_by": "2027-01-15",
    "tags": ["calendar", "data"],
    "decisions": ["example-project-42"],
    "replaces": null,
    "replaced_by": null,
    "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
  },
  "record_comment_id": "01a00000-0000-7000-8000-000000000000",
  "acceptance": {
    "decision_id": "example-project-42",
    "owners": ["owner/one"],
    "approvers": ["owner/one"],
    "policy": "any-owner",
    "evidence": "decision example-project-42",
    "record_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "operator": "owner/one",
    "at": "2026-09-27T20:00:00Z"
  },
  "proposed": null,
  "due": "ok",
  "resolved": null,
  "coverage": "newest accepted revision and its acceptance evidence; unresolved drafts are returned in proposed"
}
```

`title` and `statement` are excerpt objects (`{"text", "omitted_chars"}`) under
the `brief`/`history` contract; `sha256`, IDs and cursors are never excerpted.
`due` is `ok`, `due-soon`, `expired` or `unset` (§7.2). Unknown key: nonzero exit,
`stderr` names the key and suggests `ref list`.

### 6.2 `ref list`

```sh
b ref list --tag data --state accepted --due expired --limit 20 --json
```

```json
{
  "total": 2,
  "items": [
    {"key": "calendar.trading", "title": "Trading calendar authority", "state": "accepted",
     "owner": "alex/session1", "review_by": "2026-01-15", "due": "expired",
     "tags": ["calendar", "data"], "revision": 3, "native_id": "example-project-17"},
    {"key": "feed.units", "title": "External feed units", "state": "draft-only",
     "owner": "bob/session4", "review_by": null, "due": "unset",
     "tags": ["data"], "revision": 1, "native_id": "example-project-23"}
  ],
  "next_offset": null,
  "coverage": "one row per key: newest accepted/superseded revision, or newest draft when no revision is accepted"
}
```

Options: `--tag TAG` (repeatable, AND), `--owner ACTOR`, `--state
draft-only|accepted|superseded|all`, `--due expired|due-soon|unset`, `--limit`
1..100, `--offset` >= 0. Ordering: expired, then due-soon, then unset, then key.
Long text clips at 200 characters like `work` rows.

### 6.3 `ref propose` / `ref revise`

A closed payload; `operation` comes from the subcommand, and
`acceptance_state`, `labels`, `acceptance` and `sha256` are refused if
caller-supplied:

```json
{"schema_version": 1, "operation_id": "alex/session1-ref-1",
 "key": "calendar.trading", "title": "Trading calendar authority",
 "statement": "Every chart derives holidays and early closes from the pinned calendar module.",
 "authority": {"type": "repo-path", "path": "src/example/calendar.py",
               "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"},
 "owner": "alex/session1", "review_by": "2027-01-15", "tags": ["calendar", "data"],
 "decisions": ["example-project-42"], "supersedes": null,
 "replaces": null, "replaced_by": null}
```

```sh
b ref propose --file entry.json --json
b ref revise  --file entry.json --json
```

- `propose` creates revision 1 as `reference:draft`, or is refused if the key
  already exists.
- `revise` requires `supersedes` to be the exact `{id, revision, sha256}` of the
  current newest revision, must write exactly the next revision, and is bound to
  the key: a key swap is refused before any native write.
- Refusals: duplicate key, key-swap, stale/dangling `supersedes`, unknown
  decision link, `review_by` in the past on a **new** accepted proposal (allowed
  on a draft, since drafting a correction for an overdue entry is the normal
  repair), `repo-path` authority with an absolute/`..` path, `replaced_by`/
  `replaces` asymmetry at accept time, and (in `repo-path-only` mode, §8) any
  `url` authority.
- Idempotency: `operation_id` plus a `.reference-requests/` receipt journal, the
  same native-first pattern as `.requirement-requests`: preflight reads before
  the receipt is written, and an uncertain real write leaves a pending receipt
  reconciled from native state rather than creating a duplicate.
- Returns `{"key", "revision", "native_id", "record_comment_id", "state":
  "draft", "reconciled": false}`.

### 6.4 `ref decisions DECISION-ID`

```json
{"decision": "example-project-42", "entries": [{"key": "calendar.trading", "state": "accepted", "revision": 3}], "total": 1}
```

Read-only reverse view over the catalog. It never writes the decision.

### 6.5 `ref check`

```sh
b ref check --repo C:\path\to\project-checkout --json
```

```json
{
  "checked": 8,
  "failures": [
    {"key": "settlement.instant", "code": "authority-missing",
     "detail": "src/example/ledger.py not found at commit 0000000000000000000000000000000000000000"}
  ],
  "warnings": [
    {"key": "identity.authority", "code": "url-not-reverified",
     "detail": "url authority retrieved 2025-01-01, review_by 2026-01-15"},
    {"key": "calendar.trading", "code": "asymmetric-supersession",
     "detail": "replaced_by names feed.units but feed.units does not replace calendar.trading"}
  ],
  "coverage": "offline read-only checks against the supplied checkout; no network access"
}
```

`ref check` runs on a machine that has the project checkout, never on the server,
and makes no network request. Nonempty `failures` exits nonzero; warnings do not.
Checks: `repo-path` path and anchor present at the recorded commit, `url`
`retrieved` within the review window, `review_by` present on accepted entries,
dangling or asymmetric supersession, `decisions` links still resolving, and
malformed reserved records (reported, not repaired).

### 6.6 Operator acceptance

```sh
python3 admin.py --root <runtime> reference-apply --file acceptance.json
```

```json
{"schema_version": 1, "operation_id": "owner/one-apply-1",
 "key": "calendar.trading", "revision": 3,
 "record_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
 "acceptance_state": "accepted",
 "acceptance": {"decision_id": "example-project-42", "owners": ["owner/one"],
                "approvers": ["owner/one"], "policy": "any-owner",
                "evidence": "decision example-project-42"}}
```

Refused for a non-operator actor, for a hash that does not match the named
revision, for a missing decision/evidence pointer, and for a policy violation.
`reference-backfill` applies the controlled labels to an entry created outside
the operation, mirroring `requirement-backfill`, and writes acceptance evidence
when the target state is accepted.

## 7. Review-by surfacing in `work` and `brief`

### 7.1 Where it appears, and where it must not

`attention` does not exist in `work.py` or `briefing.py` today, so this is new
ground. A reference entry is **not** a task: it has no owner-assignee, no review
state and no lifecycle. The design therefore adds a **project-level**
`attention` block to `work` (not a per-task field, not a synthetic queue row) and
a bounded `attention` array to `brief` (tasks stay the unit of a brief).

`work` shape, additive:

```json
{"owner": null, "total": 12, "items": [], "next_offset": null,
 "coverage": "Fresh current view; ...",
 "attention": {
   "reference_review": {
     "expired": 1, "due_soon": 1, "unset": 2, "total": 4,
     "items": [
       {"key": "calendar.trading", "review_by": "2026-01-15", "due": "expired",
        "owner": "alex/session1", "state": "accepted", "revision": 3},
       {"key": "feed.units", "review_by": "2026-10-01", "due": "due-soon",
        "owner": "bob/session4", "state": "accepted", "revision": 2}
     ],
     "next_offset": null
   }
 }
}
```

New bounded options `--ref-limit` (1..100) and `--ref-offset` (>= 0). The block is
computed regardless of `--owner`/`--state`/`--mine` task filters, because the
reference catalog is project-wide and a filter on the task queue must not hide an
expired authority. Only accepted (and superseded, flagged) entries appear;
`draft-only` entries appear as `unset`-class proposals only when `--state
draft-only` is requested.

`brief TASK` shape, additive:

```json
{"attention": [
  {"kind": "reference-review", "key": "calendar.trading", "due": "expired",
   "review_by": "2026-01-15", "text": "Authority for the trading calendar is past its review date.",
   "source": "ref calendar.trading"}
],
 "attention_total": 1, "attention_more": null}
```

- `kind` is the new value `reference-review`. The checkpoint unresolved-item
  vocabulary (`blocker`, `question`, `decision`, `correction`, `dependency`,
  `briefing.py:13`) is **not** extended: those are author-declared open items, and
  a computed date reminder must not masquerade as one.
- Default selection: every project entry that is expired or due-soon, expired
  first, bounded to 3 with `attention_total`/`attention_more`; plus, when the
  caller passes `--ref-tag TAG` (repeatable), entries whose tags match (ask 4,
  implemented as an explicit opt-in rather than invented inference).
- Reading changes nothing: no status, lifecycle, checkpoint or open item is
  touched by a `work`/`brief` read, consistent with `docs/BRIEFINGS.md`.

`brief --help`/`work --help` gain the new fields, options and limits in their
machine-readable payload (`help_payload`/`help_limits`), keeping the
`cli-contract-v1` additions additive.

### 7.2 Due classification

Dates are UTC calendar dates only (`YYYY-MM-DD`), compared against the server's
UTC date:

| Class | Rule |
| --- | --- |
| `expired` | `review_by` < today |
| `due-soon` | today <= `review_by` <= today + `reference_due_soon_days` |
| `ok` | `review_by` > today + window |
| `unset` | `review_by` absent (draft only) |

`reference_due_soon_days` is a per-project coordination configuration value,
default `30`, bounded 1..365. No time-of-day, no time zone and no per-entry
window in v1 - one window keeps the projection deterministic and testable.

## 8. Repo sync/export and the conflict rule

A per-project `reference_mode` configuration selects one of two modes. Both give
"no drift"; a project picks one at adoption.

### 8.1 `repo-export` (default)

`ref export --out docs/REFERENCE.md` writes a deterministic projection of the
accepted catalog:

```markdown
<!-- orchestra-reference-export: schema=1 project=example-project digest=3333333333333333333333333333333333333333333333333333333333333333 generated=2026-09-27T20:00:00Z -->
| Key | Revision | Statement | Authority | Owner | Review by | Tags |
| --- | --- | --- | --- | --- | --- | --- |
| `calendar.trading` | 3 | Every chart derives holidays and early closes from the pinned calendar module. | `src/example/calendar.py@0000000#HOLIDAYS` | alex/session1 | 2027-01-15 | calendar, data |
```

- `digest` is `content_hash({"schema": 1, "project": <name>, "entries":
  [{"key","revision","sha256"}, ...]})` over the sorted accepted entries.
- Ordering, escaping and whitespace are fixed, so the rendering is byte-stable.
- The catalog (native records) is **authoritative**; the file is a projection and
  is never read back as authority.
- **Conflict rule.** `ref export` refuses to overwrite a file whose bytes do not
  match the digest recorded in the local `.reference-exports/` receipt journal.
  A hand-edited file is therefore reported, never silently clobbered; `--force`
  is operator-only and prints exactly what it overwrote. `ref export --check
  docs/REFERENCE.md` recomputes the catalog digest, compares the header, and
  re-renders the body in memory for a byte comparison, failing on any drift -
  suitable for CI. A hand-edit never becomes a catalog entry: the only route from
  a repo file into the catalog is `ref import` (§10), which creates **drafts**.
- Two writers cannot race: writes go through the project lock, and the export
  journal is local coordination state (`.reference-exports/`), covered by the
  backup sidecar like the other journals.

### 8.2 `repo-path-only`

No generated file. Entries may carry only `repo-path` authorities; a `url`
authority is refused at propose time, and `ref export` refuses. `ref check`
verifies each path and anchor at the recorded commit. This is the stricter option
for a project that already maintains reference documentation by hand and does not
want a second copy in the tree.

**Recommendation:** `repo-export` by default, with `repo-path` authorities for
facts the project's own code or docs already state, and `url` reserved for
external authorities that have an `owner` and a `retrieved` date. Internal facts
point at the repository; the export exists so a reader without the client still
sees the catalog.

## 9. Backup coverage and restore behaviour

- **Native backup covers the records.** Entries, revision chains and acceptance
  evidence are native comments on native issues, so
  `admin.py backup ... backup sync` and `restore-new` already carry them;
  `restore-new` retains issue IDs and comments, so keys, revision chains, links
  and acceptance evidence survive a restore with no re-keying.
- **Local journals join the coordination sidecar.** `.reference-requests/`
  (propose/revise idempotency receipts) and `.reference-exports/` (export
  digests) are the operator recovery cache, exactly like `.requirement-requests/`
  and `.requirement-backfills/`. `admin.py backup_project` must read them into
  `files` under their own path prefixes, and restore must validate them via
  `validate_coordination_files`, following the requirement-journal block
  (`admin.py:453-465`). `.reference-acceptances/` receipt cache, if the
  implementation keeps one, joins the same list.
- **Restore semantics.** Reading an accepted entry after restore does not depend
  on any local journal: the acceptance evidence comment is the authority, the
  same guarantee `requirement_records.py` documents for
  `requirement-acceptance-v1`. A lost journal costs at most an idempotency
  receipt, never an entry or an acceptance.
- **Not backed up, deliberately.** Nothing reference-specific is added to
  `.history-snapshots`-style disposable caches; the design adds no new store that
  is neither native nor a small receipt journal.
- **Authority on restore.** Acceptance authority is the deployment operator
  allowlist, whose restore policy is unchanged (native records and sidecar
  restored; allowlist not re-granted by default). A restored project therefore
  shows accepted entries, and accepting a *new* revision again requires a
  currently listed operator.

> Serialization note: the `admin.py` backup-path change touches the file owned by
> the in-progress backup-coverage claim at this base, so that
> implementation slice must be sequenced after it.

## 10. Migration path from a project's `REFERENCE.md`

`ref import` is one-way and draft-only:

```sh
b ref import --file REFERENCE.md --dry-run --json
b ref import --file REFERENCE.md --review-by 2026-12-31 --json
```

Accepted input is a strict subset, because arbitrary prose cannot be parsed
reliably and guessing would create silent wrong authorities:

- **Heading mode.** `## <key>` starts an entry; `Authority:`, `Owner:`,
  `Review-by:` and `Tags:` lines are required (`Authority` and `Owner` always;
  `Review-by` from the line or from `--review-by`); everything else until the
  next heading becomes `statement`. Anything missing is a refusal naming the
  heading.
- **Table mode.** A Markdown table with the exact columns
  `key | statement | authority | owner | review-by | tags`. Extra or missing
  columns are refused.
- Every imported entry becomes revision 1 with `acceptance_state: draft` and
  `origin: {"type": "import", "path": "REFERENCE.md", "digest": "<sha256 of the
  file>"}`. **Nothing imported is accepted.** The owner accepts entries through
  the normal operator route, one at a time, so migration cannot silently promote
  unverified text to authority.
- Keys are slugified from the source heading and must be unique; collisions and
  invalid keys are refused with the offending headings named.
- `--dry-run` prints planned keys, parsed fields, all refusals and writes
  nothing. Import is idempotent per `(path, digest, key)` via
  `.reference-requests/`.
- The source `REFERENCE.md` is left untouched. The recommended end state is:
  import, accept the still-true entries, then either delete `REFERENCE.md` after
  adding a one-line pointer to `ref list`, or keep it as the `repo-export`
  projection, where `ref export --check` guards it.
- Dates are never invented: if neither the file nor `--review-by` supplies a
  date, the entry is created with `review_by: null` and `ref check` reports it as
  a draft needing a date before acceptance.

## 11. Worked examples (placeholders only)

### 11.1 Repo-path authority: the calendar that expires

```json
{"schema_version": 1, "operation_id": "alex/session1-ref-1",
 "key": "calendar.trading",
 "title": "Trading calendar authority",
 "statement": "Every chart and settlement calculation derives holidays and early closes from the pinned calendar module, never from an inline list; the module's lists expire at year end.",
 "authority": {"type": "repo-path", "path": "src/example/calendar.py",
               "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"},
 "owner": "alex/session1", "review_by": "2026-12-31",
 "tags": ["calendar", "data"], "decisions": [], "supersedes": null,
 "replaces": null, "replaced_by": null}
```

Read: `ref get calendar.trading` -> `state: accepted`, `due: expired` after
2026-12-31, and an `attention.reference_review` item in `work`; `ref check`
reports the anchor if it no longer exists at the recorded commit.

### 11.2 URL authority: an external identity registry

```json
{"schema_version": 1, "operation_id": "bob/session4-ref-7",
 "key": "identity.registry",
 "title": "Identity registry authority",
 "statement": "Employee identifiers are issued by the corporate identity registry; the registry page is the authority for the issuer format.",
 "authority": {"type": "url", "url": "https://example.invalid/identity/issuer-format",
               "retrieved": "2026-09-01"},
 "owner": "bob/session4", "review_by": "2027-03-01",
 "tags": ["identity"], "decisions": ["example-project-42"], "supersedes": null,
 "replaces": null, "replaced_by": null}
```

Read: `ref decisions example-project-42` lists it; `ref check` warns
`url-not-reverified` once `retrieved` falls outside the review window. No network
call is made.

### 11.3 Supersession and a decision link: settlement instant changed

Accepted predecessor (retired later):

```json
{"schema_version": 1, "key": "settlement.instant", "revision": 2,
 "title": "Settlement instant",
 "statement": "Settlement is calculated at the close of the primary session.",
 "authority": {"type": "repo-path", "path": "src/example/ledger.py",
               "commit": "0000000000000000000000000000000000000000", "anchor": "SETTLEMENT"},
 "owner": "alex/session1", "review_by": "2027-01-15", "tags": ["settlement"],
 "decisions": ["example-project-42"],
 "supersedes": {"id": "example-project-31", "revision": 1,
                "sha256": "4444444444444444444444444444444444444444444444444444444444444444"},
 "replaces": null, "replaced_by": "settlement.instant-intraday",
 "acceptance_state": "superseded", "origin": {"type": "authored"},
 "sha256": "5555555555555555555555555555555555555555555555555555555555555555"}
```

Replacement accepted first with `replaces: "settlement.instant"`, then the
predecessor accepted with `replaced_by` as above. Read: `ref get
settlement.instant` returns the retired record plus `resolved` pointing at the
replacement, and `ref check` fails an asymmetric pair where one half was accepted
and the other was not.

## 12. Rejected alternatives

| Alternative | Why rejected |
| --- | --- |
| One native issue per entry typed `decision` | Corrupts the decision semantics used by `templates/DECISION.md`, the briefing `decision` attention kind and the render/backlink view. |
| A new manifest field in `requirements-baseline.json` | Needs a `REQUIREMENTS_CONTRACT` revision and publisher change; conflates "cited revision" with "operational authority". |
| A single catalog comment per project | No per-entry state or labels, unbounded comment growth, and no additivity for `work` filtering. |
| Entries only in the project repository file | Not visible to other sessions' workers without a checkout, and not durable against a bad edit - the exact failure the owner reported. |
| A new server-side JSON store | A second store beside native records would need its own backup, restore, locking and conflict policy. The kit's reviewed answer is native records plus a small receipt journal. |
| Extending the checkpoint `kind` vocabulary with `reference-review` | Confuses author-declared open items with computed date reminders; unresolved items must stay author-owned. |
| Fetching `url` authorities server-side | The server has no network authority and no checkout; verification belongs in `ref check` on a client/operator machine. |

## 13. Follow-up implementation slices (proposed, not filed)

Filed by the owner only after this design is accepted:

1. `reference_records.py`: the `reference-entry-v1` record kind, controlled
   labels, key resolution, propose/revise with the `.reference-requests/` receipt
   journal, reserved-prefix guard entries, and unit tests.
2. `admin.py reference-apply|reference-backfill` + `reference-acceptance-v1`
   evidence, reusing the F3 acceptance validation.
3. `ref` client action and endpoint dispatch: `get`, `list`, `decisions`,
   `check`, `export`, `import`, plus `docs/CLI_CONTRACT.md` additions.
4. `work`/`brief` `attention` block and `reference-review` items, `work` filter
   for `reference` rows, help/limit payloads (`work.py`/`briefing.py`, serialized
   with the checkpoint/freshness claim).
5. `admin.py backup_project` sidecar coverage for the new journals (serialized
   with the backup-coverage claim).
6. Web read views and the propose write over the existing route registry
   (serialized with the web-interface claim).
7. `docs/OPERATIONS.md` migration/runbook section and a `templates/` reference
   entry example, if the owner wants a copyable payload.

## 14. Open questions for the coordinator and owner

1. **Mandatory review-by.** This design requires `review_by` for an accepted
   entry and allows `null` on a draft. Is "no accepted entry without a review-by
   date" the right hard rule, or should timeless entries be allowed with an
   explicit `review_by: "never"` sentinel?
2. **Who accepts.** This design grants acceptance to the deployment operator
   allowlist (reusing `requirement-apply`). Should a per-project coordinator
   identity that is not a deployment operator also accept, and if so where does
   that authority live and how is it restored?
3. **key-level retirement** takes two operator acceptances (replacement first,
   then predecessor). Is the two-step asymmetry acceptable, or should one
   operator operation perform both writes atomically?
4. **Brief selection.** Should a task's brief auto-match entries by tags derived
   from the task's area, or stay explicit (`brief --ref-tag`) as designed here?
5. **`repo-export` default.** Is a generated in-tree `REFERENCE.md` acceptable in
   projects that already maintain one, or should adoption default to
   `repo-path-only`?
6. **Scale.** Reads resolve keys through a controlled label per entry. At what
   catalog size (hundreds? thousands?) should `ref list` move to a maintained
   project-level index, and is a bounded linear read acceptable until then?
7. **Export file location.** `docs/REFERENCE.md` is the suggestion; should the
   owner fix a single path per project so `ref export --check` can run in CI
   without configuration?
