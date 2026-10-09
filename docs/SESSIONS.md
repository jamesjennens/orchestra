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
again. For a new task, use an explicitly assigned task if one was supplied; otherwise
select one unheld item from `ready --json`. A task is held by another actor if it
is assigned to that actor or has an in-progress claim belonging to that actor;
unheld means neither applies. Claim one task atomically and finish/deliver it
before claiming another. The Delivery section is supplied by the separate
onboarding-template dependency (.36); ask the coordinator if it is absent or
ambiguous in the server-provided entry.

Run these through the installed client, not as bare shell commands (replace the
example client/config/project/actor with the server-provided values):

```sh
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- ready --json
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- work --mine
```

Only an explicitly loop-capable and authorized harness may, after delivery, check
every N minutes for review feedback on the actor's own tasks first, then check
`ready --json` for one next unheld task. Continue when one is available; do not
stop just because the previous task was delivered. Stop at the configured deadline
or, after checking both sources, when there is no actionable open or pending-review
task owned by this actor and no unheld task in `ready`. Use the full
client-prefixed examples above, and do not wait on other actors' held tasks or poll
an empty queue indefinitely. Office/person-started turns do not poll or loop; end
each turn with a one-line status for the person.

## Register with an installed client

```sh
python client.py --config client.local.json --project example -- session register --name cline
python client.py --config client.local.json --project example --actor RETURNED_ACTOR -- onboard
python client.py --config client.local.json --project example --actor RETURNED_ACTOR -- session show RETURNED_ACTOR
```

Registration is the only client action that does not require an actor. Other commands retain the existing explicit-actor rule. The output is JSON containing `session.actor`, `session.name`, `session.created_at` and `session.request_id`.

**Bound keys (`--principal lane:NAME`)**: For a key bound to a principal, `session register` is the **only** permitted first action. Because the principal owns no actors in the project before its first registration, any pre-registration command (including `docs`, `onboard`, or `ready`) is refused: client commands require `--actor`, and the endpoint's principal gate refuses any unowned actor. Workers in a newly bound lane must read instructions from their local checkout of the repository rather than querying served docs before registering. Operators can verify key installation non-destructively using `admin.py authorized-keys-list` on the server or plain SSH on the client (see [checking a bound key](OPERATIONS.md#checking-a-bound-key-without-spending-the-lanes-registration)).

Before sending a registration, the client prints its generated request ID to stderr. After an uncertain response, retry with the **same name and request ID**:

```sh
python client.py --config client.local.json --project example -- session register --name cline --request-id SAVED_REQUEST_UUID
```

The bootstrap also accepts `start --name cline --request-id SAVED_REQUEST_UUID`. A matching retry returns the same actor, including after restore; a changed name with the same request ID is refused. If onboarding fails after registration, the printed actor remains registered: fix the document/access issue and run `onboard` with that actor. If the request ID itself is lost before any work was claimed, another registration creates an unused extra record, not a claim; do not guess an actor from its readable name.

## Per-actor private config and credential keys

Each actor/checkout keeps its own private config (see [private config and the platform credential store](ONBOARDING.md#private-config-and-the-platform-credential-store)). `setup_assistant.py` writes it and, only when explicitly asked (`--store-credential`), stores an HTTP bearer worker credential in the platform credential store rather than in the file. The `ssh` and `local` transports need no secret, so no credential is stored by default. No module reads that store automatically: retrieve the value with the credential-store backend and pass it to `http_client --credential`.

Because two actors or two checkouts of the same project must not share one stored secret, the default credential key is `<project>` when no discriminator is known, otherwise `<project>:<12-hex digest of actor|checkout>`. Pass `--actor` (and/or `--checkout`) so the key is namespaced, or set `--credential-key` explicitly. The digest is stable for a given actor/checkout pair and keeps the key short; it is bookkeeping, not an identity or an access control.

## Limits and recovery

Explicit recurring-run resume is now available: `worker.py ... --actor SAVED_ACTOR resume` records a durable timestamped resume event, returns onboarding and shows owned work with revision requests first. `client.py ... --actor SAVED_ACTOR -- session resume` records the event alone. Both accept a retry `--request-id`; the client generates/prints one if omitted. Ownership and actor stay unchanged. See [resume, authorized handoff and reviews](REVIEWS.md) for replacement workers and requested corrections. Legacy actors continue through onboard/work under their original actor; registered resume does not silently register or rename them.

This is attribution and accidental-collision prevention, not authentication, an access role, a lease, or a detector of two processes deliberately sharing one ID. Existing explicitly named actors continue working; they do not acquire registry records automatically. A trusted participant can still reuse another actor string. Never use registration to take over existing claims.

Registration is project-scoped. UUIDs make accidental cross-project collisions extremely unlikely, but there is no global identity service. `.sessions.json` is operator-owned runtime state, included in the existing coordination backup sidecar; do not edit or delete it to free names. Restore with this kit version or newer. As with other coordination data, never operate a restored copy as a second live authority. Shared backups do not include a worker's local saved actor/request ID; keep those in the private session handoff.

The registry also carries an `owners` map (actor to principal) where a key is bound to a
principal (kittrial-5bb.194): `session register` under such a key records the new actor under
the key's principal, `session show` returns it as `principal`, and `admin.py adopt-actor`
gives an existing actor to a principal once (see
[bind a key to its principal](OPERATIONS.md#bind-a-key-to-its-principal)). The map is
written only when non-empty, so an installation that configures nothing writes the registry
exactly as before. Once it is present, a kit that predates it refuses the registry as an
unknown key: remove `owners` before a downgrade, or restore with this kit or newer.
