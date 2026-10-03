# Installation and recovery

## Install on Linux

Use an ordinary service account with a writable home, Python 3.10+, user systemd, and HTTPS access to GitHub release assets. Copy this kit to `/home/beads/beads-team-kit` in this example. Each deployment needs its own root, port and service name. Installation refuses unmanaged binaries or an existing service with the same name.

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime install --port 13317 --unit beads-team.service
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime add-project example
```

`add-project` initializes the project, records the standard native configuration, provisions the project's merge slot (idempotently) and performs the initial backup. A project whose slot is missing refuses `merge-check`, `merge-acquire` and `merge-release` with an error naming the `merge-create` coordination operation, which is also the manual repair for a project created before this behavior.

Project names use 2–24 lowercase letters/digits, starting with a letter. Run these commands as the service account. An administrator should enable its user manager at boot and after logout:

```sh
sudo loginctl enable-linger beads
loginctl show-user beads -p Linger
systemctl --user is-active beads-team.service
```

The database listens on loopback only. Its generated credential is stored in the runtime's `deployment.private.json` (0600); runtime permissions are 0700. Contributors do not need the SQL password. Do not publish this directory or open the database port on the network. The installer configures metrics off for this deployment.

If initial installation fails, it stops the service and preserves files for inspection. Do not rerun by deleting the runtime: investigate the service journal first. Repeating installation of an already initialized deployment checks its pins, port and connectivity.

## Connect a contributor

Configure the [server-owned project entry point](ONBOARDING.md) so new workers can start from an empty directory using a single SSH onboarding command. Its private instructions are included in coordination sidecar backups.

Install Python 3.10+ and OpenSSH on their machine. Configure an SSH alias `beads-team` for the server/service account with their own key; verify the server host key on first connection. Confirm an ordinary `ssh beads-team` works before using the noninteractive client.

Copy `client.example.json` to `client.local.json`, adjust the host and paths, then run:

```sh
python client.py --config client.local.json --project example --actor alex/session1 -- ready --json
python client.py --config client.local.json --project example --actor alex/session1 -- refresh
python client.py --config client.local.json --project example --actor alex/session1 -- view
```

Keep local config outside committed project content or ignored. The client transports arguments and UTF-8 file contents as JSON over SSH. It does not copy source code or run builds. `--body-file`, `--design-file`, `--file` and `-f` read files on the contributor's machine. The endpoint exposes the routine issue commands; setup and maintenance use admin.py on the host.

Copy templates/READ_ME_FIRST.md and docs/WORKFLOW.md into each project repository, fill in project details, and add links to README and AGENTS.md. This makes the entry instructions discoverable by later agents without needing a pasted chat message.

## Operator commands

A few coordination actions are host shell commands rather than contributor-client
actions. Run them as the service account on the coordination host; the client and
the endpoint do not expose them, and a worker or agent lane must not be asked to
run them. Their payload schemas and full semantics are in [native requirement
commands](REQUIREMENTS_INTEGRATION.md) (`requirement-*`) and [malformed structured
history](#malformed-structured-history) (`void-record`).

| Command | Purpose | Authority it needs |
| --- | --- | --- |
| `requirement-apply PROJECT --actor ACTOR --file record.json` | write an accepted requirement revision, or move an accepted record back to draft | the payload's `acceptance` object (named owners/approvers, policy, decision id, evidence) and the deployment operator allowlist |
| `requirement-backfill PROJECT --actor ACTOR --file backfill.json` | add the controlled requirement type/state labels to records created before this route | an `evidence` pointer for an entry that becomes `accepted`, and the deployment operator allowlist |
| `requirement-reconcile PROJECT --operation-id ID --actor ACTOR --disposition ...` | finish a requirement operation whose real write was uncertain | confirmation of the native record state |
| `reference-apply PROJECT --actor OPERATOR --file acceptance.json` | accept a reference catalog entry: the payload names the newest draft `revision` and its `record_sha256`, and the command writes the next revision as accepted, after the F3 evidence (`operation: "draft"` with full content writes a direct accepted revision 1) | the deployment operator allowlist, checked before any write, and F3 evidence (`acceptance`: owners, approvers, policy, decision id, evidence) |
| `reference-reconcile PROJECT --operation-id ID --actor ACTOR --reason TEXT --disposition ...` | finish a reference operation whose real write was uncertain; `complete` needs `--issue-id` and refuses an anchor that has no revision record yet (re-run the original `ref propose` with its `operation_id` first) | confirmation of the native record state |
| `capability-apply PROJECT --actor OPERATOR --file batch.json` | accept a batch of capabilities under one F3 decision: `items` of `{key, revision, record_sha256}` (each the newest draft reviewed). The command writes one acceptance record and one receipt per item, keyed `(operation_id, key)`, in list order. It reports `accepted`, `already-accepted`, `refused` or `uncertain` per item; an uncertain item stops the batch, and re-running the same batch resumes it. A changed list needs a new `operation_id`. The coordination lock is taken per item and released between items, with a 50 ms pause while it is free, so other writers wait behind at most one item; each item re-checks its `revision` and `record_sha256` under its own hold. An item takes about 3 seconds (two reads and four writes), so 100 items take 5 to 6 minutes: prefer batches of about 20. `operation: "draft"` with full content writes a direct accepted revision 1 | the deployment operator allowlist, checked before any write, and F3 evidence |
| `capability-retire PROJECT --actor OPERATOR --file retire.json` | supersede the newest revision of a key by a `successor` key, with evidence. The successor must exist, and a cycle is refused. A retired key refuses `revise` and acceptance. This also stands in for the design's "demote" in slice 1a | the deployment operator allowlist and F3 evidence |
| `capability-alias-reject PROJECT --actor OPERATOR --file reject.json` | reject a pending alias (`{schema_version, key, alias, reason}`); lookup then ignores it | the deployment operator allowlist |
| `capability-alias-propose PROJECT --actor OPERATOR --file alias.json` | propose an alias as a verified operator (`{schema_version, key, alias, evidence?}`). This is the only route that writes `identity: verified`; `capability propose-alias` through the endpoint always writes `unverified`, even for an operator's actor name | the deployment operator allowlist |
| `capability-verify PROJECT --actor ACTOR --file payloads.json` | record capability checks as **verified**. The file is what `capability check --repo . --payloads payloads.json` wrote at the commit being verified (one payload, or `{schema_version, items}` of up to 500). Each item is one capability and takes the coordination lock on its own; the result is `recorded`, `already-recorded` or `refused` per item, and re-running the file is safe. This is the only route that writes a verified check: `capability check --record` through the endpoint always writes an unverified report | the deployment operator allowlist or the `verifiers` list, both checked before any read |
| `verifiers list\|add\|remove [ACTOR] [--confirm-revoke]` | manage the deployment `verifiers` list: actors, other than operators, whose `capability-verify` records readers count as verified. The list is empty by default and grants nothing else. `remove` needs `--confirm-revoke`; the refusal names the capabilities whose verification would change | shell access to the coordination host; `deployment.private.json` is the only authority source |
| `capability-reconcile PROJECT --operation-id ID ...` | finish a capability operation whose write was uncertain (for a batch item, the id is `OPERATION_ID/KEY`). A transient native failure can leave a `pending` receipt with no native row behind it, and the same `operation_id` is then refused until it is cleared: run `capability-reconcile --disposition released`, then retry the original command | confirmation of the native record state |
| `record-reconcile PROJECT --kind requirement\|reference\|capability ...` | the same reconcile for any record kind | as above |
| `capability-misses-clear PROJECT` | delete the project's [capability lookup-miss log](#the-capability-lookup-miss-log). It prints what was removed (`finds`, `misses`, `phrases`), and in `repaired` any symlink, directory or unopenable lock file it removed from the three miss-log names (never following a link). It writes nothing to the tracker, takes no coordination lock and calls no `bd` | none beyond the service account: it deletes telemetry only, so there is no allowlist check and no `--actor` |
| `void-record PROJECT --actor OPERATOR --file void.json` | void a malformed or stale contribution-review record | the deployment operator allowlist (`operators` in `deployment.private.json`) |
| `handoff PROJECT --actor ACTOR --file handoff.json` | transfer a claim when the current owner cannot act | an owner decision/evidence pointer in the payload's `approval` |

All five are shell-trusted: access to the service account's shell is the boundary.
`requirement-apply`, `requirement-backfill` and `void-record` also check the
deployment operator allowlist (`operators` in `deployment.private.json`), so a
deployment that configures no operators authorizes nobody and an unlisted
`--actor` is refused before any native or journal write. The allowlist is read
strictly: a shell-only `ORCHESTRA_OPERATORS` value that disagrees with the
deployment configuration is refused rather than honoured in one place and
ignored in the other. `operators add/remove/list` maintain that list (see the
removal and restore policy below). Before the `requirement-apply`/
`requirement-backfill` check ships, enrol every actor that accepts requirements
today (starting with `james`) with `admin.py operators add` on each installation
and confirm with `operators list`; an empty or short list is a deploy blocker,
not a warning. Use the identity of the person actually running the command as
`--actor`; the owner decision is named in the payload, never by reusing the
owner's actor.

Validate a payload before writing. Each command is fail-closed and refuses before
any native write, and the same validator can be run with no native read or write
from the kit directory:

```sh
python3 -c "import json,sys,requirement_records as r; r.validate_payload(json.load(open(sys.argv[1])), operator=True)" record.json
python3 -c "import json,sys,requirement_records as r; r.validate_backfill(json.load(open(sys.argv[1])))" backfill.json
python3 -c "import json,sys,recovery as r; p=json.load(open(sys.argv[1])); r.validate(p,p['task'])" void.json
python3 -c "import json,sys,handoff as h; h.validate(json.load(open(sys.argv[1])))" handoff.json
```

The validators check payload shape only; each command rechecks native state
(record existence, revision ledger, key uniqueness, the allowlist) and refuses
before writing. There is no `--validate-only` flag today. The `requirement-apply`
line uses the operator form because the contributor form (default
`operator=False`) refuses an operator payload that accepts a first revision
(`operation: "draft"` with `acceptance_state: "accepted"`), while `operator=True`
additionally runs the F3 acceptance check.

Requirement records created before `requirement_records.py` (for example by
`create-child`) can be untyped, or typed `draft` although accepted; label them
with `requirement-backfill`. An accepted backfill needs an `evidence` pointer and
is refused when the newest revision comment says `draft`; accept those through
`requirement-apply` with F3 evidence, which writes a new accepted revision. A
record with no revision comments at all can instead get an accepted revision 1
via `operation: "draft"` with `acceptance_state: "accepted"` and an `acceptance`
object (operator only), so the revision number can match a published manifest.

## Backups

Run after meaningful work and before maintenance:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup example
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup example second
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup --all
```

This invokes Beads' native Dolt backup into `runtime/backups/example`. The native step is Dolt's own backup synchronization through the SQL client (`CALL DOLT_BACKUP('sync', ...)` over the loopback connection, bounded by an explicit 30-minute ceiling), not `bd backup sync`, whose fixed client read timeout of about ten seconds a large database cannot meet on a busy or stalled server; a project that carries no Dolt server metadata has no SQL coordinates and keeps the `bd backup sync` path. The password travels in the environment, never on a command line. One or more project names, or `--all` for every initialized project of the runtime, run in a single invocation; each project is backed up exactly as the single-project form does, one failing project does not stop the others, and the command exits non-zero when any targeted project's backup pair is not complete. The sync client runs in its own session/process group. A normal `SIGTERM` or Ctrl-C is turned into an error inside the critical section, so the kit runs its cleanup and then stops that whole process group before the backup lock is released: the `dolt` client and anything it spawned cannot keep writing `backups/PROJECT` after the lock is gone, the previous complete pair is kept, and the run exits non-zero. A `kill -9` (or OOM kill) of `admin.py` itself can run no cleanup at all, so that client can still finish writing `backups/PROJECT` after the lock is released — re-run `backup` for that project before relying on that generation, and prefer a normal stop. Same-host backups protect against some mistakes, not loss of the server. Arrange an ordinary scheduled, encrypted off-machine copy of completed backups using your existing backup system; `backup-copy` below is the kit's reference for the pair selection. The kit does not install a backup timer. Preserve the kit/version pins and a protected copy of deployment configuration separately; JSONL views are useful exports but are not a substitute for the native backup.

Restore drills deliberately create a new project:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime restore-new example examplerestore
```

It refuses a populated destination, restores status and comments, and retains original issue IDs. The native restore also carries the source project's recorded backup target, so `.beads/dolt-backup.json` and the restored `dolt_backups` row would still name `backups/SOURCE`; `restore-new` re-points the destination at `backups/DEST` immediately after the restore, and `backup` refuses (before any native command) a project whose recorded target is not its own `backups/<name>`, naming the recorded URL and the expected directory, so a clone that was never re-pointed cannot overwrite the source project's backup directory. A clone restored before that re-point existed (or whose re-point failed) is repaired in place with `admin.py backup-repoint DEST`, which runs `bd backup init backups/DEST` under the kit environment and verifies both `.beads/dolt-backup.json` and the `dolt_backups` row before reporting success; do not run a bare `bd backup init` (it fails without the kit environment, `Error 1045 Access denied for user root`) and do not re-run `restore-new` onto an existing name (it is refused and would discard the clone). Re-pointing moves no data: if the clone was ever backed up while still mis-pointed, the SOURCE project's `backups/SOURCE` may hold the clone's data, so back the SOURCE project up again before relying on that backup. Inspect records and comments before any cutover. Do not run both copies as live coordination trackers. A complete host-loss recovery requires reinstalling the pinned kit on a replacement host, placing the saved backup and its coordination sidecar under its backups directory, using restore-new, verifying it, then updating client project/host settings. A separate-deployment drill exercises backup transfer; actual replacement-host outage recovery remains an operator exercise.

### Coordination journals and interrupted recovery

Session registrations in `.sessions.json` are included in the coordination sidecar, alongside the private project onboarding entry point. Restore with a kit version supporting these records. Preserve original actor IDs for resumed workers; do not create a second live authority from a restored registry.

**Record journals and the shared slice 0.** The reference catalog, requirements-gathering and capability-index designs each keep an idempotency journal: `.reference-requests/`, `.proposal-requests/` and `.capability-requests/`. This kit writes none of them, but it treats them as a tolerant reader:
- **Backup:** it backs them up when they exist.
- **Validation:** it validates every receipt with the frozen schema (a 64-hex `sha256`, a known `status`, and optional `actor`, `id`, `revision` and `acceptance`; unknown keys are kept). A malformed receipt refuses the backup before the native sync, and refuses the restore before its first write.
- **Restore:** it restores them with the project.

Restore compatibility, exactly:
- **Backups containing the journals:** a backup with any of the three journal paths restores only with `admin.py` at this kit or later. It can run from a checkout.
- **Older kits:** any kit older than this one refuses the whole restore of such a backup, before writing anything.
- **Older backups:** this kit restores backups made by older kits normally. Those backups carry none of the journals, so at most idempotency receipts are missing.

This kit is therefore the oldest one a deployment may roll back to once any of those records exist.

**Acceptance evidence trusts the comment's native author.** A reference entry reads
accepted when its acceptance evidence comment was written by an actor on the operator
allowlist. Over SSH a comment's author is the actor the caller declared, so what stops
a contributor writing such a comment is the reserved-prefix guard (the shared slice 0),
not the author field.

- **Residual risk.** A `Kind: reference-acceptance-v1` comment written on a kit older
  than the shared slice 0, or during a rollback below it, under an operator's actor
  name, would read as a real acceptance. The same applies to the other record kinds.
- **Why acceptance is not bound to the host receipt.** Reading an accepted entry must
  not depend on a local journal: a restore may lose `.reference-requests/`, and the
  evidence comment has to remain the authority.
- **Before the first use of the catalog on an installation,** and after any rollback
  below the shared slice 0, scan each project for record comments that no writer of
  this kit made. Save `bd export --all` for the project as `export.jsonl`, then run:

  ```sh
  python3 -c "import json,sys;K=('Kind: reference-','Kind: requirement-proposal-','Kind: proposal-disposition-','Kind: contribution-settings-','Kind: capability-');[print(r['id'],c.get('id'),c.get('author'),c['text'].splitlines()[0]) for r in map(json.loads,sys.stdin) for c in r.get('comments') or [] if isinstance(c.get('text'),str) and c['text'].startswith(K)]" < export.jsonl
  ```

  It prints nothing on a project that has never used the catalog. Any line it prints
  on such a project is a comment to void or investigate before you rely on the catalog.
  The coordinator ran this scan on all seven live projects on 2026-10-02 and found none.

A row is hidden as a record anchor only when it carries one of the labels `reference`, `proposal`, `contribution-settings` or `capability` **and** a v1 record comment of the same family. A project's own task that merely uses one of those words as a label (for example jjbp-j03.20's `proposal`) stays visible and editable.

**The `verifiers` list.** The deployment configuration may carry a `verifiers` list beside `operators`. It is a second, narrow deployment-wide authority: an actor on it may run `admin.py capability-verify`, and readers then count that actor's capability checks as `verified`. It grants nothing else. Keep it empty unless a host-side verifier other than an operator exists; the coordinator's integration step verifies as an operator.

- **One authority source.** `deployment.private.json` is the only source. A shell `ORCHESTRA_VERIFIERS` that disagrees with the file is refused by `verifiers add`, `verifiers remove` and `capability-verify`; it is never read as authority. Entries are actor identities (letters, digits, `_`, `.`, `-`), so an `account:` value can never be a verifier.
- **Revocation.** `verifiers remove ACTOR` requires `--confirm-revoke`. Afterwards every check that actor recorded reads `reported`, and drift that only their passes had cleared reappears. Nothing is deleted: re-adding the actor restores the reading. An actor who is also an operator stays trusted.
- **Backup and restore.** Each project's coordination sidecar records the list beside `operators`, for information. `restore-new` never re-grants it on its own. When the backup records verifiers this host does not list, the restore prints them, says they were **NOT** restored, and their checks read `reported`. Re-grant one with `verifiers add ACTOR`, or pass `--restore-verifiers` to re-establish the whole recorded list. `--restore-operators` does not re-grant verifiers.
- **Integration step.** At the integrated commit, in a clean checkout: `capability check --repo . --payloads payloads.json`, then on the coordination host `admin.py capability-verify PROJECT --actor OPERATOR --file payloads.json`. The payloads file is created private (mode 0600) and is never written through a symbolic link. Drift clears only on such a verified pass at a commit the project's lifecycle evidence records as integrated; a reverted integration does not count.
- **Rolling back below this kit.** An older kit keeps every record, hides them as before and simply reports no `verification`. It never rewrites or removes `views/CAPABILITIES.md`, so after a rollback delete `projects/PROJECT/views/CAPABILITIES.md` by hand; otherwise the last page rendered stays readable through `view` with its old export stamp.

`add-project` initializes the project, provisions its merge slot (idempotently) and performs an initial backup, so a freshly provisioned project can run `merge-create`/`merge-check`/`merge-acquire` without a manual slot setup. A project whose slot is missing refuses `merge-check`/`merge-acquire`/`merge-release` with an error naming the `merge-create` operation, which is the manual repair. `backup` captures both the native backup directory and `backups/PROJECT.coordination.json`. The sidecar preserves pending child-request reservations and merge context outside Dolt. Keep this pair together. A pending marker is written before synchronization and becomes complete only after native sync succeeds; restore refuses an incomplete sidecar. The last complete sidecar is also kept as `backups/PROJECT.coordination.last-complete.json` before that marker replaces it, and is put back if the run fails or is interrupted, so one failed run never destroys the previous restorable pair; `restore-new` serves the canonical sidecar when it is complete and falls back to that durable copy when it is not. The operation-journal snapshot (`backups/PROJECT.http-operations.sqlite3`) is staged during the run and promoted only after the native sync and the new complete sidecar are durable, so the snapshot a restore replays always belongs to the same generation as the Dolt native backup and the complete sidecar: a run that fails after an acknowledged write leaves the previous snapshot in place, and the un-synced operation is not replayed from a restored journal. When `restore-new` cannot use the canonical sidecar it prints that it is using the durable last-complete copy and that the restored journal snapshot belongs to that generation; if a promotion after a successful sync fails, the new complete sidecar is kept rather than rolled back beside the new native directory. The complete sidecar also records a stat-only manifest (relative path, size and mtime) of the native backup directory as that generation finished, and `restore-new` recomputes it before any coordination or journal write: when the directory no longer matches, it prints a loud WARNING that the restored Dolt may hold effects whose receipts the restored journal does not have (an interrupted or killed run can leave the native directory partly rewritten while the restore serves the previous complete pair) and to take a fresh backup before relying on that pair. A sidecar written before the manifest existed records none, and the restore says the check could not be performed instead of calling the pair clean. Backup and restore serialize access to the pair, and backup excludes contributor writes through the endpoint. Direct operator/native writes bypass these locks and must be paused for backup. The completed sidecar also records the deployment operator allowlist, so a restore can report recorded authority the destination host does not list. `restore-new` does **not** apply it: re-granting an operator is deployment-wide authority and stays an explicit decision (`--restore-operators`, or `operators add OPERATOR`), so a stale backup cannot silently reverse a revocation. See [Operator removal and restore policy](#operator-removal-and-restore-policy).

Copy a completed, quiescent backup pair off-machine using your normal encrypted backup system. Do not copy it during the next sync. Preserve the pair together with its `backups/PROJECT.http-operations.sqlite3` journal snapshot, so a restored project's acknowledged operations are not re-executed. Preserve file timestamps on the way out and on the way back (`cp -a`, `rsync -a`, or an archive that keeps mtimes): the complete sidecar's stat-only manifest keys on each file's `mtime_ns`, so a copy-back that resets timestamps makes `restore-new` warn that the native directory no longer matches its generation even though the bytes are intact. This is not an atomic transaction across arbitrary filesystem copies; take a filesystem snapshot or hold the project's `backups/PROJECT.lock` while copying (the `backup-copy` helper below does this for you). Legacy backups without a sidecar warn that outstanding requests/merge context require reconciliation.

Every run of `backup` also writes `runtime/backups/backup-status.json`: a schema-versioned, sorted-key record of the run with one entry per project, naming whether that project's pair (its `backups/PROJECT` native directory plus its `complete` `backups/PROJECT.coordination.json` sidecar) is complete, the UTC time it completed, and the reason when it is not. The per-project pair state is re-read from the files, so a run that fails or is skipped — and even a "successful" call whose sidecar is still `pending` — is recorded as **not** complete rather than assumed complete. A named or single-project run updates its own entries and carries the other projects' last known state forward instead of erasing it; `generated_at` and `scope` always describe that run, so a carried-forward entry is never presented as its result. Record scope is explicit: `"scope":"all"` means the run covered every project initialized at that moment, `"named"` means it covered only the projects listed.

Read that record on the host:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-status
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-status --require-complete
```

`--require-complete` exits non-zero unless the last run used `--all` and every project initialized in the runtime is listed as complete with its pair still complete on disk, naming each project that is missing, not complete in the record, absent from the record (a project added after the last run), or no longer complete on disk. Use it as the gate in front of the off-machine copy, so a project that is absent from the schedule or whose pair is half written is noticed instead of being silently omitted.

The kit also ships a generic reference helper for the copy itself:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-copy /srv/off-machine/beads-runtime
```

`backup-copy DEST` applies exactly the `--require-complete` gate and refuses, naming what is missing, when any initialized project is not complete. It then copies each project's last complete pair — the native backup directory as `DEST/PROJECT`, the complete sidecar as `DEST/PROJECT.coordination.json`, and, when the project has one, its operation-journal snapshot as `DEST/PROJECT.http-operations.sqlite3` (the same shape a runtime's `backups/` directory has, so the copy can be copied back and restored) — and copies the record that gated the copy. Each project's `backups/PROJECT.lock` is held for that project's whole staging, while its coordination lock is held only long enough to re-check the pair and copy the small sidecar and journal snapshot; the long native copy runs after the coordination lock is released, so copying a large database does not block endpoint writes on it (both commands take the two locks in the same order, so no deadlock is possible). Files are staged in a per-run directory and then moved into place by rename rather than merged, so a concurrent `backup --all` cannot yield a mixed native directory beside a complete sidecar and stale destination files do not survive. Each project's native directory is also checked against the `mtime_ns`-keyed manifest its complete sidecar records, before and after that project's copy: a directory that no longer matches — a killed or interrupted run can leave it partly rewritten — is refused rather than copied, a directory that changes while it is being copied is refused, and a sidecar that records no manifest is reported as unverifiable instead of being called clean. The completeness record is published only after every project is in place: a failure exits non-zero with a clear error, removes the staging directory and leaves no completeness record, so the destination never looks complete when it is not. It prints exactly what it copied, is credential-free and never touches a unit, timer or schedule. The scheduled, encrypted off-machine system, its encryption, retention and destination, remain the operator's: this helper is the reference the operator can gate and schedule, not a replacement for that system, and it cannot see whether a copy actually left the host.

If restore is interrupted, the new destination may exist with only part of the restore completed. Preserve it for inspection; retry recovery into another unused destination. Do not delete the source or force reuse of the partially restored target. Verify pending reservations, comments, lifecycle events, baselines and slot context before switching clients.

### The capability lookup-miss log

The endpoint counts each `capability find` and remembers the phrases that had no exact
record match, so an operator can see which phrases miss and how often
([CLI contract](CLI_CONTRACT.md#capability-misses-which-phrases-miss-and-how-often)).
Read it with the client: `capability misses --limit 20`.

**It is telemetry, and it is not backed up.**
- It lives in two files in the project directory: `.capability-misses.json` (the log,
  mode 0600) and `.capability-misses.lock` (an empty lock file). A crashed write can
  leave `.capability-misses.json.tmp`; the next write removes it.
- `admin.py backup` does **not** collect them, and `restore-new` does not create them.
  A restored project starts with an empty log. Losing the log is acceptable: it only
  steers which capabilities to index next.
- That is deliberate. A backup carrying a path an older kit does not know is refused by
  that kit's restore. Keeping the log out of the backup means **this change does not
  move the oldest kit a deployment may roll back to.**
- It adds no tracker comment kind, label or record type. A kit without this feature
  never reads the files and ignores them; they can be left in place or deleted.
- A missing, corrupt, oversized or unknown-schema log is never an error. The next
  `find` starts a new one, and `capability misses` reports `log: unreadable`.

**What it stores:** the normalised phrase, a count, and first-seen and last-seen times
per phrase, plus the counters `finds`, `misses`, `overflow`, `dropped` and `evicted`.
No actor names. Phrases are untrusted contributor text: read them as data.

**Bounds:**
- 500 phrases per project. When full, phrases first seen in the last 2 hours are
  protected, and among the rest the one with the lowest count goes (the one seen
  longest ago among equal counts). If every phrase was first seen in the last 2 hours,
  the one seen longest ago goes. A flood of one-off phrases evicts other one-offs, and
  a new phrase that keeps recurring is kept long enough to build up a count. Protection
  is keyed on first-seen, so repeating a phrase cannot keep it protected. When the log
  is full of phrases with a count of 2 or more, a phrase that recurs less often than
  every 2 hours is not kept.
- 60 new phrases per project per clock hour (UTC). Further new phrases that hour are
  counted in `overflow` only. The bound is per project because no actor is stored, so
  one caller can use up the whole hourly quota.
- Counts are not votes: no count can be attributed to anyone, and one caller can repeat
  a phrase to push it to the top.
- 80 characters per stored phrase. The file is at most about 530 kB, and normally a few
  tens of kB; a file over 1 MB is not read and is replaced.

**Cost and locking:**
- Recording takes its own lock (`.capability-misses.lock`) with a few non-blocking
  attempts, waiting about 10 ms at most in total. If another request still holds it,
  that one find is not counted, so the counts are lower bounds. It never takes
  `.coordination.lock`, so it never waits for a writer and never makes one wait.
- It calls no `bd` and starts no process. The log is rewritten on each counted find
  (temp file, then rename, no fsync). The independent review measured 2-3 ms for normal
  logs, and for a worst-case 517 kB log about 5 ms median and up to 33 ms at the 95th
  percentile.

**Stuck states.** If the lock path is anything but a regular file (a symlink,
directory, FIFO or other special file), or the log or temp path is a directory, finds
still answer, without waiting, but nothing is recorded. The lock is checked with
`lstat` and opened non-blocking, so a FIFO there can never hang a find. `capability misses` then
reports `recording: lock-unusable` or `log-unwritable` instead of `ok`, so its zeros
are not mistaken for "no misses". `capability-misses-clear` repairs it.

**To clear it:**

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime capability-misses-clear example
```

This deletes the log and starts a new measuring window (`since`). Clear it after a batch
of aliases or capabilities has been accepted, so the next window measures the improved
index. It also removes a symlink, directory or unopenable lock file found at
`.capability-misses.json`, `.capability-misses.json.tmp` or `.capability-misses.lock`,
without following a link. Deleting those paths by hand is equally safe.

### Malformed structured history

A comment that claims a reserved machine format (`Kind: contribution-review-v1`, `Kind: task-checkpoint-v1`) but fails validation makes `brief`, `review` and `refresh` fail for that task. Repair it on the host; never edit or delete rows in the native database.

Read the incident first: `brief PROJECT-TASK` fails naming the offending comment id, and `history PROJECT-TASK` returns its exact bytes. Configure the operator allowlist once per deployment, then build a void payload and submit it with the host command:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators add OPERATOR
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators list
```

```json
{"schema_version":1,"operation":"void-record","operation_id":"void-1","task":"PROJECT-TASK",
 "target":"COMMENT-ID","target_kind":"contribution-review","target_sha256":"SHA256-OF-ORIGINAL",
 "original":"EXACT ORIGINAL COMMENT TEXT","reason":"WHY THIS RECORD IS VOID","disposition":"void"}
```

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime void-record PROJECT --actor OPERATOR --file void.json
```

`target_sha256` is the SHA-256 of `original` encoded as UTF-8, so the record proves which bytes were reconciled. Operator authority is server-side configuration, never the payload's own `operator` string: the deployment allowlist (`operators` in `deployment.private.json`) is the single authority source, the endpoint supplies it to every read and to `refresh`/`render`, and reads require the void's stored native author to be on it as well as to match the name in the payload. `ORCHESTRA_OPERATORS` is not an authority source; a host command refuses when it is set to a set that differs from the deployment configuration, instead of authorizing a write the endpoint would ignore. `void-record` refuses any actor outside the allowlist; a deployment that configures no operators authorizes nobody. A contributor therefore cannot make a void valid by naming their own actor as the operator, so a forged or self-authored void is inert even if the generic raw-write guard is bypassed. Void records are append-only and are written only by this host command; the record-void prefix is reserved, so any raw `comments add` write of one is refused on the contributor endpoint, exactly like a raw review or handoff record. The guard parses the `comments` request structurally: it resolves `add` past leading flags, refuses an attachment transported before `add`, and refuses the operator-only `-a`/`--author` spelling, so the reviewer's `["comments","@attachment:a","add",TASK,"-a","ops-james"]` forged-authorship request fails closed with zero native writes. A malformed, stale, foreign-authored or unconfigured-author void comment is ignored and reported rather than applied. Nothing is deleted: the original comment, the void record and its actor, timestamp and reason all stay in native history, in `history` reads and in rendered pages, and native backup plus `restore-new` preserve both. Reads expose applied voids in `review.recoveries`. Reads also require a void to follow its target in native order, so a void recorded before the record it targets cannot apply.

A void may not target a record that is part of the contribution history the surviving records currently form, so recovery cannot suppress a current revision or approval. If a surviving record's `previous` names a voided comment, reads keep failing closed and name that record; void it explicitly as well, or deliver a revision that repairs the chain. A directly written void that would suppress the surviving history is inert: reads stay healthy, the void is listed in `review.recoveries` with `applied: false` and its refusal reason, and a warning names it. A void is a repair of a broken history, so a void applied after the latest approval invalidates that approval: the task returns to `awaiting-review` and a fresh approval is required before integration. Re-running the identical void payload is idempotent.

#### Operator removal and restore policy

`admin.py operators remove OPERATOR` is a revocation, not a cleanup: voids that operator authored stop applying on read, the incident is reported as unreconciled again, and re-adding the operator restores those dispositions. Because that destroys recorded dispositions, `remove` refuses unless `--confirm-revoke` acknowledges the consequence.

The allowlist is deployment configuration rather than a native Beads object, so each project's complete coordination sidecar records the allowlist in force at backup time. **A restore does not re-grant it by default.** The allowlist is authority for *every* project on the deployment, so a backup taken before `operators remove ACTOR --confirm-revoke` would otherwise silently restore that actor's authority deployment-wide — including for projects the backup has nothing to do with. `restore-new` therefore restores the native records, the coordination sidecar and the operation journal, but leaves the deployment allowlist untouched. When the backup records operators this host does not list, the command prints them by name, states that they were **NOT** restored, and explains that void records they authored stay inert on the restored project until authority is granted again. Nothing is deleted: the void comments and their `original` bytes are intact, and they apply again as soon as the actor is deliberately re-added.

To re-establish the recorded authority, re-grant it explicitly, one actor at a time:

```
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators add OPERATOR
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators list
```

Or re-run the whole restore with `--restore-operators` when the entire allowlist recorded in the backup is intended to be in force again, for example on a replacement host:

```
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime restore-new example examplerestore --restore-operators
```

`--restore-operators` is additive only (it never removes an entry) and prints exactly which entries it re-granted. It is an explicit authorization decision: after a revocation, do not pass it "to make the restore look complete" — a revoked operator stays revoked until an operator re-adds them by name. The native backup preserves the original comment and the void comment; the sidecar preserves the authority reads would need to apply it, so a restore still preserves both the original and its disposition, with the authority decision left where it belongs: with the deployment operator.

### Optional scheduled backup

For an externally supervised, unprivileged foreground service and its scheduler
commands, see [the office service runbook](OFFICE_SERVICE.md). The user-systemd
timer below applies to deployments that use a user systemd manager.

Edit `templates/beads-backup.service` for the installation paths, then copy it and `templates/beads-backup.timer` into the service account's `~/.config/systemd/user/`. Its `ExecStart` uses `backup --all`, so the one timer covers every project initialized in that runtime, including projects added later, and every run writes the `backup-status.json` record described above. Enable with `systemctl --user daemon-reload` and `systemctl --user enable --now beads-backup.timer`. Check `systemctl --user list-timers`, the service journal, and `backup-status --require-complete`; lingering must already be enabled for unattended operation. The timer performs same-host backup only. Configure off-machine copying, its completeness gate and retention separately; the timer does not copy anything off the host. These templates do not replace an existing team's backup schedule, and an existing installation that already runs a long-sync wrapper for this runtime keeps it. Once this kit's native step is deployed, that wrapper's monkeypatched `run_bd` and its `--timeout` ceiling are dead code: `admin.py backup` performs the native step itself through the Dolt SQL client (bounded by the 30-minute ceiling), and the wrapper's `dolt-backup-state.json` marker is no longer written or read by the kit.

`add-project` reads every installed `~/.config/systemd/user/beads-*backup*.service` unit for the account — `beads-backup.service` is only one of the names a deployment may use — and reports factually which units it read. It states that a `backup --all` unit covers every project; a recognised long-sync wrapper for this runtime is reported as covering only the projects its `--project` arguments name (a wrapper that names no project is not coverage of anything), because a project added later needs another wrapper line. It prints the exact `ExecStart` to add when a unit that names projects individually does not cover every project (a project list is replaced with the durable form; named projects and `--all` are never combined in one command) and never steers an operator off an existing wrapper. An absent or unreadable unit is reported as no coverage rather than assumed fine, and a unit that runs the backup through `sh -c` is deliberately still reported as not backing up the runtime: only a recognised `admin.py` or wrapper `ExecStart` can be attributed to this runtime with certainty, and a wrong "already covered" answer could leave a project silently off the schedule, so the conservative direction is the safe one (it can prompt a double-check, never hide a gap). It reads the unit files only — it never edits, installs or enables a unit — and systemd drop-ins (`*.service.d/*.conf`) are not inspected, so the report is about the unit files themselves and not about a drop-in override.

### Local and Windows clients

Use `client.local.example.json` for explicit Linux same-host execution, under an account with access to the runtime. Use `beads.cmd` on Windows or `sh beads.sh` on POSIX from any directory. The wrappers take the same required config/project/actor arguments as client.py; no PowerShell execution-policy change is required. See [operational examples](OPERATIONAL_WORKFLOW.md).

Installed kit provenance is read only from the adjacent non-executable `provenance.json`
manifest. A clean archive may commit the explicit placeholder source identity
`unknown`; it must not commit the SHA of an earlier revision. A release build generates
the manifest outside the source archive after selecting the exact archive revision:
`{"schema_version":1,"component":"orchestra-kit","version":"...","source_commit":"<40 lowercase hex>","build_id":"<immutable build id>","files":{"VERSION":"<sha256>","client.py":"<sha256>","version.py":"<sha256>"}}`.
The `files` map binds the identity to the packaged bytes (excluding the manifest
itself). Installation copies the manifest and those files as one artifact and validates
every digest before use. Only `unknown` or a complete 40-character lowercase source
commit is accepted. Missing, malformed, stale, or mismatched manifests report
`source unknown`; the client never imports adjacent Python modules, consults ambient
`ORCHESTRA_SOURCE_COMMIT`, or infers identity from a parent Git checkout.

## Maintenance

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime service status
journalctl --user -u beads-team.service -n 50
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime service restart
```

If a client times out, inspect the task before retrying a mutation: the write may have committed. Do not replace a lost response with an assumed failure.

To retire the service, back up first, then `systemctl --user disable --now beads-team.service`. Preserve the runtime until recovery is no longer needed. Remove only the exact unit installed for that deployment and run `systemctl --user daemon-reload`. Linger is an account-wide setting: leave it enabled if other user services need it.

## Repeat validation

Local checks: `python -m unittest discover -s tests -v`.

Create two disposable projects and run the integration suite with an unused restore destination:

```sh
python tests/integration.py --config client.local.json --project testone --other-project testtwo --restore-project testrestore --output reports/local-integration.json
```

This restarts the configured server and creates synthetic records. Run it only on a disposable deployment, never a live team server. It checks two independent SSH clients, concurrent claims/comments, project separation, rendered corrections, restart and backup restoration.
