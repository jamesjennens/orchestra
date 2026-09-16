# PROJECT_NAME

Repository URL: FILL_IN. Clone into your own directory; do not use another worker's checkout. Read AGENTS.md and README in that clone for code/build/test instructions. Follow task-specific branch/base requirements before editing.

Owners and acceptance/integration/deployment authority: FILL_IN.
Coordination project: FILL_IN. Canonical service/endpoint: FILL_IN.

## Configure a client in your worker directory

FILL_IN exact commands to obtain the installed client (for example scp HOST:/absolute/kit/client.py ./orchestra-client.py), select a Python 3.10+ interpreter, and create a private client.local.json with host, endpoint and root. An explicit local-transport configuration may be supplied for server terminals. No paths may depend on another worker's checkout. For operational helper scripts, explain how to obtain the matching kit version.

FILL_IN the complete client command prefix, with an explicit unique actor. It must support onboard, docs, brief, history, claims and file attachments. File attachments are read from the machine running client.py.

## Project coordination rules

FILL_IN project-specific constraints, conflicting historical instructions to supersede, and required review/merge-slot rules. Preserve existing claims and permissions. Reference stable jobs/decisions if helpful; retrieve mutable task state from Beads.

If a repository, branch, data dependency or credential is unavailable, report the exact missing prerequisite. Do not fall back to another contributor's working directory or create another coordination writer.
