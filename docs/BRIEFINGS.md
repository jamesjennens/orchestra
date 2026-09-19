# Task briefings and checkpoints

Start with `brief TASK`, then use `history` for the evidence you need. Both use the canonical server and require an explicit session actor. In these examples `b` means `python client.py --config client.local.json --project example --actor alex/session1 --`.

```sh
b brief example-task
b brief example-task --json
b history example-task --limit 5 --since 2026-09-15T12:00:00-04:00
b history example-task --cursor RETURNED_CURSOR
b show example-task --json
```

The default briefing answers who owns the task, its current position, unresolved items and next action. It also includes intent, acceptance, checkpoint author/time/branch/source commit and incorporated activity cursor, freshness, native dependencies, and six independent lifecycle facts with evidence pointers. A checkpoint does not set lifecycle facts; completed code never implies deployment. The native assignee remains authoritative.

An old task without a structured checkpoint reports its current position and unresolved coverage as **unknown**, not empty or complete. It shows bounded excerpts and directs the reader to reconcile history. Do not manufacture a checkpoint from only the last few comments. `show` retains the full native output; it can still exceed a calling tool's output limit.

## Publish a checkpoint

Read `brief --json`, reconcile relevant history and repository state, and save a UTF-8 JSON file using [the template](../templates/CHECKPOINT.json). Set `previous` to the current checkpoint's `comment_id` (or null for the first checkpoint), copy the briefing's top-level `activity_cursor`, and record the exact `incorporated_digests` (`entry_id` → content digest) from the snapshot the checkpoint summarizes — `brief` does not print them, so build the checkpoint against a fresh `history`/export snapshot or use the tooling that produces the cursor. Fill in the real source commit and branch, or leave them empty when unknown. Submit:

```sh
b checkpoint example-task --file checkpoint.json
```

The server supplies author and timestamp through the native append-only comment. It rejects a stale previous checkpoint or changed activity under the same project lock used by other client mutations. Re-read and reconcile before retrying with a revised payload. After an uncertain response, retrying the exact payload under the same actor reconciles an already stored checkpoint. Direct operator writes bypass this lock.

Carry every open item forward **unchanged**, regardless of its age. To resolve or supersede one, omit its old ID from `open_items` and add that ID to `resolved` with a reason and evidence pointer. A changed or replacement item needs a new ID. Supported kinds are blocker, question, decision, correction and dependency. Historical resolutions remain in their original checkpoints. Native dependency edges are shown separately and are not cleared by checkpoint prose.

The freshness flag detects comments, events and changes to task fields, including edits/deletions. It excludes the checkpoint's own comment. It means the checkpoint needs reconciliation; it does not prove the newer activity invalidates its conclusions. This digest is an observation of exported state, not a complete database audit watermark.

When the flag is set, `brief` also returns a bounded `newer` summary: counts split between the owner's own entries and other actors' entries, separated into `fresh_count` and `changed_or_late_count`, a bounded `other_authors` list (with an `omitted` count), up to five oldest unincorporated entry references carrying stable entry IDs, per-entry clipped authors and a `changed` flag, plus `history` and `history_new` (`--since` checkpoint timestamp) read paths. In that state the checkpoint's recorded `next_action` is prefixed with a `STALE CHECKPOINT:` qualification instead of being presented as unconditional.

Coverage is per-entry: every checkpoint records the exact `entry_digests` (entry ID → content digest) it incorporated, so edited entries and late backdated arrivals are identified precisely rather than by timestamp. A legacy checkpoint without `incorporated_digests` still shows the flag but reports `coverage: 'unknown'` — reconcile with `history` before trusting its counts, and publish a fresh checkpoint (which records digests) to restore exact coverage. Counts are never asserted as complete when coverage is unknown. Reading a briefing clears nothing: acknowledging a direction, reconciling it in a new checkpoint and completing it remain separate, explicit acts. The work queue mirrors the counts as `newer_activity_by_others` / `newer_activity_own` plus `newer_activity_coverage` (`current`/`snapshot`/`unknown`, `null` without a checkpoint or when checkpoint history is malformed), so `work --mine` and session resume surface directions that arrived after the owner's checkpoint.

Only use `checkpoint` to write the structured format. Malformed structured comments are flagged; conflicting branches or missing revisions fail clearly. Such corruption requires an operator to preserve the original evidence in a correction record and reconcile the invalid native record before publishing again. The system will not choose a conflicting conclusion or silently erase evidence.

## Bounded history

History returns JSON, with up to 5 entries and 4,000 encoded body bytes by default. `--limit` accepts 1–20; `--body-budget` accepts 256–8,000. Oversized comments are split into fragments retaining `entry_id`, character offset, total characters and `continued`. Concatenate fragments by entry ID and offset to recover the entire body. Metadata is additional to the body budget.

`--since` is inclusive and requires `Z` or an explicit UTC offset. Each continuation cursor binds project, task, cutoff, snapshot and position. Continue with the exact returned token; omit `--since` or repeat the same instant. Ordering is timestamp followed by stable entry ID, including timestamp ties. An immutable server snapshot prevents gaps or duplicates when new activity arrives or old comments are edited during pagination. Start a new history request without a cursor to see that activity. The separate project activity feed supports novelty tracking across exports.

More unresolved items are available through `brief TASK --items-offset N`; the default is five per page, maximum ten. This is a fresh current-state query, so re-read after a checkpoint change. Dependency counts and omitted counts are explicit; use `show` for all native edges.

History snapshots live in each private runtime project's `.history-snapshots` directory. They contain private task evidence, are disposable, and are not part of the durable backup pair. There is no automatic retention policy yet. Operators may remove old snapshot files under the project's coordination lock; affected cursors fail explicitly and readers restart. Checkpoints and their provenance remain in native backups.
