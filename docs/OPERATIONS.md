# Installation and recovery

## Install on Linux

Use an ordinary service account with a writable home, Python 3.10+, user systemd, and HTTPS access to GitHub release assets. Copy this kit to `/home/beads/beads-team-kit` in this example. Each deployment needs its own root, port and service name. Installation refuses unmanaged binaries or an existing service with the same name.

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime install --port 13317 --unit beads-team.service
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime add-project example
```

`add-project` initializes the project, records the standard native configuration, provisions the project's merge slot (idempotently) and performs the initial backup. A project whose slot is missing refuses `merge-check`, `merge-acquire` and `merge-release` with an error naming the `merge-create` coordination operation, which is also the manual repair for a project created before this behavior.

Project names use 2–24 lowercase letters/digits, starting with a letter. To use a project in the web interface (`http_service.py --backend endpoint`), a superuser then registers it there under the same name; the web service never creates canonical projects ([HTTP deployment](HTTP_DEPLOYMENT.md#projects-on-the-endpoint-backend)). After upgrading a web service, run its upgrade check once: `http_service.py --state <STATE> --list-unconfirmed-projects` lists project records made by an older kit that the service now refuses until a superuser confirms or archives them. Run these commands as the service account. An administrator should enable its user manager at boot and after logout:

```sh
sudo loginctl enable-linger beads
loginctl show-user beads -p Linger
systemctl --user is-active beads-team.service
```

The database listens on loopback only. Its generated credential is stored in the runtime's `deployment.private.json` (0600); runtime permissions are 0700. Contributors do not need the SQL password. Do not publish this directory or open the database port on the network. The installer configures metrics off for this deployment.

The kit runs `bd`, `dolt`, the HTTP service and the endpoint with `HOME` set to `<runtime>/home` and `BD_DISABLE_METRICS=1`, so Beads keeps its configuration inside the runtime and its usage metrics stay off; the kit writes nothing to the service account's own home directory. A runtime created before this change keeps working without a `home` directory: the environment variable alone keeps metrics off. Rollback note: an older kit reads Beads' configuration from the account's home directory instead, so before rolling back make sure `~/.config/bd/config.yaml` there has metrics disabled (`bd metrics off` as that account), or Beads turns its metrics on for a runtime that was created by this kit. Dolt's own global configuration is pinned to the runtime by `DOLT_ROOT_PATH` and is unaffected.

If initial installation fails, it stops the service and preserves files for inspection. Do not rerun by deleting the runtime: investigate the service journal first. Repeating installation of an already initialized deployment checks its pins, port and connectivity.

## Connect a contributor

Configure the [server-owned project entry point](ONBOARDING.md) so new workers can start from an empty directory using a single SSH onboarding command. Its private instructions are included in coordination sidecar backups.

Install Python 3.10+ and OpenSSH on their machine. Configure an SSH alias `beads-team` for the server/service account with their own key; verify the server host key on first connection. Confirm an ordinary `ssh beads-team` works before using the noninteractive client.

Copy `client.example.json` to `client.local.json`, adjust the host and paths, then run. The
copied `"forced_command": false` is today's behaviour; a contributor whose key is confined
sets it to `true` (see [confine contributor keys](#confine-contributor-keys-with-a-forced-command)):

```sh
python client.py --config client.local.json --project example --actor alex/session1 -- ready --json
python client.py --config client.local.json --project example --actor alex/session1 -- refresh
python client.py --config client.local.json --project example --actor alex/session1 -- view
```

Keep local config outside committed project content or ignored. The client transports arguments and UTF-8 file contents as JSON over SSH. It does not copy source code or run builds. `--body-file`, `--design-file`, `--file` and `-f` read files on the contributor's machine. The endpoint exposes the routine issue commands; setup and maintenance use admin.py on the host.

Copy templates/READ_ME_FIRST.md and docs/WORKFLOW.md into each project repository, fill in project details, and add links to README and AGENTS.md. This makes the entry instructions discoverable by later agents without needing a pasted chat message.

## Confine contributor keys with a forced command

The endpoint (`endpoint.py`) is what enforces the kit's authority rules: the operator
allowlist on every host command, endpoint writes that always stay `unverified`, the
reserved comment prefixes, the HTTP actor-shape reservation, and the runtime `--root`.
Those rules bind a caller who cannot choose the remote command, and nothing else. The
shared service account is shell-trusted (see the README), so a contributor key without a
forced command can run `admin.py` or `bd` directly and every rule above holds only against
a cooperative caller. A forced-command entry makes the boundary real for that key.

`ssh_forced_command.py` is the `command=` value of a contributor key's `authorized_keys`
entry. It gives the key exactly one capability - running the configured endpoint - and
nothing else:

* only the endpoint path(s) the entry names can be selected; another program, `admin.py`,
  `bd` or a shell is refused;
* `--root` is fixed by the entry, so the caller cannot point the endpoint at another
  runtime, and no `--authority-store`, `--authority-lock` or `--require-authority` flag is
  ever passed;
* `SSH_ORIGINAL_COMMAND` is used only to select the endpoint; any other program, any flag
  and any extra argument is refused on stderr with status 2, and nothing runs.

Print the two exact lines for a public key with the helper:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime \
  authorized-keys --key-file ~/alex.pub
```

The contributor line is the confined entry; the operator line is the bare key, deliberately
unrestricted because the host commands need a shell. They are alternatives for different
keys: install exactly one entry per key, and grant the operator line only to an allowlisted
operator. The helper refuses a line that already carries options, a private key, a second
key line, or paths or an interpreter that cannot be quoted safely inside the entry. It
prints the interpreter as an absolute path with `-E -s` (ignore `PYTHON*` variables and the
user site directory; not `-I`, which would also drop the script's directory from
`sys.path`), and it refuses a `--python` outside the same bare-name-or-absolute-path
character class as `--root`, so `--python '$(touch${IFS}/tmp/canary)python3'` is refused
instead of printed into an entry the account shell would expand on every connection.

Confinement binds the key to the endpoint, not to an actor. A confined key still
self-declares its actor on every request, exactly as an unconfined one does; what the
forced command protects is the operator-gated and reserved operations, which a contributor
key could otherwise reach by running `admin.py` or `bd` directly.

### sshd settings the boundary needs

The forced command closes what the key can run; two sshd settings decide what the client can
put into the session *before* it runs:

* `PermitUserEnvironment no` (the default). With `yes`, a client can send environment
  variables through its own `~/.ssh/environment` or `SetEnv`.
* No `AcceptEnv` beyond locale variables: `AcceptEnv LANG LC_*`. With `AcceptEnv *`, a client
  `SetEnv LD_PRELOAD=...` or `SetEnv BASH_ENV=...` reaches the service account: `LD_PRELOAD`
  loads a library into the shell and the interpreter, and `BASH_ENV` runs a script in the
  shell - both act before any kit code executes, so the wrapper cannot close them. (The
  printed line starts the interpreter with `-E -s`, so a forwarded `PYTHONPATH` no longer
  runs a module there.) The
  wrapper does close the endpoint's own process: it execs the endpoint with a minimal,
  explicit environment (`PATH`, `HOME`, `LANG`/`LC_*`, and the variables the kit sets for the
  endpoint itself - none today), so a forwarded `PYTHONPATH`, `BASH_ENV`, `ENV`, `LD_PRELOAD`
  or `SSH_ORIGINAL_COMMAND` is not inherited there.

Check the effective configuration rather than the file (the running config may come from an
included file or a default):

```sh
sshd -T | grep -Ei 'permituserenvironment|acceptenv'
```

`permituserenvironment no` and an `acceptenv` line limited to `LANG`/`LC_*` (or no
`acceptenv` line at all) are what the confined setup assumes. The key line needs OpenSSH 7.2
or later on the server: an older sshd does not know `restrict` and refuses the key (it fails
closed). `restrict` in the key options
separately closes the user rc file (`~/.ssh/rc`), which sshd would otherwise run for the
session.

### What the options close

The contributor line carries the sshd options beside the forced command:

| Option | What it closes |
| --- | --- |
| `restrict` | pty, port/agent/X11 forwarding and the user rc file (`~/.ssh/rc`) in one word, and any capability a later OpenSSH adds is off until this list is edited |
| `no-pty` | no interactive terminal, so the key cannot type at a shell prompt |
| `no-port-forwarding` | no `-L`/`-R` port forwarding through the service account |
| `no-agent-forwarding` | the key cannot use its agent on the host to reach other accounts |
| `no-X11-forwarding` | no X11 channel |

### The client must know

The forced command refuses the `--root` an ordinary command line carries, so the
contributor whose key is confined sets `"forced_command": true` in their
`client.local.json`; the client then sends only the endpoint path. The `endpoint` value
there must be exactly the path printed in the entry (the helper prints both together, so
copy them as a pair); `root` must still be present, but the wrapper's fixed root wins, so a
stale value there can never move the endpoint to another runtime. Without
`"forced_command": true` the connection is refused, loudly, with nothing run. Absent or
`false` leaves today's command line byte-identical, so no existing client changes behaviour
until it opts in.

```json
{"host": "beads-team", "endpoint": "/home/beads/beads-team-kit/endpoint.py",
 "root": "/home/beads/beads-runtime", "forced_command": true}
```

One onboarding command changes: the bare `ssh beads-team python3 worker.py ... start` form
runs another program and is refused by a confined key. Register through the endpoint
instead (`python client.py --config client.local.json --project example -- session register
--name cline`), or keep a separate, deliberately unrestricted onboarding key for whoever
provisions workers.

### Migration for an existing deployment

Confining a key and setting that contributor's client flag are a coupled pair: the two change
together, per contributor. A confined entry without `"forced_command": true` makes the client
send a `--root` the wrapper refuses; `"forced_command": true` without the confined entry makes
the account shell try to execute the endpoint path as a command, which fails with
`Permission denied` (status 126) on every request. Neither half is documented as safe on its
own.

1. Keep one unrestricted operator key (the operator line the helper prints) for `admin.py`
   and the host commands; do not confine it.
2. For each contributor key, replace any existing unrestricted entry for that key with its
   confined line. sshd uses the first matching line, so a second entry is not a migration.
   Keep the session you edit `authorized_keys` in open, and verify the operator key in a
   second session before closing the one used to edit `authorized_keys`: a bad edit would
   otherwise lock out the only way back in.
3. Have each contributor add `"forced_command": true` to their `client.local.json` at the same
   time as their entry changes, then confirm `python client.py --config client.local.json
   --project example --actor <actor> -- ready --json` succeeds and that `ssh beads-team
   /path/admin.py --help` is refused for that key.
4. Rollback is per key: remove the `command=` entry (and the `"forced_command"` key) and the
   previous behaviour returns. Nothing in the runtime or the tracker changes.

Nothing here changes a live deployment by itself: the coordinator rolls it out with the
owner.

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
| `requirement-reconcile PROJECT --operation-id ID --actor ACTOR --disposition ...` | finish a requirement operation whose real write was uncertain | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `reference-apply PROJECT --actor OPERATOR --file acceptance.json` | accept a reference catalog entry: the payload names the newest draft `revision` and its `record_sha256`, and the command writes the next revision as accepted, after the F3 evidence (`operation: "draft"` with full content writes a direct accepted revision 1) | the deployment operator allowlist, checked before any write, and F3 evidence (`acceptance`: owners, approvers, policy, decision id, evidence) |
| `reference-reconcile PROJECT --operation-id ID --actor ACTOR --reason TEXT --disposition ...` | finish a reference operation whose real write was uncertain; `complete` needs `--issue-id` and refuses an anchor that has no live revision record, because its propose stopped or every record it held is voided (re-run the original `ref propose` with its `operation_id` first; if that payload is lost, use `anchor-release`) | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `capability-apply PROJECT --actor OPERATOR --file batch.json` | accept a batch of capabilities under one F3 decision: `items` of `{key, revision, record_sha256}` (each the newest draft reviewed). The command writes one acceptance record and one receipt per item, keyed `(operation_id, key)`, in list order. It reports `accepted`, `already-accepted`, `refused` or `uncertain` per item; an uncertain item stops the batch, and re-running the same batch resumes it. A changed list needs a new `operation_id`. The coordination lock is taken per item and released between items, with a 50 ms pause while it is free, so other writers wait behind at most one item; each item re-checks its `revision` and `record_sha256` under its own hold. An item takes about 3 seconds (two reads and four writes), so 100 items take 5 to 6 minutes: prefer batches of about 20. `operation: "draft"` with full content writes a direct accepted revision 1 | the deployment operator allowlist, checked before any write, and F3 evidence |
| `capability-retire PROJECT --actor OPERATOR --file retire.json` | supersede the newest revision of a key by a `successor` key, with evidence. The successor must exist, and a cycle is refused. A retired key refuses `revise` and acceptance. This also stands in for the design's "demote" in slice 1a | the deployment operator allowlist and F3 evidence |
| `capability-alias-reject PROJECT --actor OPERATOR --file reject.json` | reject a pending alias (`{schema_version, key, alias, reason}`); lookup then ignores it | the deployment operator allowlist |
| `capability-alias-propose PROJECT --actor OPERATOR --file alias.json` | propose an alias as a verified operator (`{schema_version, key, alias, evidence?}`). This is the only route that writes `identity: verified`; `capability propose-alias` through the endpoint always writes `unverified`, even for an operator's actor name | the deployment operator allowlist |
| `capability-verify PROJECT --actor ACTOR --file payloads.json` | record capability checks as **verified**. The file is what `capability check --repo . --payloads payloads.json` wrote at the commit being verified (one payload, or `{schema_version, items}` of up to 500). Each item is one capability and takes the coordination lock on its own; the result is `recorded`, `already-recorded` or `refused` per item, and re-running the file is safe. This is the only route that writes a verified check: `capability check --record` through the endpoint always writes an unverified report | the deployment operator allowlist or the `verifiers` list, both checked before any read |
| `verifiers list\|add\|remove [ACTOR] [--confirm-revoke]` | manage the deployment `verifiers` list: actors, other than operators, whose `capability-verify` records readers count as verified. The list is empty by default and grants nothing else. `remove` needs `--confirm-revoke`; the refusal names the capabilities whose verification would change | shell access to the coordination host; `deployment.private.json` is the only authority source |
| `proposal-review PROJECT --actor OPERATOR --file review.json` | record a coordinator disposition on a requirement proposal. The payload is `{schema_version, operation_id, key, previous, proposal_sha256, to_state, ...}`: `previous` is the `disposition_comment_id` and `proposal_sha256` the `sha256` that `proposal get` returned, so a stale read is refused before any write. `to_state` is `under-review` (the claim), `rejected` (with `reason`), `duplicate-of` (with `duplicate_of`), `needs-info` (with `question`), `escalated-to-owner` (with `escalation: {question, owner_identity, due_by}`) or `incorporated` (with `incorporation`, checked against the requirement record) | the deployment operator allowlist, checked before any read; the actor must be mapped to a person and must not be the submitter |
| `proposal-decide PROJECT --actor OPERATOR --file decision.json` | record the owner decision on an escalated proposal: `to_state` `approved` or `rejected` (with `reason`), and `decision: {decision_id}` naming an existing native decision issue. Never a requirement id | the allowlist; the decider must be a different person than the escalator and must not be the submitter |
| `proposal-settings PROJECT --actor OPERATOR [--map-actor ACTOR --to IDENTITY] [--namespace NAME --to IDENTITY] [--unmap-actor ACTOR] [--unmap-namespace NAME] [--add-decider IDENTITY] [--remove-decider IDENTITY]` | with no change, print the contribution settings; otherwise write the next settings record, composed from the current one and bound to its hash | the allowlist |
| `proposal-http-records PROJECT [--after UTC] [--before UTC]` | list the proposal revisions and dispositions whose native author has an HTTP account or agent id shape, with the tracker's creation time. Run it after upgrading to the kit that reserves those shapes and after rolling a rollback forward ([HTTP deployment](HTTP_DEPLOYMENT.md#requirement-proposals)) | none: read-only, no lock, no `--actor` |
| `proposal-reconcile PROJECT --operation-id ID ...` | finish a proposal submit, revise or disposition whose write was uncertain | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `capability-reconcile PROJECT --operation-id ID ...` | finish a capability operation whose write was uncertain (for a batch item, the id is `OPERATION_ID/KEY`). A transient native failure can leave a `pending` receipt with no native row behind it, and the same `operation_id` is then refused until it is cleared: run `capability-reconcile --disposition released`, then retry the original command | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `record-reconcile PROJECT --kind requirement\|reference\|capability\|proposal ...` | the same reconcile for any record kind | as above |
| `reconcile-request PROJECT --request-id ID --actor OPERATOR --reason TEXT --disposition ...` | resolve a stuck coordination request receipt ([operational workflow](OPERATIONAL_WORKFLOW.md)) | the deployment operator allowlist, checked first; then its own actor-binding rules (`--any-actor`) |
| `retire-project PROJECT --actor OPERATOR --reason TEXT [--force]` | retire a partial or drill project: move `projects/PROJECT` to `retired/PROJECT-<UTC stamp>`. Nothing is deleted; see [Retiring a project](#retiring-a-project) | the deployment operator allowlist, checked first |
| `capability-misses-clear PROJECT` | delete the project's [capability lookup-miss log](#the-capability-lookup-miss-log). It prints what was removed (`finds`, `misses`, `phrases`), and in `repaired` any symlink, directory or unopenable lock file it removed from the three miss-log names (never following a link). It writes nothing to the tracker, takes no coordination lock and calls no `bd` | none beyond the service account: it deletes telemetry only, so there is no allowlist check and no `--actor` |
| `void-record PROJECT --actor OPERATOR --file void.json` | void a malformed or stale contribution-review record, or a malformed, foreign or conflicting reference or capability record ([below](#reference-and-capability-records)) | the deployment operator allowlist (`operators` in `deployment.private.json`) |
| `anchor-release PROJECT --kind reference\|capability --issue-id ID --actor OPERATOR --reason TEXT` | close a reference or capability anchor that holds no record and free its key, when the propose that created it cannot be re-run ([orphan anchors](#orphan-anchors)). With `--duplicate` (and, for an anchor that carries acceptance evidence, `--set-aside-evidence`) it releases a named anchor of a [duplicated key](#a-duplicated-record-key) although it holds well-formed records | the deployment operator allowlist, checked before any read |
| `handoff PROJECT --actor ACTOR --file handoff.json` | transfer a claim when the current owner cannot act | an owner decision/evidence pointer in the payload's `approval` |

All five are shell-trusted: access to the service account's shell is the boundary.
`requirement-apply`, `requirement-backfill`, `void-record` and `anchor-release` also
check the deployment operator allowlist (`operators` in `deployment.private.json`), so a
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

It refuses a populated destination, restores status and comments, and retains original issue IDs. The native restore is Dolt's own restore through the SQL client (`CALL DOLT_BACKUP('restore', '--force', 'file://.../backups/SOURCE', 'DEST')` over the loopback connection, bounded by the same explicit 30-minute ceiling as `backup`), not `bd backup restore`, which has the same fixed client read timeout of about ten seconds as `bd backup sync`: a 588 MB backup failed under `bd backup restore` at exactly 10 s (`i/o timeout`, `invalid connection`) and takes about a minute through the SQL client. The client runs in its own session/process group with the password in the environment, never on a command line; a normal `SIGTERM`, Ctrl-C or the ceiling stops that whole process group before the source's backup lock is released. Because bd does not run the restore, the kit then performs the one step `bd backup restore --force` would have performed itself: it writes the restored database's `_project_id` into the destination's `.beads/metadata.json` (every other key is kept), so bd accepts the restored project instead of refusing it with `PROJECT IDENTITY MISMATCH`; a backup that records no project identity leaves the file unchanged, as bd does. The command prints `Restored backups/SOURCE into DEST through the Dolt SQL client in N s.` and, when the identity changed, the identity it adopted. A destination with no Dolt server metadata in `.beads/metadata.json` has no SQL coordinates and keeps the `bd backup restore` path, as `backup` keeps `bd backup sync`. The native restore also carries the source project's recorded backup target, so `.beads/dolt-backup.json` and the restored `dolt_backups` row would still name `backups/SOURCE`; `restore-new` re-points the destination at `backups/DEST` immediately after the restore, and `backup` refuses (before any native command) a project whose recorded target is not its own `backups/<name>`, naming the recorded URL and the expected directory, so a clone that was never re-pointed cannot overwrite the source project's backup directory. A clone restored before that re-point existed (or whose re-point failed) is repaired in place with `admin.py backup-repoint DEST`, which runs `bd backup init backups/DEST` under the kit environment and verifies both `.beads/dolt-backup.json` and the `dolt_backups` row before reporting success; do not run a bare `bd backup init` (it fails without the kit environment, `Error 1045 Access denied for user root`) and do not re-run `restore-new` onto an existing name (it is refused and would discard the clone). Re-pointing moves no data: if the clone was ever backed up while still mis-pointed, the SOURCE project's `backups/SOURCE` may hold the clone's data, so back the SOURCE project up again before relying on that backup. Inspect records and comments before any cutover. Do not run both copies as live coordination trackers. A complete host-loss recovery requires reinstalling the pinned kit on a replacement host, placing the saved backup and its coordination sidecar under its backups directory (with timestamps preserved, see below), using restore-new, verifying it, then updating client project/host settings. No manual restore through the SQL client or edit of `.beads/metadata.json` is needed at any project size: `restore-new` does both. Re-grant operators and verifiers deliberately afterwards (the restore reports them as NOT restored), and take a fresh `backup` of the restored project before relying on it. A separate-deployment drill exercises backup transfer; actual replacement-host outage recovery remains an operator exercise.

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

**Requirement proposals: triage runs on the host.** A contributor submits a proposal through the client (`proposal submit`). Everything that rests on operator authority is a host command, because over SSH the actor is self-declared: `proposal-review`, `proposal-decide` and `proposal-settings` above. A stored settings record counts only when its native author is on the operator allowlist, and a stored disposition when its author is on the allowlist or is an HTTP account (the web service wrote it for a member with `reviews.approve`, see [HTTP deployment](HTTP_DEPLOYMENT.md#requirement-proposals)); removing an operator makes their dispositions inert (the proposal reads its earlier state) and re-adding them restores it.

- **Session-start step: map the coordinator before its first disposition.** The no-self rules compare people, not actor strings, so an unmapped actor is refused with "map this actor first". A new coordinator session brings a new `session-<uuid>` actor: after it registers, run `admin.py proposal-settings PROJECT --actor OPERATOR --map-actor session-<uuid> --to person:<name>`. To map a person once for all their sessions, use `--namespace <name> --to person:<name>`: it matches every session registered under `<name>`, `<name>/...` or `<name>-...` (see the name with `session show ACTOR`). A host operator who has no session, such as `james`, is matched by a namespace equal to that actor name.
- **An owner decision needs two people.** The decider must be a different person than the coordinator who escalated, and neither may be the proposal's submitter. A project with one allowlisted operator cannot both escalate and decide over SSH.
- **Deciders.** `--add-decider IDENTITY` lists who an escalation may name. With none configured, an escalation may name any durable identity.
- **Incorporating.** After the owner approves, draft the requirement revision through the ordinary `requirement` action, accept it with `requirement-apply` when it is ready, then record `incorporated` with `proposal-review`. A draft incorporation is allowed; `work` counts it as `incorporated_unaccepted` until the requirement is accepted.
- **Attribution is not authentication.** `identity: verified` on a proposal means the native author of every revision maps to its `person:` submitter, or wrote it through the web service as its `account:` submitter. Nothing is authorized by it. Only the submitter revises: an actor that does not resolve to a `person:` submitter is refused, whatever `submitter` its payload repeats; an unmapped contributor can still revise a proposal whose every revision they wrote. A proposal with an `account:` submitter is written and revised only through the web service.
- **`proposal mine --submitter IDENTITY` is a declared query over SSH**: it lists every proposal that names the identity, verified or not, and each item says which. On the web, "my contributions" lists verified proposals only.
- **An operator with a web account** maps the operator actor to the account: `proposal-settings PROJECT --actor OPERATOR --namespace OPERATOR --to account:usr_<id>`. Otherwise the no-self rules see two people. The mapping does nothing else: the operator actor still cannot revise the account's proposals or submit proposals that read as the account's. A namespace shaped like an account or agent id (`usr_...`, `agent_...`) is refused, and `operators add` refuses such an id.
- **Coordinator text is readable over SSH.** A reason, a question and an escalation question are filtered on `proposal get` and `list` by the declared actor, and `proposal mine --submitter IDENTITY` returns them to whoever names that identity. Both are self-declared over SSH, so anyone with endpoint access can read them. Do not put secrets in them.
- **Nobody reviews their own proposal.** The check compares the reviewer with the declared submitter and with the actor that wrote revision 1, so submitting under another person's name does not open a self-review.
- **Only a real settings record makes a settings anchor.** A task that merely carries the label `contribution-settings` is ignored by every reader and by `proposal-settings`.
- **Incorporating against a draft that was accepted later.** Link the revision the proposal landed in. If that content has since been accepted, record `acceptance_state: accepted` with the `acceptance_decision_id` of the revision that accepted it. If a later revision changed the content, the link reads `draft` and `work` counts it as `incorporated_unaccepted`: link the newer revision in a new proposal's incorporation instead.
- **Removing an operator reverses what they recorded.** `operators remove` (below) prints, before it acts, the proposals whose state changes and the projects whose contribution settings change.
  - A proposal the removed operator decided reads its earlier trusted state again, and no new disposition can be recorded on it while the ledger and the trusted state disagree.
  - If the removed operator wrote the first settings record, every later record in that chain stops counting: the actor map and the deciders read empty and triage stops. Enter them again with `proposal-settings`; the new record starts a fresh chain on the same anchor.
  - Re-adding the operator restores both. There is no other repair yet: `void-record` accepts reference and capability records (kittrial-5bb.74), but not proposal or settings records. Until then the submitter can submit a new proposal that supersedes the stuck one.
- **After a restore,** re-check `operators list` and `proposal-settings` before recording anything: both the allowlist and the actor map decide what counts.

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
  on such a project is a comment to investigate before you rely on the catalog. A
  malformed one can be voided ([reference and capability records](#reference-and-capability-records)).
  A well-formed one reads like a real record, so `void-record` refuses it: supersede it
  with a newer accepted revision, or retire the key.
  The coordinator ran this scan on all seven live projects on 2026-10-02 and found none.

A row is hidden as a record anchor only when it carries one of the labels `reference`, `proposal`, `contribution-settings` or `capability` **and** a v1 record comment of the same family. A project's own task that merely uses one of those words as a label (for example jjbp-j03.20's `proposal`) stays visible and editable.

**The `verifiers` list.** The deployment configuration may carry a `verifiers` list beside `operators`. It is a second, narrow deployment-wide authority: an actor on it may run `admin.py capability-verify`, and readers then count that actor's capability checks as `verified`. It grants nothing else. Keep it empty unless a host-side verifier other than an operator exists; the coordinator's integration step verifies as an operator.

- **One authority source.** `deployment.private.json` is the only source. A shell `ORCHESTRA_VERIFIERS` that disagrees with the file is refused by `verifiers add`, `verifiers remove` and `capability-verify`; it is never read as authority. Entries are actor identities (letters, digits, `_`, `.`, `-`), so an `account:` value can never be a verifier.
- **Revocation.** `verifiers remove ACTOR` requires `--confirm-revoke`. Afterwards every check that actor recorded reads `reported`, and drift that only their passes had cleared reappears. Nothing is deleted: re-adding the actor restores the reading. An actor who is also an operator stays trusted.
- **Backup and restore.** Each project's coordination sidecar records the list beside `operators`, for information. `restore-new` never re-grants it on its own. When the backup records verifiers this host does not list, the restore prints them, says they were **NOT** restored, and their checks read `reported`. Re-grant one with `verifiers add ACTOR`, or pass `--restore-verifiers` to re-establish the whole recorded list. `--restore-operators` does not re-grant verifiers.
- **One caller can fill the failing-report pool.** Every contributor posting through the endpoint is `unverified` until SSH actors are bound to people (kittrial-5bb.68), so they share one pool of 5 open failing reports per project. One caller can fill it, and then every other contributor's failing report is refused, naming the cap, until those failures are cleared. To clear them, an operator or listed verifier records a passing check at an integrated commit for each drifted capability (`capability check --payloads`, then `capability-verify`); `capability list` shows which ones read `drifted`. If the reports are noise, that pass is still the way to clear them, because a report is never deleted.
- **Integration step.** At the integrated commit, in a clean checkout: `capability check --repo . --payloads payloads.json`, then on the coordination host `admin.py capability-verify PROJECT --actor OPERATOR --file payloads.json`. The payloads file is created private (mode 0600) and is never written through a symbolic link. Drift clears only on such a verified pass at a commit the project's lifecycle evidence records as integrated; a reverted integration does not count.
- **Rolling back below this kit.** An older kit keeps every record, hides them as before and simply reports no `verification`. It never rewrites or removes `views/CAPABILITIES.md`, so after a rollback delete `projects/PROJECT/views/CAPABILITIES.md` by hand; otherwise the last page rendered stays readable through `view` with its old export stamp.

`add-project` initializes the project, provisions its merge slot (idempotently) and performs an initial backup, so a freshly provisioned project can run `merge-create`/`merge-check`/`merge-acquire` without a manual slot setup. A project whose slot is missing refuses `merge-check`/`merge-acquire`/`merge-release` with an error naming the `merge-create` operation, which is the manual repair. `backup` captures both the native backup directory and `backups/PROJECT.coordination.json`. The sidecar preserves pending child-request reservations and merge context outside Dolt. Keep this pair together. A pending marker is written before synchronization and becomes complete only after native sync succeeds; restore refuses an incomplete sidecar. The last complete sidecar is also kept as `backups/PROJECT.coordination.last-complete.json` before that marker replaces it, and is put back if the run fails or is interrupted, so one failed run never destroys the previous restorable pair; `restore-new` serves the canonical sidecar when it is complete and falls back to that durable copy when it is not. The operation-journal snapshot (`backups/PROJECT.http-operations.sqlite3`) is staged during the run and promoted only after the native sync and the new complete sidecar are durable, so the snapshot a restore replays always belongs to the same generation as the Dolt native backup and the complete sidecar: a run that fails after an acknowledged write leaves the previous snapshot in place, and the un-synced operation is not replayed from a restored journal. When `restore-new` cannot use the canonical sidecar it prints that it is using the durable last-complete copy and that the restored journal snapshot belongs to that generation; if a promotion after a successful sync fails, the new complete sidecar is kept rather than rolled back beside the new native directory. The complete sidecar also records a stat-only manifest (relative path, size and mtime) of the native backup directory as that generation finished, and `restore-new` recomputes it before any coordination or journal write: when the directory no longer matches, it prints a loud WARNING that the restored Dolt may hold effects whose receipts the restored journal does not have (an interrupted or killed run can leave the native directory partly rewritten while the restore serves the previous complete pair) and to take a fresh backup before relying on that pair. A sidecar written before the manifest existed records none, and the restore says the check could not be performed instead of calling the pair clean. Backup and restore serialize access to the pair, and backup excludes contributor writes through the endpoint. Direct operator/native writes bypass these locks and must be paused for backup. The completed sidecar also records the deployment operator allowlist, so a restore can report recorded authority the destination host does not list. `restore-new` does **not** apply it: re-granting an operator is deployment-wide authority and stays an explicit decision (`--restore-operators`, or `operators add OPERATOR`), so a stale backup cannot silently reverse a revocation. See [Operator removal and restore policy](#operator-removal-and-restore-policy).

Copy a completed, quiescent backup pair off-machine using your normal encrypted backup system. Do not copy it during the next sync. Preserve the pair together with its `backups/PROJECT.http-operations.sqlite3` journal snapshot, so a restored project's acknowledged operations are not re-executed. Preserve file timestamps on the way out and on the way back (`cp -a`, `rsync -a`, or an archive that keeps mtimes): the complete sidecar's stat-only manifest keys on each file's `mtime_ns`, so a copy-back that resets timestamps makes `restore-new` warn that the native directory no longer matches its generation even though the bytes are intact. This is not an atomic transaction across arbitrary filesystem copies; take a filesystem snapshot or hold the project's `backups/PROJECT.lock` while copying (the `backup-copy` helper below does this for you). Legacy backups without a sidecar warn that outstanding requests/merge context require reconciliation.

Every run of `backup` also writes `runtime/backups/backup-status.json`: a schema-versioned, sorted-key record of the run with one entry per project, naming whether that project's pair (its `backups/PROJECT` native directory plus its `complete` `backups/PROJECT.coordination.json` sidecar) is complete, the UTC time it completed, and the reason when it is not. The per-project pair state is re-read from the files, so a run that fails or is skipped — and even a "successful" call whose sidecar is still `pending` — is recorded as **not** complete rather than assumed complete. A named or single-project run updates its own entries and carries the other projects' last known state forward instead of erasing it; `generated_at` and `scope` always describe that run, so a carried-forward entry is never presented as its result. Record scope is explicit: `"scope":"all"` means the run covered every project initialized at that moment, `"named"` means it covered only the projects listed. A named run refuses a name that is not an initialized project before it backs up or records anything (`backup NOSUCHPROJECT` leaves the record unchanged), and no run carries forward an entry for a name that is not an initialized project, so an entry an older kit recorded for a mistyped name is dropped by the next run.

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

If restore is interrupted, the new destination may exist with only part of the restore completed. When the native step of `restore-new` fails, reaches its ceiling or is stopped (`SIGTERM`/Ctrl-C), the kit stops the restore client's process group, prints `restore-new did not complete: ...` naming the cause and the partial project, and exits non-zero without the re-point, the coordination sidecar, the journals or the operation journal: the destination then exists with an initialized but partial database (its `.beads/metadata.json` still holds its own fresh identity, so bd may refuse it). A `kill -9` of `admin.py` itself runs no cleanup, so the client may still be writing the destination's database for a while. Preserve it for inspection; retry recovery into another unused destination. Do not delete the source or force reuse of the partially restored target. While a destination left by a stopped or timed-out restore remains in the runtime, a `backup` of it fails (`backup 'default' not found`), so expect `backup --all`, `backup-status --require-complete` and `backup-copy` to report the runtime incomplete until it is dealt with; the source project's own backup pair is not touched. Retire such a project with `retire-project` ([Retiring a project](#retiring-a-project)); recovery drills are still better run in a separate runtime than in a live one. When the native step fails on a damaged backup or a refused password instead, the destination is an empty, working project rather than a partial one, and the notice says so; retiring it needs no flag. When the restore stops before `add-project` initialized the destination, the notice says the directory exists but is not a working project. A `SIGTERM` during the identity step that follows the native restore is reported like a stop during the restore itself. Every way the restore can stop after the destination starts to exist prints the notice, naming the step: a failure, `SIGTERM` or Ctrl-C in the `add-project` step, in the native restore, or in the re-point and coordination step. If the destination's directory has disappeared by then, the notice says that instead. Ctrl-C ends with exit status 130, and a refusal ends with one line (`ValueError: ...`), not a traceback. Only the kit's own refusals are shortened: any other error keeps its traceback. A `deployment.private.json` or `--file` payload that is not valid JSON is a refusal that names the file. Verify pending reservations, comments, lifecycle events, baselines and slot context before switching clients.

### Retiring a project

`admin.py retire-project PROJECT --actor OPERATOR --reason TEXT` takes a project out of the runtime without deleting anything. It is for a project a stopped `restore-new` left behind, or a drill project you no longer want backed up.

What it does:
- It moves `projects/PROJECT` to `retired/PROJECT-<UTC stamp>` with one rename, under the project's backup lock and coordination lock.
- The project is then not initialized. `backup --all`, `backup-status --require-complete` and `backup-copy` stop counting it, and the endpoint answers "Unknown/uninitialized project" for it.
- `backups/PROJECT`, every other project's backups and the Dolt database are not touched. To undo, move the directory back.
- Both steps are written to `retired/journal.jsonl`: the intent before the move and the result after it, each with the actor, the reason, what the checks found and whether `--force` was used.

`--force` is needed when the project looks like a working tracker, holds the merge slot, or has pending reservations. The checks fail closed: what cannot be read is treated as the dangerous answer. The refusal names what it found:
- **It looks like a working tracker:** bd reads the project and it holds issues. The state of its backups does not matter; a healthy project whose last backup failed is still a healthy project.
- **Nothing could be checked:** the Dolt server (or bd itself) could not be reached, or the server did not answer the probe within 15 seconds (a frozen server does not make the command hang with its locks held). The project may be a healthy tracker, so this needs `--force` too. Start the server and retry instead when you can.
- **Its `.beads/metadata.json` is there but unusable:** it cannot be opened, is not JSON, or does not record the Dolt server. The kit cannot tell what the project is, so this needs `--force`. bd is never run on such a project: without the server coordinates bd would fall back to an embedded database, create `.beads/embeddeddolt` inside the project and report it as empty.
- **The merge slot** is held, or bd reads the project but could not read its slot.
- **Reservations:** pending receipts in its request journals, or receipts that could not be read.

A project a stopped restore left behind needs no flag: the server answers and bd rejects the project. An empty project that bd reads (what a restore that failed before writing leaves) needs no flag either, and neither does a directory that was never initialized (no `.beads/metadata.json`: what a stop during the `add-project` step leaves).

"bd rejects the project" is any refusal by bd while the server answers. By design that also covers a project whose recorded `project_id` or `dolt_database` was changed by hand so that it no longer matches its database: bd refuses it exactly as it refuses a partial restore, the kit cannot tell the two apart, and it retires with no flag. Nothing is deleted, and moving the directory back undoes it.

`retire-project` is refused, with or without `--force`, while a `restore-new` into that name is running: the restore holds `backups/PROJECT.restore.lock` for its whole run. Wait for it to finish or stop it first.

If the move itself fails (for example `retired/` is on another filesystem), nothing is changed, the journal gets a `failed` line after the `intent` line, and the message says so. `retired/` must be a real directory: while it is a symlink, `retire-project`, `add-project` and `restore-new` all refuse, because retired names could not be checked.

Afterwards:
- **The name stays reserved.** `add-project PROJECT` and `restore-new SOURCE PROJECT` refuse a retired name and print the retired entry, because the database of that name is still on the server and a new project would adopt it.
- **The gate passes at once.** `backup-status --require-complete` ignores the entry the last run recorded for the retired project. The record's own `status` stays `incomplete` until the next `backup --all`, which drops the retired entry; a health line built on that status recovers then.
- **Retired names are visible.** `backup-status` prints them in a `retired` list (the key appears only when there are any), and `backup-copy` prints them and does not copy them.
- **The web interface.** The command cannot see the web service's state. If the project is registered there, archive it in the web interface first; otherwise its task pages answer the endpoint's "Unknown/uninitialized project". The command prints this reminder every time.
- **Deleting for good** (dropping the database and the retired directory) has no command yet.

**Upgrade note: reconcile commands and an empty allowlist.** `requirement-reconcile`, `reference-reconcile`, `capability-reconcile`, `record-reconcile` and `reconcile-request` now refuse an actor that is not on the deployment operator allowlist. A deployment that has never configured operators must add the acting operator first, or every one of these commands refuses with "No operator allowlist is configured":

```bash
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators add OPERATOR
```

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

### A duplicated record key

A reference or capability key normally has exactly one anchor. If a second anchor carries the same key label, `get` reports `duplicate-key` (when exactly one anchor has live acceptance evidence, that one is still shown) or `conflicted` (no record is shown), `list` and coverage name every anchor, and **every write on that key is refused** until an operator reconciles the anchors. Contributors cannot create a duplicate through the endpoint; it takes native access on the host.

When the extra anchor holds no record, or only malformed comments, repair it with the kit: void each malformed comment (`admin.py void-record`), then release the anchor (`admin.py anchor-release --kind reference|capability --issue-id ID --actor OPERATOR --reason TEXT`); the key leaves the duplicated state and writes work again.

When the extra anchor holds a well-formed record, a void cannot repair it: a void never withdraws a well-formed record the entry reads, and that includes acceptance evidence. Release the wrong anchor by name instead. First read every anchor (`capability get KEY` or `ref get KEY` names them, and `history` shows each one) and decide which is the genuine one. Then, on the host:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime anchor-release PROJECT --kind capability --issue-id PROJECT-FORGED --actor OPERATOR --reason 'forged beside PROJECT-GENUINE' --duplicate
```

`--duplicate` releases the NAMED anchor only. Every refusal comes before the first write:

- The actor is not on the deployment operator allowlist (checked before any read).
- The row is not an anchor of that kind, was already released, or carries more than one key label.
- The key is not duplicated. The anchors are counted exactly as every write counts them, so the last anchor of a key is never released this way.
- The named anchor holds a record this kit cannot read (a newer record version).
- The named anchor holds a readable record and no remaining anchor does. "Readable" is what a reader would present: an anchor that is record-less, whose records are all voided, or that reads `malformed`, `unsupported` or incomplete does not count, even when its revisions themselves parse (one other bad comment on an anchor makes readers refuse the whole entry). Every other reader state counts, including an entry whose acceptance is inert because its operator was removed (it reads `draft-only` with `acceptance-inert`), a superseded entry and a drifted one. A key is never left with nothing readable: deal with the other anchor first (release it; void its malformed records first if the plain mode asks for that), which leaves the readable one as the key's only anchor.
- The named anchor carries acceptance evidence that no void names, **live or inert**. It needs the extra flag `--set-aside-evidence`. That covers the anchor readers currently select: among duplicates only live acceptance evidence selects an anchor. Inert evidence (its operator was removed from the allowlist) counts, because it would read live again if that operator were re-added.
- The named anchor reads `malformed` and carries live acceptance evidence. It may be the key's accepted record behind one bad comment: void that comment first (`admin.py void-record`), then run the release again.
- The named anchor reads an accepted record and no remaining anchor reads one. The key never loses its only accepted record; release the other anchor instead. Live acceptance evidence on a remaining anchor is not enough: an operator accept that stopped after writing its evidence and before the accepted revision leaves evidence on a draft.

What a release does, in order: it settles the pending receipt the row's `request:` label names, closes the row if it is open, adds one audit comment as the operator, then removes the state, `request:` and `request-content:` labels and, last, the key label. The type label and every comment stay, so nothing is deleted and the row stays hidden from `work`. Each step is skipped when already done, so a re-run after an uncertain write finishes the release.

The audit comment names the operator, the reason, the key, the anchors that remain, which anchor readers selected before and select now, every record set aside (revision and hash), every piece of acceptance evidence set aside (revision, record hash, operator, decision id, live or inert), and the count of other records on the anchor (capability aliases and verifications) that stop counting for the key. The printed JSON carries the same facts: `remaining`, `selected_before`, `selected_after`, `records`, `evidence_set_aside`, `stop_counting`.

A released row is no longer an anchor of any key and is never read for one again. **Evidence set aside stays set aside**: re-adding the operator who recorded it does not bring it back, and the key reads exactly as it did after the release. The release cannot be undone through the kit. Aliases and verifications on the released anchor stop counting; record them again on the remaining anchor if they are still wanted. Take a backup afterwards.

Which anchor to name, by shape:

| The key's anchors | The safe sequence |
| --- | --- |
| a genuine draft and a record-less anchor (orphan) | plain `anchor-release` of the orphan |
| a genuine draft and a forged draft | `--duplicate` on the forged one |
| two accepted anchors, both with live evidence | `--duplicate --set-aside-evidence` on the forged one |
| an accepted anchor and a draft whose accept stopped after its evidence | `--duplicate --set-aside-evidence` on that draft |
| a forged accepted anchor whose evidence is inert (or by an unlisted author) and a genuine draft | `--duplicate --set-aside-evidence` on the forged one |
| a genuine entry whose acceptance is inert (its operator was removed) and a forged draft | `--duplicate` on the forged draft; the genuine entry keeps its evidence and reads accepted again if the operator is re-added |
| two anchors whose acceptance is inert | `--duplicate --set-aside-evidence` on the forged one: its inert evidence is set aside for good. If you cannot tell which is genuine from the records, re-add the operator(s) first so the entries read as they were accepted, decide, then release with the same command; or use the host procedure below |
| an anchor that reads `malformed` beside a readable one | void the malformed comment, or `--duplicate` on the malformed anchor when it is the forged one and carries no live evidence |

Two shapes have no clean kit path:

- **A forged accepted anchor with LIVE evidence beside a genuine draft.** The forged anchor holds the key's only accepted record, so it is not released. Release the genuine draft with `--duplicate` (the forged anchor is readable, so this is allowed), then revise and accept the right content on top of the anchor that remains.
- **A forged anchor that holds a `-v2` record or an unknown record kind of the family.** This kit cannot judge it, so it refuses to release it, and it refuses to release the genuine anchor beside it (nothing readable would remain). Use the host procedure below, or a kit that reads the record.

When two anchors both have live acceptance evidence, the key reads `conflicted` and neither is selected. Decide which is wrong, with the project owner if it is not obvious, and release that one with `--duplicate --set-aside-evidence`; the other becomes the selected anchor, and the output shows it. Do not try to void the acceptance evidence first: that void is refused.

On an older kit (rollback), a released anchor that still holds records is not read for its key either, but that kit lists it as a `malformed` entry with no key in `coverage` until the kit is rolled forward. A project backup taken with duplicates restores them as they were, and the release works on the restored project.

Last resort, when the command refuses a case you have decided by other means: on the host, as an operator, remove the type, key and state labels from the wrong anchor with the native tool under the kit environment, for example for a capability:

```sh
bd update PROJECT-FORGED --remove-label capability --remove-label capability-key:KEY-SLUG --remove-label capability:accepted
```

Use the labels the forged anchor actually carries (`bd show PROJECT-FORGED --json`). The stripped issue stays in the database, closed, with its comments, as evidence; it no longer counts as an anchor, and writes on the key work again. Record what you did and why on the genuine anchor's project, and take a backup afterwards.

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

#### Reference and capability records

A comment that claims a reference or capability record kind but fails its schema makes
only its own entry read `malformed`: `ref` and `capability` reads, `work` and `brief`
keep working and name the anchor in `coverage`, but a `revise` of that key is refused
until the record is repaired. `void-record` repairs it (kittrial-5bb.74). The payload is
the one above, with the anchor as `task` and one of these as `target_kind`:
`reference-entry`, `reference-acceptance`, `capability-entry`, `capability-acceptance`,
`capability-verification` or `capability-alias`. `ref get KEY` (or `capability get KEY`)
gives the anchor as `native_id`; on the host, read the comment's `id` and exact `text`
from `bd export --all`, as in the scan above.

- **Same command, same authority.** The host command, the allowlist check before any
  read, the payload binding, the idempotent retry and the refusal of a reused
  operation ID or a second void of one target are the review void's. Reads trust a void
  by the same rule (`recovery.records`). Like a contribution-review void it is one
  appended native comment and writes no host journal: only the integration-revert
  retraction is journaled, because it must prove that the host issued the revert.
- **What a void applies to.** A void applies only to a comment the entry cannot read:
  one that fails its kind's schema (a BOM or CRLF lookalike of a v1 prefix included),
  belongs to another anchor or key, or holds a revision, or the acceptance evidence for
  a revision, that an **earlier** comment of the same kind already holds. Of a
  conflicting or duplicated pair, only the later one can be voided: the writer never
  writes a second holder, so the earliest is the only one it can have written, and a
  void cannot itself be voided. A well-formed record the entry reads is refused at
  write and ignored on read. A void repairs history and never withdraws a decision: to
  replace an entry, revise it and accept the new revision, or retire the key.
- **What readers show.** The entry reads as if the voided comment were absent, and its
  `warnings` carry `record-voided`. A void written around the host command is reported
  instead: `void-invalid` (malformed, stale, out of order, or not written by a
  configured operator) or `void-refused` (it names a record the entry reads, the
  earliest holder of a revision, or a record of another kind). The anchor stays hidden on every surface, because all its
  comments are kept.
- **Writes agree.** The endpoint gives contributor writes the allowlist, so `ref
  revise` and `capability revise` see the same entry `get` shows once the void applies.
- **Revocation.** After `operators remove`, that operator's voids stop applying here too
  and the entry reads `malformed` again; re-adding the operator restores the repair.
  Without `--confirm-revoke`, `operators remove` counts the operator's voids of
  reference and capability records that apply today and names the entries whose
  reading changes (for example `example/reference calendar.trading draft-only ->
  malformed`).
- **Reconcile agrees.** `reference-reconcile` and `capability-reconcile --disposition
  complete` read the allowlist too, and refuse an anchor whose every record is voided,
  as they refuse one whose propose never posted its record.
- **Rolling back below this kit.** An older kit's reference and capability readers do
  not read voids at all, so a repaired entry reads `malformed` again there (only that
  entry). The anchor stays hidden. A review read of the anchor, if one reaches it, lists
  the void among ignored operator void comments. An older `void-record` refuses these
  target kinds before any write. No sidecar path is added, so backups restore on either
  kit, and rolling forward restores the repair with nothing to clean up.
- **Limits.** A record of a newer version (`Kind: reference-entry-v2`) or of an unknown
  kind of the family cannot be a void target: the target must claim the v1 prefix of
  the declared kind, exactly or through the BOM/CRLF view the readers use. It stays
  `unsupported` (or `malformed`) until a kit that reads it handles it.

#### Orphan anchors

`ref propose` and `capability propose` create the entry's anchor, close it and post
revision 1 inside one locked operation. If that write stops in between, the anchor holds
no record: readers report it as an incomplete anchor, every other operation is refused
the key, and if it stopped before the close the row is open and claimable in `work`. Only
the same operation's retry, with the identical payload, finishes it. When that payload is
lost, release the anchor:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime anchor-release PROJECT --kind reference --issue-id ANCHOR --actor OPERATOR --reason 'original payload lost'
```

- It checks the operator allowlist before any read. It refuses a row that is not an
  anchor of that kind, or that still holds a live record. An anchor whose every record an
  applied void names holds none, so the same command frees it. An anchor that holds a
  well-formed record is released only as a named duplicate of its key, with
  `--duplicate` ([a duplicated record key](#a-duplicated-record-key)).
- It marks the pending receipt that the row's `request:` label names as `released`, with
  the audit, so that operation ID is settled.
- It closes the row if it is open, adds one plain audit comment, then removes the state,
  `request:` and `request-content:` labels and, last, the lookup label, which frees the
  key. The type label stays, so an anchor with voided records stays hidden, and a row
  that never held a record reads as the ordinary closed task it already was.
- Each step is skipped when it is already done, so a re-run after an uncertain write
  finishes the release. Once the lookup label is gone the row is no longer an anchor, and
  a re-run is refused.
- It is a host command only. Over SSH the endpoint actor is self-declared, so nothing
  that rests on the operator allowlist is offered there.

On an older kit a released row holds no key either, and it is never claimable:
- a row that never held a record is the ordinary closed task it is on this kit;
- an anchor whose records were voided stays hidden as a task, but that kit does not
  read voids, so its catalog lists it as a `malformed` entry (with no key) in `coverage`
  and counts it as `malformed` in the `work` attention block until the kit is rolled
  forward.

#### Operator removal and restore policy

`admin.py operators remove OPERATOR` is a revocation, not a cleanup: voids that operator authored stop applying on read, the incident is reported as unreconciled again, and re-adding the operator restores those dispositions. The same holds for the requirement-proposal dispositions, owner decisions and contribution settings they recorded; the refusal names the proposals and settings that change (see "Requirement proposals" above). Because that destroys recorded dispositions, `remove` refuses unless `--confirm-revoke` acknowledges the consequence.

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
