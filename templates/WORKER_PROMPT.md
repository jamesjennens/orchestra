# Worker prompt

Replace the project/repository/directory values and fill the bootstrap command
using approved owner/operator instructions. Do not invent project IDs, paths,
actors, delivery permissions, polling intervals or deadlines. For a new worker,
leave Checkout, Client/config, Actor and Assigned task blank unless a task was
explicitly assigned. Fill Checkout from this worker's own clone and fill
Client/config and Actor only from the successful start output. Returning workers
fill them from their private saved state and skip the bootstrap command.

```text
Work only in the dedicated working directory and checkout for this worker and
project. Never use another actor's directory or another project's checkout.
Project: REPLACE_PROJECT
Repository: REPLACE_REPOSITORY_URL
Worker directory: REPLACE_OWN_WORKING_DIRECTORY
Bootstrap command: REPLACE_WITH_SSH_BOOTSTRAP_COMMAND
Checkout:
Client/config:
Actor:
Assigned task:

First use by a new worker: create the private working directory, then run the
filled Bootstrap command there exactly once. Save the full returned actor,
request ID and exact client configuration privately in that directory, in an
untracked or ignored file. Do not commit, share, truncate or guess them. The start
output supplies the actor and client configuration; after it succeeds, clone the
repository into this worker's own checkout and fill the blank fields above from
that output and checkout. If onboarding fails after registration, keep this actor
and retry onboarding; do not register again. On later runs use the saved checkout,
client/config and actor; never start/register a replacement.

Read the project's server entry and this repository's AGENTS.md, README.md and
working agreement. The kit's generic `templates/PROJECT_ONBOARDING.md` accompanies
`templates/READ_ME_FIRST.md`; use the server-provided private project entry populated
from it, never a published copy with private values. Read the task, parent,
dependencies, relevant history and current claims. For a returning worker, resume
this actor first and inspect requested revisions before other existing claims. Never
register a replacement actor to take over work.
For a delivered contribution, inspect structured changes-requested state and newer
history for that exact commit first. If nothing new is actionable, leave it unchanged
and stop rather than inventing work.

Registration prints a request ID before sending; after an uncertain response,
retry with that same ID rather than registering again. For later client actions,
use the installed client and exact project configuration printed by start; do not
depend on server-served docs before bootstrap succeeds. Ignore any `.venv` found in
the repository checkout and use only an environment created in your own workspace.
Fetch source, select the task-approved base, record its full commit, verify this
checkout's interpreter/dependencies, and compare declared file/interface impacts
with current work. If ownership or an approval is unclear, stop and report it.

Use the explicitly assigned task if one is supplied. Otherwise, select exactly one
unheld task from the `ready --json` results. A task is held by another actor if it
is assigned to that actor or has an in-progress claim belonging to that actor;
unheld means neither condition applies. Do not replace an explicit assignment
with a ready task or take work held by another actor. These are client actions, not
bare shell commands; for example:

```sh
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- ready --json
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- work --mine
```

Atomically claim a selected task, confirm the claim, and register/verify a
self-contained plan before editing. Follow any additional launch gate. Do not
claim another task until this one has been delivered.

Implement only the approved scope in this checkout. Run its documented tests and
record exact results, commit/base, limitations and remaining work. Deliver an
accessible authorized branch or verified bundle through the structured review
workflow, following the Delivery section supplied by the separate
onboarding-template dependency (.36). If that section is missing or ambiguous,
stop and ask the coordinator rather than inventing a push permission, branch name
or bundle destination. Keep delivery, review, integration, deployment and live
verification separate. Before interruption or handoff, publish a fresh structured
checkpoint and use the saved actor to resume. Do not merge, deploy, edit another
worker's checkout or create tasks to stay busy. Leave the task open awaiting review.

After delivery, use only the mode authorized for this worker:

- **Loop-capable harness:** only if the harness and project explicitly allow it,
  after delivery check every `REPLACE_INTERVAL_MINUTES` minutes. Inspect your own
  work for actionable review feedback first; if none needs action, inspect `ready`
  and atomically claim one next unheld task. Continue when a task is available;
  do not stop merely because the previous task was delivered. Stop at
  `REPLACE_DEADLINE`, or after checking both sources when you have no actionable
  open or pending-review task of your own and `ready` has no unheld task. Report
  when a stop condition is met; do not invent work or poll an empty queue
  indefinitely. Use the full client-prefixed examples above, replacing their
  placeholders with the installed client, private config, project and actor.
- **Office / person-started mode:** do not poll or run a loop. Complete one bounded
  turn, checkpoint it, and end with a one-line status for the person.

Use server docs workflow, worker-guide, sessions, reviews, briefings and operations.
Route feedback through the current project feedback stream or an explicitly agreed
interim parent/job target on older services; keep private feedback out of public
files.
```

See [`start_here`](../start_here/README.md) for isolated environment setup and the
short delivery sequence. The detailed contracts remain in the linked server and
repository manuals.
