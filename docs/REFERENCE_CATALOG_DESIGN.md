# Durable per-project reference catalog - design proposal

Status: **proposal, revision 3. Not implemented, not accepted.** This document
changes no code. It proposes a record kind, an authority model, client commands,
attention surfacing, repo sync, backup/rollback coverage and a migration path for
review by the coordinator and acceptance by the project owner.

Nothing here is implemented: there is no `ref` command, no `reference` record
kind and no review-by surface in the current kit. Every change named below is a
follow-up implementation slice (section 15), to be filed only after acceptance.

Revision 3 revises revision 2 on the same base, `main`
`8e1f9ec6f4d818de8983f59f80aa896111a99712` (revision 1 was written 71 commits
earlier, against `a3e33a4`), in response to the second review round (comment
`01a0f121`, items `hidden-overstated`, `authority-edges`, `slice0-schema`,
`export-conflict` and `consistency`). Every code-level claim below was
re-verified against that checkout, and the closed-issue behaviour relied on in
section 3.2 was verified by driving the pinned `bd 1.2.2` binary against a
disposable database (section 3.2, evidence note).

## Revision 3 changes (review 01a0f121)

| Review item | Where it is answered |
| --- | --- |
| `hidden-overstated` | 3.2 and 6.0 - "every reader hides it" is corrected: the HTTP task list reads `bd list --all`, so a closed anchor still shows as a closed task in the web Closed/All tabs and `q` search, and the task detail/history/brief routes show the raw `Kind: reference-*` comments. `GET /tasks/{id}`, `/history` and `/brief` are added to the filter list and must 404 (or point at the reference route). 7.1 states the SSH routing rule. |
| `authority-edges` | 3.4 refuses the real `session-<uuid>` actor shape; 4 and 7.1 raise an `acceptance-inert` attention item to approvers after an operator removal or a restore without `--restore-operators`; 4 states plainly that a project owner accepts only as an allowlisted operator with shell access, and that the allowlist holds actor strings, not `account:` identities. |
| `slice0-schema` | 10.3 and 15 - slice 0 freezes the `.reference-requests/` receipt schema and ships its validator, not only the path whitelist; slice 0 does not wait for `.58`, and `.58` gets its own tolerant-reader step. |
| `export-conflict` | 9.1 - refuse only when `body_digest` does not match the body (a hand edit); a stale `catalog_digest` is the expected reason to regenerate. |
| `consistency` | 3.2 keeps the `request:`/`request-content:` labels that reconcile looks records up by; 12.1 and 15 require byte-identical requirement record, evidence and receipt bytes with a byte-compatibility test; 7.1 says My work lands in slice 2. |

## Revision 2 changes (review 01a0f0d0)

| Review item | Where it is answered |
| --- | --- |
| `reserved-labels` | 3.7 - reserve `reference`, `reference:` and `reference-key:` in the shared label guard. |
| `authority-claim` | 3.10 and 4 - one authority: the deployment operator allowlist. |
| `rollback` | 10.3 - rollback compatibility and the staged tolerant-reader release. |
| `entries-as-tasks` | 3.2 - entry anchors are created **closed**; 6.0 lists every reader that must still filter. |
| `shared-core` | 12.1 - one extracted keyed-record core for requirements, references and `.58`. |
| `drift-check` | 9 - pointer mode default, self-verifying export, `authority-changed-since-pinned`. |
| `owner-identity-attention` | 3.4, 7.1 and 7.3 - durable owner identity, counts-vs-items routing, per-entry failure isolation. |
| `http-untrusted-text` | 8 - HTTP read routes only, `token()`/`label()` in prompts, `trust` in briefs, plain-text web. |
| `simplify-and-slices` | 3.5, 6.3, 6.4 and 15 - expected revision, one `ref retire`, slices 0-2. |
| `owner-questions-and-58` | 12.2, 16 - recommended answers and the shared plan with kittrial-5bb.58. |

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

The task's asks, and where this design answers them:

| Ask | Where |
| --- | --- |
| storage: a reserved machine-record kind, and how it relates to `requirements.py`, BRD/decision traceability (kittrial-5bb.22) and the web interface (kittrial-5bb.20) | 3, 12.1 |
| who may create, revise, supersede or retire an entry | 4 |
| client commands and JSON shapes | 6 |
| review-by dates surfaced in `work` and `brief` as attention | 7 |
| briefs show entries tagged to the areas a task touches | 7.1 |
| repo sync without drift; conflict rule | 9 |
| backup coverage and restore behaviour | 10.1, 10.2 |
| migration path from a project's `REFERENCE.md` | 11 |
| three worked examples with placeholders | 13 |

Plus the two design constraints carried from revision 1: this stays design-only
(no `ref` command, record kind, validator, publisher or web route is
implemented), and the public repository stays free of private hostnames, project
names and firm-specific detail - every example below is a placeholder.

## 2. Design at a glance

| Question | Decision |
| --- | --- |
| Storage | Reserved machine-record kind (`Kind: reference-entry-v1`) on one native anchor issue per entry, with controlled labels. Not a new bd issue type, not a manifest field. |
| Anchor | The anchor issue is created and **closed** by the write operation before the first revision comment, so the work surfaces - including an older kit's - hide it (3.2). Closed status alone does not hide it everywhere: the HTTP task list reads `bd list --all` and the web Closed/All tabs, `q` search and the task detail/history/brief pages still show the closed row, so slice 0 filters it explicitly (3.2, 6.0). |
| Key | Immutable lowercase dotted key, unique per project, mirrored in a controlled `reference-key:` label for lookup. |
| Versioning | Monotonic revision per key; `ref revise` must state the exact next `revision` and the content hash it expects to replace (`expected_sha256`). Same key/revision with different content is refused. No hash chain. |
| Retirement | One operator operation, `ref retire KEY --successor KEY2`, writes the superseded revision. `replaces` is derived at read time; there is no asymmetric window. |
| Owner | A durable person/account identity (`account:<uid>` or `person:<name>`), never a session actor (3.4). |
| Authority | Contributors draft and revise; only the deployment operator allowlist accepts, with F3 acceptance evidence bound to the record hash (4). |
| Reads | `ref get`, `ref list`, `ref decisions`, `ref check` (read-only); writes `ref propose`, `ref revise` (contributor), and operator `ref retire` / acceptance. |
| Attention | Additive project-level `attention` block in `work` (counts always, items only for the entry owner or an approver) and a bounded `attention` array in `brief` (at most 3, trust-marked); reading changes nothing. |
| Repo sync | Pointer mode is the **default** (entries point at repo paths; no generated file). Export is opt-in per project, writes a **self-verifying** file (catalog digest and body digest in its header) and needs no sidecar journal. |
| Backup | Entries are native comments, so the native backup covers them; the only new local journal is `.reference-requests/`, which joins the coordination sidecar. |
| Rollback | Staged release. Slice 0 is a **tolerant reader** (reserve, whitelist, filter, no writes) and is the oldest kit a catalog deployment may roll back to (10.3). |
| Migration | `ref import` from `REFERENCE.md` creates **draft** entries only, and is deferred to a later slice (11). |
| Implementation now | None. This document only. |

## 3. Storage model

### 3.1 Why a reserved machine-record kind

`requirements.py:34` already has a field named `REFERENCE_FIELDS`
(`id`, `revision`, `sha256`), but that is **not** a reference catalog: it is an
in-manifest content pointer resolved against the manifest's own record index
(`requirements.py:147`). It answers "which exact revision of this record does
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
second storage mechanism, and revision 2 extracts the shared machinery instead
of copying it (section 12.1).

### 3.2 Native anchor: one closed issue per entry

Each entry is one native issue, created by the dedicated `ref propose` operation:

- native issue type `task` (the requirement-record precedent uses the ordinary
  issue type; the controlled labels, not the type, identify the record);
- **status `closed`**, set by the operation before the first revision comment;
- controlled type label `reference`;
- controlled state label, exactly one of `reference:draft`,
  `reference:accepted`, `reference:superseded`;
- controlled lookup label `reference-key:` + the key with `.` replaced by `-`;
- the idempotency labels the requirement records already write at create time,
  kept unchanged: `request:` + the request identity and `request-content:` + the
  content digest. They are not decoration: the native-first path finds a record
  created by an earlier or uncertain write by the `request:` label
  (`requirement_records.py:707-712`) and `requirement_records.reconcile` finds it
  the same way (`:953`), so a catalog that dropped them could not reconcile an
  uncertain create. The catalog keeps both labels alongside the
  `.reference-requests/` receipt.

The anchor is created closed so that **the work surfaces, including an older kit
that has never heard of the catalog, already hide it** - but closed status alone
does **not** hide it everywhere, and revision 2 overstated that:

- `work.py:175` filters by `issue_type` (`event`, `gate`, `merge-slot`), not by
  label, so a label filter would not have helped; `work.py:180` already skips any
  closed row whose review state is not one of
  `changes-requested`/`awaiting-review`/`awaiting-integration`/`legacy-review-ready`/`error`,
  and a freshly written anchor has no review state at all;
- the canonical queue drops a closed row with no active review
  (`http_service.py:664-680`, and the backend rule at `:1220`); `/v1/me/work`
  (`http_service.py:2494`) consumes that queue; and `agent_prompts.classify`
  (`agent_prompts.py:104-136`) never places a closed row in a work class;
- **but the HTTP task list reads the whole project**: `read_tasks` is
  `bd list --all --limit 0 --json` (`http_service.py:1006-1018`), and `tasks_list`
  applies only the caller's own `status`/`review_state`/`assignee`/`q` filters
  (`http_service.py:2318-2341`). So the web project page's **Closed** and **All**
  tabs and its `q` search (`web/js/views/project.js:28`) show the anchor as an
  ordinary closed task;
- `GET /tasks/{id}` (`http_service.py:2359-2363`),
  `GET /tasks/{id}/history` (`http_service.py:2451-2469`) and
  `GET /tasks/{id}/brief` (`http_service.py:2365-2395`) apply no catalog filter
  at all, so an anchor id returns the row and its history **including the raw
  `Kind: reference-*` comments**, which the task page renders as plain prose
  (`web/js/views/task.js:120-123`).

Closing the anchor is therefore necessary but not sufficient. It covers `work`,
the queue, `/v1/me/work`, the agent prompts and every kit older than slice 0 at
once - and it needs no new filter on any historical version - while slice 0's
explicit filter (6.0) covers the HTTP task list, the task detail/history/brief
routes and the web. A tolerant kit filters; a kit older than slice 0 shows only a
closed task and its raw comments, never an open claimable one.

**Verified: `bd 1.2.2` accepts comments and labels on a closed issue.** The pin
is `versions.json:3-8` (`bd 1.2.2`, sha256 `8140098a…`) with the same pin stated
in `README.md:5`. Against the installed pinned binary
(`bd version 1.2.2 (6c124203e)`) and a disposable database created with
`bd init --non-interactive` under a temporary directory, the sequence
`create` -> `close` -> `comments add` -> `update --add-label probe:x` returned
exit code 0 for both writes and left the issue with
`labels=['probe:x'] status='closed'` and the comment present. No live
installation was touched. `bd create` has no `--status` flag, so the operation
creates the anchor and closes it explicitly before writing the first revision
comment; both steps happen inside the one locked operation, so no reader ever
sees an open anchor.

What the tolerant reader (slice 0, section 10.3) must filter even before any
writer exists is listed in 6.0.

### 3.3 Key

A key is an immutable lowercase dotted slug, at most 80 characters:

```text
^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$
```

Examples: `calendar.trading`, `identity.authority`, `settlement.instant`. Keys are
unique per project. Key changes are refused for an existing entry: a different
key is a different entry, related by retirement (3.5).

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
  "owner": "account:u-0001",
  "review_by": "2027-01-15",
  "tags": ["calendar", "data"],
  "decisions": ["example-project-42"],
  "acceptance_state": "draft",
  "successor": null,
  "origin": {"type": "authored"},
  "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
}
```

Field rules:

| Field | Rule |
| --- | --- |
| `schema_version` | integer `1`. |
| `key` | 3.3, immutable. |
| `revision` | positive integer, exactly the next revision for the key. |
| `title` | nonempty, <= 200 characters. |
| `statement` | nonempty, <= 2000 characters; state the fact, not the evidence. Untrusted text (section 8). |
| `authority` | exactly one closed object, `repo-path` or `url` (3.6). |
| `owner` | durable person/account identity, `account:<uid>` or `person:<name>`; a session actor string is refused at write. Attribution, not authenticated identity, but durable enough to route attention and attribute contributor statistics. |
| `review_by` | `YYYY-MM-DD`, real calendar date. Required for an accepted revision, optional for a draft. |
| `tags` | 0..12 unique lowercase slugs `^[a-z][a-z0-9-]{0,31}$`. |
| `decisions` | unique native decision issue IDs (3.9). |
| `acceptance_state` | `draft`, `accepted` or `superseded`; written by the operation, never caller-supplied. |
| `successor` | `null` for every revision except a retirement revision, which names the key that replaces this one. Set only by `ref retire`. |
| `origin` | `{"type":"authored"}` or `{"type":"import","path":"REFERENCE.md","digest":"<64 hex>"}`. |
| `sha256` | content hash of every other field. |

There is deliberately **no** `supersedes`, `replaces` or `replaced_by` field
(3.5), and no `acceptance` object in the record: acceptance evidence is a
separate reserved comment (4).

The owner identity rule is concrete:

- `account:<uid>` names a project member account in an office deployment - the
  durable HTTP canonical user id (`docs/HTTP_TRANSPORT_DESIGN.md:103`: accounts
  have stable opaque user IDs and display names). This is the preferred form and
  the one `.58` statistics attribute against.
- `person:<name>` names a person-level actor for a project with no account
  registry. It must not contain a session marker.
- A value that is, or contains, a session actor is **refused at write**. The
  kit's real session actors are `session-<uuid>`: `sessions.validate` requires
  `actor.startswith('session-')` with a UUID suffix (`sessions.py:19`), and
  `sessions.register` allocates exactly `'session-' + str(uuid.uuid4())`
  (`sessions.py:170`). The refusal matches that shape - and the legacy `/session`
  or `name/sessionN` markers older clients wrote - rather than `name/sessionN`
  alone, which would have let `person:session-4e40...` through. Sessions are
  ephemeral, so a session owner could never receive routed attention and could
  never be attributed in a statistic.
- Shape and the session refusal are enforced on every write. `account:`
  membership in the project is resolved where a member directory is reachable
  (the office deployment); where it is not, the write is accepted and `ref check`
  reports `owner-unresolved` as a warning. The record notes the resolution rule;
  it never claims the owner string is authenticated.

### 3.5 Versioning, revision expectation and retirement

Revisions are append-only and never rewritten. A meaningful change - including a
change to `acceptance_state`, a `review_by` change, or an authority move - is a
new revision with a new hash. Reposting identical bytes for the same key and
revision is tolerated (idempotent); conflicting bytes for the same key and
revision are refused, which is the same rule `export_requirements.check_history`
applies to requirement revisions.

Revision 2 replaces the revision hash chain and the two-step
`replaces`/`replaced_by` acceptance with two simpler mechanisms, as reviewed:

- **Expected revision (compare-and-swap).** `ref revise` must state `revision`,
  exactly the next revision for the key, and `expected_sha256`, the content hash
  of the newest existing revision it believes it is replacing. A mismatch in
  either is refused before any native write. The revision ledger on a single
  native issue is already ordered by comment order, so a per-revision
  `supersedes {id, revision, sha256}` pointer added nothing but a second
  invariant to validate. This is the rule `requirement_records.revise` uses
  (`requirement_records.py:568` `_check_revision`).
- **One retirement operation.** `ref retire KEY --successor KEY2` (operator
  only) writes exactly one revision of `KEY` with `acceptance_state:
  "superseded"`, `successor: "KEY2"`, and the acceptance evidence for the
  retirement. `replaces` is **derived at read time**: `ref get KEY2` reports
  `replaces: ["KEY", ...]` by scanning the catalog for retirement revisions whose
  `successor` is `KEY2`. There is no window in which one half of a pair exists
  without the other, so the `asymmetric-supersession` check of revision 1 is
  gone; the replacement's own content is unchanged by the retirement.
- `ref get KEY` on a retired key returns the retired revision plus a `resolved`
  pointer to `successor`, and never loops: a chain longer than 8 hops or a cycle
  is reported as a warning and the walk stops at the last well-formed record.
- `ref check` checks the derived relation instead: `retirement-target-missing`
  (a `successor` naming no key) and `retirement-cycle`.

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
- `url`: `url` must be `https://`, must not carry userinfo, and is at most 2048
  characters; `retrieved` is the date a human last verified the page and must not
  be later than today. The kit makes no network request: `ref check` reports a
  `url` authority whose `retrieved` date is older than the entry's `review_by`
  window as a warning, and never fetches it.

The server never reads a repository path. Authority resolution is a client/operator
concern (`ref check`, section 9), which is why an entry can be accurate without the
coordination server having the project checkout.

### 3.7 Reserved namespace: guards, labels and malformed records

**Reserved comments.** Implementation adds two prefixes to
`reserved_comments.RESERVED` (`reserved_comments.py:164-174`, with `PREFIXES`
derived at `:176`):

- `Kind: reference-entry-v1` -> writer `ref propose|revise`;
- `Kind: reference-acceptance-v1` -> writer `admin.py reference-apply`.

Raw `comments add` of either prefix is refused on the contributor endpoint, in
every pflag/attachment ordering the existing guard already normalizes, so the
dedicated operations are the only writers.

**Reserved labels (review item `reserved-labels`).** Reserving only the comment
prefixes is not enough. Revision 1 left the label namespace open, so a
contributor could run `update X --add-label reference:accepted` on an anchor, or
inherit an accepted-looking label through `create --parent`, and forge a row that
reads accepted to every reader. This is exactly the fix requirements needed
(kittrial-pth.26) and it is applied the same way, through the one shared
predicate:

```python
# reserved_comments.py, replacing :654-655
RESERVED_LABEL_PREFIXES = ('request:', 'request-content:', 'requirement:',
                           'reference:', 'reference-key:')
RESERVED_EXACT_LABELS = frozenset({'requirement', 'brd-section', 'reference'})
```

All three additions are required and none implies another: `reference:` is a
prefix, `reference-key:` is a **separate** prefix (it does not begin with
`reference:`), and `reference` is an exact type label. This mirrors requirements
exactly (`requirement` exact, `requirement:` prefix, `brd-section` exact).

One namespace feeds both checks (`reserved_comments.py:645-651`), so extending it
closes both routes at once:

- `reserved_label_in_args` (`reserved_comments.py:752`) inspects every
  label-writing spelling in `LABEL_WRITE_FLAGS` (`:667-670`, verified against
  bd 1.2.2 including the undocumented `create --label` alias) and refuses
  `update X --add-label reference:accepted`, `--set-labels`, `--remove-label`
  and the `create` spellings before any native write;
- `first_reserved_label` (`reserved_comments.py:764`) is the read-before-write
  guard used by `endpoint._guard_reserved_labels` (`endpoint.py:64`), which
  refuses `create --parent X` when the parent already holds a reserved label -
  bd inherits parent labels unless `--no-inherit-labels` is given - and refuses
  `update X --set-labels` that would move the namespace off a record that
  currently holds it.

An unrecognized `create`/`update` flag still fails closed through
`unresolved_bd_flags` (`endpoint.py:225`), so a future hidden bd alias cannot
silently reopen the route. The design records no new label mechanism: it adds
three names to the existing namespace and nothing else.

**Malformed records fail per entry.** A comment that claims one of these prefixes
but fails schema validation makes **only that entry** read as `state: malformed`;
it must never fail `ref list`, `work` or `brief` for the whole project (7.3).
Repair is the existing operator `void-record` path; nothing is deleted.
kittrial-5bb.74 adds the reference (and capability) record kinds as void targets, with
two rules the review voids already follow:
- a void applies only to a comment the entry cannot read: malformed (a BOM or CRLF
  lookalike included), another anchor's or key's, or the later holder of a revision an
  earlier comment already holds. The earliest holder is never voided, because the writer
  never writes a second one, and a void cannot itself be voided. A well-formed record the
  entry reads is refused at write and ignored on read, because a void repairs history
  and never withdraws a decision; replacing an entry is a new revision or a retirement;
- readers and writers both leave out a voided comment, so `ref revise` sees what
  `ref get` shows.

An anchor left with no live record, by an interrupted propose whose payload is lost or
because every record it held is voided, is closed and its key freed by the operator's
`admin.py anchor-release` (`docs/OPERATIONS.md`, "Orphan anchors"). A reader that meets an unknown newer `Kind: reference-entry-v2`
comment reports that entry as `unsupported` and leaves it visible to an operator,
rather than failing the read (3.8).

### 3.8 Forward compatibility

The field set is closed, so under revision 1 every future field would have been a
5bb.44-style break. State the rule now:

- a new field means a new kind version, `Kind: reference-entry-v2`, with its own
  closed field set and validator;
- a reader that meets a `Kind: reference-entry-vN` comment for an unknown `N`
  marks that one entry `unsupported`, reports it, and keeps reading the entries
  it does understand; it never fails the whole catalog;
- an older reader meeting a v2 comment therefore loses one entry, not the
  catalog - the same tolerant-reader principle as 10.3, applied at the record
  level.

### 3.9 Relation to requirements.py and BRD/decision traceability

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
  backlink scanner (`render.py:33`) and the review workflow are untouched.
- **Impact views.** An impact selection's `context_ids` already names decisions
  and discussions to retain alongside selected requirements
  (`export_requirements.selection_from_manifest`); a catalog key is not added
  there in this design, because that would require touching the impact contract.
  `requirement_impact.py` stays requirements-specific: it validates manifests and
  walks a work graph (`requirement_impact.py:23`), and the catalog does not feed
  it.

### 3.10 Relation to the agent registry, the HTTP service and the web interface

At this base commit, the integrated personal-agent work is the owner-bound agent
registry plus the office web interface slice 1 (kittrial-5bb.20). Three
corrections to revision 1 belong here:

- **Acceptance authority is the deployment operator allowlist, not
  `requirement-apply`.** Revision 1 said acceptance "reuses the operator
  allowlist used by `void-record` and `requirement-apply`". That is false on this
  base and is corrected in section 4.
- **The web interface is not "in progress" with a plan to add a propose write.**
  Revision 1's "in progress" note and its planned web write are removed. Slice 1
  exposes references as **HTTP reads only** (section 8). There is no separate
  `http_web.py` module: the browser client is static assets under `web/` served
  by `http_service.py` (`DEFAULT_WEB_ROOT` at `:91`, the `STATIC_*` allowlists at
  `:93-148`). The read routes and the rendering rules are specified in section 8
  and reuse the file's existing route and escaping conventions.
- **The catalog's `owner` field is attribution, not authority.** Where an actor
  is an agent, the actor string is the agent identity the registry already
  issues; the catalog treats it as attribution, exactly as every other record
  does, and the registry's owner-bound cap is enforced by the existing HTTP
  authority layer, not re-implemented here. Attention routing uses the durable
  `owner` identity (3.4), not the agent credential.

## 4. Authority: who writes and who accepts

Revision 2 picks **one** authority explicitly, as reviewed: acceptance requires
the **deployment operator allowlist**, the same source `void-record` enforces.

- **Where it lives and how it is configured.** The allowlist is the `operators`
  list in `deployment.private.json` on the coordination host. `admin.operators`
  (`admin.py:262-294`) is the reader and the single authority source; a
  deployment that configures no operators authorizes nobody. `ORCHESTRA_OPERATORS`
  is **not** an authority source, and a host write command with `strict=True`
  refuses when the shell variable disagrees with the configuration
  (`admin.py:288-293`). The list is maintained with
  `admin.py operators add|remove|list` (`admin.py:2246-2269`), and
  `docs/OPERATIONS.md:58-66` documents it for coordinators.
- **Why not `requirement-apply`.** On this base `requirement-apply` is
  shell-trusted only: `admin.py:2214-2223` calls
  `apply_native(payload, args.actor, run, path, operator=True)` and never reads
  the allowlist. `docs/OPERATIONS.md:61-63` states it plainly: "All five are
  shell-trusted: access to the service account's shell is the boundary. Only
  `void-record` also checks the deployment operator allowlist". By contrast
  `void-record` (`admin.py:2234-2245`) calls `operators(root, strict=True)` and
  passes that set into `apply_void`. A reference statement is instruction-grade
  text that agents will act on, so the catalog adopts the allowlist and the
  requirement-apply gap is recorded as a separate follow-up (15, "later").
- **What happens when the accepting operator is removed.** `admin.py:2262-2265`
  already states the policy for voids: `operators remove ACTOR --confirm-revoke`
  warns that "voids they authored stop applying on reads (re-add restores them)".
  References adopt the same single, reversible policy. The acceptance evidence
  comment stays in native history forever; the **reader** requires the stored
  native author of the evidence comment to be on the live allowlist. After a
  removal:
  - the entry no longer reads accepted: `ref get` returns
    `state: draft-only`, `acceptance: null` and `acceptance_inert: true` with the
    removed operator named;
  - `ref list` marks it `draft-only`, and the review-by item is **replaced, not
    silently dropped**: the entry raises an **`acceptance-inert` attention item**
    to approvers - the live allowlist - with a project-level count and the removed
    operator named (7.1). An entry whose authority went inert must never go quiet:
    an expired authority that suppresses its own reminder is worse than one that
    never had a review date, because the silence looks like health. The same item
    is raised after a restore that leaves the accepting operator off the host
    allowlist (below);
  - recovery is deliberate and cheap: re-add the operator with
    `admin.py operators add`, and the same acceptance evidence applies again with
    no rewrite; or accept the entry again with a currently listed operator, which
    writes a new acceptance evidence record.
  Nothing is deleted in either direction, so this is the same restorable
  behaviour as a void, not a new authority source.
- **Restore does not re-grant.** `docs/OPERATIONS.md:174` and `:183`: the
  allowlist is deployment-wide, `restore-new` leaves it untouched, and the
  operator re-grants it explicitly with `--restore-operators` when the whole
  recorded list is intended. A restored project therefore reads an entry whose
  accepting operator the host does not list as `acceptance_inert: true`, and that
  entry raises the same `acceptance-inert` attention item to approvers rather than
  disappearing from review-by attention.

The rest of the split mirrors `requirement_records.py`'s F3 split, because that is
the kit's reviewed answer to "contributor proposes, operator accepts":

| Action | Who | Evidence written |
| --- | --- | --- |
| Create a new entry as `reference:draft` (`ref propose`) | any contributor actor | one `reference-entry-v1` revision, `acceptance_state: draft` |
| Revise an entry's draft (`ref revise`) | any contributor actor (trusted team) | next `reference-entry-v1` revision, `draft` |
| Accept an entry (`admin.py reference-apply`) | a deployment operator: an actor on the allowlist, with shell access. A project owner accepts only in that form - ownership alone is not acceptance authority | next revision with `acceptance_state: accepted` **plus** `reference-acceptance-v1` evidence bound to that revision's `record_sha256` |
| Retire a key (`ref retire KEY --successor KEY2`) | operator only | one revision with `acceptance_state: superseded`, `successor`, plus acceptance evidence |
| Propose a change to an accepted entry | any contributor | a higher draft revision; the **accepted** pointer does not move until an operator accepts |
| Set or change `review_by` | any contributor proposes; operator accepts | takes effect only on acceptance; a draft's date is shown as proposed, never as the effective deadline |

**Two identity spaces, never mixed.** A record's `owner` is the durable
`account:`/`person:` attribution identity (3.4). Acceptance authority is a
different thing: an **actor string** from the deployment allowlist.
`recovery.identity` (`recovery.py:50-53`) accepts only
`[A-Za-z0-9][A-Za-z0-9_.-]{0,160}`, so a colon is not even a legal character in
an allowlist entry: `account:u-0009` can never be an operator, and revision 2's
example that wrote it as one mixed the two spaces. The `operator` reported by
`ref get` (6.1) is that allowlisted actor string, e.g. the placeholder
`operator-0009` (13); it is taken from the stored **native author** of the
evidence comment, never from a caller-supplied string, because the F3 evidence
object has no `operator` field (`requirements.ACCEPTANCE_FIELDS`,
`requirements.py:35-42`). `admin.py operators list`
(`admin.py:2252`) prints the exact strings. A project owner is therefore not an
approver by virtue of ownership: they accept only after an operator adds their
actor to the allowlist with `admin.py operators add`, and only with shell access
to the coordination host - the same boundary `docs/OPERATIONS.md:61-66` states.

Acceptance evidence reuses the existing F3 shape (`requirements.ACCEPTANCE_FIELDS`
minus `manifest_sha256`, plus `record_sha256` - `requirement_records.py:81-82`),
the existing policies `any-owner` / `all-owners`, and the existing subset rule
(`approvers` must be a subset of `owners`; `all-owners` requires identical sets).
`decision_id` and `evidence` are required, so an acceptance always points at a
recorded decision and a durable evidence pointer. The acceptance record is
written **before** the accepted revision and label move, so an uncertain write can
never leave an entry that reads accepted with no evidence - the ordering
`requirement_records.py:55-58` already documents.

A payload's own `operator`/`owner` string is never authority; the deployment
allowlist is, exactly as `docs/OPERATIONS.md` states for void records. Agent and
worker credentials are never approvers (section 8).

## 5. What authority means to a reader

`ref get` returns both halves so no reader can mistake a proposal for authority:

- `record`: the newest revision whose `acceptance_state` is `accepted` (or
  `superseded`, for a retired key) and whose acceptance evidence author is still
  an allowlisted operator, or `null` if the key has only drafts;
- `proposed`: the newest `draft` revision, or `null`;
- `state`: `accepted`, `superseded`, `draft-only`, `malformed` or `unsupported`;
- `acceptance`: the operator evidence for `record`, or `null`;
- `acceptance_inert`: `true` when an acceptance exists but its operator is no
  longer allowlisted (4), naming the operator;
- `warnings`: bounded, per-entry warnings such as `authority-changed` (9) and
  `retirement-cycle` (3.5).

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

### 6.0 Surfaces that must hide catalog rows

Even with closed anchors (3.2), the tolerant reader (slice 0) and the writer
slice must each guarantee the same list, and tests must assert it on every
version. A row is a catalog anchor when it carries the `reference` label, and a
comment is a catalog comment when its body starts with `Kind: reference-`.
Catalog rows and comments must never appear in:

1. `work` / `work --mine` (`work.py:174-193`);
2. the HTTP task list `GET /v1/projects/{pid}/tasks` (`http_service.py:2318`);
3. `GET /v1/projects/{pid}/queue` (`http_service.py:2472`);
4. `GET /v1/me/work` (`http_service.py:2494`);
5. the agent prompts built by `agent_prompts.classify`
   (`agent_prompts.py:104-136`);
6. rendered task views (`render.py`, the review-queue projection at
   `render.py:36-47`);
7. `GET /v1/projects/{pid}/tasks/{id}` for an anchor id
   (`http_service.py:2359-2363`);
8. `GET /v1/projects/{pid}/tasks/{id}/history` (`http_service.py:2451-2469`);
9. `GET /v1/projects/{pid}/tasks/{id}/brief` (`http_service.py:2365-2395`).

Surfaces 7-9 have no catalog filter at all today: an anchor id returns the row
and its history, including the raw `Kind: reference-*` comments, and the web task
page renders the history body as prose (`web/js/views/task.js:120-123`). Slice 0
and slice 1 must therefore make each of the three **return 404 for a row carrying
the `reference` label, or return a pointer to the reference route**
(`ref get <key>`), never the raw record; the browser task page follows the same
rule. The HTTP task list (surface 2) keeps the closed row out of the Closed/All
tabs and `q` search by filtering on the label.

Slice 0 filters on all nine even though it writes nothing, because a newer kit in
the same deployment can create entries that a tolerant-but-older kit reads, and
because `create-child` can create a labelled row without the reference
operation. Closed status alone is the backstop for kits older than slice 0; the
explicit filter is what a tolerant kit promises.

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
    "owner": "account:u-0001",
    "review_by": "2027-01-15",
    "tags": ["calendar", "data"],
    "decisions": ["example-project-42"],
    "successor": null,
    "sha256": "2222222222222222222222222222222222222222222222222222222222222222"
  },
  "record_comment_id": "01a00000-0000-7000-8000-000000000000",
  "acceptance": {
    "decision_id": "example-project-42",
    "owners": ["account:u-0001"],
    "approvers": ["account:u-0001"],
    "policy": "any-owner",
    "evidence": "decision example-project-42",
    "record_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "operator": "operator-0009",
    "at": "2026-09-27T20:00:00Z"
  },
  "acceptance_inert": false,
  "replaces": [],
  "proposed": null,
  "due": "ok",
  "resolved": null,
  "warnings": [],
  "coverage": "newest accepted revision and its acceptance evidence; unresolved drafts are returned in proposed"
}
```

`title` and `statement` are excerpt objects (`{"text", "omitted_chars"}`) under
the `brief`/`history` contract; `sha256`, IDs and cursors are never excerpted.
`acceptance.operator` is the allowlisted **actor string** whose shell command
produced the evidence - the native author of the evidence comment, not the
record's `owner` and not an `account:` identity (4); the placeholder above is
`operator-0009` for that reason. `due` is `ok`, `due-soon`,
`expired` or `unset` (7.2). Unknown key: nonzero exit,
`stderr` names the key and suggests `ref list`. A malformed or unsupported entry
returns that entry with the matching `state` and a bounded `warnings` entry; it
does not fail the command for other keys.

### 6.2 `ref list`

```sh
b ref list --tag data --state accepted --due expired --limit 20 --json
```

```json
{
  "total": 2,
  "items": [
    {"key": "calendar.trading", "title": "Trading calendar authority", "state": "accepted",
     "owner": "account:u-0001", "review_by": "2026-01-15", "due": "expired",
     "tags": ["calendar", "data"], "revision": 3, "native_id": "example-project-17"},
    {"key": "feed.units", "title": "External feed units", "state": "draft-only",
     "owner": "person:bob", "review_by": null, "due": "unset",
     "tags": ["data"], "revision": 1, "native_id": "example-project-23"}
  ],
  "next_offset": null,
  "coverage": "one row per key: newest accepted/superseded revision, or newest draft when no revision is accepted; 1 entry skipped as malformed"
}
```

Options: `--tag TAG` (repeatable, AND), `--owner IDENTITY`, `--state
draft-only|accepted|superseded|all`, `--due expired|due-soon|unset`, `--limit`
1..100, `--offset` >= 0. Ordering: expired, then due-soon, then unset, then key.
Long text clips at 200 characters like `work` rows. A malformed entry is reported
in `coverage` and never fails the page (7.3).

### 6.3 `ref propose` / `ref revise`

A closed payload; `operation` comes from the subcommand, and
`acceptance_state`, `successor`, `labels`, `acceptance` and `sha256` are refused
if caller-supplied:

```json
{"schema_version": 1, "operation_id": "alex-ref-1",
 "key": "calendar.trading", "title": "Trading calendar authority",
 "statement": "Every chart derives holidays and early closes from the pinned calendar module.",
 "authority": {"type": "repo-path", "path": "src/example/calendar.py",
               "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"},
 "owner": "account:u-0001", "review_by": "2027-01-15", "tags": ["calendar", "data"],
 "decisions": ["example-project-42"], "revision": 1,
 "expected_sha256": null}
```

```sh
b ref propose --file entry.json --json
b ref revise  --file entry.json --json
```

- `propose` creates the closed anchor and revision 1 as `reference:draft`, or is
  refused if the key already exists.
- `revise` requires `revision` to be exactly the next revision and
  `expected_sha256` to be the content hash of the newest existing revision, and
  is bound to the key: a key swap is refused before any native write.
- Refusals: duplicate key, key-swap, stale `expected_sha256` or wrong `revision`,
  unknown decision link, session-actor `owner`, `review_by` in the past on a
  **new** accepted proposal (allowed on a draft, since drafting a correction for
  an overdue entry is the normal repair), `repo-path` authority with an
  absolute/`..` path, a non-`https` or userinfo `url`, and a `url` authority in a
  project whose policy refuses external authorities.
- Idempotency: `operation_id` plus a `.reference-requests/` receipt journal and
  the `request:`/`request-content:` labels on the created anchor (3.2), the same
  native-first pattern as `.requirement-requests`: preflight reads before the
  receipt is written, and an uncertain real write leaves a pending receipt
  reconciled from native state - found by its `request:` label, exactly as
  `requirement_records.py:953` does - rather than creating a duplicate.
- Returns `{"key", "revision", "native_id", "record_comment_id", "state":
  "draft", "reconciled": false}`.

### 6.4 `ref retire KEY --successor KEY2`

One operator operation, replacing revision 1's two-step acceptance:

```sh
b ref retire calendar.trading --successor calendar.trading-v2 --file acceptance.json --json
```

It writes exactly one revision of `KEY` with `acceptance_state: "superseded"`,
`successor: "KEY2"`, the unchanged content fields, and the `reference-acceptance-v1`
evidence for the retirement. `replaces` is derived at read time (3.5). Refused
for a non-operator actor, for a missing/unknown successor key, for a successor
that would create a cycle, and for a `KEY` that is already retired. Because both
halves are written by one operation, no asymmetric pair can exist and `ref check`
has no asymmetry case to detect.

### 6.5 `ref decisions DECISION-ID`

```json
{"decision": "example-project-42", "entries": [{"key": "calendar.trading", "state": "accepted", "revision": 3}], "total": 1}
```

Read-only reverse view over the catalog. It never writes the decision.

### 6.6 `ref check`

```sh
b ref check --repo /path/to/project-checkout --json
```

```json
{
  "checked": 8,
  "failures": [
    {"key": "settlement.instant", "code": "authority-missing",
     "detail": "src/example/ledger.py not found at commit 0000000000000000000000000000000000000000"}
  ],
  "warnings": [
    {"key": "calendar.trading", "code": "authority-changed-since-pinned",
     "detail": "src/example/calendar.py#HOLIDAYS differs at HEAD from pinned commit 0000000000000000000000000000000000000000; review_by 2027-01-15"},
    {"key": "identity.authority", "code": "url-not-reverified",
     "detail": "url authority retrieved 2025-01-01, review_by 2026-01-15"},
    {"key": "feed.units", "code": "owner-unresolved",
     "detail": "account:u-0042 is not a current member of this project"}
  ],
  "coverage": "offline read-only checks against the supplied checkout; no network access"
}
```

`ref check` runs on a machine that has the project checkout, never on the server,
and makes no network request. Nonempty `failures` exits nonzero; warnings do not.
Checks:

- `repo-path` path and anchor present at the recorded commit;
- **`authority-changed-since-pinned`** (review item `drift-check`): the anchored
  content at HEAD differs from the same content at the pinned commit. This is the
  real drift signal and the stale-calendar case;
- `url` `retrieved` within the review window;
- `review_by` present on accepted entries;
- `retirement-target-missing` and `retirement-cycle`;
- `owner-unresolved`;
- `decisions` links still resolving;
- malformed and unsupported reserved records (reported, not repaired).

### 6.7 Operator acceptance and reconcile

```sh
python3 admin.py --root <runtime> reference-apply --file acceptance.json
```

```json
{"schema_version": 1, "operation_id": "owner-apply-1",
 "key": "calendar.trading", "revision": 3,
 "record_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
 "acceptance_state": "accepted",
 "acceptance": {"decision_id": "example-project-42", "owners": ["account:u-0001"],
                "approvers": ["account:u-0001"], "policy": "any-owner",
                "evidence": "decision example-project-42"}}
```

Refused for an actor outside the deployment operator allowlist (4), for a hash
that does not match the named revision, for a missing decision/evidence pointer,
and for a policy violation. `reference-apply` with `operation: "draft",
acceptance_state: "accepted"` writes a **direct accepted revision 1**, the
operator route requirements gained in kittrial-5bb.56
(`docs/REQUIREMENTS_INTEGRATION.md:162-199`), so an owner writing an obviously
true entry does not need propose-then-apply.

`reference-reconcile` finishes a `reference-*` operation whose real write was
uncertain, exactly as `requirement-reconcile` does today
(`admin.py:2224-2233`, `docs/REQUIREMENTS_INTEGRATION.md:258-271`). In the shared
core (12.1) both become one generalized `record-reconcile`; the
`reference-reconcile` spelling is kept for operators.

## 7. Attention: review-by in `work`, `brief`, My work and prompts

### 7.1 Where it appears, and where it must not

`attention` does not exist in `work.py` or `briefing.py` today, so this is new
ground there. It does already exist in the HTTP service for the agent registry:
`http_service._agent_attention` (`http_service.py:2164-2239`) returns
`{'state','summary','counts','actions','truncated','computed_at'}`, and the
catalog reuses that shape rather than inventing a second one (12.2).

A reference entry is **not** a task: it has no owner-assignee, no review state and
no lifecycle. The design therefore adds a **project-level** `attention` block to
`work` (not a per-task field, not a synthetic queue row) and a bounded `attention`
array to `brief` (tasks stay the unit of a brief).

`work` shape, additive:

```json
{"owner": null, "total": 12, "items": [], "next_offset": null,
 "coverage": "Fresh current view; ...",
 "attention": {
   "reference_review": {
     "acceptance_inert": 1,
     "expired": 1, "due_soon": 1, "unset": 2, "total": 4, "truncated": false,
     "items": [
       {"key": "calendar.trading", "review_by": "2026-01-15", "due": "expired",
        "owner": "account:u-0001", "state": "accepted", "revision": 3}
     ],
     "next_offset": null
   }
 }
}
```

Routing rules (review item `owner-identity-attention`):

- `work` **always** returns the project-wide counts (`expired`, `due_soon`,
  `unset`, `total`) so a filter on the task queue can never hide an expired
  authority. It returns full `items` only when the caller is the entry's `owner`
  or an approver; otherwise `items` is empty and `truncated` is true with a
  coverage note naming the count.
- `brief` shows at most 3 items, deterministically: entries whose tags match the
  task's own labels, plus expired and due-soon entries, expired first, with
  `attention_total`/`attention_more`.
- **My work** (`GET /v1/me/work` and the browser view) shows expired and due-soon
  entries to the entry owner and to project owners, and counts to everyone else.
  **This is a slice-2 deliverable, not slice 1** (15): slice 1 ships the `work`
  counts/items and the `brief` block, and the My work and prompt-count promises in
  this list arrive with slice 2 and its own tests.
- **Agent prompts** get counts and keys only, and only for approvers
  (`agent_prompts.classify` already tailors by `CAP_APPROVE`/`CAP_TASKS`,
  `agent_prompts.py:100-136`). No statement text ever reaches a prompt (8). These
  prompt counts are slice 2 as well.
- **The SSH/endpoint route has no roles.** The endpoint sees actors, not project
  roles, and its session actors are `session-<uuid>` (3.4), so on that route
  "approver" means exactly: the caller's actor is on the deployment operator
  allowlist (`recovery.configured_operators`, `recovery.py:56-86`, fed from
  `deployment.private.json` `operators`). There is no role lookup to consult and
  no session-derived ownership: an entry's owner is matched **only** through
  `ref list --owner IDENTITY`, which filters on the durable `owner` field. A
  session actor is never treated as an owner or an approver.
- **`acceptance-inert`.** When an entry's acceptance evidence author is no longer
  on the live allowlist - an operator was removed, or the project was restored
  without `--restore-operators` (4) - the block adds an `acceptance_inert` count
  and, to approvers, an item naming the key and the removed operator. It is
  recomputed on every read like the due classes, and it is never suppressed: the
  review-by items for such an entry are reported under `acceptance_inert` rather
  than silently dropped.

New bounded options `--ref-limit` (1..100) and `--ref-offset` (>= 0). The block is
computed regardless of `--owner`/`--state`/`--mine` task filters, because the
reference catalog is project-wide. Only accepted (and superseded, flagged)
entries appear; `draft-only` entries appear as `unset`-class proposals only when
`--state draft-only` is requested.

`brief TASK` shape, additive:

```json
{"attention": [
  {"kind": "reference-review", "key": "calendar.trading", "due": "expired",
   "review_by": "2026-01-15", "trust": "accepted",
   "text": "Authority for the trading calendar is past its review date.",
   "source": "ref calendar.trading"}
],
 "attention_total": 1, "attention_more": null}
```

- `kind` is the new value `reference-review`. The checkpoint unresolved-item
  vocabulary (`blocker`, `question`, `decision`, `correction`, `dependency`,
  `briefing.py:13`) is **not** extended: those are author-declared open items, and
  a computed date reminder must not masquerade as one.
- `trust` is `accepted` or `draft` and is always present when a brief carries
  entry text (8). An item built only from an accepted entry carries
  `trust: "accepted"`; a draft excerpt is framed as untrusted.
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

### 7.3 Failure isolation

A malformed entry must fail **only itself**. The rule is concrete:

- every catalog read is per entry: one unparseable revision comment, one bad
  label, an unresolvable `owner`, a duplicate key, an unknown
  `Kind: reference-entry-vN` or an invalid field marks that entry with
  `state: malformed` (or `unsupported`) and excludes it from the rows it cannot
  describe;
- the enclosing read still succeeds: `ref list`, `ref get`, `work`, `brief`,
  My work and the agent prompts return every other entry, and add a bounded
  `coverage` note with the number of affected entries and at most the first 10
  anchor ids;
- `work` never degrades the task queue because one entry is malformed: the
  `attention.reference_review` counts are computed over the entries that parsed,
  `malformed` is counted separately and reported, and no task row changes state;
- no read raises. Repair remains the operator `void-record` path on the offending
  comment.

This is stricter than the existing handoff-journal handling, which surfaces a
malformed journal by setting `state: 'error'` on every item in the read
(`work.py:191-192`); the catalog deliberately does not copy that blast radius.

## 8. HTTP and web exposure of untrusted text

Personal agents reach Orchestra only over HTTP, so slice 1 needs read routes:

- `GET /v1/projects/{pid}/references` and
  `GET /v1/projects/{pid}/references/{key}`, both `CAP_READ`
  (`http_authority.py:314`), registered with the existing
  `@route('GET', ...)` decorator and the `self._project(ctx, CAP_READ)` guard
  used by every project read (`http_service.py:2318-2320`). Dotted keys route
  fine since `3935d59`.
- **No HTTP writes in v1.** A propose write needs a new credential scope, and
  adding a scope is itself a rollback concern because older kits validate scope
  lists (`docs/HTTP_DEPLOYMENT.md:188-193`). HTTP writes are deferred with the
  rest of the web writes (15).

Agent and worker credentials must never be accepted as approvers:

- `docs/HTTP_DEPLOYMENT.md:190-193`: "No credential can administer accounts or
  projects, issue or revoke credentials, or approve a review, whatever the role
  of the account that issued it. Issuing a credential never lends the issuer's
  authority to it."
- `docs/HTTP_DEPLOYMENT.md:791-794`: "Review approval is owner-only; a
  contributor cannot approve, and a worker credential never can."
- Web acceptance, when it is added later, requires a human session with
  `CAP_APPROVE` (`reviews.approve`, `http_authority.py:319`) and never an agent
  or worker credential.

Untrusted text rules. A `statement` is up to 2000 characters of
instruction-grade text, and a **draft** can be written by any contributor or
agent, so it is untrusted input wherever it is displayed or quoted:

1. **Prompts.** Agent prompts carry only server-derived tokens: key, due class,
   date and count, formatted with `agent_prompts.token()`
   (`agent_prompts.py:56-59`, whose `_SAFE_TOKEN` allowlist is at `:42`). A
   title, if shown at all, goes through `agent_prompts.label()`
   (`:45-53`), which strips control and format characters, collapses whitespace,
   collapses every quote-like character to an apostrophe and truncates. A
   statement is **never** placed in a prompt, not even quoted.
2. **Briefs.** A brief carries entry text only as an excerpt object with an
   explicit `trust: "accepted" | "draft"` field. A draft excerpt sits under the
   existing untrusted framing (`agent_prompts.UNTRUSTED_LINE`,
   `agent_prompts.py:34`) and the same quote collapsing.
3. **Web rendering.** The web renders a statement as **plain text**, not through
   `markdown()`. Requirement and decision bodies currently go through
   `markdown()` (`web/js/md.js`, used at `web/js/views/requirements.js:56`, `:85`
   and `:159`); that is acceptable for owner-authored baseline text, but a
   reference draft is attacker-writable instruction text, so it must not become
   markup, headings or autolinks. A URL authority becomes a link **only** when it
   is a validated `https` URL (3.6), rendered with
   `rel="noopener noreferrer"`; anything else is shown as inert text.
4. **Audit and errors.** No statement text goes into HTTP audit records or error
   bodies; the audit read is `GET /v1/projects/{pid}/audit`
   (`http_service.py:2590`), and it records ids, actors and outcomes, not record
   bodies. A refusal names the key, never the statement.

The browser client is static assets under `web/` served by `http_service.py`
(`DEFAULT_WEB_ROOT` `:91`; the served set is `STATIC_DIRECTORIES` `:103`,
`STATIC_TOP_LEVEL` `:104`, with `prototype.html`, `js/prototype.js` and
`js/mock.js` excluded at `:107`). There is no separate `http_web.py`; the read
routes and these rendering rules follow the conventions in `http_service.py` and
`web/js/`.

## 9. Repo sync without drift

**Pointer mode is the default.** A per-project `reference_mode` configuration
selects the projection behaviour; the default is `pointer`: entries point at repo
paths (or validated `url` authorities), no file is generated, and there is no
second copy of the catalog in the tree - which is what the owner asked for. A
project that wants a generated file opts in with `reference_mode: export`.
Revision 1's separate `repo-path-only` mode is dropped: it is just pointer mode
plus a policy that refuses `url` authorities.

### 9.1 Opt-in export, self-verifying

When export is enabled, `ref export --out docs/REFERENCE.md` writes a
deterministic projection of the accepted catalog:

```markdown
<!-- orchestra-reference-export: schema=1 project=example-project catalog_digest=3333333333333333333333333333333333333333333333333333333333333333 body_digest=4444444444444444444444444444444444444444444444444444444444444444 generated=2026-09-27T20:00:00Z -->
| Key | Revision | Statement | Authority | Owner | Review by | Tags |
| --- | --- | --- | --- | --- | --- | --- |
| `calendar.trading` | 3 | Every chart derives holidays and early closes from the pinned calendar module. | `src/example/calendar.py@0000000#HOLIDAYS` | account:u-0001 | 2027-01-15 | calendar, data |
```

- `catalog_digest` is `content_hash({"schema": 1, "project": <name>, "entries":
  [{"key","revision","sha256"}, ...]})` over the sorted accepted entries.
- `body_digest` is the SHA-256 of the rendered body bytes.
- **There is no `.reference-exports/` journal.** Revision 1's journal was
  unnecessary and lived in the wrong place: the file is written in a client
  checkout while the journal would live in the server runtime sidecar. The file
  is **self-verifying** instead - the header carries both digests, so a hand edit
  breaks `body_digest` and a stale catalog breaks `catalog_digest`, with no
  sidecar receipt to lose, back up or restore.
- **Conflict rule.** `ref export` refuses to overwrite a file **only when
  `body_digest` does not match the body bytes** - that is a hand edit, and a
  hand-edited file is reported, never silently clobbered. A stale
  `catalog_digest` is **not** a refusal: it is the expected state after any
  accepted change, and it is precisely why the file is being regenerated.
  Revision 2 refused on a header that did not verify against the current catalog,
  which refused every legitimate update - the very change the export exists to
  publish. A stale `catalog_digest` is instead reported by `ref export --check`
  against the catalog (9.2). `--force` is operator-only and prints exactly what it
  overwrote. A hand edit never becomes a catalog entry: the only route from a
  repo file into the catalog is `ref import` (11), which creates **drafts**.
- Ordering, escaping and whitespace are fixed, so the rendering is byte-stable.
- The catalog (native records) is **authoritative**; the file is a projection and
  is never read back as authority. In pointer mode no file exists at all.

### 9.2 What can run in CI, and what cannot

Revision 1 claimed `ref export --check` was "suitable for CI". That is wrong:
a CI runner has no access to the coordination catalog. Be precise:

- **In CI** only an offline self-consistency check of the committed file can run:
  the header parses, `body_digest` matches the body, the schema version is known,
  and the table is well-formed. That catches hand edits and merge damage. CI
  cannot check staleness against the catalog.
- **On a machine that can reach the catalog** (the coordinator or a contributor
  with client access), `ref export --check` recomputes `catalog_digest` from the
  catalog and re-renders the body, so it detects staleness. A stale file is
  surfaced as an `export-stale` attention item in the same
  `attention.reference_review` block (7), never as a CI failure that CI cannot
  actually compute.

### 9.3 Authority drift: `ref check --repo`

Ask 5 is about **authority** drift, and revision 1 did not meet it: its `ref check`
only confirmed that the anchored path existed at the pinned commit, which never
changes. Slice 2 adds the real signal:

- for every accepted `repo-path` authority, compare the anchored content at HEAD
  with the same content at the pinned `commit`. With `anchor` set, compare only
  that symbol or `## <anchor>` section; without it, compare the whole file.
- when they differ, emit the warning `authority-changed-since-pinned`. The
  warning contains the key, the path, the anchor, the pinned commit, whether the
  difference is at the file or the anchor level, and the entry's `review_by`.
- the check runs only with `--repo` (a client/operator machine with a git
  checkout; e.g. `git show <commit>:<path>` versus the working tree). The server
  never reads a repo path (3.6).
- surfacing: in `ref check`'s `warnings`; as an `authority-changed` class in the
  `attention.reference_review` item set (7); and in `ref get`'s `warnings` array.
  It is a warning, not a failure: an authority that legitimately changed is
  repaired by a new revision with a new pinned commit, which is the whole point
  of the catalog.

This is the check that would have caught the stale trading calendar: the
`HOLIDAYS` anchor changed at HEAD after the pinned commit, so the entry's
authority moved while its `review_by` was still in the future.

## 10. Backup, restore and rollback compatibility

### 10.1 Backup coverage

- **Native backup covers the records.** Entries, revision ledgers and acceptance
  evidence are native comments on native issues, so
  `admin.py backup ... backup sync` and `restore-new` already carry them;
  `restore-new` retains issue IDs and comments, so keys, links and acceptance
  evidence survive a restore with no re-keying. A closed anchor is an ordinary
  native issue and is covered the same way.
- **One new journal joins the coordination sidecar.** `.reference-requests/`
  (propose/revise idempotency receipts) is the operator recovery cache, exactly
  like `.requirement-requests/` and `.requirement-backfills/`.
  `admin.py backup_project` must read it into `files` under its own path prefix,
  and restore must validate it - which requires both the whitelist change and the
  receipt-schema validator in 10.3. The requirement-journal block is the model
  (`admin.py:1287`, `:1315`, and the validator at `:922-927`).
- **Revision 2 removes two journals.** `.reference-exports/` is gone (9.1), and
  no `.reference-acceptances/` receipt cache is introduced: acceptance evidence
  is a native comment, so it needs no local journal. That leaves exactly one new
  sidecar path, and therefore one rollback hazard instead of three.
- **Not backed up, deliberately.** Nothing reference-specific is added to
  `.history-snapshots`-style disposable caches; the design adds no new store that
  is neither native nor a small receipt journal.

### 10.2 Restore semantics

Reading an accepted entry after restore does not depend on any local journal: the
acceptance evidence comment is the authority, the same guarantee
`requirement_records.py` documents for `requirement-acceptance-v1`. A lost
journal costs at most an idempotency receipt, never an entry or an acceptance.

Acceptance authority after restore is the deployment operator allowlist, whose
restore policy is unchanged: `restore-new` restores the native records and the
coordination sidecar and leaves the deployment allowlist untouched
(`docs/OPERATIONS.md:174`), printing by name any operators the backup records
that the host does not list, unless `--restore-operators` is passed
(`docs/OPERATIONS.md:183`). A restored project therefore shows accepted entries,
and accepting a *new* revision again requires a currently listed operator.

### 10.3 Rollback compatibility and the staged release

This is the kittrial-5bb.44 class of problem, and `docs/REVIEWS.md:102-114`
already prescribes the answer: "ship a **tolerant reader** first (accept and
ignore the optional field, write nothing new), deploy it, and only then ship the
**writer** ... rolling the writer back to the tolerant reader is then safe."
`review_workflow.py:13-23` records the same hazard and remedy for a different
change.

**Hazard 1 - the sidecar refuses the whole restore.** `admin.py:899-904`
validates every path in a coordination backup: `:903` matches only
`.coordination-requests`, `.handoffs`, `.handoff-requests`,
`.handoff-recoveries`, `.requirement-requests` and `.requirement-backfills`, and
`:904` raises `ValueError('Invalid coordination backup path')` for anything else.
A backup that contains `.reference-requests/...` therefore cannot be restored by
a kit that predates the catalog - the **entire** restore is refused, not just the
reference part.

**Hazard 1b - the whitelist alone is not enough.** Whitelisting the path is
necessary but not sufficient, and revision 2 stopped one step short.
`validate_coordination_files` validates each recognised journal's **contents**,
not only its path: `admin.py:922-927` calls `requirement_records.validate_receipt`
for `.requirement-requests/` and `.requirement-backfills/`. A slice 0 that only
adds `.reference-requests/` to the `:903` regex would restore reference receipts
**unvalidated**; a slice 0 that adds nothing would make a slice-1 backup
unrestorable. Slice 0 therefore ships the **frozen receipt schema and its
validator** together with the whitelist, and slice 0 and slice 1 use the same
schema. The frozen fields are the ones `requirement_records.validate_receipt`
already enforces (`requirement_records.py:453-484`): a 64-hex `sha256`, a
`status` in `RECEIPT_STATUSES` (`requirement_records.py:88`:
`pending`/`complete`/`failed`/`released`), an optional nonempty `actor`, an
optional nonempty `id`, an optional integer `revision >= 1`, and an optional
bound `acceptance` object. Slice 0's validator must **accept a well-formed
slice-1 receipt and reject malformed bytes**, with a test for each direction -
otherwise slice 0 either resurrects an unreadable receipt or refuses a valid one.

**Slice 0 does not wait for `.58`, and `.58` is not in it.** `.58`'s journals,
prefixes and receipt schema are not designed yet, so folding them into this
slice would freeze schemas that do not exist and would block `.41` on an
undesigned task. `.58` ships **its own** tolerant-reader step with its own design
- the same reserve/whitelist/filter/validator shape, its own frozen receipt
schema - and the two steps may be deployed together if the designs land close
together. That is an ordering choice, not a dependency, and it replaces revision
2's "slice 0 shared with `.58`".

**Hazard 2 - older views and older guards.** After a kit rollback:

- older `work` still hides a closed anchor (`work.py:180`), but a pre-catalog
  kit's HTTP task list (`bd list --all`, `http_service.py:1006-1018`), its `q`
  search and its task detail/history/brief routes show the anchor as an ordinary
  closed task **with its raw `Kind: reference-*` comments** (3.2, 6.0); closed
  anchors are the one mitigation that survives the rollback, and they only cover
  the work surfaces;
- the older reserved guard does not know `reference`, `reference:` or
  `reference-key:` (3.7), so raw forging of `Kind: reference-*` comments and
  `reference:*` labels is allowed again;
- an acceptance written under the catalog reads as ordinary prose, and the older
  kit neither enforces the operator allowlist for it nor treats it as authority.

**The staged release.** Slice 0 is the tolerant reader and is the oldest kit a
catalog deployment may roll back to:

| Release | Contents | Writes |
| --- | --- | --- |
| Slice 0 - tolerant reader (reference namespace only; `.58` ships its own step) | reserve the two comment prefixes and the three labels (3.7); whitelist `.reference-requests/` in `validate_coordination_files` **and ship the frozen receipt schema with its validator** (`admin.py:903`, `:922-927`); filter catalog rows and comments out of all nine surfaces in 6.0, with `GET /tasks/{id}`, `/history` and `/brief` returning 404 (or a pointer to the reference route) for an anchor id; report an unknown `Kind: reference-entry-vN` as `unsupported` per entry | **none anywhere** |
| Slice 1 - core writers | shared keyed-record core; closed anchors; draft/revise; operator accept with F3 evidence and the allowlist; `ref get`/`ref list`; HTTP GET routes; `work`/`brief` attention | reference records only |
| Slice 2 - the rest | `ref check --repo` with `authority-changed-since-pinned`; `ref retire`; `ref decisions`; My work and prompt counts; optional export | export file only, opt-in |

Rolling slice 1 back to slice 0 is safe: entries exist, but slice 0 hides them
from all nine surfaces in 6.0, still refuses raw writes into their namespace, and
whitelists and validates their sidecar so a backup round-trips. Rolling slice 1
back to a **pre-catalog** kit is not safe, and an operator must know exactly what
is lost:

- a backup containing `.reference-requests/` cannot be restored at all
  (hazard 1);
- entries created by slice 0's filter rules are no longer filtered, because a
  pre-catalog kit has no filter (hazard 2) - the closed anchors are all that keep
  them out of work surfaces, and the HTTP task list, `q` search and the task
  detail/history/brief pages show them as closed tasks with raw record comments;
- the label and comment namespace is forgeable again, and the allowlist check on
  an acceptance is not applied by that kit.

Operator recovery from an accidental pre-catalog rollback:

1. Keep or redeploy the slice 0 tolerant reader on the host. It reads and hides
   the catalog without writing, so it is the safe landing point.
2. If a pre-catalog kit must be restored from a catalog-era backup, remove the
   `.reference-requests/` paths from the coordination sidecar **before** the
   restore, and understand that the entries themselves are native comments the
   old kit will import but not understand.
3. On a pre-catalog kit, treat every `reference`-labelled row as a foreign record:
   do not claim it, do not edit it, and `void-record` the `Kind: reference-*`
   comments only if the older kit exposes them as malformed structured history
   (`docs/OPERATIONS.md`, "Malformed structured history"). Nothing is deleted;
   re-installing the tolerant reader makes the entries readable again.
4. Re-grant the deployment allowlist deliberately (`admin.py operators add` or
   `--restore-operators`) before accepting anything.

## 11. Migration path from a project's `REFERENCE.md`

`ref import` is one-way and draft-only, and slice 2 defers it: it is migration
tooling for a problem that exists in one project, so it ships last (15).

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
- Every imported entry becomes revision 1 on a closed anchor with
  `acceptance_state: draft` and
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
  adding a one-line pointer to `ref list`, or keep it as the opt-in export
  projection (9.1), where the self-verifying header guards it.
- Dates are never invented: if neither the file nor `--review-by` supplies a
  date, the entry is created with `review_by: null` and `ref check` reports it as
  a draft needing a date before acceptance.

## 12. Shared machinery and the kittrial-5bb.58 relationship

### 12.1 One keyed-record core, not a parallel module

Revision 1 proposed `reference_records.py` as a parallel implementation of
roughly 1000 lines of `requirement_records.py` (which is 1029 lines today):
controlled labels, native-first receipts, reconcile, backfill and F3 acceptance
binding. That duplication is rejected. Instead, extract a **shared keyed-record
core** used by requirements, references and the kittrial-5bb.58 proposals.

**Module boundary.**

- New `keyed_records.py` holds the kind-independent machinery. Its public
  surface is a small set of primitives plus a spec object:
  - `RecordSpec`: `kind`, `type_label`, `state_labels` (`draft`/`accepted`/
    `superseded`), `revision_prefix`, `acceptance_prefix`, `journal`,
    `key_regex`, `fields`, `validator`, and the flags
    `allow_accepted_first_revision` and `supports_retire`;
  - envelope validation: `refuse_injected_labels`, `checked_fields`,
    `validate_envelope(payload, spec)`;
  - controlled labels: `controlled_labels(spec, acceptance_state)` and
    `apply_controlled_labels(run, task, current, spec, acceptance_state)` -
    mirroring `requirement_records.py:320` and `:554`;
  - ledger reads: `existing_revisions(row, spec)` and `latest_revision(existing)`
    - mirroring `requirement_records.py:353` and `:381`;
  - acceptance evidence: `existing_acceptances(row, spec)`,
    `bound_acceptance(acceptance)`, `bind_acceptance(acceptance, record)` and
    `acceptance_evidence(...)` - mirroring `:385`, `:142`, `:166`, `:414`;
  - receipts and recovery: `validate_receipt(record, spec)`, the `.*-requests/`
    journal helpers, and `reconcile(project, operation_id, actor, reason,
    disposition, run, spec, issue_id=None)` - mirroring `:453` and `:899`;
  - selection and keys: `require_typed(row, payload, spec, allow_untyped)`,
    `existing_key(existing)`, `require_bound_key(task, payload, existing)`,
    `check_key_unique(rows, payload, task)` - mirroring `:498`, `:513`, `:520`,
    `:530`;
  - the single accept/revise entry point `apply_native(payload, actor, run,
    project, spec, operator=False)` - generalizing `:662`.
- `requirement_records.py` keeps what is genuinely specific: the
  `requirement`/`brd-section` kinds and their `description`/`parent` semantics;
  `publication_acceptance(acceptance, manifest)` (`:182`); the
  `requirements.ACCEPTANCE_FIELDS` binding; the backfill field set
  (`:83-84`); and the F3 acceptance shape. It becomes a spec plus thin wrappers,
  and its existing tests must keep passing unchanged.
- `reference_records.py` keeps what is specific to the catalog: the key regex
  (3.3), the `authority` object validator (3.6), `review_by`/`successor`, the due
  classification (7.2), the `decisions` link check (3.9), the owner identity rule
  (3.4), and `retire` (6.4).
- `.58` proposals keep their disposition vocabulary (12.2) and their statistics;
  they use the same core for draft/accepted state, labels, receipts and F3
  evidence.

**Byte compatibility is a hard requirement, not a preference.** The extraction
must leave the bytes requirements write **unchanged**: the canonical JSON of a
`requirement-revision-v1` record, the `requirement-acceptance-v1` evidence and
every `.requirement-requests/` receipt must be byte-identical before and after
the core extraction, because an older kit must still read them and a rollback
must not invalidate them (10.3, `docs/REVIEWS.md:102-114`). The extracted core
must not "normalise" a field order, a separator, a label set or a status
spelling on the way through, and it must keep the `request:`/`request-content:`
labels on the created record (3.2). Slice 1 therefore carries a
**byte-compatibility test**: it produces a requirement record, its acceptance
evidence and a receipt through the pre-extraction code path and through the
extracted core and asserts identical bytes and identical `sha256` in both
directions. Without that test the refactor is its own rollback hazard, which is
exactly what this section exists to avoid.

**Routes requirements gained on this base, covered by the core.**

- The operator-only **direct accepted revision 1** (kittrial-5bb.56,
  `docs/REQUIREMENTS_INTEGRATION.md:162-199`): `operation: "draft"` with
  `acceptance_state: "accepted"` and F3 evidence. Generalized as
  `RecordSpec.allow_accepted_first_revision`, so `ref propose`/`reference-apply`
  get the same route in slice 1 with no second code path - and the same refusal
  for the contributor form.
- A **reconcile command**: `admin.py requirement-reconcile` (`admin.py:2224-2233`,
  `requirement_records.reconcile` `:899`). Generalized to one
  `admin.py record-reconcile --kind ...`; `requirement-reconcile` and
  `reference-reconcile` stay as operator spellings, and `.reference-requests/`
  reconciles exactly like `.requirement-requests/`.
- The existing operator `backfill` path (`:824`) is requirements-specific
  (relabelling records created before the operation); the core exposes
  `apply_controlled_labels` for it rather than a generic catalog backfill, which
  the catalog does not need.

`requirement_impact.py` stays out of the core: it is a manifest/work-graph view
(`requirement_impact.py:23`), and the catalog has no manifest.

### 12.2 Sharing with kittrial-5bb.58

In `.58`, contributors submit requirement proposals into a coordinator input
queue; the coordinator incorporates, rejects or escalates each one to the owner,
and contributors get a log and statistics. The two designs share:

1. **One proposal intake and input queue, one outcome vocabulary.** A reference
   draft **is** a proposal. Requirement proposals and reference drafts go into
   one queue with one disposition vocabulary - `incorporated`, `rejected`,
   `escalated-to-owner` - and accepting a reference is "incorporate". No second
   vocabulary is introduced for the catalog.
2. **The keyed-record core** (12.1): draft/accepted states, controlled labels,
   reserved prefixes, receipts, reconcile and F3 evidence.
3. **One attention block.** Reuse the existing shape from
   `http_service._agent_attention` (`http_service.py:2164-2239`:
   `state`, `summary`, `counts`, `actions`, `truncated`, `computed_at`) with
   kinds `reference-review`, `proposal-pending` and `proposal-escalated`. Design
   the shape once and let `work`, `brief` and My work project it (7.1).
4. **Durable contributor identity.** The `account:`/`person:` rule (3.4) serves
   both the catalog `owner` and `.58` attribution and statistics. A session actor
   is refused in both.
5. **One owner acceptance surface.** Shell in v1 (`reference-apply`,
   `requirement-apply`), one owner "accept" screen later for requirements,
   references and escalated proposals together - never for agent or worker
   credentials.
6. **A tolerant-reader step per feature, deployable together.** Each design ships
   its own reserve/whitelist/filter/validator step with its **own frozen receipt
   schema** (10.3); if the two designs land together, the two steps deploy as one.
   `.41` does not wait for `.58`, and `.58` does not depend on `.41`'s schema -
   revision 2's single shared slice 0 froze journals and prefixes `.58` has not
   designed.

Kept separate: review-by and due logic (catalog only), and statistics and the
scoreboard (`.58` only).

Ordering recommendation (16.9): build the shared core and the tolerant reader
once; the catalog's slice 1 goes first because it is smaller and more concrete;
`.58` builds on the same core. The owner should accept the two designs together,
or `.41` waits for the `.58` design.

## 13. Worked examples (placeholders only)

Every value below is a placeholder: `example-project` is a project, `u-0001` is a
member account and `operator-0009` is an allowlisted operator actor,
`src/example/...` is a repository path and the commits and hashes are zero-filled
or repeated digits. None of it refers to a real project, host or firm.

### 13.1 Repo-path authority: the calendar that expires

```json
{"schema_version": 1, "operation_id": "alex-ref-1",
 "key": "calendar.trading",
 "title": "Trading calendar authority",
 "statement": "Every chart and settlement calculation derives holidays and early closes from the pinned calendar module, never from an inline list; the module's lists expire at year end.",
 "authority": {"type": "repo-path", "path": "src/example/calendar.py",
               "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"},
 "owner": "account:u-0001", "review_by": "2026-12-31",
 "tags": ["calendar", "data"], "decisions": [], "revision": 1,
 "expected_sha256": null}
```

Read: `ref get calendar.trading` -> `state: accepted`, `due: expired` after
2026-12-31, and a counts-plus-owner-item `attention.reference_review` in `work`.
When someone edits `HOLIDAYS` in `src/example/calendar.py` at HEAD, slice 2's
`ref check --repo` reports the `authority-changed-since-pinned` warning and the
same warning appears on `ref get` - the stale-calendar case, caught.

### 13.2 URL authority: an external identity registry

```json
{"schema_version": 1, "operation_id": "bob-ref-7",
 "key": "identity.registry",
 "title": "Identity registry authority",
 "statement": "Employee identifiers are issued by the corporate identity registry; the registry page is the authority for the issuer format.",
 "authority": {"type": "url", "url": "https://example.invalid/identity/issuer-format",
               "retrieved": "2026-09-01"},
 "owner": "person:bob", "review_by": "2027-03-01",
 "tags": ["identity"], "decisions": ["example-project-42"], "revision": 1,
 "expected_sha256": null}
```

Read: `ref decisions example-project-42` lists it; `ref check` warns
`url-not-reverified` once `retrieved` falls outside the review window. No network
call is made. On the web the URL is a link only because it is a validated
`https` URL; the statement is rendered as plain text.

### 13.3 Retirement: the settlement instant changed

`settlement.instant` (an accepted entry at revision 2, pinned to
`src/example/ledger.py@0000000#SETTLEMENT`) is replaced by a different key. One
operator operation writes the retirement:

```sh
b ref retire settlement.instant --successor settlement.instant-intraday --file retirement.json --json
```

The retirement revision of `settlement.instant` carries
`"acceptance_state": "superseded"` and
`"successor": "settlement.instant-intraday"`, and its acceptance evidence names
the operator and the decision. Read: `ref get settlement.instant` returns the
retired record plus `resolved` pointing at the successor, and `ref get
settlement.instant-intraday` reports `replaces: ["settlement.instant"]`, derived
at read time. `ref check` reports `retirement-target-missing` if the successor
key does not exist, and `retirement-cycle` if a retirement loop is ever written -
there is no half-accepted pair to detect, because one operation wrote both
halves.

## 14. Rejected alternatives

| Alternative | Why rejected |
| --- | --- |
| One native issue per entry typed `decision` | Corrupts the decision semantics used by `templates/DECISION.md`, the briefing `decision` attention kind and the render/backlink view. |
| A new manifest field in `requirements-baseline.json` | Needs a `REQUIREMENTS_CONTRACT` revision and publisher change; conflates "cited revision" with "operational authority". |
| A single catalog comment per project | No per-entry state or labels, unbounded comment growth, and no additivity for `work` filtering. |
| Entries only in the project repository file | Not visible to other sessions' workers without a checkout, and not durable against a bad edit - the exact failure the owner reported. |
| A new server-side JSON store | A second store beside native records would need its own backup, restore, locking and conflict policy. The kit's reviewed answer is native records plus a small receipt journal. |
| **Open** anchor issues plus a per-surface label filter | Four surfaces (HTTP task list, queue, `/v1/me/work`, agent prompts) do not filter at all today, `work.py:175` filters by `issue_type`, and an older kit has no filter. Closed anchors hide the row everywhere at once, including on kits that predate the catalog. |
| A filter by the `reference` label alone, ignoring `issue_type` | Keeps the row visible to every reader that has no filter, and cannot be honoured by an older kit. Retained only as the tolerant reader's extra filter (6.0), never as the primary mechanism. |
| Extending the checkpoint `kind` vocabulary with `reference-review` | Confuses author-declared open items with computed date reminders; unresolved items must stay author-owned. |
| Fetching `url` authorities server-side | The server has no network authority and no checkout; verification belongs in `ref check` on a client/operator machine. |
| A per-revision `supersedes {id, revision, sha256}` hash chain | A second invariant over a single native issue whose comment ledger is already ordered; it added a malformed-record class and an asymmetric-pair window. Replaced by an expected revision plus an expected content hash (3.5). |
| Two-step `replaces`/`replaced_by` acceptances | Could leave one half accepted and the other not, which `ref check` then had to detect. One `ref retire` writes both at once, and `replaces` is derived. |
| A `.reference-exports/` receipt journal | The file is written in a client checkout while the journal would live in the server sidecar; it was a second sidecar path and a second rollback hazard for no benefit. The file is self-verifying instead (9.1). |
| `repo-export` as the default mode | A generated file is a second copy of the catalog, which is what the owner wanted to avoid. Pointer mode is the default and export is opt-in (9). |
| A separate `repo-path-only` mode | It is pointer mode plus a policy that refuses `url` authorities; a mode adds configuration for nothing (9). |
| Claiming `ref export --check` is CI-ready | CI cannot reach the coordination catalog, so CI can only self-check the committed file. Staleness belongs on the coordinator side as attention (9.2). |
| A session actor as `owner` (revision 1's `alex/session1`) | Sessions are ephemeral: routed attention could never reach a person, and `.58` statistics could never attribute an entry durably. A durable `account:`/`person:` identity is required (3.4). |
| Session actors as approvers, or any agent/worker credential | The HTTP authority layer already refuses this: review approval is owner-only and a worker credential never can (8). |
| Rendering a reference statement with the web's `markdown()` helper | A draft statement is attacker-writable, instruction-grade text; `markdown()` is for owner-authored baseline text. Statements render as plain text, and only validated `https` URLs become links (8). |
| An HTTP propose write in v1 | Needs a new credential scope, and adding a scope is itself a rollback concern because older kits validate scope lists (8). |
| A parallel `reference_records.py` that copies `requirement_records.py` | Duplicates roughly 1000 lines of labels, receipts, reconcile, backfill and F3 binding. Extract the keyed-record core instead (12.1). |
| Refusing an export whose `catalog_digest` no longer matches the current catalog (revision 2) | Every accepted change breaks that match, so the normal regeneration was refused - the export could never publish a change. Only a `body_digest` mismatch (a hand edit) is a refusal; a stale `catalog_digest` is why `ref export` runs (9.1). |
| Suppressing review-by attention when an acceptance goes inert (revision 2) | An expired authority that suppresses its own reminder goes silent, and the silence looks like health. The entry raises an `acceptance-inert` attention item to approvers instead (4, 7.1). |
| Matching a session actor by the `name/sessionN` shape alone (revision 2's refusal) | Real session actors are `session-<uuid>` (`sessions.py:19`, `:170`), so `person:session-4e40...` passed the check. The refusal matches the real shape (3.4). |
| An `account:`/`person:` owner identity as the acceptance operator (revision 2's example) | The allowlist holds actor strings and `recovery.identity` refuses a colon (`recovery.py:50-53`), so `account:u-0009` can never be an operator. Acceptance evidence names the allowlisted actor; a project owner accepts only as one, with shell access (4). |
| One shared tolerant-reader release for `.41` and `.58` (revision 2's slice 0) | `.58`'s journals, prefixes and receipt schema are not designed, so a shared slice 0 would freeze schemas that do not exist and block `.41` on an undesigned task. Each ships its own step; they may deploy together (10.3, 12.2). |
| Whitelisting `.reference-requests/` without shipping its validator (revision 2's slice 0) | `validate_coordination_files` checks journal contents (`admin.py:922-927`), so the whitelist alone restores receipts unvalidated or refuses valid slice-1 receipts. Slice 0 ships the frozen schema and its validator (10.3, 15). |
| Claiming a closed anchor hides the catalog from **every** reader (revision 2) | The HTTP task list reads `bd list --all` (`http_service.py:1006-1018`), and the task detail/history/brief routes show the row and its raw `Kind: reference-*` comments (`:2359-2363`, `:2451-2469`, `:2365-2395`). Closed status hides the work surfaces; slice 0's explicit filter covers the rest, including those three routes (3.2, 6.0). |
| Deferring the whole catalog design until `.58` lands | The catalog's slice 1 is smaller and more concrete; the shared core and the two tolerant-reader steps can be built independently, in either order, so `.41` should not block on `.58` (12.2, 16.9). |

## 15. Follow-up implementation slices (proposed, not filed)

Filed by the owner only after this design is accepted. Exactly three slices, then
a "later" list. No web write and no import before the later list.

**Slice 0 - tolerant reader (reference namespace only; does not wait for
`.58`).** Reserve `Kind: reference-entry-v1` and `Kind: reference-acceptance-v1`
in `reserved_comments.RESERVED` and add `reference`, `reference:` and
`reference-key:` to `RESERVED_LABEL_PREFIXES`/`RESERVED_EXACT_LABELS`
(`reserved_comments.py:164`, `:654-655`); whitelist `.reference-requests/` in
`admin.validate_coordination_files` **and ship the frozen receipt schema with its
validator** (`admin.py:903`, `:922-927`), so a slice-0 restore accepts a
well-formed slice-1 receipt and refuses malformed bytes; filter catalog rows and
comments out of the nine surfaces in 6.0, with `GET /tasks/{id}`, `/history` and
`/brief` returning 404 (or a pointer to the reference route) for an anchor id;
report an unknown `Kind: reference-entry-vN` as `unsupported` per entry. **No
writes anywhere.** A test must assert that slice 0 writes nothing. `.58`'s
journals, prefixes and receipt schema are not designed yet, so **`.58` gets its
own tolerant-reader step with its own design** (10.3); the two steps may be
deployed together, but `.41` does not block on `.58`.

**Slice 1 - core draft/accept, `ref get`/`ref list`, HTTP GET, attention.**

- extract `keyed_records.py` from `requirement_records.py` and re-point
  requirements at it, with the existing requirement tests unchanged (12.1), plus
  the **byte-compatibility test**: requirement record, acceptance evidence and
  receipt bytes (and `sha256`) identical before and after the extraction (12.1);
- `reference_records.py`: the `reference-entry-v1` kind, closed anchors,
  propose/revise with `expected_sha256` and the `.reference-requests/` receipt
  journal, key resolution, the owner identity rule;
- `admin.py reference-apply` (+ `reference-reconcile`) with
  `reference-acceptance-v1` evidence, F3 binding, the deployment operator
  allowlist check, and the direct accepted revision 1 enabling
  `allow_accepted_first_revision`;
- `ref get` and `ref list --tag/--due/--state`, the `ref` client action and
  endpoint dispatch, plus `docs/CLI_CONTRACT.md` additions;
- `GET /v1/projects/{pid}/references` and `/references/{key}` at `CAP_READ`, with
  the untrusted-text rules of section 8;
- `work` counts plus the entry-owner/approver items, and `brief` top-3 with
  `trust`; failure isolation per entry (7.3); help/limit payloads.

**Slice 2 - `ref check`, retire, `ref decisions`, My work, optional export.**

- `ref check --repo` including `authority-changed-since-pinned` (9.3);
- `ref retire KEY --successor KEY2` (6.4) and the derived `replaces` view;
- `ref decisions DECISION-ID`;
- My work and agent-prompt counts (7.1, which states they land here and not in
  slice 1);
- optional per-project export with the self-verifying header (9.1);
- `docs/OPERATIONS.md` migration/runbook section and a `templates/` reference
  entry example.

**Later, only if asked.** `ref import` (11); any web write and web acceptance
(human `reviews.approve` session only); tag auto-match beyond task labels; and a
separate follow-up task to close the `requirement-apply` allowlist gap this
design recorded in section 4.

## 16. Owner questions, with recommended answers

Every question below now carries a recommended answer. They are decisions for the
owner, not open design gaps; the recommendation is what slice 1 will implement
unless the owner chooses otherwise.

1. **Mandatory review-by.** *Recommended:* `review_by` is required on every
   accepted entry and there is no `"never"` sentinel; cap it at 24 months ahead.
   Re-confirming a timeless fact once every two years costs little, and it keeps
   one rule in the validator. Drafts may omit it.
2. **Who accepts.** *Recommended:* an actor on the deployment operator allowlist,
   through the shell route in v1 (`admin.py reference-apply`). A project owner
   accepts only in that form - their actor must be added with
   `admin.py operators add` and they need shell access; ownership alone is not
   acceptance authority, and the allowlist holds actor strings, not
   `account:`/`person:` identities (4). Web acceptance for project owners comes
   later, with a human session holding `reviews.approve`; never an agent or worker
   credential. Contributors and agents only propose.
3. **Retirement.** *Recommended:* one operation,
   `ref retire KEY --successor KEY2`. No two-step acceptance; `replaces` is
   derived at read time (3.5).
4. **Brief selection.** *Recommended:* deterministic. Show entries whose tags
   match the task's own labels, plus expired and due-soon entries, at most 3,
   expired first. No inference; `--ref-tag` remains an explicit override.
5. **Default repo mode.** *Recommended:* pointer mode by default, export opt-in
   per project. A generated file is a second copy of the catalog (9).
6. **Scale.** *Recommended:* a bounded linear read of up to 500 entries per
   project, with a `coverage` note beyond that; add a project-level index only if
   a project approaches 500.
7. **Export path.** *Recommended:* a fixed `docs/REFERENCE.md` per project when
   export is enabled, so the file location needs no per-project configuration.
8. **Owner identity.** *Recommended:* a durable person/account identity -
   `account:<uid>` for an office deployment (the project member account), or
   `person:<name>` for an SSH-only project. Session actors are refused at write
   (3.4).
9. **Order relative to kittrial-5bb.58.** *Recommended:* build the shared
   keyed-record core once; the catalog's slice 1 goes first because it is smaller
   and more concrete; `.58` builds on the same core. Each feature ships its **own**
   tolerant-reader step with its own frozen receipt schema, because `.58`'s
   journals and prefixes are not designed yet; the two steps may deploy together
   (10.3, 12.2). The owner should accept both designs together, or `.41` proceeds
   without waiting for the `.58` design.

**Recorded gap, not a question.** On this base `requirement-apply` does not
check the deployment operator allowlist (`admin.py:2214-2223`,
`docs/OPERATIONS.md:61-63`). The catalog deliberately does not copy that gap, and
the fix belongs in a separate follow-up task against `requirement-apply`, not in
this design.
