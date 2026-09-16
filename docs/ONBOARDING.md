# Server-owned worker onboarding

Start a worker in an empty directory. It needs SSH access to the configured service account, not access to another contributor's checkout. Give it the installation's bootstrap command (replace paths, host, project and actor):

```sh
ssh beads-team python3 /home/beads/orchestra/worker.py --root /home/beads/beads-runtime --project example --actor alex/session1 onboard
```

`worker.py` only exposes `onboard` and `docs [NAME]`, prints readable text and propagates failure. It uses the installed endpoint locally. It cannot claim work or write records. Normal contributions use `client.py` and their configured project endpoint, including any project-specific guard. SSH access and actor names retain the existing trusted-team security model.

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
Work from your own directory. Run the following command with a unique
session actor and follow the returned project onboarding instructions:

REPLACE_WITH_SSH_BOOTSTRAP_COMMAND

Inspect the assigned task REPLACE_TASK_ID (or find appropriate unclaimed
work). Obtain your own repository checkout as instructed. Respect existing
claims and register your plan before implementation. Do not depend on
another worker's local checkout or start another coordination database.
```

For workers already connected, use the client `onboard` command in that prompt. The longer [worker prompt](../templates/WORKER_PROMPT.md) remains a reference, but no longer needs to be pasted into every new session.
