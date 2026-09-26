# Resume, contribution revisions and actionable reviews

These commands use `b` for your configured client prefix, including project and actor. Read `docs sessions` and `docs worker-guide` too. These records add workflow state; they do not set any of the six lifecycle facts.

## Returning worker: resume before choosing unrelated work

Keep the actor returned by your original registration. Reuse it on a later run of the same worker:

```sh
ssh HOST python3 /PATH/kit/worker.py --root /PATH/runtime --project PROJECT --actor SAVED_ACTOR resume
```

This records a durable resume event without allocating another actor or changing task ownership, then prints onboarding and your work queue with revision requests first. The emitted request ID can be reused after an uncertain response. With a local client, `b session resume` records the event and `b work --mine` retrieves the queue.

Do not call `start` again merely because the harness started a fresh process. Retain the actor in private worker configuration or handoff notes. Registered resume requires a registered actor. Existing legacy actors can still use `onboard` and `work --mine` under their original actor; do not invent a registration to replace them implicitly. Matching names and elapsed time never authorize takeover.

## Replacement worker: explicit handoff

For a new actor taking over an existing task, the current owner supplies this JSON (use an exact saved target actor, not a display name):

```json
{
  "schema_version": 1,
  "operation_id": "handoff-001",
  "task": "example-task",
  "from_actor": "OLD_ACTOR",
  "to_actor": "NEW_ACTOR",
  "reason": "Continue the recorded contribution in response to review.",
  "approval": "Pointer to the agreed handoff or owner authorization."
}
```

The **old owner** runs `b handoff example-task --file handoff.json`. The new worker cannot invoke this operation under its own actor to acquire someone else's work.

If the old worker has ended and cannot perform the handoff, an operator acting on explicit coordinator/project-owner authorization runs:

```sh
python3 admin.py --root /PATH/runtime handoff PROJECT --actor COORDINATOR_ACTOR --file handoff.json
```

This operator-only path records the initiator, approval evidence, old/new actor and reason. It is not a new permission system: service-account shell access remains trusted, and approval evidence must reflect actual authorization. The contributor endpoint does not accept an operator override.

Handoff checks the expected current owner, journals the operation, records native intent/completion comments and changes only the assignee. It does not reopen/close tasks, change review/lifecycle facts or transfer a merge slot. If interrupted, inspect and retry the exact payload under the same initiating actor/authority. A completed retry never reassigns a task that has subsequently moved again. Pending journals are included in backups and must be reconciled after restore. Reopen a closed task explicitly under existing authority before requesting revisions.

When a handoff request exists, its task, original owner and destination are stored in the request journal. A completed direct transfer settles only the matching pending request. If the native owner update succeeded but its response was lost, an authorized retry reconciles the durable receipt before applying the current-owner check; conflicting request or receipt identities fail closed. Queue reads validate the full request journal before state or owner filtering so malformed records remain visible, including empty filtered pages.

## Structured contribution delivery

Read `b review example-task`. It returns `latest_comment_id`, the current contribution and unresolved review requests. Copy `latest_comment_id` into `previous` for every new operation; use null only for the first operation. Each new assertion has a new operation ID; uncertain retries retain the exact original payload/actor.

The assigned owner submits `b review example-task --file contribution.json`:

```json
{
  "schema_version": 1,
  "operation": "contribute",
  "operation_id": "contribution-001",
  "task": "example-task",
  "previous": null,
  "supersedes": null,
  "repository": "https://example.org/team/repository.git",
  "commit": "1111111111111111111111111111111111111111",
  "base_commit": "2222222222222222222222222222222222222222",
  "delivery": {
    "kind": "bundle",
    "path": "HOST:/absolute/review/contribution-v1.bundle",
    "sha256": "3333333333333333333333333333333333333333333333333333333333333333"
  },
  "summary": "Delivered change and verification pointers."
}
```

For remote branches, use `delivery: {"kind":"remote","remote":"git@example.org:team/repository.git","branch":"worker/task"}`. Replace synthetic hashes with exact full commit/base IDs and the actual bundle SHA-256. Remote access and bundle contents are not verified automatically: the reviewer must retrieve, verify and test them. The protocol rejects incomplete delivery fields, not false assertions.

For a revised contribution, set `previous` to the latest workflow comment and `supersedes` to the current contribution's `comment_id`. Record its exact new branch/bundle path, commit, base and checksum, even when only the bundle filename changed. Old delivery records remain in history. A new contribution does not resolve feedback automatically.

## Request changes and respond

A reviewer records requests against the **current contribution comment ID**:

```json
{
  "schema_version": 1,
  "operation": "request-changes",
  "operation_id": "review-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "items": [{"id":"fix-1","text":"Describe the required correction and acceptance."}]
}
```

Submit with the same `review TASK --file` command. This removes a legacy `review-ready` label and projects `changes-requested`, independently of any older checkpoint. The task must be open. Pending items remain visible across subsequent checkpoints and contribution revisions.

After publishing a corrected contribution, its assigned owner explicitly responds:

```json
{
  "schema_version": 1,
  "operation": "respond",
  "operation_id": "response-001",
  "task": "example-task",
  "previous": "LATEST_WORKFLOW_COMMENT_ID",
  "contribution": "CURRENT_CONTRIBUTION_COMMENT_ID",
  "resolutions": [{"request":"REVIEW_REQUEST_COMMENT_ID","item":"fix-1","reason":"What changed or why the request is superseded.","evidence":"Exact commit/test/decision evidence."}]
}
```

Responses are owner assertions, not reviewer acceptance. When all requests have explicit responses, the contribution returns to `awaiting-review`. A reviewer may accept using an `approve` operation with the same common fields plus `contribution` and a `summary`. Approval requires no unresolved requests and refers to the current contribution. It projects `awaiting-integration`; it does not set reviewed/integrated/deployed lifecycle facts. A new contribution requires review again. Review participation remains subject to project policy; actor names are attribution, not verified authority.

## Target the contribution record, not the latest comment

`contribution` in a `request-changes`, `respond` or `approve` operation names the **contribution record's `comment_id`** — the `contribution.comment_id` returned by `review TASK`, or `contribution_id` on an item returned by `work --mine`. The Git SHA is a separate value: `contribution.commit` in `review TASK`, or `commit` in a work item. The contribution record ID is **not** `latest_comment_id`, which is the newest record in the chain and advances with every reviewer request, response and approval. The two coincide only while the contribution is also the most recent record.

Supplying the wrong id is refused before any native write, and the refusal names the operation, the id you supplied and the id you should have used:

```text
Review operation approve supplied 2, which is the task latest comment id;
reference the current contribution instead (the current contribution id is 1).
Review operation approve supplied not-a-real-id, but the current contribution id is 1.
Review operation approve supplied unknown, but no current contribution has been recorded yet.
```

Read the current ids from `review TASK`. A refusal changes nothing: the review state, the pending items and the comment chain stay exactly as they were, so correct the payload and resubmit with a new operation ID. An **exact** repeat of an already-recorded operation — same operation ID, same canonical payload, same actor — still reconciles to the original comment without writing a second record, including after a later handoff or later reviews.

## Stale `previous`: the refusal returns the current head

Every new operation compares `previous` with the chain's current `latest_comment_id`. A mismatch is refused before any native write, and the refusal carries the current values so the retry does not need a blind extra fetch:

```text
Stale review workflow previous; reread brief. Current latest_comment_id is record-B and review_state is changes-requested; re-read the chain content before deciding.
{"code":"stale-previous","http_status":409,"latest_comment_id":"record-B","review_state":"changes-requested","supplied_previous":"record-A","task":"example-task"}
```

The first line is the human refusal; the second is the same structured error as one canonical JSON line. The CLI prints both on stderr, writes nothing and exits with status 2. `latest_comment_id` is `null` when no review record exists yet, which is the actionable value for the first write. An HTTP transport returns the same JSON object as the `409` detail.

The returned values are an identifier and a state label, not the chain content. Re-read `review TASK` before deciding, then resubmit with a new operation ID and `previous` set to the returned `latest_comment_id`. The field saves one round trip; it never replaces reading.

## Discover current work

```sh
b work --mine
b work --state changes-requested
b work --state awaiting-review
b work --state awaiting-integration
b brief example-task
b review example-task
```

The queue sorts requested changes first and shows task status, owner, review state, exact contribution commit and separate lifecycle values/scope. `--owner ACTOR`, `--limit` and `--offset` support coordinator scans. Pages are fresh views, not immutable history snapshots. Closed tasks with outstanding review state remain visible; closure is not acceptance. An approved contribution with passed scoped integration evidence can display integrated. Check the scope-match field before applying historical lifecycle evidence to the current contribution.

`brief` prioritizes pending review actions over an older checkpoint's next action. It shows up to five pending items with a pointer to `review TASK` for the rest. Generated CURRENT.md includes a bounded review queue; native `list` remains unchanged. Legacy `review-ready` labels remain discoverable, but structured review state takes precedence. If a task has no checkpoint, its description and acceptance are shown without calling it legacy work.

The structured protocol is an append-only native comment chain with previous-comment checks. Do not hand-edit those comments. Forked/malformed records fail clearly and need operator reconciliation. Report/label changes are separate writes; a retry repairs interrupted removal of review-ready, while structured state stays authoritative.

For example, if review returns contribution.comment_id = record-A, contribution.commit = <Git SHA>, and latest_comment_id = record-B, submit contribution = record-A and previous = record-B. Do not put the Git SHA in either comment-ID field.

Handoff acceptance binds the complete disposition (including kind and supersedes) before transfer. The `.handoff-recoveries` journal is included in coordination backups and validated on restore. Interrupted completion keeps the original disposition ID on the accepted request. Older incomplete recovery identities fail closed and require explicit operator reconciliation; they are not silently promoted to exact retry evidence.
