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

`work`, `review`, `handoff`, `brief`, `history` and `checkpoint` answer `-h`/`--help`
on stdout with exit code `0`:

```sh
b work --help
b review --help
b handoff --help
b brief --help
b history --help
b checkpoint --help
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

`brief` decodes unresolved items as `open_items[]` with `id`, `kind`, `text`, `source`;
`history` pages entries with `entry_id`, `body`, `body_offset`, `body_total_chars` and
`continued`, so a long body is reassembled by concatenating fragments in offset order.

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
