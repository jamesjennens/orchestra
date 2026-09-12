# Pilot feedback and next implementation tranche

Reported experience: roughly thirty tasks handled by three kinds of agent. This is user-reported operational feedback, distinct from the kit's synthetic integration tests. Project-specific history is not reproduced here.

## Preserve

Self-contained briefs (intent, deliverable, scope, acceptance and working rules); corrections that preserve original evidence; decisions as issues; and six distinct facts: implemented, tested, reviewed, integrated, deployed and live-verified. Do not replace these with one overloaded notion of done.

## Priorities and acceptance

### 1. Queryable lifecycle facts

Use the pinned Beads `set-state` / `state` mechanism rather than introducing an independent status database. Its help documents an event record plus a dimension:value label used for current lookup. Keep Beads' normal open/in-progress/closed task status separate.

Proposed dimensions: `implemented`, `tested`, `reviewed`, `integrated`, `deployed`, `live-verified`. Proposed values: `unknown`, `pending`, `passed`, `failed`, `not-applicable`. A missing legacy dimension means unknown, never false or passed. A successful fact requires an evidence reference and the relevant source commit/artifact or deployment identity. Preserve whether a fact was performed, reported by another worker, or imported.

Store source commit, integration commit, release/deployment ID and evidence entry references in a documented structured record alongside these dimensions; confirm storage/round-trip behavior before choosing metadata versus dimension labels for identity fields. A test on commit A does not certify commit B. A deployment being live-verified is scoped to that exact release/environment. New revisions and retracted assertions must invalidate affected current claims without deleting earlier evidence. Do not mechanically mark all six facts true when closing an issue.

Acceptance: list implemented-but-not-deployed tasks including unknown deployment state; CURRENT renders a six-column table plus commit/release pointers; corrections update current facts and retain the original event; new revisions cannot inherit irrelevant green evidence. Test that label-backed queries and exported event/metadata evidence agree after an interrupted multi-step update. No claim that separate dimension writes form one transaction.

### 2. Activity without one database call per issue

The pilot reports that adding a comment does not advance issue.updated_at. Do not use that timestamp as the sole activity watermark. Existing exports already include comments: one fresh export followed by local filtering is the immediate coordinator read path.

Add an activity command/view that orders comment timestamps and lifecycle events, keeps stable IDs, accepts a timezone-aware since/cursor, and includes an export freshness watermark. Handle equal timestamps, corrections on old tasks and resumed reads without loss; do not deduplicate distinct events merely because they share a timestamp. If the source does not expose every mutation, label the feed's coverage rather than promising a complete audit log.

Acceptance: a comment on an old issue appears in since results even if updated_at is unchanged; hundreds of tasks require one export, not hundreds of comments calls; repeated reads and timestamp ties behave deterministically. Verify native comments options before adding wrapper syntax.

### 3. Identity and child-ID correctness

Require an explicit actor or an explicitly configured per-session actor; never silently fall back to the Git user. The portable client and endpoint already require actor. Preserve that property and add regression coverage; the older local PowerShell wrapper needs equivalent behavior when it is separately upgraded.

Stop predicting child IDs in briefs. First create the child under its parent, then use the returned ID in links and subsequent commands. Verify native `create --parent` allocation and concurrency before implementing an allocator. If readable numeric children require a wrapper, serialize allocation AND creation at the canonical service, reserve IDs durably, and reconcile uncertain responses. Client-side list/max+1 is unsafe. Deleted/reserved identifiers must not be reused; do not renumber existing tasks.

Acceptance: concurrent creates return distinct stable child IDs; interrupted/uncertain requests do not create duplicate logical tasks when reconciled; links use the actual returned ID. Bulk brief creation resolves references after allocation rather than embedding guessed numbers.

### 4. Shared-file merge coordination

Pinned Beads exposes `merge-slot create/check/acquire/release`; its documented primitive is one exclusive slot per project/rig. It serializes merge/conflict resolution, not edits to every file. Do not describe it as native arbitrary per-file locking.

Add a project-maintained list of high-conflict paths. Tasks affecting those paths declare overlap in the brief, and integrators acquire the merge slot before shared integration or conflict resolution, refresh the target branch, integrate/test, then record and release. Independent branch work can proceed while another contribution integrates. If a project wants exclusive editing too, define a separate scoped ownership convention explicitly. Record holder, task and integration target; a paused holder is not automatically evicted. Define evidence-based handoff/recovery.

Acceptance: only one of two concurrent acquisitions succeeds; nonholders cannot silently release another holder's slot; interruption and reacquisition tested. Expose only the needed native commands through the endpoint after validation. No GitHub CLI/MCP dependency for ordinary local slot coordination.

### 5. Generated coordination views, stable rules

Mutable task pointers and report summaries belong in Beads and generated views. Do not ask each lane to append them to a shared Markdown file on its source branch. Generate a compatibility coordination summary from reports and lifecycle data, with a timestamp and source links.

Keep human-maintained authority/rules in a separate stable document. During any existing-project migration, preserve old decisions/reports as historical evidence and update entry-point links before replacing the mutable summary. Do not overwrite a mixed rules/history file wholesale. Generated exports should be refreshed by the canonical operator/integration workflow and excluded from individual feature PRs or kept as separate artifacts.

Acceptance: two feature branches finish without coordination-summary merge conflicts; refresh is deterministic apart from the freshness timestamp; existing authority remains discoverable; no hand-editing of generated statuses or pointers.

### 6. Operational fit and decision lint

- Provide a `.cmd` entry point that calls the Python client directly, requiring no PowerShell execution-policy change. Document Python selection and test quoted paths/arguments and returned exit codes.
- Make refresh resolve configured canonical paths from any working directory and fail loudly on errors. The kit endpoint already selects a canonical project; separately test older migration helpers before changing them.
- Keep `backup init` in setup/migration, verify an initial backup and document scheduling and restore. The kit's add-project already initializes backups; scheduled/off-machine backup and replacement-host recovery remain separate obligations. Do not erase evidence of existing deployment-local scheduling.
- Supply a decision template with Decision, Rationale and Alternatives, and exercise the pinned lint/validation path before making creation validation mandatory.

## Implementation boundary

This tranche changes the kit first, with disposable integration tests. The live project must receive a separately reviewed migration that reads its current files, respects active claims, preserves existing backup scheduling and records any new wrapper commands. The public kit must not gain private project histories or local configuration as examples.

Currently the endpoint does not expose `state`, `set-state` or `merge-slot`; the renderer does not show these facts and there is no activity-since client command. The items above are implementation requirements, not claims that those features have shipped.
