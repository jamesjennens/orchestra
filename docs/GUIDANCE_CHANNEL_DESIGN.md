# Standing guidance channel and the composed turn prompt

Status: draft design note for owner acceptance (kittrial-5bb.99). Slice 1 (the
guidance record, its version and the acknowledgement) is implemented in the same
contribution; slices 2-4 are proposed, not built.

## Intent

Owner request (James, 2026-10-03): workers should have a standing instruction to
check for worker guidance on the server at each turn, so an instruction inserted
there is picked up by every worker. Follow-up: the whole turn prompt can live on
the server, shrinking the worker's local prompt to a bootstrap.

Today the server returns instructions at `onboard` (the installed kit's shared
worker instructions plus the project entry point set by `admin.py set-onboarding`),
but a worker reads it once, nothing tells it the text changed, and the shared
steps do not mention the capability or reference lookups. The kittrial
`ONBOARDING.md` carries a dated "Current coordinator guidance" section and tells
workers to re-run `onboard`; that is the interim this channel replaces.

## Model

* One **guidance record per project**, separate from the onboarding entry point.
  It is bounded (8000 bytes), plain text, and set only by the operator host route
  `admin.py set-guidance PROJECT --actor ACTOR --file FILE`. The configured
  operator allowlist (`deployment.private.json`) is checked first, so an empty
  allowlist authorizes nobody. `--actor` is a typed string checked against that
  file, which the host user can edit: it separates a contributor client from the
  host route, but the real boundary is access to the host shell (and, until SSH
  keys are confined under kittrial-5bb.89, that is the only boundary).
* The record is **audited**: every set records who (`set_by`), when (`set_at`),
  the new version, the previous version and the previous text. The history is
  bounded (50 entries); the acknowledgement table is bounded (500 actors). The
  text is attributed to a setter only when the stored version is the hash of the
  exact text read, so a hand edit or a crash between the two writes reads as a
  mismatch with no setter (`warning`, `set_by: null`), never as the previous
  setter's work. An operator set with the same text repairs a missing or
  mismatched record, and the writer never credits an unbound generation to the
  previous operator in the history.
* The **version is the SHA-256 of the guidance text**, computed by every reader
  from the text it actually read. A crash between the text write and the metadata
  write therefore cannot advertise a version that does not describe the text; it
  leaves an unbound record that the next set (even with the same text) repairs.
* Every **work**, **brief** and session **resume** response carries a compact
  guidance block: `present`, `version`, `set_at`, `set_by`, `previous_version`,
  `acknowledged`, and `attention` (true exactly when guidance is set, or cannot be
  read, and the calling actor has not acknowledged the current version).
  `guidance get` returns the full text and what changed since a named version;
  `guidance ack --version VERSION` records the calling actor's own read of that
  exact version (a stale or unnamed version is refused, naming the current one);
  `guidance status` (operator only) lists which lanes have acknowledged which
  version. An unreadable record is reported (`unreadable: true`, `attention:
  true`, a `warning`), never as an error.
* The **endpoint never writes guidance**. Its subcommands are an explicit allowlist
  (`get`, `version`, `ack`, `status`); every write-shaped subcommand is refused,
  and the tests assert that.
* The **acknowledgement table is bounded and pruned**: acks for versions other than
  the current and previous one are dropped at every set; a new lane evicts the
  oldest entry when the table is full rather than being refused; and the operator
  can drop stale acks with `admin.py compact-guidance-acks` (audited in the record
  as `acks_compacted_by`/`acks_compacted_at`). An acknowledgement is **not
  authentication**: actors are self-declared, so naming another actor is always
  possible and an ack proves only that *some* caller named that actor and version.
  The endpoint therefore requires a registered session (or a configured operator
  name) for an ack, which removes unregistered made-up names as the cheap way to
  fill the table; a future composed prompt must not treat an ack as proof that the
  text was read or followed.

## Trust boundary

Guidance is an **instruction channel to agents**, so the trust rules are the
strict ones:

* Only the operator host route writes it. The endpoint can read it and record a
  registered actor's own acknowledgement of the version it names and nothing else.
* Every read marks who set the current text and when **only when the stored version
  is the hash of the text read**; a mismatched or missing record is reported as
  unattributed with a warning.
* The host route is protected by the operator allowlist plus host shell access, and
  `--actor` is a typed string checked against a file the host user can edit. Until
  SSH keys are confined under kittrial-5bb.89, host shell access is the only real
  boundary. An acknowledgement is unauthenticated: any caller that can reach the
  endpoint can name another actor, so an ack proves only that some caller named
  that actor and version, not that the text was read.
* It never overrides the user's authorization, the machine's limits for that
  agent, or the worker safety rules. The worker instructions say so explicitly,
  and a worker declines and reports guidance that asks for something outside its
  user's authorization.
* Contributor-written text (task titles, proposal text, capability summaries,
  reference entries) can never reach the guidance file: no client action writes
  it, and the shared instructions keep marking such text as data.

## Rollback and backup behaviour

* `GUIDANCE.md` and `.guidance.json` are included in the project coordination
  backup and restored as one generation; `validate_coordination_files` and
  `restore_coordination` validate both individually and that the record's version
  is the hash of the text, so a malformed or mismatched pair refuses the backup or
  restore rather than silently dropping or misattributing acks.
* **Rollback gap:** an older kit (at or before `dca96b9d`) validates the
  coordination sidecar against a fixed path set that does not include the two
  guidance files, so `restore-new` by that kit on a backup that contains them fails
  with `Invalid coordination backup path`; a backup taken by the older kit during a
  rollback succeeds but silently omits guidance (an older-kit project has no
  guidance files at all). [OPERATIONS.md](OPERATIONS.md#standing-guidance) states
  the gap and the exact step: clear the guidance on this kit
  (`admin.py clear-guidance PROJECT --actor OPERATOR`) and take the backup before
  rolling back, then re-set the guidance after rolling forward.
* Tolerant readers: a missing `GUIDANCE.md` reads as `present: false`; a missing,
  older or malformed `.guidance.json` leaves the text and its computed version
  readable with `set_by: null` and a warning, and unknown metadata keys are
  ignored. A worker whose server or guidance cannot be read keeps following the
  version it last acknowledged, names that version when it reports the failure,
  does not invent rules, and reads and acknowledges the current version when the
  server is reachable again.
* The operator can roll the text back by re-running `set-guidance` with the
  previous text (the previous text is kept in the audit record, and the history
  keeps the earlier versions), or by restoring the project's previous backup.

## The composed turn prompt (proposed, slices 2-4)

The widened direction: the worker's local prompt shrinks to a bootstrap (host,
project, private working directory, saved actor and client config, and the limits
its user sets), and one command per run (`worker.py resume`, or a new next
action) returns the whole turn prompt composed by the server.

Composition order (all server-side):

1. the kit's shared worker instructions for the installed version;
2. the project's operator-set guidance (the record above);
3. the actor's own state: claims, requested revisions, pending review items,
   attention, and the suggested next action;
4. per-role variants (worker, coordinator, reviewer) and a size bound.

What must stay local and must NOT be overridable from the server: the user's
authorization and limits for that agent on that machine (which directories it
may touch, whether it may push, deploy or run host commands), where the server
is and which key or actor to use, and the rule that server text is the
coordinator's instruction to be followed only within those limits.

Risks the design must keep handling, stated here so the owner can decide with them
in front of him: whoever can set the server prompt directs every agent, so writes
stay operator-only, audited and versioned (and the confined SSH setup,
kittrial-5bb.89, matters more); acknowledgements are unauthenticated and
self-declared, so a composed prompt must not present `acknowledged` as proof that a
lane read or followed the text; contributor-written text is marked as data inside
the composed prompt; an unreachable server leaves the worker following the last
version it acknowledged (it names that version; the server keeps the record); the
rollback gap above means guidance is lost if a project is rolled back to an older
kit without the documented clear-first step; size bounds apply per part and overall.

## Slice plan

1. **Done here:** guidance record, version, read, acknowledge, attention in
   brief/work/resume, shared instructions, backup/restore.
2. The composed turn prompt for workers (server action plus `worker.py resume`).
3. Coordinators and reviewers (role variants of the same composition).
4. The short bootstrap template and migration notes for existing worker folders,
   replacing the long `templates/WORKER_PROMPT.md`.
5. The HTTP mirror (`/v1/agents/me/next`) carries the same guidance block, so the
   office agents and SSH workers read one channel.

## Open questions for the owner

* Should acknowledgement be automatic on a successful read, or an explicit
  `guidance ack --version VERSION` as implemented? The explicit form records which
  version a lane named back, so a coordinator can see who is behind; it is not
  proof that the lane read or followed the text (see Trust boundary).
* Should an acknowledgement require a registered session? Decided for this slice:
  yes at the endpoint (a configured operator name is also accepted), because
  unregistered names are the cheap way to fill the table; the library itself still
  accepts any valid actor identity, since actors are self-declared throughout.
* Should a project be able to carry no guidance at all (current: yes, `present:
  false`), or should the operator be required to set an explicit "no guidance"
  placeholder?
* For the composed prompt, which exact parts of the current 124-line
  `templates/WORKER_PROMPT.md` must remain local (the safety/authorization text)
  versus server-composed?
