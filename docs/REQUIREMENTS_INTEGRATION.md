# Native exports, impact and worker launches

These tools extend the [offline snapshot contract](REQUIREMENTS_CONTRACT.md). They use the existing Beads comment/export interface; no database schema change is required. The kit's real baseline remains draft.

## Capture an explicit revision in Beads

An issue's current prose and open/closed status do not define its requirement revision or acceptance. Store each reviewed structured record in an additive comment whose text is exactly:

```text
Kind: requirement-revision-v1
<canonical JSON record>
```

The record is the existing schema-1 requirement or narrative object, including canonical issue ID, positive revision, acceptance state, full description and content hash. Use `export_requirements.revision_comment(record)` to generate the body. Post with the ordinary explicit-actor client. Preserve older comments. Reposting identical content is tolerated; conflicting content for the same ID/revision is rejected. Meaningful changes, including acceptance-state changes that affect the record hash, require a new revision.

Prepare a selection using `selection_from_manifest(manifest, context_ids, blocking_ids)`. It contains the manifest metadata plus ordered exact `{id, revision, sha256}` references for narrative and requirements. `context_ids` names decisions and discussions to retain alongside the selected issues; `blocking_ids` is the explicit unresolved-blocker list. Accepted selection rejects any listed blocker. The adapter does not guess whether a closed issue resolved an ambiguity, or whether a comment expresses approval.

After `client.py ... refresh`, save `client.py ... view issues.jsonl` to a local UTF-8 file. Then:

```sh
python export_requirements.py --export issues.jsonl --selection selection.json --publish dist/requirements > snapshot.json
```

This reads one saved export, validates the selected revisions, publishes the BRD and emits a snapshot envelope containing `manifest`, `provenance` and the local publication path. Provenance retains the full selected/context issue rows, original comments, exact selected comment IDs and an export-content hash. Keep that envelope with the publication evidence. It may contain private project discussion, so keep it in project-local storage.

Use `--previous previous-manifest.json` to check revision monotonicity and detect exported rewrites of known historical revisions, including revisions not selected for the new publication. Accepted inputs also need `--acceptance` and, for an accepted previous snapshot, `--previous-acceptance`. Prior snapshots remain the evidence when an export no longer contains an old revision.

The kit trial appended 13 draft revision comments after checking their current issue bodies exactly matched the retained snapshot. A single subsequent native export reconstructed the original manifest hash without changing any issue body or requirement wording.

## Analyze a change

`requirement_impact.py` consumes validated old/new manifests and a work graph. It produces a view; it does not mutate Beads tasks or infer any of the six implementation lifecycle facts.

A graph has `schema_version: 1` and `nodes`. Each node has:

- `id`, `kind` (`design`, `task` or `evidence`);
- `requirements`: exact revision references;
- `depends_on`: work-node IDs;
- `evidence`: durable evidence pointers;
- `rationale` when it has neither requirement references nor dependencies.

```sh
python requirement_impact.py --previous old.json --current new.json --graph work.json > impact.json
```

Changed or removed revisions flag direct and transitive work as `needs-reassessment`. Cycles terminate safely; missing links fail visibly. Added requirements without linked work appear in `unplanned_requirements`. Draft inputs produce a `proposed` impact view, not an accepted change.

An accepted transition requires both the new baseline's owner acceptance object and `--change` JSON with exactly `classification`, `state`, `decision_id`, `evidence`, `old_manifest_sha256`, `new_manifest_sha256`. Classification is `defect`, `ambiguity` or `scope-change`; accepted changes require decision/evidence pointers bound to the two hashes. A defect fix cannot alter requirement revisions. This checks recorded declarations, not authenticated owner identity.

To reaffirm a node, add `reaffirmation` containing `baseline_sha256`, exact replacement `requirements`, `rationale` and an `evidence` pointer. It must cover the node's direct and inherited requirement scope. Each affected node needs its own reassessment; clearing a design does not silently clear downstream tasks. Reaffirming a task cannot hide an unresolved dependency.

Keep original graph references and evidence. For later transitions, `--history history.json` accepts a list of `{manifest, acceptance}` objects (acceptance is null for drafts), allowing older evidence and reaffirmations to resolve. An unchanged scope stays unaffected across unrelated changes. Retain old graph/impact files and record a report pointer in Beads; this tool does not overwrite history or automatically approve changes.

## Draft or revise a requirement record in one step

Contributors cannot use `label add` (outside the contributor interface) and
`create-child` cannot set labels, so a requirement record can lack the
`requirement` / `requirement:draft` type and state labels that
[REQUIREMENTS_CONTRACT](REQUIREMENTS_CONTRACT.md) F2 and the web requirements
view depend on. `requirement_records.py` supplies one locked, idempotent
operation that drafts or revises a record, applies only those controlled labels
and posts the `requirement-revision-v1` comment.

The payload is a closed schema (`schema_version` 1). The `operation` field is
supplied by the subcommand, not the file:

```json
{"schema_version": 1, "operation_id": "session-1/req-1", "kind": "requirement",
 "parent": "sample-job", "title": "R01: Intent", "key": "R01",
 "description": "## Requirement\n...", "acceptance_state": "draft"}
```

```sh
python requirement_records.py draft  --config client.json --project example --actor alex --file record.json
python requirement_records.py revise --config client.json --project example --actor alex --file record.json
```

- The contributor operation may only write **draft** revisions:
  `acceptance_state` must be `draft`, and an accepted record can be neither
  selected nor demoted. Acceptance and demotion are owner/operator-only.
- `draft` creates the record at revision 1 labeled `requirement:draft`, or
  selects an existing `task` id that already carries the `requirement` or
  `brd-section` type label; it needs `parent` only when creating. Untyped
  records, ordinary tasks and epics are refused, so the command cannot relabel
  an arbitrary record.
- `revise` selects an existing `task`, must write exactly the next revision, and
  is bound to the record's existing kind and key: a `requirement` record cannot
  be revised as `brd-section`, and a keyed record cannot change key.
- `kind` is `requirement` (needs `key`) or `brd-section` (narrative, no key).
  Requirement keys are unique across the project.
- Labels are controlled: they are derived from `kind` and `acceptance_state`. A
  caller-supplied `labels` field is refused, and unrelated labels are untouched.
  The contributor guard also refuses raw `update --add-label requirement...` and
  raw `comments add` of any `requirement-revision-v1` body, so this operation is
  the only writer.
- Retrying the same `operation_id` with identical content reconciles without a
  second record, comment or label write; reusing it with different content is
  refused. A native `bd create --dry-run` preflight and the revision check run
  before any receipt is written, so a refusal reserves nothing. A receipt stays
  pending only for an uncertain real-write failure and is completed with
  `admin.py requirement-reconcile`.
- `decided_by` from the open kittrial-pth.25 change proposal is not accepted
  yet, so passing it is refused rather than written as an unvalidated field.

### Accept a record or demote an accepted record (owner/operator)

F3 requires named owners and recorded evidence for acceptance. Only the operator
route may set `requirement:accepted`, or move an accepted record back to draft,
and either transition carries an `acceptance` object. `manifest_sha256` is not
caller-supplied: the command binds it to the exact revision it writes.

```json
{"schema_version": 1, "operation_id": "session-1/req-1-r2", "kind": "requirement",
 "task": "sample-job.7", "title": "R01: Intent", "key": "R01",
 "description": "## Requirement\n...", "revision": 2, "acceptance_state": "accepted",
 "acceptance": {"owners": ["owner-a"], "approvers": ["owner-a"], "policy": "any-owner",
                "decision_id": "decision-2026-09-25", "evidence": "review 01a0d..."}}
```

```sh
python admin.py --root /path/to/runtime requirement-apply \
  --project example --actor operator --file record.json
```

Acceptance without evidence, a contributor acceptance attempt, and contributor
demotion are all refused before any native write.

### Backfill labels on existing records (operator)

Records created before this operation (for example through `create-child`) can
be labelled without rewriting their content. The operator runs this against the
project on the server; it writes only the controlled labels and never a revision
comment:

```json
{"schema_version": 1, "operation_id": "backfill-2026-09-25",
 "records": [{"task": "sample-job.7", "kind": "requirement", "acceptance_state": "draft"}]}
```

```sh
python admin.py --root /path/to/runtime requirement-backfill \
  --project example --actor operator --file backfill.json
```

A record backfilled to `requirement:accepted` needs an `evidence` field
(a nonempty decision/evidence pointer); a draft entry must not carry one. The
command is idempotent: an identical repeated run reports `changed: false`.
Unknown records, duplicate entries and caller-supplied labels are refused before
any native write.

### Reconcile an uncertain requirement operation (operator)

```sh
python admin.py --root /path/to/runtime requirement-reconcile \
  --project example --operation-id session-1/req-1 --actor operator \
  --disposition complete --issue-id sample-job.7 --reason "confirmed native record"
```

`complete` requires the exact native record id and confirms it before the receipt
is completed. `failed`/`released` is allowed only when the operator confirms no
native record was created, so the operation ID can be resubmitted.

## Require an acknowledged plan before launching

`worker_gate.py` supplies `register`, `check` and `run`. Every command requires explicit `--config`, `--project`, `--task`, `--actor`, `--launch-id`, `--plan`, `--cwd` and `--receipt`. For example:

```sh
python worker_gate.py register --config client.local.json --project example --task example-task --actor alex/session1 --launch-id trial-1 --plan plan.md --cwd /path/to/checkout --receipt receipt.json
python worker_gate.py check --config client.local.json --project example --task example-task --actor alex/session1 --launch-id trial-1 --plan plan.md --cwd /path/to/checkout --receipt receipt.json
python worker_gate.py run --config client.local.json --project example --task example-task --actor alex/session1 --launch-id trial-1 --plan plan.md --cwd /path/to/checkout --receipt receipt.json -- python worker.py
```

Register claims or verifies the task and stores the complete plan text, its byte hash and launch context in a canonical comment. It reads that comment back before writing a local receipt. A failed/uncertain write is reconciled by read-back without a blind retry. Reusing an ID with conflicting content is refused.

Run rechecks exact canonical comment content, author and ID, task ID/assignment/status, plan bytes, working directory and connection configuration before invoking the supplied argv with `shell=False`. It never claims automatically. Changed or missing evidence refuses execution. The operator chooses the worker command and is responsible for keeping it within the plan. An explicitly invoked shell remains a shell; `shell=False` is not command authorization.

The gate enforces this prerequisite for launches through this command. It is not OS isolation and cannot stop a user or worker launching another way. It does not provide exactly-once execution or a transaction with reassignment: do not blindly rerun after an uncertain worker outcome. It checks the claim immediately before launch but does not hold a server lease for the worker's lifetime.

Hermes/Cline can be the explicit worker argv. Keep provider configuration in the existing local harness. The planning phase itself stays separate; use register/run only after a concrete plan has been produced and its scope is ready for implementation.
