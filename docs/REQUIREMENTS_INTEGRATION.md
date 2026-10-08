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
  The operator's `requirement-apply` route may instead write an accepted
  revision 1 with F3 acceptance evidence, including on an existing untyped
  task with no revision comments. Contributor `draft` remains draft-only and
  requires a typed task when selecting one.
- `revise` selects an existing `task`, must write exactly the next revision, and
  is bound to the record's existing kind and key: a `requirement` record cannot
  be revised as `brd-section`, and a keyed record cannot change key.
- `kind` is `requirement` (needs `key`) or `brd-section` (narrative, no key).
  Requirement keys are unique across the project.
  If native issue metadata makes a row unreadable, native label-filtered ID
  membership identifies requirement rows. A separate comments read validates the
  immutable revision ledger to establish their bound keys. A potentially
  conflicting unreadable requirement refuses the write; a proven different key
  and an unreadable ordinary task do not. Titles never establish record kind or key.
- Labels are controlled: they are derived from `kind` and `acceptance_state`. A
  caller-supplied `labels` field is refused, and unrelated labels are untouched.
  The contributor guard also refuses raw `update --add-label requirement...` and
  raw `comments add` of any `requirement-revision-v1` body, so this operation is
  the only writer. It matches every label-writing spelling bd 1.2.2 accepts,
  including the undocumented `create --label` alias of `--labels`, and it fails
  closed on any unrecognized `create`/`update` flag rather than assuming the
  flag cannot move the reserved namespace. The same reserved-label guard refuses
  a raw `create --parent X` whose parent holds a controlled requirement label (a
  child cannot inherit `requirement:accepted` without F3 evidence) and any raw
  `update X --set-labels/--remove-label` on a record that holds one, so a
  replacement cannot silently drop the operator's acceptance. Pass
  `--no-inherit-labels` when a raw child of a requirement record is genuinely
  wanted.
- The raw-comment guard also refuses a BOM-prefixed or CRLF body that is a
  reserved machine-record prefix once the BOM is dropped and CRLF folded to LF.
  The strict parsers ignore such a lookalike, so without this it would be filed
  as ordinary prose; refusing it keeps the reserved namespace unambiguous. A
  legitimate writer emits exact canonical UTF-8 with `\n`, so nothing valid is
  refused.
- The contributor path must not carry an `acceptance` object at all: a draft is
  unaccepted by definition, so a contributor acceptance is refused before any
  native read or write.
- Revise follows the **trusted-team** model: any contributor actor may revise any
  draft requirement record, and the actor string is an attribution, not an
  authenticated owner identity. `revise` is bound to the record's kind and key,
  not to the actor who first drafted it, so two contributors may co-edit one
  draft through successive revisions; the revision comment history records who
  wrote what. This attribution model does not confine native edits: a
  contributor can still change an accepted record's native title, description or
  status, or reparent it, through ordinary bd operations. The
  `requirement-revision-v1` and `requirement-acceptance-v1` comments remain
  authoritative for the requirement's content, revision and acceptance, so those
  native edits do not change what the kit exports or accepts.
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
and either transition carries an `acceptance` object. The content hash is not
caller-supplied: the command binds it to the exact revision it writes, and the
stored field is `record_sha256` (the accepted revision's content hash). It is
deliberately not called `manifest_sha256`, because no publication manifest exists
when a single record is accepted; `publish_brd` keeps that name for the
manifest-level object. `requirement_records.publication_acceptance` can rebind a
record decision to a manifest, but it has **no production caller today**:
`publish_brd` does not yet use it, and the bridge exists only for a future
publisher integration. Until then a published manifest's acceptance continues to
be supplied through `publish_brd`'s existing `--acceptance` path.

```json
{"schema_version": 1, "operation_id": "session-1/req-1-r2", "operation": "revise",
 "kind": "requirement",
 "task": "sample-job.7", "title": "R01: Intent", "key": "R01",
 "description": "## Requirement\n...", "revision": 2, "acceptance_state": "accepted",
 "acceptance": {"owners": ["owner-a"], "approvers": ["owner-a"], "policy": "any-owner",
                "decision_id": "decision-2026-09-25", "evidence": "review 01a0d..."}}
```

For a first revision accepted before it entered Orchestra, the operator uses
`operation: "draft"` (the operation names revision creation, not the acceptance
state). Supply `parent` to create a task, or replace it with `task` to select
an existing task with no revision comments:

```json
{"schema_version": 1, "operation_id": "operator/accepted-r1", "operation": "draft",
 "kind": "requirement", "parent": "sample-job", "title": "R01: Intent",
 "key": "R01", "description": "## Requirement\n...", "revision": 1,
 "acceptance_state": "accepted",
 "acceptance": {"owners": ["owner-a"], "approvers": ["owner-a"],
                "policy": "any-owner", "decision_id": "decision-example",
                "evidence": "approved baseline example"}}
```

Pass this payload to `admin.py requirement-apply` as below. A later accepted
revision 2 uses `operation: "revise"`, a new operation ID and a new decision.
For a new task, the command creates draft labels, then writes acceptance
evidence, the accepted revision comment, and finally the accepted label. A
retry with the same operation ID and identical payload adds nothing.

```sh
python admin.py --root /path/to/runtime requirement-apply example \
  --actor operator --file record.json
```

The file passed to `admin.py` must carry `operation` itself; this is the one place
it differs from the `requirement_records.py draft`/`revise` payloads above, where
the subcommand supplies it. Accepting or demoting an existing record writes the
record's next revision, so the first example uses `"operation": "revise"`, and that
`revise` example is accepted unchanged by the contributor form
`requirement_records.validate_payload(payload)` (run it first; the validator reads
the file and writes nothing). The accepted-first-revision example is an operator
payload: check it with the operator form
`requirement_records.validate_payload(payload, operator=True)`, because the
contributor form (default `operator=False`) refuses `operation: "draft"` with
`acceptance_state: "accepted"`, while `operator=True` additionally runs the F3
acceptance check.

Acceptance without evidence, a contributor acceptance attempt, a contributor
acceptance object on a draft, and contributor demotion are all refused before any
native write.

`requirement-apply` also checks the deployment operator allowlist, exactly as
`void-record` does (`kittrial-5bb.65`): `--actor` must be in the `operators` list
of `deployment.private.json`, an empty or absent list authorizes nobody, and an
unlisted actor is refused before any journal directory, receipt or native
write. The allowlist is read strictly, so a shell-only `ORCHESTRA_OPERATORS`
value that disagrees with the deployment configuration is refused rather than
honoured by the CLI and ignored by the endpoint. Enrol every actor that accepts
requirements (starting with `james`) with `admin.py operators add` and confirm
with `admin.py operators list` before deploying this check; an empty list is a
deploy blocker.

The operator acceptance is **durable on the record**: beside the
`requirement-revision-v1` comment the command writes one
`Kind: requirement-acceptance-v1` record bound to that revision. The evidence
comment is written **before** the accepted revision comment and the
`requirement:accepted` label, so an uncertain evidence write leaves the record
reading as draft (never accepted without evidence) and is healed by retry or
`admin.py requirement-reconcile`:

```text
Kind: requirement-acceptance-v1
{"acceptance_state":"accepted","at":"...","decision":{"approvers":["owner-a"],
 "decision_id":"decision-2026-09-25","evidence":"review 01a0d...",
 "owners":["owner-a"],"policy":"any-owner"},"evidence":null,"id":"sample-job.7",
 "operator":"operator","record_sha256":"<64 hex>","revision":2,"schema_version":1,
 "sha256":"<64 hex>","source":"requirement-apply"}
```

Every reader of the native record can therefore see the acceptance decision
without the local journal, and the coordination backup/restore sidecar now
carries `.requirement-requests/` and `.requirement-backfills/` too. A retry with
the same decision is idempotent; a different decision for the same revision is
refused rather than silently rewriting the operator's evidence.

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
python admin.py --root /path/to/runtime requirement-backfill example \
  --actor operator --file backfill.json
```

A record backfilled to `requirement:accepted` needs an `evidence` field
(a nonempty decision/evidence pointer); a draft entry must not carry one. The
command is idempotent: an identical repeated run reports `changed: false`.
`requirement-backfill` checks the deployment operator allowlist like
`requirement-apply` and `void-record`: an unlisted `--actor` is refused before
any write. Unknown records, duplicate entries and caller-supplied labels are
refused before any native write. Accepting by backfill also writes the durable
`Kind: requirement-acceptance-v1` evidence record (with `source`
`requirement-backfill`), bound to the record's latest revision when it has one,
so the evidence is visible on the record and not only in `.requirement-backfills/`.
Backfill to `accepted` is refused when the record's latest revision comment says
`draft`: the revision ledger stays authoritative, so accepting such a record goes
through `admin.py requirement-apply` with F3 evidence, which writes a new
accepted revision.

### Reconcile an uncertain requirement operation (operator)

```sh
python admin.py --root /path/to/runtime requirement-reconcile example \
  --operation-id session-1/req-1 --actor operator \
  --disposition complete --issue-id sample-job.7 --reason "confirmed native record"
```

`complete` requires the exact native record id and confirms it before the receipt
is completed. `failed`/`released` is allowed only when the operator confirms no
native record was created, so the operation ID can be resubmitted. The
`.requirement-requests/` and `.requirement-backfills/` journals are part of the
native backup/restore sidecar (`admin.py backup`, `admin.py restore-new`), so a
restored project keeps its pending operation IDs and F3 acceptance evidence.

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
