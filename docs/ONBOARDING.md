# Server-owned worker onboarding

Start a worker in an empty directory. It needs SSH access to the configured service account, not access to another contributor's checkout. Give it the installation's bootstrap command (replace paths, host, project and actor):

```sh
ssh beads-team python3 /home/beads/orchestra/worker.py --root /home/beads/beads-runtime --project example start --name cline
```

`worker.py start` allocates a durable actor and prints it with onboarding instructions. Run it for a new worker from that worker's dedicated local working directory; use one directory and checkout per actor, never another worker's directory or another project's checkout. Save the full actor privately in that directory alongside the exact client configuration supplied by the server-owned project entry, outside Git or in local ignored state. Use the installed client and printed project-specific endpoint/configuration; do not copy settings from another project. The readable name need not be unique. See [session registration and retries](SESSIONS.md). `onboard` and `docs [NAME]` remain read-only for existing actors. Bootstrap uses the installed endpoint locally and cannot claim tasks or write task records. Normal contributions use `client.py` and their configured project endpoint, including any project-specific guard. SSH access retains the existing trusted-team security model.

With a configured client, the equivalent is:

```sh
python client.py --config client.local.json --project example --actor alex/session1 -- onboard
python client.py --config client.local.json --project example --actor alex/session1 -- docs
python client.py --config client.local.json --project example --actor alex/session1 -- docs briefings
```

The response includes installed shared worker instructions, the private project entry point and a fixed document catalog. Documents are read-only through this interface. Arbitrary paths and symlinks are rejected. Missing, empty or oversized documents fail explicitly rather than returning partial instructions. Shared documents are limited to 64 KB; the shared start and project entry point each to 8 KB.

## Operator setup

Copy [the project template](../templates/PROJECT_ONBOARDING.md) to a private file and fill it in. Include the repository URL, how to establish an independent checkout, client installation/configuration, ownership and coordination authority. Do not require another contributor's local paths. Keep code/build instructions in the repository and reference them relative to the new checkout. Avoid copying task state into this file; workers query Beads.

```sh
python3 admin.py --root /home/beads/beads-runtime set-onboarding example --file /private/example-onboarding.md
python3 admin.py --root /home/beads/beads-runtime backup example
```

This atomically installs `projects/example/ONBOARDING.md` under the coordination lock. It is owner-maintained, outside generated views, and preserved as text in the existing coordination backup sidecar. Restore using this version or newer; older kits reject the new sidecar entry. Back up after updates. Project creation does not invent instructions: `onboard` fails until configured.

Host operator commands - accepted requirement revisions, backfills, operator voids and operator handoffs - are not part of worker onboarding. See the [start guide](../start_here/README.md) and [installation and recovery](OPERATIONS.md) before running one or handing it to the owner or host operator.

The project entry point should explicitly supersede obsolete checkout-local coordination paths where necessary, while retaining repository build rules and current claims. Repository README/AGENTS.md can point to `onboard`; keep a minimal connection command there or in the initial worker prompt. The client/server connection information is the irreducible bootstrap input.

## Private config and the platform credential store

`setup_assistant.py` writes the private `client.local.json`-shaped config that `client.py` reads (`transport`, `host`, `endpoint`, `root`, `python`) and, only when asked, stores a secret in the operating system's credential store instead of in a file:

```sh
python3 setup_assistant.py --help
python3 setup_assistant.py --project example --config /private/example/client.local.json \
    --host example-host --endpoint /srv/orchestra/endpoint.py --root /srv/orchestra/runtime \
    --actor SAVED_ACTOR --non-interactive
```

The `ssh` and `local` transports need no secret, so **none is stored by default**. The only secret the tool stores is an HTTP bearer worker credential for the office/API endpoint; opt in with `--store-credential` (or pipe the value with `--secret-stdin`, which implies it). **No module reads that store automatically yet**: retrieve the value with the credential-store backend (`default_store(service=...).retrieve(key)`) and pass it to `http_client --credential`, or use your own consumer. The config's `credential` block records the store, service, key and purpose and never the value. Set an explicit `--credential-key`, or let the default separate actors/checkouts: `<project>`, or `<project>:<12-hex digest of actor|checkout>` when an actor or checkout is known.

Backends are Windows Credential Manager (`pywin32`), the macOS keychain (`security -i`, so the secret stays out of argv), and the Linux Secret Service (`secret-tool`, from `libsecret-tools`). There is no plaintext fallback: an unavailable store is a clean error (exit 5) with an install/enable hint, reported before anything is written. Secrets are read only from a hidden prompt or stdin, never from argv, and every message is redacted. The Windows path has been exercised for real; the Linux `secret-tool` and macOS `security` paths are unit-tested against fakes that mirror the real tools but have not been run on those hosts here.

Safe re-runs and dry runs:

- An existing config or stored credential is replaced only with `--force-config` / `--force-credential` (or an interactive `y`). `--force-config` first copies the previous file to an owner-only `.bak` sibling, so keys this tool does not manage are not lost; a further replacement keeps the older backups as `.bak.1`, `.bak.2`, ... rather than overwriting them.
- If a replaced credential was overwritten and the config write then fails, the previous value is restored; a brand-new credential is deleted. A failed rollback is reported, never swallowed.
- `--dry-run` validates and prints the config without writing, storing a secret, or requiring a reachable credential store.
- Setup warns on stderr (non-fatal) when `--config` — or the `.bak` it writes — points inside a git checkout and is not git-ignored, so a private config is not published by a stray `git add`.
- The config file is written atomically (temporary file plus rename) and owner-only where the platform supports it.

## Minimal prompt

```text
Create a new, private working directory for this worker and project, then run the
following start command once with a readable name. Save the full returned actor
and the printed client configuration privately in that directory; do not commit or
share them. Use no other actor's directory or another project's checkout:

REPLACE_WITH_SSH_BOOTSTRAP_COMMAND

For first use, leave Actor and Client/config blank until start returns them; do not
assume the checkout already exists. After successful start, save its actor,
request ID and exact client configuration privately, then clone the repository
into this worker's own checkout. If onboarding fails after registration, retain
the actor and retry onboarding; do not run start again. Returning workers fill
Actor, Client/config and Checkout from private saved state and skip registration.

If the coordinator explicitly supplied a task, use that task. Otherwise inspect
task ownership and dependencies, then select one unheld task from `ready --json`.
A task is held by another actor if it is assigned to that actor or has an
in-progress claim belonging to that actor; unheld means neither applies. Claim it
atomically and register/verify your plan before implementation. Do not claim
another task until this one is delivered. The Delivery section is supplied by the
separate onboarding-template dependency (.36); if it is absent or unclear in the
server-owned entry, ask the coordinator rather than guessing.

Run ready/work through the installed client, not as bare shell commands. Example
after replacing the client, config, project and actor with the printed values:

```sh
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- ready --json
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- work --mine
```

Only if the harness and project explicitly permit a loop, after delivery check
every REPLACE_INTERVAL_MINUTES minutes for review feedback on your own tasks first,
then check `ready --json` for one next unheld task. Continue when a task is
available; do not stop just because the previous task was delivered. Stop at
REPLACE_DEADLINE or, after checking both sources, when you have no actionable open
or pending-review task and `ready` has no unheld task. Use the full
client-prefixed command examples above; do not run bare `work --mine` or
`ready --json` as shell commands. Do not wait on work held by other actors. In
office/person-started mode, do not poll or loop; end each turn with a one-line
status for the person. Do not depend on another worker's checkout or start another
coordination database.
```

For workers already connected, use the client `onboard` command in that prompt. The longer [worker prompt](../templates/WORKER_PROMPT.md) remains a reference, but no longer needs to be pasted into every new session.
