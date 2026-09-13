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
