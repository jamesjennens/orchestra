# Operational workflow

For compact per-task reads, use [briefings and checkpoints](BRIEFINGS.md): `brief`, `checkpoint` and snapshot-paginated `history`. These preserve provenance and unresolved items independently of lifecycle facts.

These examples use synthetic project `example`, task `example-task` and actor `alex/session1`. Substitute existing project/task IDs, real evidence and your own explicit actor. Run Python commands from the kit directory; keep configuration, payloads, exports and cursors outside committed source. The server must run a kit version supporting the `lifecycle` and `coordinate` actions.

## Private project feedback

Feedback is a project-scoped append-only stream, separate from task claims and native
issue comments. It is stored in the coordination runtime as `.feedback.jsonl`, is not
included in generated Git views or source exports, and is retained by the native
backup/restore pair. Use an explicit operation ID so an uncertain write can be
reconciled safely:

```json
{
  "operation_id": "alex/feedback-001",
  "created_at": "2026-09-19T23:00:00+00:00",
  "body": "Observed behavior and bounded recommendation.",
  "source": {"task": "example-task", "version": "base-commit-or-release"},
  "evidence": ["test://fixture/1"],
  "triage": {"task": "example-task", "label": "review"},
  "reminder": {"kind": "checkpoint", "text": "Mention during handoff"},
  "supersedes": null
}
```

Submit with `feedback add --file payload.json`; use `feedback correct --file
payload.json` with `supersedes` set to the original entry ID for an additive
correction. Retry the exact same payload after an uncertain response. Retrieve
bounded live pages with `feedback list --limit 20`, then pass the returned opaque
`next_cursor` to `--cursor` while more pages remain; `resume_cursor` is always
available for polling after a final or empty page. Entries appended after a page are
visible on resume. Each response includes a durable `watermark`, including final and
empty pages. Cursors are bound to the project/feed path and cumulative digests of
both the exact resumed prefix and the entire prefix through the observed end
watermark; foreign, ahead, rollback, or changed-prefix cursors are rejected even
after later appends. Pagination is live rather than a frozen
snapshot, so a caller should retain `resume_cursor` for polling. Entries are never
rewritten. A torn final JSONL record is quarantined idempotently and the validated
acknowledged prefix remains readable; a valid final record without a newline is
normalized before append, while a fully newline-terminated invalid record still
fails visibly. Quarantine evidence and the recovered prefix are published through
same-directory atomic replacements; interrupted publication leaves the original
tail readable for retry. JSONL records split only at LF, so Unicode line separators
inside JSON strings round-trip normally. `reminder.kind` may be
`none`, `checkpoint` or `handoff` and is informational only; it never interrupts a
worker.

Durable `.feedback.jsonl.*.incomplete` tails are included as base64 bytes in the
coordination backup sidecar and restored unchanged. A later feed read removes
orphaned temporary recovery files left by a killed process. If an existing legacy
truncated-digest quarantine name contains different bytes, recovery preserves it
and retries under the full-digest name; it never overwrites conflicting evidence.

## Connect and refresh

SSH remains the default configuration:

```json
{"host":"beads-team","endpoint":"/home/beads/beads-team-kit/endpoint.py","root":"/home/beads/beads-runtime"}
```

For a terminal already on the Linux coordination server, explicitly select local transport:

```json
{"transport":"local","python":"/usr/bin/python3","endpoint":"/home/beads/beads-team-kit/endpoint.py","root":"/home/beads/beads-runtime"}
```

Save the selected configuration as `client.local.json`. Local transport invokes the endpoint directly without SSH or a shell. It does not elevate permissions: the terminal user must have the service account's required runtime access. Another account on the same server does not automatically have that access. Unknown transports fail; there is no fallback.

```sh
python client.py --config client.local.json --project example --actor alex/session1 -- refresh
python client.py --config client.local.json --project example --actor alex/session1 -- view CURRENT.md
```

On Windows, invoke `C:\path\to\kit\beads.cmd` with the same client arguments; on Linux, use `sh /path/to/kit/beads.sh`. Both resolve `client.py` beside the wrapper, work from another current directory and return the client exit code. Configuration and attachment paths remain relative to the caller unless absolute. Set `BEADS_PYTHON` to one interpreter executable path if needed; otherwise the wrappers use `python` and `python3` respectively. No PowerShell execution-policy change is required. Quote shell arguments normally; use UTF-8 attachment files for substantial prose.

Refresh generates `CURRENT.md`, `COORDINATION.md`, issue pages and journals. Do not append to or hand-edit these views on contribution branches. Put assertions, corrections and pointers in Beads, then refresh. Keep stable working rules in the project's entry-point documentation.

## Six independent lifecycle facts

The dimensions are `implemented`, `tested`, `reviewed`, `integrated`, `deployed` and `live-verified`. Each accepts `unknown`, `pending`, `passed`, `failed` or `not-applicable`. Closing an issue implies none of them; requirement acceptance is separate.

First register the exact scope to which subsequent evidence applies. Save this as `scope-event.json`:

```json
{
  "schema_version": 1,
  "operation_id": "alex/scope-001",
  "task": "example-task",
  "dimension": "lifecycle-scope",
  "value": "compute-below",
  "scope": {
    "source_commit": "1111111111111111111111111111111111111111",
    "integration_commit": "",
    "release_id": "",
    "environment": "development"
  },
  "evidence": [],
  "provenance": "performed",
  "actor": "alex/session1"
}
```

Compute the scope hash using the kit's canonical JSON convention, then submit:

```sh
python -c "from pathlib import Path; from requirements import load_json,content_hash,canonical_bytes; p=load_json('scope-event.json'); p['value']=content_hash(p['scope']); Path('scope-event.json').write_bytes(canonical_bytes(p))"
python lifecycle.py record --config client.local.json --project example --actor alex/session1 --file scope-event.json
```

Save a matching fact as `tested-event.json`:

```json
{
  "schema_version": 1,
  "operation_id": "alex/tested-001",
  "task": "example-task",
  "dimension": "tested",
  "value": "passed",
  "scope": {
    "source_commit": "1111111111111111111111111111111111111111",
    "integration_commit": "",
    "release_id": "",
    "environment": "development"
  },
  "evidence": ["https://example.org/builds/123/test-report"],
  "provenance": "performed",
  "actor": "alex/session1"
}
```

```sh
python lifecycle.py record --config client.local.json --project example --actor alex/session1 --file tested-event.json
```

Replace the synthetic commit and report before use. `passed`, `failed` and `not-applicable` require evidence pointers. `performed` means the contributor performed the work; `reported` means another source reported it; `imported` denotes imported evidence. These declarations provide attribution, not authenticated identity or proof that an assertion is true.

Passed implementation/test/review facts require `source_commit`; integration requires `integration_commit`; deployment/live verification require `release_id` and `environment`. All four scope keys are always present. Changing any scope field requires a new scope event. Facts from another scope display `unknown`; retain their historical evidence and record new facts only when justified for the new scope.

Use a new operation ID for each new assertion or correction. After an uncertain response, inspect canonical events/state first; any retry must use the **same operation ID and exact payload**. Reusing an ID with different content is refused. An interrupted repeat-value update may temporarily appear unknown until reconciled. Never invent a successful outcome from a timeout.

Raw `bd` labels alone are insufficient evidence: an old `tested:passed` label can remain after scope changes. The contextual `lifecycle.py list` output is the authoritative lifecycle view; it checks scope, native event order, payload attribution and label agreement. Missing, mismatched or ambiguous evidence becomes unknown.

Three payload fields are optional and additive: `note` (a per-task note, any fact), `trigger` (a free-text next trigger, only on a `pending` fact) and `defect_task` (only on `enabled=enabled-with-known-defect`, below). Every payload that validated before still validates; a kit that does not know one of these fields reads that fact as `unknown`, never as `passed`.

## Record one release deploy for many tasks

A release covers every task already integrated into it, so one operation records the release scope and the deployed fact for all of them instead of one command per task. Save the canonical export as above, keep a Git checkout that can see the release commit, and save `release.json`:

```json
{
  "schema_version": 1,
  "operation_id": "alex/release-2026-10-05",
  "dimension": "release-deploy",
  "value": "passed",
  "scope": {
    "source_commit": "",
    "integration_commit": "5555555555555555555555555555555555555555",
    "release_id": "release-2026-10-05",
    "environment": "production"
  },
  "evidence": ["https://example.org/builds/987"],
  "provenance": "performed",
  "actor": "alex/session1",
  "live_verified": false,
  "targets": []
}
```

```sh
python lifecycle.py release --config client.local.json --project example --actor alex/session1 --file release.json --export issues.jsonl --repo /path/to/checkout --dry-run
python lifecycle.py release --config client.local.json --project example --actor alex/session1 --file release.json --export issues.jsonl --repo /path/to/checkout
python lifecycle.py release --config client.local.json --project example --actor alex/session1 --file release.json --export issues.jsonl --repo /path/to/checkout --live-verified
```

Run `--dry-run` first: it prints the resolved `targets`, the tasks it `skipped` with a reason, any `flags`, the `total_targets`, the `chunks` plan and an `expected_seconds` estimate, and writes nothing. Selection is what is NEW in this release. A task is a target only when its chosen (newest) trusted `integrated=passed` scope is an ancestor of the release commit — decided by `git merge-base --is-ancestor` in `--repo` — is not named by an operator `Kind: integration-revert-v1` revert record, and has not already been recorded `deployed=passed` for this environment in a release that contains that integration commit and is itself contained in this release. That last rule is what stops a second release from rewriting the scope and deployed fact of every task ever integrated. A task whose integrated events cannot be trusted (raw native `set-state`, two `integrated:` labels, ambiguous event order, or an untrusted scope) is reported under `skipped` with that reason instead of being dropped; a task whose passing integration commit is missing from the checkout (a shallow clone), not a full lowercase commit, or undecidable is likewise listed under `skipped`, never guessed. `--previous-release-commit PREV` restricts the selection to integrations in `PREV..R`.

Because the selection is a subset, `targets` in the input file is an optional caller subset: task names, or full `{task,source_commit,integration_commit}` entries the command checks against its resolution (a caller may also leave it empty). `--target TASK` adds one name and may repeat; `--page-size N --page P` page through a large resolution. One request still covers at most 200 targets, so the command splits the write into request groups of `--chunk-size` (default 50) and the project lock is released between groups: the whole operation can therefore cover more than 200 tasks, and no single request approaches the client's 150 s timeout. `--dry-run` prints `expected_seconds`, a conservative upper bound for the resolved page. Measured on the authoritative Linux host against a synthetic 900-row export (300 decoy tasks, 200 target tasks, 400 lifecycle events): 200 targets took **0.106 s with ONE export and 400 native writes** (0.0005 s per target), and the exact retry took 0.131 s and reconciled every target; the same 200 targets with `live_verified` took 0.123 s with one export and 600 writes. That harness replaces the `bd` binary, so it isolates the change that matters — the pre-fix path paid a full export per fact (measured by the reviewer at 731 s, 3.65 s per target, 249 s for the exact retry); with one export per request the remaining cost is the native writes themselves, now bounded per group.

Each target keeps its own `source_commit`/`integration_commit` and adds the release `release_id` and `environment`, so the recorded scope is the one a hand-written scope event would have. Every target is verified against ONE export read once for the whole request, and every derived per-task operation ID is checked for a conflicting planted fact, before the first write: a stale target or a used ID refuses the whole group and nothing is written.

The endpoint then records the scope event (only when it differs from the task's current scope), one `deployed=passed` fact carrying the shared evidence block and the target's optional `note`, and, with `--live-verified` or `"live_verified": true`, one `live-verified=passed` fact. The note rides the deployed fact ONLY: the scope event keeps exactly its four scope fields, so a kit that predates the optional fields (and rejects unknown ones) still reads the scope. Add `"note": "..."` (one line, up to 500 characters) to a target for a per-task note. Deterministic per-task operation IDs make an exact retry reconcile instead of duplicating; changed evidence or notes need a new operation ID.

Because the release scope becomes each target's current scope, the six-fact readers (`brief`, `work`, `lifecycle.py list`) show `integrated=unknown` (and any earlier-scope fact unknown) for every target until it is recorded for the release scope; the per-scope integration evidence and the review reads are unaffected, and the command prints that reminder as `reader_note`.

Ancestry is checked ONLY in the client: the endpoint re-verifies that each target's `source_commit`/`integration_commit` is a trusted `integrated=passed` scope in its own export, but it does not run git and cannot tell whether that commit is an ancestor of the release. The target list is therefore an assertion under the kit's existing trust model, like `commit`/`base_commit` in contribution review.

When a selected target's `source_commit` is not the task's current contribution (a release shipping a superseded revision, or a revision replaced while its successor awaits review), the dry run and the result carry a `flags`/`flag` entry naming the delivery and the current contribution, and `brief`/`work` show `deployed_delivery` (release, environment, source commit) plus `deployed_delivery_is_current_contribution`.

## Read what a deployed release still owes

```sh
python lifecycle.py evidence-owed --export issues.jsonl
```

Derives the list from the whole trusted scope history, not only each task's current scope, and groups it by environment and release. It prints, for every recorded scope whose `deployed` is `passed`, `pending` or `failed`, the task, `deployed`, the `enabled` flag (below), `remaining_evidence`, `responsible` and `next_trigger`. A later release or another environment therefore never hides an earlier debt: a task live-verified on staging but not on production and then released again to production still shows the production `live-verified` owed for the first release. `remaining_evidence` is `deployed`/`live-verified` when they are not `passed`/`not-applicable`, plus any dimension explicitly recorded `pending` or `failed` in that scope. `responsible` is the task's assignee, falling back to the actor that recorded the deployed fact. `next_trigger` is the free-text `trigger` carried by a pending fact:

```json
{
  "schema_version": 1,
  "operation_id": "alex/verify-001",
  "task": "example-task",
  "dimension": "live-verified",
  "value": "pending",
  "scope": {"source_commit": "1111111111111111111111111111111111111111", "integration_commit": "", "release_id": "release-2026-10-05", "environment": "production"},
  "evidence": ["plan:nightly-job-42"],
  "provenance": "performed",
  "actor": "alex/session1",
  "trigger": "after the nightly job on 2026-10-06"
}
```

Reading changes nothing, and a deployed release with no trusted scope is never invented.

## Switched on but not effective

`deployed=passed` says the release is live; it does not say the feature works. Record that separately with the `enabled` dimension, whose values are `enabled`, `disabled` and `enabled-with-known-defect`. The defect value names the task that will fix the problem, so `deployed=passed` no longer reads as fully done while the flag is on and broken:

```json
{
  "schema_version": 1,
  "operation_id": "alex/enabled-001",
  "task": "example-task",
  "dimension": "enabled",
  "value": "enabled-with-known-defect",
  "scope": {"source_commit": "1111111111111111111111111111111111111111", "integration_commit": "", "release_id": "release-2026-10-05", "environment": "production"},
  "evidence": ["issue:example-defect-7"],
  "provenance": "performed",
  "actor": "alex/session1",
  "defect_task": "example-fix-task"
}
```

`enabled` is deliberately outside the six facts, so `brief`, `work` and `lifecycle.py list` keep their documented shape; `evidence-owed` shows the flag and the fixing task beside `deployed=passed`. An `enabled` fact uses the same scope rules and the same trust rule as the six facts: unattributed, unscoped, ambiguous or tampered events read `unknown` rather than being guessed. `defect_task` must name an existing task: a fact that points at no task is refused before it is written. Nothing here infers `enabled` from `deployed`, and no release write records `enabled` on its own.

## Create children without guessing IDs

Save `child.json`:

```json
{
  "operation": "create-child",
  "request_id": "alex/child-001",
  "parent": "example-job",
  "title": "Add a parser fixture",
  "description": "Intent: cover the agreed input. Scope: tests/parser. Acceptance: fixture passes and the task records its commit.",
  "type": "task"
}
```

```sh
python coordination.py --config client.local.json --project example --actor alex/session1 --file child.json
```

When children will run in parallel, include the task template's `Owns / must not
change` declaration in `description`, so each child states the files or areas it may
change and the files or areas it must not change before it is authorized.

Use the returned native child ID in all references. The request is bound to its actor and content, reserved durably, and marked on the native issue so a retry can reconcile instead of creating a duplicate. Inspect uncertain outcomes and retry only the same request. A reserved request with **no visible native issue** stops for operator reconciliation; do not bypass it with a new request ID or delete its reservation. Duplicate native matches also require reconciliation.

Before anything is reserved, `create-child` runs the **identical create with `--dry-run`** (plus `--validate` for a `decision`). bd is the single source of truth for validation: the preflight writes nothing, so a refused description or a missing parent leaves the request ID free for corrected content instead of stranding it. The kit never re-implements bd's section matching.

A real create that exits nonzero **after** a passing preflight is not proof that nothing was written: bd can commit the issue and then fail (for example while adding the request label), and that commit may not be visible to an immediate label read. Such a request stays `pending` and must be reconciled; a retry under the same ID is refused rather than risking a duplicate.

Type templates are still documented here as help. For a `decision`, `bd create --type decision --validate` expects the section names `Decision`, `Rationale` and `Alternatives Considered`; bd matches them case-insensitively as text, so headings, bold text and prose mentions all qualify. The required-heading hint is added to the create-child error only when bd itself reports missing sections; unrelated errors such as a missing parent are returned unchanged.

Release or complete a stuck reservation with the operator command, which inspects the pending request under the project lock, checks native state, and records the actor, reason and disposition in the receipt:

```sh
python admin.py --root /srv/runtime reconcile-request example --request-id alex/child-001 --actor operator-1 --reason "native validation refused before the fix" --disposition released
```

`--actor` must be on the deployment operator allowlist (`admin.py operators list`); the command refuses anyone else before it reads the receipt.

`released` (default) and `failed` confirm natively that **no** issue exists, then free the ID. When the receipt records its original actor, that actor stays bound to the freed ID; `released --any-actor` deliberately opens it to any actor. A receipt that records **no** actor (the older `{sha256, status: pending}` reservations) refuses `failed`/`released` unless `--any-actor` is supplied on that same first call, because otherwise the ID is bound to nobody and can never be resubmitted; an actorless release already recorded by an older build can still be upgraded with a later `released --any-actor`. `complete` requires an explicit `--issue-id`: it completes the receipt from that single labelled native issue and prints the issue's parent, creator and title in the confirmation, and it refuses an issue that does not carry the matching `request-content:` label (a planted or foreign issue with the guessable `request:` label alone). If a commit may exist without its request label, the automated label check cannot see it, so inspect native state by title and parent before releasing. The command is idempotent for an identical retry (reporting `already: true`, including an identical `complete`), refuses a *differing* retry with the recorded audit instead of silently keeping the first one, and refuses `complete` when no labelled native issue exists.

Operator runbook for a stuck reservation:

1. Inspect native state by title and parent (`bd show`/`list`) so you know whether an issue exists.
2. If an issue exists, run `--disposition complete --issue-id <id>` and confirm the printed `title`, `creator` and `parent` are the expected child; if the issue lacks the matching `request-content:` label, stop and investigate instead of completing.
3. If no issue exists and the receipt records an actor, release it (the original actor resubmits). If you must hand the ID to another actor, add `--any-actor`.
4. If no issue exists and the receipt records **no** actor, pass `--any-actor` on this first release (a plain release is refused); then any actor may resubmit the same request ID.
5. Never delete the reservation or allocate a new request ID to bypass a refusal.

## Integrate through one project-wide merge slot

Save each desired operation to `merge.json` and invoke:

```sh
python coordination.py --config client.local.json --project example --actor alex/session1 --file merge.json
```

The payloads are:

```json
{"operation":"merge-create"}
```

```json
{"operation":"merge-check"}
```

```json
{"operation":"merge-acquire","task":"example-task","target":"main@expected-base-commit"}
```

```json
{"operation":"merge-release"}
```

Initialize the slot once, check it, acquire before integration, inspect the returned `acquired` value, and release when finished. `add-project` provisions the slot for a new project, so this is only needed for a project that predates that or whose slot was lost; when no slot exists, `merge-check`, `merge-acquire` and `merge-release` refuse with an error naming `merge-create` instead of returning a contention-shaped `acquired:false`. A competing holder returns `acquired:false`; a successful command exit alone does not mean you acquired the slot. Only the current actor-holder may release it. Same-holder reacquisition reconciles only the exact recorded task and target; changed or missing context requires inspection and explicit handoff. After an uncertain acquisition, check native ownership and retry the same context.

This is one integration slot for the project, not a lock for each file and not enforcement of repository merge permissions. Develop in separate checkouts. Declare likely file/interface impacts in each task. Keep a short list of high-conflict paths in the project entry point—for example shared schemas, central configuration and generated manifests—and agree that contributions touching them use the slot for conflict resolution and integration. A paused holder leaves a checkpoint and coordinates release/handoff; another worker does not silently take over.

## Read lifecycle and activity from one export

After refresh, save the canonical export as UTF-8 without depending on shell redirection encoding:

```sh
python -c "from pathlib import Path; from client import request; from requirements import load_json; r=request(load_json('client.local.json'),'example','alex/session1',[],action='view',path='issues.jsonl'); assert r['returncode']==0,r['stderr']; Path('issues.jsonl').write_text(r['stdout'],encoding='utf-8')"
python lifecycle.py list --export issues.jsonl
python lifecycle.py list --export issues.jsonl --implemented-not-deployed
python activity.py --export issues.jsonl --since 2026-09-15T12:00:00Z --json
```

The lifecycle filter includes passed implementation whose deployment is neither passed nor not-applicable, including unknown deployment. The timestamp filter is inclusive and needs a timezone.

For durable novelty tracking, choose a stable scope identifying this deployment/project/feed, not an individual snapshot timestamp:

```sh
python activity.py --export issues.jsonl --scope beads-team/example --cursor-out activity-cursor.json --json
python activity.py --export issues.jsonl --scope beads-team/example --cursor activity-cursor.json --cursor-out activity-cursor.json --json
```

A cursor tracks entry identities and hashes, delivering new, late-arriving and changed entries while suppressing unchanged ones. Do not combine cursors with `--since`; a time cutoff could discard late entries. Cursor scope must match exactly, and its size grows with every seen identity. Partial exports retain previously seen identities but cannot reveal omitted content. Resetting a cursor can redeliver history; cursor writes do not acknowledge delivery to another system.

Coverage is exported comments and native state-change events only, not a complete audit of every mutation. The export file timestamp is local snapshot freshness, not an authoritative server mutation watermark. Refresh before relying on current views. Activity displays evidence text; lifecycle interpretation belongs to the contextual lifecycle view.
