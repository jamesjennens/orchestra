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
  allowlist authorizes nobody and a contributor cannot set guidance even if it
  reaches a host shell.
* The record is **audited**: every set records who (`set_by`), when (`set_at`),
  the new version, the previous version and the previous text. The history is
  bounded (50 entries); the acknowledgement table is bounded (500 actors).
* The **version is the SHA-256 of the guidance text**, computed by every reader
  from the text it actually read. A crash between the text write and the metadata
  write therefore cannot advertise a version that does not describe the text.
* Every **work**, **brief** and session **resume** response carries a compact
  guidance block: `present`, `version`, `set_at`, `set_by`, `previous_version`,
  `acknowledged`, and `attention` (true exactly when guidance is set and the
  calling actor has not acknowledged the current version). `guidance get`
  returns the full text and what changed since a named version; `guidance ack`
  records the calling actor's own read; `guidance status` (operator only) lists
  which lanes have acknowledged which version.

## Trust boundary

Guidance is an **instruction channel to agents**, so the trust rules are the
strict ones:

* Only the operator host route writes it. The endpoint can read it and record an
  actor's own acknowledgement and nothing else.
* Every read marks who set the current text and when.
* It never overrides the user's authorization, the machine's limits for that
  agent, or the worker safety rules. The worker instructions say so explicitly.
* Contributor-written text (task titles, proposal text, capability summaries,
  reference entries) can never reach the guidance file: no client action writes
  it, and the shared instructions keep marking such text as data.

## Rollback and backup behaviour

* `GUIDANCE.md` and `.guidance.json` are included in the project coordination
  backup and restored with the same generation as the onboarding entry point;
  `validate_coordination_files` validates both, so a malformed audit record
  refuses the backup or restore rather than silently dropping acks.
* An older kit that does not know the channel ignores two extra files in the
  project directory: it never enumerates them, and a restore by that kit leaves
  them in place. An older-kit project has no guidance files at all.
* Tolerant readers: a missing `GUIDANCE.md` reads as `present: false`; a missing,
  older or malformed `.guidance.json` still leaves the text and its computed
  version readable, and unknown metadata keys are ignored. A worker whose server
  is unreachable keeps its last acknowledged version and must not invent rules.
* The operator can roll a project back by re-running `set-guidance` with the
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

Risks the design must keep handling: whoever can set the server prompt directs
every agent, so writes stay operator-only, audited and versioned (and the
confined SSH setup, kittrial-5bb.89, matters more); contributor-written text is
marked as data inside the composed prompt; an unreachable server leaves the
worker on its last acknowledged version; size bounds apply per part and overall.

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
  `guidance ack` as implemented? The explicit form is what lets a coordinator
  distinguish "fetched" from "picked up".
* Should a project be able to carry no guidance at all (current: yes, `present:
  false`), or should the operator be required to set an explicit "no guidance"
  placeholder?
* For the composed prompt, which exact parts of the current 124-line
  `templates/WORKER_PROMPT.md` must remain local (the safety/authorization text)
  versus server-composed?
