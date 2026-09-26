# Registered worker sessions

New workers should let the server allocate their actor ID. Readable names do not need to be unique. Registration records name, actor, creation time and request ID in the project's durable registry under its coordination lock. The allocator checks generated UUID actors against the registry and exported native actor/author/assignee fields before saving.

## Start from an empty directory

```sh
ssh beads-team python3 /home/beads/orchestra/worker.py --root /home/beads/beads-runtime --project example start --name cline
```

Replace the installation paths/host/project. Run the command for a new worker from
that worker's dedicated local working directory; keep one directory/checkout per
actor and never use another worker's directory or another project's checkout. The
command prints a registration record, an actor such as `session-UUID`, and the
normal onboarding instructions. Save the exact actor privately in that directory
alongside the client configuration supplied in the server-owned project entry,
outside Git or in local ignored state. Use that installed client and exact
project-specific configuration; do not copy settings from another project. Do not
invent or truncate the actor. Another worker using the same readable name gets a
different actor.

`start` registers a session; `onboard` only reads instructions for an existing actor. Resuming the same worker uses its saved actor, for example `worker.py ... --actor SAVED_ACTOR onboard`. Do not register a replacement identity merely because the session was interrupted; task and merge ownership still belong to the original actor. A genuinely new worker registers separately and follows explicit handoff rules.

If onboarding fails after registration, retain the printed actor and retry
`onboard` with that actor after correcting the reported issue. Do not call `start`
again. For a new task, select one unheld item from `ready --json`, claim it
atomically, and finish/deliver it before claiming another. Follow the project's
server-provided Delivery section; ask the coordinator if it is absent or ambiguous.

Only an explicitly loop-capable and authorized harness may check every N minutes
for review feedback on the actor's own tasks with `work --mine` first, then the
next task with `ready --json`. Stop at the configured deadline or when there is
no actionable open or pending-review task owned by this actor and no unheld task
in `ready`; do not wait on other actors' held tasks. Office/person-started turns
do not poll or loop; end each turn with a one-line status for the person.

## Register with an installed client

```sh
python client.py --config client.local.json --project example -- session register --name cline
python client.py --config client.local.json --project example --actor RETURNED_ACTOR -- onboard
python client.py --config client.local.json --project example --actor RETURNED_ACTOR -- session show RETURNED_ACTOR
```

Registration is the only client action that does not require an actor. Other commands retain the existing explicit-actor rule. The output is JSON containing `session.actor`, `session.name`, `session.created_at` and `session.request_id`.

Before sending a registration, the client prints its generated request ID to stderr. After an uncertain response, retry with the **same name and request ID**:

```sh
python client.py --config client.local.json --project example -- session register --name cline --request-id SAVED_REQUEST_UUID
```

The bootstrap also accepts `start --name cline --request-id SAVED_REQUEST_UUID`. A matching retry returns the same actor, including after restore; a changed name with the same request ID is refused. If onboarding fails after registration, the printed actor remains registered: fix the document/access issue and run `onboard` with that actor. If the request ID itself is lost before any work was claimed, another registration creates an unused extra record, not a claim; do not guess an actor from its readable name.

## Limits and recovery

Explicit recurring-run resume is now available: `worker.py ... --actor SAVED_ACTOR resume` records a durable timestamped resume event, returns onboarding and shows owned work with revision requests first. `client.py ... --actor SAVED_ACTOR -- session resume` records the event alone. Both accept a retry `--request-id`; the client generates/prints one if omitted. Ownership and actor stay unchanged. See [resume, authorized handoff and reviews](REVIEWS.md) for replacement workers and requested corrections. Legacy actors continue through onboard/work under their original actor; registered resume does not silently register or rename them.

This is attribution and accidental-collision prevention, not authentication, an access role, a lease, or a detector of two processes deliberately sharing one ID. Existing explicitly named actors continue working; they do not acquire registry records automatically. A trusted participant can still reuse another actor string. Never use registration to take over existing claims.

Registration is project-scoped. UUIDs make accidental cross-project collisions extremely unlikely, but there is no global identity service. `.sessions.json` is operator-owned runtime state, included in the existing coordination backup sidecar; do not edit or delete it to free names. Restore with this kit version or newer. As with other coordination data, never operate a restored copy as a second live authority. Shared backups do not include a worker's local saved actor/request ID; keep those in the private session handoff.
