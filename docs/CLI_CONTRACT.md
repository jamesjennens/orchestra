# CLI compatibility contract (v1)

This document is the versioned contract between an Orchestra client (this kit's
`client.py` and its Windows/POSIX wrappers) and an endpoint. The contract identifier
is `cli-contract-v1`; `work --help` returns it in the `contract` field. The kit version
`0.1.0` implements v1. Additive fields and new optional flags keep v1; removing or
renaming a documented field, changing an ID's meaning, or changing the envelope
requires a new contract version.

The endpoint is the program that runs beside the canonical Beads service
(`endpoint.py` for SSH/local transport). Clients talk to it through one JSON envelope
and never run the native `bd` binary directly.

## Response envelope and streams

Every request is one JSON object on stdin; every response is one JSON object on stdout:

```json
{"returncode": 0, "stdout": "<the command result>", "stderr": "<diagnostics/warnings>"}
```

- `stdout` is the command result. For structured commands it is JSON text (often
  pretty-printed); for document/help commands it is text.
- `stderr` carries native warnings, progress notes and errors. It is **never** mixed
  into `stdout`.
- The client writes `stdout` to its own stdout and `stderr` to its own stderr, and
  exits with `returncode`. With the additive client option `--out FILE` it writes
  `stdout` to `FILE` as UTF-8 with LF line endings and no BOM instead, and leaves its
  own stdout empty.
- On validation or transport failure, `returncode` is nonzero (`2` for endpoint
  validation), `stdout` is empty, and `stderr` starts with the error type
  (`ValueError: ...`). Automation should parse stdout only when the exit code is `0`.

Native commands can succeed (exit `0`) while printing warnings. The endpoint forwards
those warnings on `stderr` and keeps the success JSON clean; it does not silently drop
them and does not merge them into `stdout`.

### Native boundary: what may reach an error, a warning or a log

The endpoint applies one reviewed policy to native output before it crosses into a
structured result:

- **Native stderr on success is forwarded verbatim.** It is operator-facing native
  output (warnings, progress notes), not an error echo; the endpoint does not redact
  or truncate it.
- **Raw `bd` passthrough is intentionally unchanged.** The `bd` action and `refresh`
  return native `stdout`/`stderr` and the native exit code as they are, so an operator
  can run arbitrary allowed `bd` commands. That path is not bounded or redacted and is
  not part of the structured-error contract below.
- **A nonzero exit in a structured command becomes a labelled, bounded error.** The
  message is `Native command failed (<rc>)` plus, at most, one short diagnostic line
  (<= 160 characters) with path-shaped tokens replaced by `<path>`. Longer output — and
  any first line that is itself longer than one diagnostic line — is withheld behind
  `[native output withheld: N line(s), M characters, sha256:<12 hex>]`, so an operator
  can correlate with server logs without the payload appearing in the error.
- **stdout is expected to carry only JSON, and is classified by one policy.**
  That policy is **at most one data document per stream.** The whole stream is
  decoded first: when it
  is one JSON object or array — indented or not — that object/array is the result
  document, with any non-JSON warning lines before or after it re-labelled on
  `stderr`. A single multi-line line-anchored document is one document even when
  wrapped in warning lines, so it never degrades into the one fragment line that
  happens to parse alone. Line mode (JSON Lines, one result row per line) is used
  only when no multi-line document is present and every data line is a complete
  JSON object or array; every one of those rows is returned together (the native
  `export --all` shape), so no row is dropped. Two data documents in one stream —
  two indented documents, or an indented document plus a one-line row — are
  **refused**: `Native stdout carries more than one JSON document (exit 0)`
  followed by the withheld-output count and digest, never a silent pick of one
  document with the others re-labelled as notes (that was data loss with rc `0`).
  A line that is a bare scalar (`42`, `"label"`) or a diagnostic object is never a
  data row: a log record (`{"level": "warn"}`, keys drawn only from `level`,
  `severity`, `time`/`timestamp`/`ts`, `msg`/`message`, `logger`, `caller`,
  `thread`, `module`, `service`, `component`, `pid`) or a warning/notice shape
  (`{"warning": ...}`, keys drawn only from the log keys plus `warning(s)`, `warn`,
  `notice(s)`, `error(s)`, `hint(s)`, `detail(s)`, `reason(s)`, `code`) is a note on
  `stderr`. A warning object next to a one-line row therefore yields one data line
  and one note. A whole-stream `null` is kept verbatim as a compatible empty
  result (base accepted it; callers use `json.loads(...) or []`); any other bare
  scalar, including a lone diagnostic object such as `{"message": ...}`, leaves no
  data document and fails with `Native stdout is not JSON (exit 0)` naming the
  bounded, redacted first line or the withheld-output label — never a bare
  `JSONDecodeError`. The lone-diagnostic refusal is a deliberate tightening
  against base, which accepted it. A truncated JSON Lines stream keeps its
  complete rows and notes the incomplete one.
- **Forwarded stdout-noise lines are capped.** At most 8 noise lines are re-labelled
  individually as `native stdout note:`, each redacted or withheld like the diagnostic
  line; any remaining lines become one `native output withheld` note with a count and
  digest, so a native flood cannot fill the caller's stderr.

Errors are bounded and labelled. They name the offending field, index or option, but
they do not echo whole private payloads: attachment bodies, comment text, oversized
native output and caller-supplied field names are truncated or withheld rather than
reproduced in the error or in logs.

### Redaction boundary for the one echoed line

The single diagnostic or note line that is echoed has whole path-shaped tokens
replaced by `<path>`, in this order: quoted spans that contain a path separator
(`"C:\Users\James Smith\private notes\db.txt"` becomes `"<path>"`), URLs
(`file:///…`, `https://…`), home-relative `~/`/`~\` tokens (`~/x` becomes
`<path>`, so the leading `~` never survives), absolute Windows/UNC paths
(spaces inside and between directory segments are kept together, so an unquoted
`C:\Users\James Smith\private notes` loses **every** segment, including the last
space-free one), absolute POSIX paths, and relative path tokens that look like a
path (two or more separators, or a dotted final segment, so `data/private/tok.txt`
becomes `<path>`). A two-segment token without a dot — an actor such as
`alice/session` — is deliberately left alone; it is an identity, not a path.

An unquoted Windows path is taken greedily over space-separated segments once the
path proper contains a space (so `...\private notes` is removed whole); if the
path proper contains no space, the following space-separated text is ordinary
prose and is kept, so `cannot open C:\db file is locked` stays readable. A
space-broken home-relative path such as `~/private notes/tok.txt` becomes
`<path> <path>`: the tilde token and the following relative token are each removed
whole.

This is a reviewed boundary, not complete sanitisation: other private-looking
values (tokens, message bodies) are not pattern-matched. The bound that matters is
that the echo is one short line and that path-shaped tokens lose their whole path,
not just a suffix.


## Command help

`work`, `review`, `handoff`, `brief`, `history`, `checkpoint`, `capability`, `ref` and
`proposal` answer `-h`/`--help` on stdout with exit code `0`:

```sh
b work --help
b review --help
b handoff --help
b brief --help
b history --help
b checkpoint --help
b capability --help
b capability lookup --help
b ref --help
```

Help recognition is uniform: `-h`/`--help` is honoured wherever it appears as a
standalone token, except when the token before it is an option that takes a value, so
`work --owner -h` is still the option error it has always been and not a help request.
`review TASK --help` and `handoff --json --help` return help, and help never runs a
native command, takes the coordination lock or needs an attachment.

`work --help` returns machine-readable usage, options, limits, output shape, identity
fields and exit codes. The briefing commands return the same envelope with their own
usage, options, limits and notes. `work --json` is accepted for consistency; `work`
always returns JSON, so the flag is a no-op. The same is true of `checkpoint`:
`--json` is accepted in any position (`checkpoint TASK --file checkpoint.json --json`,
`checkpoint --json TASK --file checkpoint.json`) and the saved checkpoint is returned
as JSON on stdout, exactly as the usage line and option list say. The `checkpoint`
usage line is `checkpoint TASK --file checkpoint.json [--json]`. Help is data, not a
`SystemExit`, so it survives the endpoint envelope.

## `work`: list/queue shape

`work` returns:

| Field | Meaning |
| --- | --- |
| `owner` | the actor filter that was applied, or `null` for the full queue |
| `total` | number of matching tasks in the fresh view |
| `items` | the current page, priority-sorted |
| `next_offset` | offset for the next page, or `null` |
| `coverage` | what the view covers; malformed journals add `journal_errors` |

Each `items[]` entry has `task` (the **native task ID**, e.g. `kittrial-5bb.7`),
`title`, `owner`, `status`, `review_state`, `contribution_id` (the **contribution
record's comment ID**, not a Git SHA and not `latest_comment_id`), `commit`,
`pending_review_items`, `pending_handoff_requests`, `pending_handoff_total`,
`pending_handoff_next_offset`, `lifecycle`, `lifecycle_scope`,
`lifecycle_matches_contribution`, `error`, and the additive `workflow_state` and
`integration` fields from the shared review-state projection. `review_state` may be
`integrated`; `workflow_state` keeps the raw workflow state. See
[REVIEWS.md](REVIEWS.md) for their meaning.

`work` also returns an additive `attention.proposal_queue` block, the contributed
requirement proposal queue; see [`proposal`](#proposal-contributed-requirement-proposals).
`work` also returns an additive `attention.capability_index` block, the capability
index's attention; see [capability attention](#capability-attention-in-work-and-brief).

`work` also returns an additive `attention.reference_review` block. It is computed
over the whole project, whatever the task filters, from the export `work` already
reads.

- **Counts, always:**
  - `expired` and `due_soon`, for accepted reference entries;
  - `unset`, for draft-only entries;
  - `acceptance_inert`, for entries whose acceptance evidence was written by an
    actor who is not on the allowlist (a removed operator, or one who never was);
  - `malformed`;
  - `total`.
- **`items`** are returned only to an approver, which on the SSH route means an actor
  on the deployment operator allowlist.
  - Each item has `key`, `review_by`, `due`, `owner`, `state` and `revision`, plus
    `inert_operator` on an inert entry.
    `owner` is a quoted, bounded `agent_prompts.label()` value (or null).
  - Inert entries come first, then expired, then due-soon.
  - Items are paged by `--ref-limit` (1..100) and `--ref-offset`.
- **Other callers** get `items: []` with `truncated: true` and a `coverage` note.
  - An entry owner sees their own entries with `ref list --owner IDENTITY`.
  - Draft entries are listed by `ref list --state draft-only`.

`show TASK --json` returns native issue rows. `comments TASK --json` returns native
comment rows. In raw rows a task's `assignee` is a plain string; in briefing/history
views identity and authorship become excerpt objects (below).

## `brief` and `history`: excerpt objects

`brief --json` and `history` intentionally bound output. Fields such as `title`,
`owner`, `author`, `intent`, `acceptance`, `depends_on_id` and `type` are **excerpt
objects**:

```json
{"text": "first 96 characters", "omitted_chars": 12}
```

`omitted_chars == 0` means the text is complete. A worker must read `.text`, not assume
a bare string; treat `omitted_chars > 0` as "read `show`/`history` for the full value".
Opaque cursor fields (`activity_cursor`, `next_cursor`) are never excerpted: they are
complete tokens.

`brief` adds an `attention` array, plus `attention_total` and `attention_more`. It
holds at most 3 items of each kind: `reference-review` items first, then
`proposal-review` items (see [`proposal`](#proposal-contributed-requirement-proposals)),
then `capability` items (see [capability attention](#capability-attention-in-work-and-brief)).
The two totals count every kind.

The `reference-review` items:

- **Selection:** entries tagged with one of the task's labels, plus expired and
  due-soon entries, with expired first.
- **Each item** has `key`, `due`, `review_by`, `trust` (`accepted`), `title` (an
  excerpt object), `text` and `source` (`ref get KEY`).
  The title's text uses `agent_prompts.label()`; its omission count describes the
  normalized source characters replaced by the label's ellipsis.
- **`text`** is server-derived from the key, the due class and the date. It never
  includes the entry's statement.
- **Separate from open items.** These items are not checkpoint items, and reading a
  brief changes nothing.

`brief` decodes unresolved items as `open_items[]` with `id`, `kind`, `text`, `source`;
`history` pages entries with `entry_id`, `body`, `body_offset`, `body_total_chars` and
`continued`, so a long body is reassembled by concatenating fragments in offset order.

## `capability`: client-side code and design lookup

`capability lookup`, `resolve` and `index` are answered by the client itself and are
read-only. The capability **records** commands (`find`, `get`, `list`, `misses`,
`propose`, `revise`, `propose-alias` and `verify`) go to the endpoint and need `--config` and
`--project`; see [capability records](#capability-records-the-index-on-the-endpoint)
below. `capability check` runs in the client against your checkout but reads the
records from the endpoint; see [the drift check](#capability-check-the-drift-check).
The local commands:
- never contact the endpoint (`lookup` adds the endpoint's records only when you pass
  `--config` and `--project`);
- it writes nothing: no Beads record, no cache file, no `__pycache__` beside the client,
  and no refresh of Git's index (every Git call runs with `--no-optional-locks`, so it
  never races the caller's own `git add` or `commit`);
- it needs no `--config`, `--project` or `--actor`.

The client loads `capabilities.py` from its own directory by path. A standalone
client copied without that file refuses with exit `2`; copy `capabilities.py` from
the same kit next to it. The module also runs on its own as `python capabilities.py ...`.

`capabilities.py` is **not** covered by `provenance.json`: `--version` and onboarding
parity vouch for `client.py`, `version.py` and `VERSION` only. Whoever can write the
client's directory can change what `capability` runs, just as they could change
`client.py` there, so keep that directory writable by its owner only.

```sh
b capability lookup "reserved label guard"
b capability resolve endpoint.py::_guard_reserved_labels docs/CLI_CONTRACT.md#documented-limits
b capability index --source ast
```

The index is built on demand from the checkout named by `--repo`. The default is the
current directory, widened to its Git top level.

- **Code (`--source auto|ast|graphify`):**
  - `ast` indexes Python modules, classes, functions and methods with the standard-library
    parser. It resolves calls and test references through imports, `self`/`cls` and
    same-module names, and marks them `INFERRED`.
  - `auto`, the default, uses graphify's `graphify-out/graph.json` instead when it is
    present and accepted (see [Producing `graph.json`](#producing-graphjson-optional)).
  - `--source graphify` or `--graph FILE` requires a graph; `--source ast` ignores one.
- **Design anchors:** Markdown headings are always indexed from the checkout, with
  GitHub-style anchors (repeated headings get `-1`, `-2`, ...).
- **Files:** with Git, tracked plus untracked-but-not-ignored files. Without Git, a walk
  that skips hidden and dependency directories.
- **Skipped files** are counted in `index.skipped` by reason, and each count of
  `parse-error`, `too-complex`, `heading-limit` or `definition-limit` also adds one line
  to `warnings`:
  - `parse-error`, which includes MemoryError and Python 3.11+ raising RecursionError on
    extremely deep expressions;
  - `too-complex`: a file that could nest more deeply, or hold more flat expressions,
    than the run can parse safely (see "Parsing deep Python" below);
  - `file-too-large`;
  - `unsafe-path`;
  - `unreadable`, including a link out of the checkout.
- **Parsing deep Python.** CPython 3.10 crashes, rather than raising, when it converts
  a very deep expression, such as a long `1+1+...`, `a.b.b...`, `f()()...`, `a[0][0]...`
  or `1<<1<<...` chain.
  - The source is decoded with its declared encoding first (PEP 263). A `# coding:
    utf-7` cookie hides ASCII operators inside a base64 `+...-` shift sequence, so the
    count must run on the decoded text, never on the raw bytes. A cookie whose codec
    cannot decode the file is a `parse-error`, never a fall-through to `ast.parse`.
  - Parsing therefore runs on a worker thread with a 512 MB stack, or 255 MB where the
    platform allows no more (Windows). If the thread cannot be started at one size, the
    next size is tried before falling back; trusted depth is capped at 50,000 levels
    (about 25 MB of C stack), so one hostile file cannot commit hundreds of MB.
  - Ordinary files, dot-heavy ones included, are parsed directly. Only a file whose
    cheap per-logical-line pre-count (one byte scan, no tokenizer) passes the limit is
    tokenized first; the exact count then decides, and it counts expression depth, not
    raw tokens: comma-separated elements of a display (a 60,000-row generated table) are
    siblings, not 60,000 levels.
  - A flat but huge expression (`1<1<...`, `(1,1,...)`, `1 and 1 and ...`) is not deep,
    so a second cheap count bounds the whole file at 300,000 expression tokens:
    operators, brackets and commas outside strings and comments, every NAME token, and
    every newline or `;` at bracket depth zero (so a statement-dense file — 900,000
    one-name lines, 190,000 calls or a 190,000-name list — is covered as well). A file
    beyond that budget is skipped as `too-complex` before `ast.parse` can build
    hundreds of MB of sibling nodes, with a warning. The budget is high enough that a
    60,000-row generated table (about 240,000 counted nodes) still parses.
  - If no such thread can be started, the run falls back to a per-logical-line guard
    on the calling thread:
    - the limit is 5,000 levels, or 2,000 on Windows;
    - tokenizing stops at the first line over the limit;
    - each run tokenizes at most 16 MB; files beyond that budget are skipped as
      `too-complex`, with a warning.

A **pointer** is `file::Qualified.name` for code
(`http_auth.py::Service._refresh_authority`), `file.md#anchor` for a heading, or a bare
file. Paths are repo-relative with `/` separators.

| Command | `schema` | Top-level fields |
| --- | --- | --- |
| `lookup PHRASE` | `capability-lookup-v1` | `schema`, `contract`, `trust`, `query`, `found`, `match_type`, `matches`, `total_matches`, `candidates`, `hint`, `index`, `warnings` |
| `resolve POINTER...` | `capability-resolve-v1` | `schema`, `contract`, `trust`, `results`, `summary`, `index`, `warnings` |
| `index` | `capability-index-v1` | `schema`, `contract`, `trust`, `index`, `entries`, `warnings` |

**Exact match.** A phrase matches exactly when it equals an entry's name, qualified name,
dotted name, heading or pointer. The comparison ignores case, underscores, camelCase and
punctuation.
- The result has `found: true` and `match_type: "exact"`.
- Every exact match is counted in `total_matches`, and non-test code is listed first.

**Miss.** A miss is a result, not an error: it exits `0`.
- It returns `found: false`, up to `--limit` ranked `candidates` (each with a `score`),
  and a `hint`.
- A phrase word that no indexed name uses is read as the closest word that one does. The
  substitution is reported in `query.corrections`.

**Entry fields:**

| Field | Meaning |
| --- | --- |
| `id` | the pointer |
| `kind` | `module`, `class`, `function`, `method` or `doc-section` |
| `name`, `summary` | excerpt objects |
| `file`, `line`, `end_line` | where the entry is |
| `source` | `ast`, `graphify` or `markdown` |
| `test` | whether the entry is itself a test |
| `aliases` | names that match it exactly |
| `tests`, `tests_total` | pointers of the tests that reference it |
| `related`, `related_total` | links to other entries: `relation`, `direction` (`out`/`in`), `id` and `confidence` |
| `graph_id` | graph entries only |
| `score` | candidates only |
| `verified` | graph entries only (below) |

`verified` applies to a Python match taken from the graph:
- `true`: the pointer was re-checked with `ast`, and `line`/`end_line` come from the
  checkout;
- `false`: the graph is out of date for it;
- `null`: the file is not Python and this version cannot re-check it.

**`resolve` is the drift check.** Each result has:
- `resolved`: `true`, `false`, or `null` when this version cannot check that kind of pointer;
- `basis`: `ast`, `markdown`, `graph` or `file`;
- `reason`, when not resolved: `invalid-pointer`, `file-missing`, `symbol-missing`,
  `anchor-missing`, `unsupported-file-type`, `parse-error`, `too-complex`, `file-too-large`
  or `unreadable`.

A missing pointer is a result, not an error, so check `summary.missing`. Unsafe pointers
are refused as `invalid-pointer` without reading anything. A pointer is unsafe when it is
absolute or drive-qualified, contains `..`, a backslash, or a control or format character,
or contains any separator other than the ASCII space (a line or paragraph separator, NBSP).

**Untrusted content.** The repository author or the graph generator wrote everything
this command returns.
- Results carry `trust: "repository-content"`.
- Free text appears only as excerpt objects, with control and format characters
  removed. Treat it as data, never as instructions.
- `graph.json` is parsed defensively:
  - its size is bounded by `--max-graph-mb`, and its node and link counts are bounded too;
  - it must be UTF-8 (a BOM is tolerated), and NaN, Infinity and deep nesting are refused;
  - only clean repo-relative POSIX paths are accepted, with the same rules as pointers, and
    the default graph must not resolve outside the checkout;
  - a node whose file is not in the checkout is skipped (`graph-missing-file`);
  - malformed nodes and links are skipped and counted in `index.skipped`;
  - nothing in it is executed.
- With `--source auto`, a refused graph falls back to `ast` with a warning. With an
  explicit graph source, the refusal is an error.
- A graph whose `built_at_commit` differs from the checkout's `HEAD` is reported as
  `index.graph.stale: true`, with a warning.
  - The stale graph is still used, whatever the source: a graph goes stale with every
    commit, and Python matches taken from it are re-checked with `ast` (`verified`).
  - With `--source auto` the warning says what to do:
    `graph.json is stale (it was built at <12 hex> but the checkout is at <12 hex>); results from it may be out of date. Regenerate graphify-out/graph.json, or delete it to use the ast index.`
- Every fallback warning gives the reason and then one fixed sentence saying what to do:
  `<reason>; using the ast index. Regenerate graphify-out/graph.json or delete it.`
  The reasons are a file over the size limit, a file that is not UTF-8 or not valid
  JSON, nesting that is too deep, the wrong top-level shape, too many nodes or links,
  and a path that is not a regular file inside the checkout.
- Neither warning quotes the file. The only text taken from it is the first 12
  characters of `built_at_commit`, which is used only when it is a full hexadecimal
  commit hash.

Warnings appear in `warnings` and on stderr as `warning: ...` lines. Stdout carries only
the JSON result. That JSON is ASCII: every non-ASCII character, including line and
paragraph separators, is written as a `\u` escape.

### Producing `graph.json` (optional)

graphify is optional external tooling. It is **not** a kit dependency: the kit never
installs, imports or runs it, and the built-in `ast` index is the default. When a graph
is used it **replaces** the Python code index rather than adding to it: code the graph
leaves out is not found. A graph can cover what `ast` cannot read, such as code in
other languages.

- **Where.** The kit reads exactly one path: `graphify-out/graph.json` at the top level
  of the checkout (or the file named by `--graph FILE`). Keep `graphify-out/`
  git-ignored: it is generated output, and a committed graph can never name the commit
  that contains it, so one that records its commit would always read as stale.
- **How.** A worker or a build step runs graphify so that its JSON output lands at that
  path. The kit does not wrap the tool and does not pin its command line: see
  graphify's own documentation for how to install and run it.
- **Shape.** A JSON object with a `nodes` list and a `links` (or `edges`) list.
  - A code node has `id`, `label`, `file_type: "code"`, a repo-relative `source_file`
    and a `source_location` such as `L12`.
  - A link has `source`, `target` and `relation`, and may have `confidence`.
  - An optional top-level `built_at_commit` (a full commit hash) is what the stale check
    compares with `HEAD`. Without it, or outside Git, staleness is unknown
    (`index.graph.stale: null`) and the graph is used.
- **Limits.** 64 MB by default (`--max-graph-mb`, 1..512), 500,000 nodes and 2,000,000
  links. The safety rules are under "Untrusted content" above.
- **Stale.** The graph was built at another commit, so its pointers may have moved. It
  is still used, with the warning above. Regenerate it at the current commit, or delete
  the file to use the `ast` index.
- **Keep it current.** Because a graph goes stale on every commit, regenerate it
  routinely (for example with a git hook, if graphify provides one; see graphify's own
  documentation).

Recorded capability records, alias proposals and a recorded drift check are designed
separately. The entry shape and pointer syntax here are what they are meant to feed.

## Capability records: the index on the endpoint

The capability index records what a part of the system is for: its name, summary,
aliases, requirement links, design anchors, owner, and where its code and tests live
([design](CAPABILITY_INDEX_DESIGN.md), slices 1a and 1b). Records live in Beads.
Meaning is accepted by an operator. Location is checked against a checkout with
`capability check`, and every read reports the result as `verification`
([the drift check](#capability-check-the-drift-check)).

```sh
b capability find "merge slot" --limit 5 --json
b capability get review.structured-contribution --json
b capability list --state draft-only --json
b capability misses --limit 20 --json
b capability propose --file capability.json --json
b capability propose-alias merge.slot "single integrator" --evidence coordination.py::merge_acquire
```

**Reading.** `find`, `get` and `list` are read-only. They take no coordination lock and
read no more than they need, as `ref` does: `get` reads only its own key (and the
retired keys, for `replaces`); `list` and `find` read the index in two native reads
(one `bd list`, then one `bd show` up to 20 entries or one `bd export --all` above).
`propose` and `revise` read only their own key before writing; `propose-alias` reads
the index once, because its collision and cap rules span every capability.

- **`find PHRASE`** returns `{found, match_type, records, total_records, candidates,
  hint, coverage}`.
  - The phrase is at most 200 characters, and `--limit` is 1..20 (default 5).
  - **Exact matches** come only from a key, a name, or an accepted alias.
  - A **pending alias**, or an alias in an unaccepted draft, only lifts its capability
    among the candidates (`score`).
  - Every record carries `trust: accepted|draft`, and pending aliases carry
    `alias_state: proposed` with the submitter's `person` and `identity`.
  - A miss suggests `propose-alias` or `propose`.
- **`get KEY`** returns the record shape of `ref get`: `state`, `record`, `acceptance`,
  `acceptance_inert`, `proposed`, `aliases_pending`, `replaces` (derived), `resolved`
  (the successor of a retired key), `warnings` and `coverage`. `name` and `summary` are
  excerpt objects.
- **`list`** returns one row per key. It takes `--tag`, `--owner`,
  `--state draft-only|accepted|superseded|all`, `--limit` and `--offset`.
- **Failures stay per entry.** A malformed or unsupported entry fails only itself. A
  malformed alias or verification record is a warning on its entry. The operator's
  `void-record` repairs a malformed capability, alias or verification record, and
  `anchor-release --kind capability` frees a key held by an anchor with no record,
  exactly as for references. `anchor-release --duplicate` releases a named anchor of a
  duplicated key that holds well-formed records
  ([operations](OPERATIONS.md#a-duplicated-record-key)).

**`capability lookup` with `--config` and `--project`** runs the local code lookup,
unchanged, then one endpoint `find`. It adds these fields to `capability-lookup-v1`:
- `records`: the exact records and candidates, each with `match: exact|candidate` and
  every `code`, `tests` and `anchors` pointer resolved live against your checkout
  (`{pointer, live: resolved|missing|unknown}`);
- `records_found`, `records_hint` and `records_coverage`.

If the endpoint cannot answer, the local result is still returned, with
`records_warning`.

**The worker loop: check the record first, and feed a miss back.**
1. **Look up with your config**, so the recorded capabilities are included:
   `b capability lookup "<phrase>"` with `--config` and `--project`. An exact record
   with `trust: accepted` and pointers that are `live: resolved` is the answer.
2. **On a miss** (`records_found: false`, or only candidates), use the code
   `candidates` the same lookup returned, then search the checkout by hand.
3. **Update the index with what you found:**
   - it exists under another name: `capability propose-alias KEY "<phrase that missed>"
     --evidence POINTER`;
   - it is not indexed at all: `capability propose --file capability.json`, a draft
     with the pointers you found.
4. **A pending alias or a draft is never authoritative.** Until an operator accepts
   it, it only lifts a candidate (`match: candidate`, `trust: draft`,
   `alias_state: proposed`). Do not cite one as the accepted meaning of a capability,
   and check its pointers yourself before relying on them.

**Writing.**
- **`propose` and `revise`** take a closed JSON payload:
  - `key`: lowercase dotted.
  - `name`: at most 120 characters.
  - `summary`: at most 1,200 characters. It is untrusted text.
  - `aliases`: at most 32 phrases, each at most 80 printable characters.
  - `requirements`: at most 16 `{key, revision?}` links. Each must name an existing
    requirement record (and revision).
  - `anchors`: at most 16 `file.md#anchor` pointers.
  - `code`: at most 32 `file::Qualified.name` pointers.
  - `tests`: at most 32 test pointers or files. All pointers are repo-relative, with no
    `..`, absolute path, drive or backslash, and at most 400 characters.
  - `owner`: `account:<uid>` or `person:<name>`. A session actor is refused. A draft
    needs no owner; an accepted capability needs one.
  - `tags`: at most 8.
  - `status` and `verified_at` are derived, never stored.
  - The rules on `revision`, `expected_sha256`, retries and interrupted proposes are
    those of `ref`.
- **`propose-alias KEY "PHRASE" [--evidence POINTER]`** adds a pending alias. It is
  only a candidate until an operator folds it into the next accepted revision's
  `aliases`, or rejects it.
  - A phrase that is another capability's key, name or accepted alias is refused.
  - An identical pending proposal is returned, not written twice.
- **Pending-alias caps** are keyed on the resolved person:
  - 3 per capability and 50 per project for a verified person;
  - one shared pool of 1 per capability and 10 per project for all unverified
    proposers;
  - 20 per capability overall.
- **Interim attribution:**
  - `capability propose-alias` through the endpoint **always** writes
    `identity: unverified`, whatever actor the caller names. Over SSH the actor is
    self-declared, so an operator's actor name proves nothing there. Every endpoint
    proposer shares the unverified pool.
  - Only the operator shell route, `admin.py capability-alias-propose`, writes
    `identity: verified` (`operator:<actor>`), after its allowlist check.
  - A reader counts an alias as verified only when **both** hold: the record says
    `verified`, and its stored native author is that operator, on the allowlist. The
    same two conditions make a rejection take effect.
  - Attribution beyond operators arrives with kittrial-5bb.68 (and over HTTP once the
    actor is bound to the principal).
  - `submitted_by_agent` is `false` until then.

Acceptance, retirement, alias rejection and a verified alias proposal are operator
commands ([operations](OPERATIONS.md#operator-commands)). There is no demotion in slice 1a: the
design's "demote" (section 4) is covered by retiring the key for now.

**Batch acceptance results** (`admin.py capability-apply`). Each item names the newest
revision the operator reviewed, by `revision` and `record_sha256`, and gets one result:

| Result | Meaning | Effect on the batch |
| --- | --- | --- |
| `accepted` | this run wrote the acceptance evidence and the next revision as accepted | continues |
| `already-accepted` | the item's accepted revision and evidence were already there | continues; no native write |
| `refused` | the item was refused before any write: a stale `revision` (a newer one exists), a wrong `record_sha256`, an unknown key | **continues** with the next item; the reason is reported |
| `uncertain` | a native write did not confirm | **stops**; later items are `not-run` |

- **`complete`** is `true` unless an item was `uncertain`. A refused item does not make
  the batch incomplete.
- **Re-running the identical batch** resumes it. Items already accepted are reported
  `already-accepted` from their own receipts, with no native write. An `uncertain`
  item is finished, or reconciled first with
  `admin.py capability-reconcile --operation-id OPERATION_ID/KEY`.
- **A changed list** under the same `operation_id` is refused.
- **The lock is per item.** The batch takes the project's coordination lock for one
  item at a time and releases it between items, so another writer waits behind at most
  one item, never the whole batch. Each item reads only its own key and re-checks
  `revision` and `record_sha256` under its own hold: a capability revised between two
  items is seen, and its item is `refused` as stale.
- **Re-accepting.** Naming a revision that is already accepted, and still the newest,
  is a new decision: it writes the next revision with identical content, and new
  evidence. `ref` acceptance follows the same rule.

### `capability check`: the drift check

```sh
b capability check --repo . --json
b capability check --repo . --key merge.slot --record
b capability check --repo . --payloads payloads.json
```

`check` runs where the code is. It pages the records from the endpoint
(`capability list --pointers`), then resolves every `code`, `tests` and `anchors`
pointer of each capability's current revision against your checkout, exactly as
`capability resolve` does. It needs `--config`, `--project` and `--actor`. Retired
capabilities are skipped. `--key KEY` (repeatable) limits the check.

The output is `capability-check-v1`:

| Field | Meaning |
| --- | --- |
| `repo` | `{git, commit, dirty}` of the checkout |
| `index` | `{code_source: ast\|graphify, graph}` |
| `capabilities` | one row per capability: `key`, `state`, `revision`, `record_sha256`, `passed` and `results` |
| `results` | per pointer: `{pointer, resolved, basis, reason}`; `resolved` is `true`, `false` (missing) or `null` (this kit cannot check that kind of pointer) |
| `passed` | `true` when every pointer resolved, `false` when one is missing, `null` when none is missing but some could not be checked, or there is no pointer |
| `summary` | counts of capabilities and pointers; with `--record` or `--payloads`, counts per `recorded` outcome |
| `recording` | `record`, `payloads` or `null` |

A missing pointer is a result: the exit code is `0`. Exit `2` is a refusal or a
transport error, with nothing on stdout.

**Nothing is written without `--record`.**
- `--record` first refuses a checkout that is not a git checkout at a full commit, or
  that has uncommitted or untracked changes. It then posts one `capability verify` per
  capability whose `passed` is `true` or `false`. Each row gains `recorded`:
  `recorded`, `already-recorded`, `refused` (with `refusal`), `not-recordable`
  (`passed` is `null`), `uncertain` (the transport failed; later rows are `not-run`,
  and re-running the same command resumes).
- `--payloads FILE` writes the same verification payloads to `FILE` and posts nothing.
  The file is created private (mode 0600) and replaced whole. A `FILE` that is a
  symbolic link or a directory is refused before anything is read.
  An operator or listed verifier records them as verified with
  `admin.py capability-verify`.

**A check you record is a report, never a confirmation.** Over SSH the actor is
self-declared, so the endpoint always writes `identity: unverified`, whatever actor
you name. Only the host command `admin.py capability-verify`, run by an actor on the
operator allowlist or the deployment `verifiers` list, writes a verified record. A
reader trusts a record only when it says `verified` **and** its native author is that
listed actor.

**`capability verify --file verification.json`** is the endpoint write `check --record`
uses. The payload is closed: `schema_version` (1), `key`, `revision`, `record_sha256`,
`commit` (40 or 64 lowercase hex), `checked_at` (UTC, `2026-10-02T12:00:00Z`),
`source` (`ast` or `graphify`), `graph_built_at_commit` (or `null`), `tool`
(`{name, version}`), `results` (`[{pointer, resolved, reason}]`) and `passed`.
- The record is bound to the capability's current revision (the accepted one, or the
  newest draft when none is accepted) by its exact `record_sha256`; a stale revision is
  refused. `results` must name exactly that revision's pointers.
- `passed` must be `true` exactly when every pointer resolved. A payload with
  unchecked pointers (`null`) and none missing is refused: it is neither a pass nor a
  failure.
- It is idempotent per `(key, revision, commit, actor)`: an identical repeat returns
  the first record (`reconciled: true`); a different result at the same commit is
  refused.
- **Caps.** Passing and failing reports are bounded separately, so passing reports
  can never stop anyone from raising drift.
  - At most 20 untrusted **passing** reports per capability revision. A failing report
    is still accepted when that cap is full.
  - An untrusted **failing** report is refused only when the project already holds 5
    open failing reports from unverified submitters (10 per person once SSH actors are
    bound to people, kittrial-5bb.68). "Open" means not yet cleared by a trusted pass.
  - Trusted records are never capped. The refusal names the cap.

**What reads report.** `get` returns a `verification` block for the capability's
current revision; `list` and `find` return its `state` as `verification`.

| `state` | Meaning |
| --- | --- |
| `drifted` | some check of this revision failed, from anyone, and no trusted pass at an integrated commit came after it |
| `verified` | the newest trusted check passed; `verified_at` is `{commit, checked_at, person, integrated}` |
| `reported` | there are passing reports, none from a trusted verifier |
| `unverified` | this revision has no check |
| `superseded-revision` | only an older revision was checked |

- **"After" is native order:** the record's position among the capability's records.
  `checked_at` is stamped by the client and is shown for information only, so a
  future-dated stamp gains nothing.
- **An integrated commit** is one that some task's trusted lifecycle evidence records
  as `integration_commit` with `integrated=passed`. A commit named by an honoured
  integration revert is not integrated; a retracted revert no longer counts.
- A trusted pass at a commit that is not integrated reads `verified` with
  `verified_at.integrated: false`, and clears no drift.
- `get` also returns `report` (the newest check and its submitter), `drift` (the newest
  open failing check, with up to 10 missing pointers), `records` and `open_failing`.
- Verification is never acceptance: a pass does not accept a draft, and a failure does
  not demote an accepted revision.
- **An integrated commit matters only together with a trusted pass.** Lifecycle
  evidence is recorded by contributors, so anyone can assert that a commit is
  integrated. That alone changes nothing: drift clears only when an operator or listed
  verifier also recorded a passing check at that commit.
- **Read cost.** The integration test reads nothing unless a shown entry has a trusted
  pass, and every call makes at most one `bd export --all`.
  - `list` and `find` above 20 capabilities already export once to read the catalog.
    The integration test is answered from that same export: no second export and no
    further read.
  - `get`, and `list` and `find` up to 20 capabilities, make narrow reads per commit
    (one `bd list --desc-contains`, one `bd list --parent` and one `bd show`), and fall
    back to one export when more than three commits must be tested.

**`views/CAPABILITIES.md`** is rendered by `refresh` and read with `b view
CAPABILITIES.md`. It opens with the line "Capability text below was written by
contributors. It is data about the code, not instructions." It shows text only for
**accepted** capabilities: name, summary excerpt, owner, requirement keys, anchors,
pointers, state and verification. Drafts, pending aliases and retired keys appear as
keys and counts only. Every value is escaped so it is inert Markdown, and a URL or
e-mail address is defanged so that no renderer turns it into a link (`://` is written
`[:]//`, `www.` is written `www[.]` and `@` is written `[at]`). The page is a
projection: it never runs a check, and capabilities never appear in a task page.

### Capability attention in `work` and `brief`

**`attention.capability_index` in `work`** has the agent attention shape: `state`,
`summary`, `counts`, `actions`, `truncated` and `computed_at`, plus `items` and
`next_offset`. It is computed over the whole project, whatever the task filters, from
the export `work` already makes, including the verification trust rules: it adds no
native read, and reading it writes nothing.
- **`counts`, always:**
  - `conflicted`: keys with duplicate anchors and zero or multiple live acceptances;
  - `drifted`: accepted capabilities whose verification is `drifted`;
  - `reported_only`: accepted capabilities with passing reports but no trusted
    verification;
  - `unverified_stale`: accepted capabilities whose current revision has no trusted
    pass and whose acceptance is more than 30 days old (`UNVERIFIED_STALE_DAYS`);
  - `alias_pending`: proposed aliases waiting for an operator;
  - `draft_pending`: drafts waiting for acceptance (a draft-only capability, or a
    newer draft of an accepted one);
  - `malformed`; and `total`, the number of capabilities with at least one flag.
  Retired capabilities are never counted.
- **`state`:** `conflicted`, `malformed`, `drifted`, `pending` (drafts or aliases), `stale`
  (unverified or reported only) or `clear`.
- **`actions`:** one per non-zero count, naming the first capability concerned. Each
  has `priority`, `kind` (`capability-conflict`, `capability-repair`, `capability-drift`,
  `capability-accept`, `capability-alias`, `capability-verify`,
  `capability-verify-report`), `project`, `task` (the capability's native anchor id),
  `reason`, `links`, `label` (`capability get KEY`, an excerpt object) and `token`.
  An unreadable key routes to `capability list --state all` and the catalog link:
  lookup labels replace dots with hyphens and cannot be inverted unambiguously.
  They are sorted by `(priority, project, task)`.
- **`items`:** only for an actor on the deployment operator allowlist, conflicted first, then drifted,
  paged by `--capability-limit` (1..100, default 20) and `--capability-offset`. Each
  has `kind` (`capability`), `key`, `task`, `state`, `verification`, `flags`,
  `owner`, `aliases_pending`, `accepted_days` and `title` (an excerpt object with
  `trust`). Anyone else gets the counts with `truncated: true`; an owner reads their
  own capabilities with `capability list --owner IDENTITY`.
  Owner and title text use quoted, bounded `agent_prompts.label()` values; a missing
  owner stays null. The surrounding double quotes are literal JSON string content.
  Each label contains at most 60 normalized characters inside those quotes
  (62 including the quotes); truncation uses an ellipsis. The title's omission count describes the normalized source
  characters replaced by the label's ellipsis.
- At most 1,000 capabilities are scanned; beyond that `coverage` says so.

**`capability` items in `brief`.** At most 3 accepted capabilities whose `tags` match
one of the task's labels, drifted first, then by key. Each has `kind`
(`capability`), `key`, `trust` (`accepted`), `verification`, `title` (an excerpt
object), `text` (server-derived from the key and the verification state, never the
summary) and `source` (`capability get KEY`).
The title text uses the same quoted label as work attention.

**Duplicate reference and capability anchors.** Only exactly one anchor with valid
acceptance evidence from a live operator can supply a duplicate key's record.
It remains readable with a `duplicate-key` warning and an `anchors` array naming
every matching native ID with its original state and trust. Zero or multiple live
acceptances make the key `conflicted`: `get` has no selected native ID, record,
proposal or acceptance, and returns every anchor's ID/state/trust in `anchors`.
`list` and capability `find` show the individual rows as `conflicted`, with their
original `anchor_trust`; content is a discovery label, not an authoritative record.
Conflicted capability rows expose no code/test/requirement pointers or verification
authority, are excluded from the exact lookup index and accepted briefs, and an
exact conflict lookup returns `found: false`, `match_type: conflicted`.
Existing result limits still apply; coverage names every duplicate native ID.
Display order is live acceptance, readable draft, then malformed/unsupported or
incomplete content, with native ID used only to order rows within a rank.
Operator attention counts each conflicted key once and includes all its anchors;
capability attention offers a `capability-conflict` action to reconcile it.
All writes (propose, revise, apply/accept/retire, verify and alias writes), including
receipt-bound retries, refuse a duplicated key before mutation and name every
matching anchor. An operator must reconcile it before any write, even when one
uniquely accepted record remains readable. Records and evidence are never combined
across anchors. Distinct readable keys sharing a lookup slug remain distinct;
malformed or incomplete lookup-labelled rows participate in every plausible key's
duplicate group because their exact key cannot be established from a lossy label.
`capability-retire` also refuses a duplicated key as the successor. `capability find`
lists each anchor of a conflicted key once, in `records`, never also as a candidate.
The host command `admin.py anchor-release --duplicate` is the reconciliation: it
releases one named anchor of the duplicated key, never the last anchor and never the
key's only accepted record, and needs `--set-aside-evidence` for an anchor that carries
acceptance evidence (live or inert) or that readers currently select. A typed row with
no key label that carries the release audit comment is not read for any key
([operations](OPERATIONS.md#a-duplicated-record-key)).

### `capability misses`: which phrases miss, and how often

The endpoint counts every `capability find` for a project. When a `find` has no exact
record match (`found: false`), it also remembers the phrase. `capability lookup` with
`--config` makes exactly one `find`, so every such lookup is counted, from any client
version. A purely local lookup (no config) reaches no endpoint and records nothing.
Recording happens inside `find`, not through a separate call: older clients are
covered without an upgrade, there is no second round trip, and the contributor
interface gains no new write action.

**What is stored, per project:**
- per phrase: the normalised phrase, a count, and first-seen and last-seen times
  (server clock, UTC);
- five counters: `finds`, `misses`, `overflow`, `dropped` and `evicted`, and the time
  the log started.

Nothing else is stored. In particular no actor, session or person: the log cannot be
turned into a per-person record. Whether code candidates were offered is not recorded
either, because the client computes them and the endpoint never sees them.

**The phrase is untrusted text.**
- It is stored only after the lookup's own normalisation (`capabilities.clean`, then
  `capabilities.normalized`): control, format (bidi overrides, zero-width and tag
  characters) and separator characters become spaces, punctuation is dropped, case is
  folded, and words are joined by single spaces. This is the key `find` itself matches on.
- What remains must be at most 80 characters of letters, digits and single ASCII
  spaces. Anything else (empty, longer, or still not of that shape) is counted in
  `dropped` and not stored.
- The file and the `capability misses` output are ASCII-escaped.
- A stored phrase can still read like an instruction (`ignore previous instructions`).
  The output is marked `trust: "untrusted-text"`; read it as data. Nothing in the kit
  places a recorded phrase into an agent prompt, a briefing, a view or onboarding text.

**Bounds** (fixed in this version):
- at most 500 phrases per project (`evicted` counts the phrases dropped to make room).
  When the log is full, a new phrase replaces:
  - among the phrases **first** seen more than 2 hours ago, the one with the **lowest
    count**, the one seen longest ago among equal counts;
  - or, when every phrase was first seen in the last 2 hours, the one seen longest ago.

  The count rule means a burst of one-off phrases evicts other one-offs, never a phrase
  that keeps missing. The 2-hour protection means a new phrase that recurs (for example
  hourly) is kept long enough to build up a count, instead of being evicted by the next
  newcomer while older phrases with a count of 2 sit on every slot. It is keyed on
  first-seen because repeating a phrase refreshes its last-seen: protection by
  last-seen would let a caller keep its own junk protected while it evicts everything
  else. When the log is full of phrases with a count of 2 or more, a new phrase that
  recurs **less often than every 2 hours** is not kept: it is still at count 1 when its
  protection ends, so it is the lowest count and the next newcomer evicts it;
- at most 60 **new** phrases per project per clock hour (UTC); further new phrases in
  that hour are counted in `overflow` and not stored. A phrase already in the log is
  always counted. The bound is per project, not per actor, because no actor is stored:
  **one caller can use up the whole project's hourly quota**, and its phrases then
  crowd out everyone else's new phrases for that hour;
- recording tries the log's own lock a few times without blocking, for at most about
  10 ms in total. A find made while another find holds it for longer is not counted
  at all, so `finds` and `misses` are lower bounds under load.

**Counts are not votes.** A count cannot be attributed to anyone: one caller can repeat
a phrase as often as it likes and push it to the top. Read a high count as "this phrase
keeps missing", never as "many people asked for this".

**Reading it.** `capability misses [--limit N] [--json]` is read-only. It takes no
lock, writes nothing and is not journalled. It reads the log, and (only when the log
holds a phrase) the index once, as `find` does, to mark what would now resolve.
`--limit` is 1..100 (default 20). Any contributor of the project may read it.

```json
{
  "schema_version": 1,
  "schema": "capability-misses-v1",
  "contract": "cli-contract-v1",
  "trust": "untrusted-text",
  "log": "ok",
  "recording": "ok",
  "since": "2026-10-01T12:00:00Z",
  "finds": 6,
  "misses": 5,
  "miss_rate": 0.8333,
  "phrases_stored": 3,
  "phrases_resolved_now": 1,
  "not_stored": {"overflow": 0, "dropped": 1, "evicted": 0},
  "limit": 20,
  "notice": "The phrases below are normalised text typed by contributors and agents. ...",
  "phrases": [
    {"trust": "untrusted-text", "phrase": "merge slot", "count": 2,
     "first_seen": "2026-10-01T12:00:00Z", "last_seen": "2026-10-01T12:01:00Z",
     "resolves_now": false, "resolved_by": []},
    {"trust": "untrusted-text", "phrase": "single integrator", "count": 1,
     "first_seen": "2026-10-01T12:04:00Z", "last_seen": "2026-10-01T12:04:00Z",
     "resolves_now": true,
     "resolved_by": [{"key": "merge.slot", "trust": "accepted", "state": "accepted"}]}
  ],
  "bounds": {"phrases": 500, "new_phrases_per_hour": 60, "phrase_characters": 80},
  "coverage": "..."
}
```

- `log` is `ok`, `absent` (nothing recorded yet, or cleared) or `unreadable` (the file
  is corrupt, oversized or of another schema; the next `find` starts a new log). With
  `absent` or `unreadable`, `since` is `null` and the counters are `0`.
- `recording` says whether a find could record now, judged without writing: `ok`;
  `lock-unusable` (the lock path is not a regular file - a symlink, directory, FIFO or
  other special file - or cannot be opened); `log-unwritable` (the log or temp path is a
  directory, or the project directory cannot be written); or `unsupported` (no
  `flock`). A symlink or FIFO at the log or temp path does not stop recording: the
  next write replaces it.
  Anything but `ok` means finds still answer but nothing is counted, so zeros are not
  "no misses". `admin.py capability-misses-clear` repairs the first two.
- `notice` comes immediately before `phrases`, and every row starts with
  `trust: "untrusted-text"`. A stored phrase can be an 80-character imperative sentence
  pushed to the top by repetition; it is still only text someone typed.
- `since` is when the log started. `finds`, `misses` and the `not_stored` counters
  cover everything since then, including phrases later evicted.
- `miss_rate` is `misses / finds`, rounded to four places, or `null` when `finds` is `0`.
- `phrases` are the top `limit` phrases by `count`; ties go to the most recently seen.
  `phrases_stored` counts all of them.
- `resolves_now` is `true` when a `find` for that phrase would be an exact match now:
  it equals a capability's key, its name, or an accepted alias (the same rule as
  `find`). `resolved_by` names up to five such capabilities, accepted first, each with
  `trust: accepted|draft` and the capability's `state` (`accepted`, `draft-only`,
  `superseded`, ...). A retired key still matches exactly, so check `state` before
  treating the miss as fixed. A pending alias never makes a phrase resolve.
  `phrases_resolved_now` counts the stored phrases that resolve, whatever the limit.
- `not_stored`: misses that are in `misses` but whose phrase is not (or no longer) in
  the log: `overflow` (over the hourly bound), `dropped` (empty, too long or unsafe
  after normalising) and `evicted` (pushed out by a newer phrase when the log was full).

The log is telemetry, not coordination state. It is one file in the project's
coordination directory, it is not backed up, and an operator can delete it at any
time; see [Operations](OPERATIONS.md#the-capability-lookup-miss-log).

## `ref`: the reference catalog

The reference catalog holds operational facts and their authority: what is
authoritative for X, where it lives, and when it must be checked again. It is
described in [the reference catalog design](REFERENCE_CATALOG_DESIGN.md); slice 1
ships the commands below.

```sh
b ref get calendar.trading --json
b ref list --tag data --state accepted --due expired --limit 20 --json
b ref propose --file entry.json --json
b ref revise --file entry.json --json
```

**Reading.** `ref get` and `ref list` are read-only. They take no coordination lock and
never read more than they need:
- `ref get` reads only its own key: one `bd list` by the key's lookup label and one
  `bd show` of that row. Its cost does not grow with the catalog.
- `ref list` reads the catalog in two native reads: one `bd list --label reference`,
  then one `bd show --include-comments` for up to 20 entries, or one `bd export --all`
  above that.
- `ref propose`, `revise` and acceptance read only their own key before writing.

- **`ref get KEY`** returns these fields:
  - `key`, `state` and `native_id`.
  - `record`: the newest accepted revision. Its `title` and `statement` are excerpt
    objects.
  - `record_comment_id`.
  - `acceptance`: the F3 decision. Its `operator` is the allowlisted actor taken from
    the evidence comment's **native author**.
  - `acceptance_inert` and `inert_operator`.
  - `proposed`: the newest draft after `record`, with `proposed_comment_id`.
  - `due`: `ok`, `due-soon`, `expired` or `unset`.
  - `replaces` (`[]` in slice 1) and `resolved` (`null`).
  - `warnings`, `trust` and `coverage`.
- **`ref get` states:**
  - `state` is `accepted`, `draft-only`, `malformed` or `unsupported`.
  - A `draft-only` entry is **not** authoritative.
  - An unknown key, or a key whose anchor has no record yet, exits nonzero and names
    the key.
- **`ref list`** returns `{total, items, next_offset, coverage}`, one row per
  unambiguous key, or every anchor row for a conflicted key (see duplicate rules above).
  - Rows are ordered expired, due-soon, unset, then ok, and by key within each.
  - Each row has `key`, `title` (at most 200 characters), `state`, `owner`,
    `review_by`, `proposed_review_by`, `due`, `tags`, `revision`, `native_id` and
    `acceptance_inert`.
  - Options: `--tag` (repeatable; all tags must match), `--owner IDENTITY`,
    `--state draft-only|accepted|superseded|all`, `--due expired|due-soon|unset`,
    `--limit` and `--offset`.
- **Failures stay per entry.** A malformed entry, an unsupported newer record kind
  (`Kind: reference-entry-v2`), or an anchor left without its record by an interrupted
  propose fails only itself.
  - `coverage` names such entries, with at most 10 anchor ids.
  - A read never fails the whole catalog.
  - **Repair.** The operator voids a malformed record with `admin.py void-record`
    ([operations](OPERATIONS.md#reference-and-capability-records)). Reads and writes
    then see the entry as if that comment were absent, and its `warnings` carry
    `record-voided`. A void that does not apply is reported as `void-invalid` (not a
    valid void by a configured operator) or `void-refused` (it names a well-formed
    record the entry reads, the earliest holder of a revision, or a record of another
    kind).
  - An anchor that holds no record, because its propose cannot be re-run or because
    every record it held is voided, is closed and its key freed with `admin.py
    anchor-release` ([operations](OPERATIONS.md#orphan-anchors)).

**Writing.** `ref propose` and `ref revise` take a closed JSON payload. The command
sets `operation`.

```json
{"schema_version": 1, "operation_id": "alex-ref-1", "key": "calendar.trading",
 "title": "Trading calendar authority",
 "statement": "Charts derive holidays and early closes from the pinned calendar module.",
 "authority": {"type": "repo-path", "path": "src/example/calendar.py",
               "commit": "0000000000000000000000000000000000000000", "anchor": "HOLIDAYS"},
 "owner": "account:u-0001", "review_by": "2027-01-15", "tags": ["calendar", "data"],
 "decisions": [], "revision": 1, "expected_sha256": null}
```

- **Fields:**
  - `key`: lowercase dotted, at most 80 characters.
  - `title`: at most 200 characters.
  - `statement`: at most 2,000 characters. It is untrusted text, and it never
    appears in an error.
  - `authority`: a `repo-path` (a relative path with no `..`, an optional 40-hex
    `commit` and an optional `anchor`) or a `url` (`https` only, no userinfo, with a
    `retrieved` date no later than today).
  - `owner`: `account:<uid>` or `person:<name>`. A session actor is refused.
  - `review_by`: a real date at most 24 months ahead.
  - `tags`: at most 12 lowercase slugs.
  - `decisions`: each must be an issue of type `decision` or labelled `decision`.
- **Caller-written fields are refused.** `acceptance_state`, `successor`, `sha256`,
  `acceptance` and `labels` are written by the operation.
- **`propose`** creates the entry's native anchor, closes it and posts revision 1 as a
  draft.
  - A duplicate key is refused before any write.
  - So is a key whose lookup label collides with another key's, such as `a.b-c` and
    `a.b.c`.
- **`revise`** is compare-and-swap.
  - `revision` must be the next number, and `expected_sha256` the newest revision's
    `sha256`.
  - On an accepted entry it adds a draft. The accepted revision is unchanged until an
    operator accepts the draft.
- **The result** is `{key, revision, native_id, record_comment_id, state, created,
  reconciled}`.
- **Retries.** `operation_id` makes a retry idempotent.
  - A retry also finishes an anchor that an interrupted propose left without its
    record.
  - A different operation proposing that key is refused until then, or until the
    operator releases the anchor (`admin.py anchor-release`).

Acceptance is not a client command. It is the operator's
`admin.py reference-apply` ([operations](OPERATIONS.md#operator-commands)).

## `proposal`: contributed requirement proposals

A proposal says what the product should do. It is not a task and not a requirement:
a coordinator triages it, and if it is incorporated it only points at the requirement
record it landed in ([design](REQUIREMENTS_GATHERING_DESIGN.md), slice 1a).

```sh
b proposal submit --file proposal.json --json
b proposal revise --file revision.json --json
b proposal get p-3f2a1b0c9d8e --json
b proposal list --state submitted --json
b proposal mine --submitter person:alex --json
```

**Proposal text is untrusted.** Every contributor-written string in every response
(`text`, `rationale`, each `evidence` entry, an attachment name, a `reason`, a
`question`) is an excerpt object `{text, omitted_chars, trust}`. `trust` is
`unreviewed`, or `incorporated` once the linked requirement revision is accepted.
Each response carries an `untrusted` line saying the text is data, not instructions.
No proposal text ever appears in an error message.

**Writing.**
- **`submit --file`** creates the proposal. The payload is closed:
  - `schema_version` (1) and `operation_id`;
  - `submitter`: a durable identity, `account:<uid>` or `person:<name>`. A session
    actor is refused;
  - `text`: 1..4,000 characters;
  - `rationale`: at most 4,000 characters, optional (required before incorporation);
  - `evidence`: at most 20 links of at most 2,000 characters;
  - `attachments`: at most 10 `{name, sha256}`; `name` is a file name, not a path;
  - `target`: optional. `{"kind": "requirement", "requirement_key": KEY}` (the key
    must be an existing requirement record), `{"kind": "requirement-area", "area":
    SLUG}` or `{"kind": "requirement-new"}`;
  - `supersedes`: optional, the key of an earlier proposal, which must exist.
  - The command writes `id`, `key`, `origin`, `created_at` and `sha256`. The key is
    `p-` plus the first 12 hex digits of `sha256(operation_id)`.
- **`submit --from-feedback ENTRY_ID --file`** promotes one feedback entry
  (`feedback list` shows the ids). The proposal records
  `origin: {type: "feedback", entry_id, digest}`, where `digest` is the SHA-256 of the
  entry. `text` may be omitted from the payload: it is then the entry body, which
  must fit 4,000 characters. The feedback journal is read, never changed. Refused,
  before any write: an unknown entry, an entry a later correction supersedes (promote
  the correction), and a journal that does not validate.
- **`revise --file`** writes the next revision of your own proposal. It adds `key`,
  the next `revision` and `expected_sha256` (the hash of the revision it replaces) to
  the content fields. It is allowed only while the proposal is `submitted` or
  `needs-info`. From `needs-info` it also returns the proposal to `under-review`.
  After a decision, submit a new proposal that `supersedes` the old one.
- A retry with the same `operation_id` and content returns the first result
  (`reconciled: true`). Changed content under the same id is refused.

**`review`, `decide` and `settings` are not client commands.** Over SSH the actor is
self-declared, so everything that rests on the operator allowlist is a host command:
`admin.py proposal-review`, `admin.py proposal-decide` and
`admin.py proposal-settings` ([operations](OPERATIONS.md#operator-commands)). The
client refuses the three names with a message that says so.

**The web service is the other authority.** A signed-in member with `reviews.approve`
triages and decides through the HTTP routes
([HTTP deployment](HTTP_DEPLOYMENT.md#requirement-proposals)), and a member or an
agent submits there with the submitter bound to the account. The endpoint accepts
`review` and `decide` only when the HTTP service launched it and it has re-validated
that capability itself.

**Reserved actor shapes.** An actor shaped like an HTTP account or agent id (`usr_`
or `agent_` plus 16 hex digits, also as the head of `ID/...`) is refused on every
action unless the HTTP service launched the endpoint. Declare your own session actor.

**States.** `submitted`, `under-review`, `needs-info`, `escalated-to-owner`,
`approved`, `incorporated`, `rejected`, `duplicate-of`. The last three are terminal.
`--state` also accepts the short forms `escalated` and `duplicate`.

| From | To | Written by |
| --- | --- | --- |
| `submitted` | `under-review` | a coordinator (`proposal-review`) |
| `under-review` | `rejected`, `duplicate-of`, `needs-info`, `escalated-to-owner`, `incorporated` | a coordinator |
| `needs-info` | `under-review` | the submitter's `revise` |
| `escalated-to-owner` | `approved`, `rejected` | the owner (`proposal-decide`), with a native decision issue |
| `approved` | `incorporated` | a coordinator |

**Reading.** Reads take no lock and write nothing.
- **`get KEY [--history N]`** returns `state`, `stale`, `due`, `next_actor`,
  `next_action`, `revision`, `sha256`, `record_comment_id`, `disposition_comment_id`,
  `submitter`, `identity`, `target`, the excerpts, `supersedes`, `supersedes_chain`,
  `supersedes_warning`, `superseded_by`, `superseded_by_total`, `submitted_at`, `age_days`, `time_to_disposition_days`, `linked_requirement`,
  `disposition` (the newest counted one), `timeline` (the newest N, 1..50, default
  10), `inert_dispositions` and `warnings`.
- **`list`** returns `{total, items, next_offset, coverage}`, oldest first. Filters:
  `--state`, `--target` (a requirement key or an area), `--submitter`; `--limit`
  1..100 (default 20) and `--offset`. Each item, and `get`, carries
  `incorporated_unaccepted`: the reader's own judgement that an incorporated proposal
  is not the accepted requirement, so a caller never derives it from
  `acceptance_state`.
- **`mine --submitter IDENTITY`** is the same list for one person, with the full
  newest disposition, `next_action` and `linked_requirement`. A session actor is not
  a durable identity, so the identity is named.
- **A reason, a question and an escalation question** are returned by `get` and
  `list` only to an actor on the operator allowlist; otherwise the field is `null`
  and `withheld` is `true`. `mine` returns them, because the submitter must be able
  to read the question they answer.
  - **This is a filter, not confidentiality.** Over SSH the actor is self-declared,
    and so is `--submitter`. Anyone with endpoint access who declares an operator's
    actor name reads these fields on `get` and `list`, and `mine --submitter
    IDENTITY` returns them to any actor that names that identity. Coordinators must
    not put secrets in a reason, a question or an escalation question.
- **Excerpts are cleaned.** Every excerpt drops the characters in Unicode categories C
  (controls including C1, format characters such as bidi overrides and zero-width
  characters, private use) and Z (separators) other than the plain space. A line
  break, a tab or another separator becomes one space; the `text` and `rationale` of
  `get` keep line feeds. `omitted_chars` counts the cleaned text. The stored record
  is not changed.
- **`supersedes` and `superseded_by`.**
  - `superseded_by` lists the proposals whose records name this key. They are found
    by the reserved label `proposal:supersedes:<key>` on their anchors, which a
    contributor cannot write, replace or remove. At most 100 are read;
    `superseded_by_total` is how many anchors carry the label.
  - `supersedes_chain` lists the keys this proposal supersedes, nearest first. The
    walk stops after 8 hops, at a cycle, or at a proposal that cannot be read, and
    `supersedes_warning` then says which; otherwise it is `null`.
- **`identity`** is `verified` when the native author of **every** revision stands
  for the proposal's `submitter`.
  - For a `person:` submitter: the author maps to it through the project's actor map.
  - For an `account:` submitter: the revision was written through the web service (its
    author is the account itself, or the agent that the revision's
    `submitted_by_agent` names). Nothing else counts. An SSH actor that the actor map
    maps to that account is still not the account: the map joins people for the
    no-self rules and never makes a record read as written by a web account.
  - Otherwise it is `unverified`; when revision 1 was the submitter's and a later one
    was not, an `identity-broken` warning names that revision. Over SSH this is
    attribution, not authentication: the actor is self-declared, and no authority
    rests on it.
- **Who may revise.** Only the submitter. A proposal with an `account:` submitter is
  revised only through the web service, mapped actor or not. A proposal with a
  `person:` submitter is revised over the endpoint by an actor the map resolves to
  that person, or by the actor that wrote every earlier revision (an unmapped
  contributor revising their own proposal). Repeating the stored `submitter` in the
  payload is never enough.
- **`mine` over the endpoint is a declared query**, as in slice 1a: it lists every
  proposal whose `submitter` is the identity named, each with its `identity`
  (`verified` or `unverified`), so an unmapped contributor sees their own
  submissions. Through the HTTP service the same read lists verified proposals only
  and adds `unverified_omitted`: there the submitter is an authenticated account.
  `list` and `mine` take `--order oldest|newest` (default oldest). `get` returns
  `deciders`, the configured owner deciders.
- **Help is a command.** `proposal --help` and `proposal list --help` return the help
  payload. `--help` in an option's value position is that option's bad value.
- **`linked_requirement`** is `{id, revision, sha256, acceptance_state,
  manifest_sha256}` for an incorporated proposal. `acceptance_state` is read live
  from the requirement record: `accepted`, `draft` or `missing`.
  - `accepted` means the linked content is the accepted requirement today: the record
    is accepted, its newest revision is the accepted one, and that revision has the
    same title, description and key as the linked revision.
  - `draft` covers everything else that still exists: never accepted, demoted, or
    replaced by a later revision with different content (accepted or not).
- **Inert records.** A disposition counts when its native author is on the operator
  allowlist (a host command wrote it) or is an HTTP account (the web service wrote it
  for a member who held `reviews.approve` at that moment). Any other disposition does
  not count: `timeline[].standing` is `inert`, `inert_dispositions` counts them, and
  the state is the one the trusted records give. Web authority is checked when the
  disposition is written, not when it is read.
- **Failures stay per proposal.** A malformed record, a state label that disagrees
  with the ledger, or a newer record version makes that one proposal read `malformed`
  or `unsupported`; lists and the queue keep working and name it in `coverage`.
- **Read cost.** `get` reads its own anchor and the settings in one `bd list` and one
  `bd show`, then one `bd list` for the proposals that supersede it (and one `bd show`
  when there are any). A proposal that itself supersedes another adds one `bd list`
  and one `bd show` per hop, at most 8. `list` and `mine`
  read the proposals in two native reads (one `bd list`, then one `bd show` up to 20
  rows or one `bd export --all` above), plus one `bd show` of the requirement records
  the returned page links to.

**The queue in `work`.** `attention.proposal_queue` has the agent attention shape:
`state`, `summary`, `counts`, `actions`, `truncated` and `computed_at`, plus `items`
and `next_offset`.
- `counts`: `submitted`, `under_review`, `needs_info`, `escalated`, `approved`,
  `stale`, `incorporated_unaccepted`, `malformed` and `total`. They are always
  returned, whatever the task filters.
- `state`: `malformed`, `escalated`, `triage`, `stale`, `pending` or `clear`.
- `actions`: each has `priority`, `kind`, `project`, `task` (the proposal's native
  id), `reason`, `links`, `label` (an excerpt object) and `token`, sorted by
  `(priority, project, task)`.
- `items`: only for an actor on the operator allowlist, paged by `--proposal-limit`
  (1..100, default 20) and `--proposal-offset`. Each has `kind` (`requirement`),
  `proposal`, `task`, `state`, `stale`, `age_days`, `submitter`, `identity`, `target`
  and `title` (an excerpt).
- `stale`: an open proposal that has not moved for more than `stale_days` (14).
- `incorporated_unaccepted`: an incorporated proposal whose requirement revision is
  not accepted today.
- At most 1,000 proposals are scanned; beyond that `coverage` says so.

**`brief` items.** A `proposal-review` item has `kind`, `proposal`, `state`,
`age_days`, `trust`, `text` (server-derived, never the proposal text) and `source`
(`proposal get KEY`). `brief` selects the proposals whose target names the briefed
requirement record or an area equal to one of the task's labels; for an actor on the
operator allowlist it adds the oldest proposals waiting for triage, a decision or
incorporation. Rejected and duplicate proposals are never selected.

## IDs and cursor roles

- **Native task ID**: `task`/`items[].task`/the positional argument to `show`, `brief`,
  `review`, `handoff`. It is stable and is what dependencies reference.
- **Comment ID**: `contribution_id`, `contribution.comment_id`, `latest_comment_id`,
  checkpoint `comment_id`. In a `contribute`/`request-changes`/`respond`/`approve`
  payload, `contribution` is the **contribution record's comment ID** — never a Git SHA
  and never `latest_comment_id`.
- **Git commit**: `contribution.commit` / `base_commit` are exact 40- or 64-character
  hexadecimal revisions. They are not comment IDs.
- **`activity_cursor`**: identifies the current task snapshot. `brief` emits it;
  `checkpoint` requires the same task's current token. It does not continue history.
- **`next_cursor`**: continues one snapshot-bound `history` page. Omit it (and repeat
  `--since`, if used) to start a new read; it fails explicitly when its saved snapshot
  is unavailable.
- **Activity-feed cursor**: the separate project-wide novelty cursor used by
  `activity.py`.

Copy cursor tokens exactly; never retype or elide them. Display truncation surfaces as
a clear refusal, not a wrong read.

## Documented limits

| Command | Option | Range |
| --- | --- | --- |
| `work` | `--limit`, `--handoff-limit` | 1..100 |
| `work` | `--offset`, `--handoff-offset` | >= 0 |
| `work` | `--state` | one of the documented review states |
| `work` | `--ref-limit` / `--ref-offset` | 1..100 (default 20) / >= 0 |
| `work` | `--capability-limit` / `--capability-offset` | 1..100 (default 20) / >= 0 |
| `work` | `attention.capability_index` | first 1,000 capabilities; `unverified_stale` after 30 days |
| `brief` | `attention` | at most 3 items of each kind |
| `work` | `--proposal-limit` / `--proposal-offset` | 1..100 (default 20) / >= 0 |
| `proposal` | `text` / `rationale` | 1..4,000 / <= 4,000 characters |
| `proposal` | `evidence` / `attachments` | <= 20 links of <= 2,000 characters / <= 10 |
| `proposal` | `reason` / `question` | <= 2,000 characters |
| `proposal list`, `mine` | `--limit` / `--offset` | 1..100 (default 20) / >= 0 |
| `proposal get` | `--history` | 1..50 (default 10) |
| `proposal` | title excerpt in lists and the queue | <= 160 characters |
| `proposal` | proposals scanned by a list or the queue | first 1,000 |
| `admin.py proposal-settings` | actor map / deciders | <= 200 actors, 100 namespaces / <= 50 |
| `brief` | `--items-offset` | >= 0 |
| `brief` | `--items-limit` | 1..10 |
| `history` | `--limit` | 1..20 |
| `history` | `--body-budget` | 256..8000 encoded bytes |
| `review` | `items`/`resolutions` | 1..20 entries |
| `review` | `summary` | <= 1200 characters |
| `checkpoint` | `open_items` / `resolved` | <= 100 each |
| `checkpoint` | item `text`/`reason` | <= 400 characters |
| `checkpoint` | item `source`/`evidence` | <= 240 characters |
| `checkpoint` | whole payload | <= 80 KB canonical bytes |
| any checkpoint error | listed unknown/missing field names | <= 8 names, each <= 60 characters |
| forwarded `native stdout note:` lines | individually re-labelled | <= 8; the rest withheld with a count and digest |
| data documents in one native stdout stream | one document, or a line-mode row stream | more than one document with a multi-line document present is refused, not silently picked |
| `capability lookup` | `--limit` | 1..20 (default 5) |
| `capability lookup` | phrase | <= 200 characters |
| `capability resolve` | pointers | 1..100, each <= 400 characters |
| `capability` | `--max-graph-mb` | 1..512 (default 64); at most 500,000 nodes and 2,000,000 links |
| `capability` | indexed files | first 20,000 files; files over 2 MB skipped; 256 MB in total |
| `capability` | entries | 200,000 in total; 2,000 headings per Markdown file and 5,000 definitions per Python file (the rest counted as `entry-limit`, `heading-limit`, `definition-limit`, each with a warning); `resolve` still reads a whole file |
| `capability` | Python nesting | source decoded with its declared encoding (UTF-7 cookie cannot hide operators); parsed on a 512 MB (Windows: 255 MB) worker stack, trusted to at most 50,000 levels, retrying 255 MB if the 512 MB thread cannot start; a flat or statement-dense file over 300,000 expression tokens (operators, brackets, commas, NAME tokens and depth-zero statement separators) is `too-complex`; fallback guard 5,000 levels per logical line (Windows: 2,000) with a 16 MB per-run tokenize budget (`too-complex`); `parse-error` skips warn with counts |
| `capability` | Markdown | heading lines over 1,000 characters are text; a summary is looked for in the 40 lines after its heading |
| `capability` | entry `aliases` / `tests` / `related` | first 8 shown; `tests_total` / `related_total` count all |
| `capability` | entry `name` / `summary` | excerpt objects of <= 120 / <= 200 characters |
| `ref list` | `--limit` / `--offset` | 1..100 (default 20) / >= 0 |
| `ref` | `key` / `title` / `statement` | <= 80 / <= 200 / <= 2,000 characters |
| `ref` | `tags` / `review_by` | <= 12 slugs of <= 32 characters / at most 24 months ahead |
| `ref` | `authority.url` | `https`, no userinfo, <= 2,048 characters |
| `ref` | due-soon window | 30 days (fixed in slice 1) |
| `capability find` | phrase / `--limit` | <= 200 characters / 1..20 (default 5) |
| `capability list` | `--limit` / `--offset` | 1..100 (default 20) / >= 0 |
| `capability misses` | `--limit` | 1..100 (default 20) |
| lookup-miss log | phrases / new phrases / phrase length | 500 per project (phrases first seen in the last 2 hours protected; otherwise lowest count evicted, oldest first among equals) / 60 per project per clock hour (the rest counted in `overflow`) / <= 80 characters after normalising (else counted in `dropped`) |
| `capability` record | `name` / `summary` / `aliases` | <= 120 / <= 1,200 characters / <= 32 phrases of <= 80 |
| `capability` record | `code` / `tests` / `anchors` / `requirements` / `tags` | <= 32 / 32 / 16 / 16 / 8, pointers <= 400 characters |
| `capability propose-alias` | pending aliases | person 3 per capability and 50 per project; unverified pool 1 and 10; 20 per capability |
| `admin.py capability-apply` | batch items | 1..100 |
| `capability list` | `--pointers` | adds `record_sha256`, `code`, `tests` and `anchors` to each row |
| `capability verify` | `results` / `reason` | <= 80 results; `reason` is a code of <= 40 characters |
| `capability verify` | untrusted passing reports | <= 20 per capability revision; failing reports are not counted |
| `capability verify` | open failing reports | unverified pool 5 per project; 10 per verified person; trusted uncapped |
| `admin.py capability-verify` | batch items | 1..500 |
| `views/CAPABILITIES.md` | accepted capabilities shown | first 500; summary excerpt <= 300 characters |

Out-of-range values fail with a nonzero exit code and an error that names the option or
field **and** the limit, for example:
`open_items[0].text: expected text up to 400 characters` or
`Invalid work page: --limit and --handoff-limit must be 1..100; ...`.

## Common mistaken flags and fields

| Mistake | Correct form | Error hint |
| --- | --- | --- |
| `work --review-state X` | `work --state X` | `hint: use --state for the review-state filter` |
| `work --assignee X` | `work --owner X` or `work --mine` | `hint: use --owner ACTOR or --mine` |
| `work --task X` / `work --review X` | `review X` for one task | `hint: work lists the queue; use "review TASK" for one task` |
| `--mine --owner X` | one of the two | argparse mutual-exclusion error |
| `brief --limit N` | `brief --items-limit N` | `hint: use --items-limit for the unresolved-item page size` |
| `brief --offset N` | `brief --items-offset N` | `hint: use --items-offset for the unresolved-item page offset` |
| `previous = latest_comment_id` in a review payload | `contribution = contribution.comment_id` | review workflow names the misused ID |
| reading `brief.owner` as a string | read `brief.owner.text` | excerpt-object contract above |
| `work --owner -h` | `work --owner ACTOR` | argparse `expected one argument`; `-h` is the option's value, not a help request |

## Wrapper option ordering

`beads.cmd` (Windows) and `beads.sh` (POSIX) forward their arguments unchanged to
`client.py`. Put client options before the `--` separator and the endpoint action after
it:

```sh
# POSIX
sh /path/to/kit/beads.sh --config client.local.json --project example \
  --actor alex/session1 -- work --mine --json

# Windows CMD
C:\path\to\kit\beads.cmd --config client.local.json --project example ^
  --actor alex/session1 -- work --mine --json
```

The wrappers use one interpreter: `BEADS_PYTHON` if set (one executable path, no
flags), otherwise `python` on Windows and `python3` on POSIX. Configuration and
attachment paths are resolved relative to the caller's directory unless absolute.

### PowerShell capture

Windows PowerShell 5.1 `>` / `Out-File` does not write UTF-8. Executed on Windows
PowerShell 5.1 (revision probe; its raw logs are kept outside this repository):

| Capture | Bytes written | Decodes as |
| --- | --- | --- |
| PowerShell `-NoProfile` `>` | `FF FE ...` (UTF-16LE with BOM) | `utf-16`, not `utf-8` |
| PowerShell with a profile that sets `$PSDefaultParameterValues['Out-File:Encoding']` | `EF BB BF ...` (UTF-8 **with BOM**) | `utf-8-sig`, not `utf-8` |
| `cmd /c ... > file` | plain UTF-8, no BOM | `utf-8` |
| client `--out FILE` | plain UTF-8, no BOM, LF endings | `utf-8` |

Preferred capture, in order:

```powershell
# 1. Client-owned capture: the client writes UTF-8 without a BOM; stdout stays empty.
python client.py --config client.local.json --project example --actor alex/session1 `
  --out queue.json -- work --json

# 2. CMD redirection, clean UTF-8 whether or not PowerShell has a profile.
cmd /c "python client.py --config client.local.json --project example --actor alex/session1 -- work --json > queue.json"

# 3. Capture in-process and write UTF-8 explicitly.
python -c "from pathlib import Path; from client import request; from requirements import load_json; r=request(load_json('client.local.json'),'example','alex/session1',[],action='view',path='issues.jsonl'); assert r['returncode']==0,r['stderr']; Path('issues.jsonl').write_text(r['stdout'],encoding='utf-8')"
```

`--out` is a client option placed before the `--` separator, like `--config`. On a
nonzero exit it writes no file and leaves the failure on `stderr` with the command's
exit code.

If a PowerShell-redirected file already exists, detect the encoding from its BOM
instead of assuming one:

```python
raw = open('queue.json', 'rb').read()
encoding = ('utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff')
            else 'utf-8-sig' if raw[:3] == b'\xef\xbb\xbf' else 'utf-8')
payload = json.loads(raw.decode(encoding))
```

Passing arguments as a literal array (`subprocess.run([...])`, or PowerShell's normal
argument passing) is supported; the client cannot detect a flag that a shell dropped
before Python started.

## `create-child` version difference

`coordinate`/`create-child` is endpoint-side: the **server kit** must be a version that
supports the `coordinate` action (this contract, `0.1.0`+). A client on a newer kit
talking to an older endpoint fails with `ValueError: Unknown action` from that endpoint;
`onboard` reports client/kit version and provenance parity so the mismatch is visible
before mutating. The native `create` call uses `--no-inherit-labels` and, for
`type=decision`, `--validate`, both of which require the pinned Beads 1.2.2. Request IDs
and content hashes are durable, so an uncertain create is reconciled with the same
request ID instead of creating a duplicate.

## Backward compatibility

Consumers should key on documented fields, tolerate additional fields, and treat any
nonzero exit code as failure. The envelope, `work` top-level shape, `brief`
excerpt-object fields, opaque cursors and the ID meanings above are stable for v1.
