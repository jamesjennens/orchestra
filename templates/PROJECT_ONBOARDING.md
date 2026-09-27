# PROJECT_NAME

Repository URL: FILL_IN. Clone into your own directory; do not use another worker's checkout. Read AGENTS.md and README in that clone for code/build/test instructions. Follow task-specific branch/base requirements before editing.

Owners and acceptance/integration/deployment authority: FILL_IN.
Coordination project: FILL_IN. Canonical service/endpoint: FILL_IN.

## Configure a client in your worker directory

FILL_IN exact commands to obtain the installed client (for example scp HOST:/absolute/kit/client.py ./orchestra-client.py), select a Python 3.10+ interpreter, and create a private client.local.json with host, endpoint and root. An explicit local-transport configuration may be supplied for server terminals. No paths may depend on another worker's checkout. For operational helper scripts, explain how to obtain the matching kit version.

FILL_IN the complete client command prefix, with an explicit unique actor. It must support onboard, docs, brief, history, claims and file attachments. File attachments are read from the machine running client.py.

## Delivery (required)

Fill in every FILL_IN in this section before installing the document. Workers follow it when they deliver a contribution; if it is absent or unclear, they ask the coordinator instead of guessing.

May workers push contribution branches to the project repository? FILL_IN (yes or no). Branch naming: FILL_IN (for example contrib/TASK-ID, one branch per task, created from the exact base commit named in the contribution). Never push the shared default/integration branch FILL_IN, never force-push any branch, and never merge, integrate or deploy from a contribution branch.

If pushing is not permitted, deliver a fallback bundle instead: create it in your own checkout with `git bundle create <name>.bundle <branch>`, copy it to FILL_IN (the operator-accessible host and directory workers may use; never another worker's directory or another project's inbox), and record it in the contribution payload as {"kind":"bundle","path":"FILL_IN:<name>.bundle","sha256":"<64 hex>"} using the SHA256 of the exact bytes copied. Name the exact 40-character commit and base commit in every contribution.

Leave the contribution awaiting review. Only the reviewer accepts or merges it; a contributor never closes, approves or integrates their own contribution.

## Project coordination rules

FILL_IN project-specific constraints, conflicting historical instructions to supersede, and required review/merge-slot rules. Preserve existing claims and permissions. Reference stable jobs/decisions if helpful; retrieve mutable task state from Beads.

If a repository, branch, data dependency or credential is unavailable, report the exact missing prerequisite. Do not fall back to another contributor's working directory or create another coordination writer.
