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

`add-project` initializes the project, provisions its merge slot (idempotently) and performs an initial backup, so a freshly provisioned project can run `merge-create`/`merge-check`/`merge-acquire` without a manual slot setup. A project whose slot is missing refuses `merge-check`/`merge-acquire`/`merge-release` with an error naming the `merge-create` operation, which is the manual repair. `backup` captures both the native backup directory and `backups/PROJECT.coordination.json`. The sidecar preserves pending child-request reservations and merge context outside Dolt. Keep this pair together. A pending marker is written before synchronization and becomes complete only after native sync succeeds; restore refuses an incomplete sidecar. The last complete sidecar is also kept as `backups/PROJECT.coordination.last-complete.json` before that marker replaces it, and is put back if the run fails or is interrupted, so one failed run never destroys the previous restorable pair; `restore-new` serves the canonical sidecar when it is complete and falls back to that durable copy when it is not. The operation-journal snapshot (`backups/PROJECT.http-operations.sqlite3`) is staged during the run and promoted only after the native sync and the new complete sidecar are durable, so the snapshot a restore replays always belongs to the same generation as the Dolt native backup and the complete sidecar: a run that fails after an acknowledged write leaves the previous snapshot in place, and the un-synced operation is not replayed from a restored journal. When `restore-new` cannot use the canonical sidecar it prints that it is using the durable last-complete copy and that the restored journal snapshot belongs to that generation; if a promotion after a successful sync fails, the new complete sidecar is kept rather than rolled back beside the new native directory. The complete sidecar also records a stat-only manifest (relative path, size and mtime) of the native backup directory as that generation finished, and `restore-new` recomputes it before any coordination or journal write: when the directory no longer matches, it prints a loud WARNING that the restored Dolt may hold effects whose receipts the restored journal does not have (an interrupted or killed run can leave the native directory partly rewritten while the restore serves the previous complete pair) and to take a fresh backup before relying on that pair. A sidecar written before the manifest existed records none, and the restore says the check could not be performed instead of calling the pair clean. Backup and restore serialize access to the pair, and backup excludes contributor writes through the endpoint. Direct operator/native writes bypass these locks and must be paused for backup. The completed sidecar also records the deployment operator allowlist, so a restore can report recorded authority the destination host does not list. `restore-new` does **not** apply it: re-granting an operator is deployment-wide authority and stays an explicit decision (`--restore-operators`, or `operators add OPERATOR`), so a stale backup cannot silently reverse a revocation. See [Operator removal and restore policy](#operator-removal-and-restore-policy).

Copy a completed, quiescent backup pair off-machine using your normal encrypted backup system. Do not copy it during the next sync. Preserve the pair together with its `backups/PROJECT.http-operations.sqlite3` journal snapshot, so a restored project's acknowledged operations are not re-executed. This is not an atomic transaction across arbitrary filesystem copies; take a filesystem snapshot or hold the project's `backups/PROJECT.lock` while copying (the `backup-copy` helper below does this for you). Legacy backups without a sidecar warn that outstanding requests/merge context require reconciliation.

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

`backup-copy DEST` applies exactly the `--require-complete` gate and refuses, naming what is missing, when any initialized project is not complete. It then copies each project's last complete pair — the native backup directory as `DEST/PROJECT`, the complete sidecar as `DEST/PROJECT.coordination.json`, and, when the project has one, its operation-journal snapshot as `DEST/PROJECT.http-operations.sqlite3` (the same shape a runtime's `backups/` directory has, so the copy can be copied back and restored) — and copies the record that gated the copy. Each project's `backups/PROJECT.lock` is held for that project's whole staging, while its coordination lock is held only long enough to re-check the pair and copy the small sidecar and journal snapshot; the long native copy runs after the coordination lock is released, so copying a large database does not block endpoint writes on it (both commands take the two locks in the same order, so no deadlock is possible). Files are staged in a per-run directory and then moved into place by rename rather than merged, so a concurrent `backup --all` cannot yield a mixed native directory beside a complete sidecar and stale destination files do not survive. The completeness record is published only after every project is in place: a failure exits non-zero with a clear error, removes the staging directory and leaves no completeness record, so the destination never looks complete when it is not. It prints exactly what it copied, is credential-free and never touches a unit, timer or schedule. The scheduled, encrypted off-machine system, its encryption, retention and destination, remain the operator's: this helper is the reference the operator can gate and schedule, not a replacement for that system, and it cannot see whether a copy actually left the host.

If restore is interrupted, the new destination may exist with only part of the restore completed. Preserve it for inspection; retry recovery into another unused destination. Do not delete the source or force reuse of the partially restored target. Verify pending reservations, comments, lifecycle events, baselines and slot context before switching clients.

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
