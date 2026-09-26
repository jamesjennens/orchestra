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

The project entry point should explicitly supersede obsolete checkout-local coordination paths where necessary, while retaining repository build rules and current claims. Repository README/AGENTS.md can point to `onboard`; keep a minimal connection command there or in the initial worker prompt. The client/server connection information is the irreducible bootstrap input.

## Minimal prompt

```text
Create a new, private working directory for this worker and project, then run the
following start command once with a readable name. Save the full returned actor
and the printed client configuration privately in that directory; do not commit or
share them. Use no other actor's directory or another project's checkout:

REPLACE_WITH_SSH_BOOTSTRAP_COMMAND

Use the installed client/config printed in the returned project entry. Inspect
task ownership and dependencies, then choose one unheld task from `ready --json`.
Claim it atomically and register/verify your plan before implementation. Do not
claim another task until this one is delivered. Follow the project's Delivery
section; if absent or unclear, ask the coordinator rather than guessing.

Only if the harness and project explicitly permit a loop, check every
REPLACE_INTERVAL_MINUTES minutes for review feedback on your own tasks with
`work --mine` first, then look for the next task with `ready --json`. Stop at
REPLACE_DEADLINE or when you have no actionable open or pending-review task and
`ready --json` has no unheld task. Do not wait on work held by other actors. In
office/person-started mode, do not poll or loop; end each turn with a one-line
status for the person. Do not depend on another worker's checkout or start another
coordination database.
```

For workers already connected, use the client `onboard` command in that prompt. The longer [worker prompt](../templates/WORKER_PROMPT.md) remains a reference, but no longer needs to be pasted into every new session.
