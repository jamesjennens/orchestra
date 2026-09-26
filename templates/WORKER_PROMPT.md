# Worker prompt

Fill every marked value from the server-owned project entry. Do not invent project
IDs, paths, actors or permissions.

```text
Work only in your own checkout and use the supplied project and saved actor:
Project: REPLACE_PROJECT
Repository: REPLACE_REPOSITORY_URL
Checkout: REPLACE_OWN_CHECKOUT_PATH
Client/config: REPLACE_INSTALLED_CLIENT_AND_PRIVATE_CONFIG
Actor: REPLACE_REGISTERED_ACTOR
Task: REPLACE_TASK_ID

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

For registration and resume command syntax, follow server-served `docs start`.
Registration prints a request ID before sending; after an uncertain response, retry
with that same ID rather than registering again. Ignore any `.venv` found in the
repository checkout and use only an environment created in your own workspace.
Fetch source, select the task-approved base, record its full commit, verify this
checkout's interpreter/dependencies, and compare declared file/interface impacts
with current work. If ownership or an approval is unclear, stop and report it.
Atomically claim only the unassigned task you were given. Register and verify a
self-contained plan before editing; follow any additional launch gate.

Implement only the approved scope in this checkout. Run its documented tests and
record exact results, commit/base, limitations and remaining work. Deliver an
accessible authorized branch or verified bundle through the structured review
workflow. Keep delivery, review, integration, deployment and live verification
separate. Before interruption or handoff, publish a fresh structured checkpoint and
follow the server-served `docs start` resume sequence. Do not merge, deploy, edit
another worker's checkout or create tasks to stay busy. Leave the task open awaiting
review.

Use server docs workflow, worker-guide, sessions, reviews, briefings and operations.
If no revision or authorized existing claim is actionable, stop and report; do not
invent work. Route feedback through the current project feedback stream or an
explicitly agreed interim parent/job target on older services; keep private feedback
out of public files.
```

See [`start_here`](../start_here/README.md) for isolated environment setup and the
short delivery sequence. The detailed contracts remain in the linked server and
repository manuals.
