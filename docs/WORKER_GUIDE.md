# Independent workers: preflight, environment and delivery

Use your existing client prefix for the Beads commands below. This guide is available as `docs worker-guide`. Keep project-specific dependency commands in the repository or server project entry point.

## Preflight before claiming implementation

Read the prospective task first, then verify:

1. Coordination: `onboard` and `brief TASK --json` succeed against the intended project with your actor.
2. Repository: obtain your own clone using authorized credentials; confirm the remote URL. Run `git fetch origin` before selecting a starting revision. Do not reset or discard existing work. Follow the task's required base/branch; record the exact `git rev-parse HEAD` after selecting it. Git supplies source; Orchestra supplies current ownership, decisions and handoff context.
3. Workspace: confirm your own checkout, temporary and environment directories are writable from the actual agent execution context.
4. Interpreter: locate an approved interpreter matching the repository's version requirement. Create an environment in your own workspace; never refer to another checkout's `.venv`.
5. Dependencies: install from the repository's documented lockfile or package configuration. Verify the imports and lightweight checks required for the task using that environment's interpreter in the same execution boundary where implementation/tests will run.
6. Overlap: recheck current claims, task dependencies and file/interface impacts immediately before claiming. A successful preflight does not reserve the task; confirm `update TASK --claim --json`, then register and acknowledge the plan.

Environment provisioning may occur before the implementation claim in your isolated directory. It does not authorize application edits, deployment or taking another claim. If provisioning itself needs substantial shared changes, coordinate a separate setup task first.

Record interpreter path/version, source base, dependency source/lock revision and exact commands/results. Without a lockfile, a fresh package install is not proof of reproducibility; record resolved versions and raise any missing reproducibility requirement rather than inventing a lock or changing project policy.

If a check fails, report the command, working directory, interpreter, execution context, exit status and relevant error, excluding secrets. Distinguish permission/path/sandbox denial, missing package, incompatible runtime and application failure. Host imports succeeding while sandbox imports fail indicates an execution-boundary difference to investigate; it is not evidence that the packages are broken. Do not repeatedly reinstall packages or switch to another worker's environment to conceal the problem. Use an approved accessible interpreter/environment or request the required access change. Never bypass the execution boundary.

## Deliver work that another worker can retrieve

Before a handoff, preserve the actual implementation as either:

- A contribution branch on an authorized remote, with repository URL, branch, exact commit and PR if applicable. Push only when branch-push permission is already established. Verify the remote branch tip, for example with `git ls-remote REMOTE refs/heads/BRANCH`, and record it. Remote visibility under your credentials alone does not prove the reviewer has access; identify the intended recipient's access or obtain acknowledgement.
- An explicit Git bundle transfer when pushing is unavailable or unauthorized. Create a self-contained bundle of the contribution branch, for example `git bundle create contribution.bundle BRANCH`, verify it with `git bundle verify contribution.bundle`, then place it at an agreed recipient-accessible location. Record its checksum, contained branch, exact commit and transfer location. A bundle sitting only in the same isolated workspace is not a transfer. The recipient verifies/fetches it before relying on the handoff.

Push permission for a contribution branch is separate from permission to merge, update a shared branch or deploy. If project instructions do not establish push authority, ask for that narrow decision or agree a bundle transfer. Do not infer broad Git permissions from a claimed task.

A local commit alone is insufficient. Uncommitted files and local-only evidence must be listed as outstanding work, not as a transferable completed implementation. Do not put credentials or private runtime data into a branch/bundle.

## Make review-ready work discoverable

For new structured contributions, prefer [resume and contribution reviews](REVIEWS.md), served as `docs reviews`: exact delivery revisions, persistent requests/responses and `work --mine`/`work --state awaiting-review`. Structured state overrides legacy labels and old checkpoints. Recurring workers resume the saved actor; replacement workers need an explicit authorized handoff. The label convention below remains for existing unstructured tasks.

`work --mine` and `brief TASK` also report `newer_activity_by_others` / `newer_activity_own` counts and a bounded `newer` summary when comments or edits arrived after your task's checkpoint. Treat any non-zero other-actor count as a direction to read `history TASK` before continuing; a checkpoint's recorded `next_action` is prefixed `STALE CHECKPOINT:` in that state. Reading clears nothing — reconcile explicitly with a new checkpoint.

Use the `review-ready` label as a workflow convention, not a lifecycle fact. Once the scoped implementation and required checks are complete, deliverable access is established, and the report/checkpoint names the next reviewer action:

```sh
b comments add TASK --file handoff.md --json
b update TASK --add-label review-ready --json
```

Here `b` means your configured client prefix. Publish a fresh checkpoint **after** the report/label change so it incorporates both. Include delivery pointers, actual checks, remaining concerns and the next reviewer/coordinator action. Keep the normal coding task open while awaiting review/integration; retain ownership unless an explicit handoff is agreed. A coordinator finds the queue with:

```sh
b list --label review-ready --limit 0 --json
```

This label means ready for review, not reviewed, accepted, integrated or deployed. Record the six lifecycle facts independently. If changes are requested, evidence fails or the deliverable becomes inaccessible, append the reason, remove the label with `update TASK --remove-label review-ready --json`, and checkpoint the next action. Remove it after integration too. A separately scoped implementation task may close only under the project's agreement with a linked remaining review/integration task; label that open task so it stays discoverable.

Report/label/checkpoint writes are separate operations. After interruption, inspect and reconcile partial updates; never infer a complete handoff merely from the label.
