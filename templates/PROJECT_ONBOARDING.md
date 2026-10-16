# PROJECT_NAME

Repository URL: FILL_IN. Clone into your own directory; do not use another worker's checkout. Read AGENTS.md and README in that clone for code/build/test instructions. Follow task-specific branch/base requirements before editing.

**Git access to the code host comes first.** Before claiming any task, the worker must be able to fetch from and push to the project's repository: an SSH key or a token added to the person's OWN account on the code host (GitHub, Bitbucket, GitLab, or the office's own host), with write access granted by the project owner. Check it first: `ssh -T git@github.com` (GitHub), `ssh -T git@bitbucket.org` (Bitbucket), `ssh -T git@gitlab.com` (GitLab), or `ssh -T git@<office-host>` for an office host - the answer names the account; and `git ls-remote <repository URL>` - it lists the refs, so a checkout without access fails here rather than in the middle of the work. A token is used in place of a key where the host or the office prefers one (for example over HTTPS with the host's credential helper). This is access to the code host, not to the Orchestra server: the Orchestra service is reached by the client configuration or the agent credential, and having one says nothing about the other. An agent that does not have git access records a checkpoint and claims nothing.

Owners and acceptance/integration/deployment authority: FILL_IN.
Coordination project: FILL_IN. Canonical service/endpoint: FILL_IN.

## Configure a client in your worker directory

Workers reach the service by one of two routes. Say which one this project uses (a project may use both) and fill in that route; the other can be deleted.

**Route A - SSH: a contributor with a checkout on the host.** FILL_IN exact commands to obtain the installed client (for example scp HOST:/absolute/kit/client.py ./orchestra-client.py), select a Python 3.10+ interpreter, and create a private client.local.json with host, endpoint and root. client.py is the whole client for `wait` and the endpoint commands: `wait` needs no other kit file beside it, and only the `capability` commands also need `capabilities.py` from the same kit. State the `python` value when the server needs one: over SSH that key names the interpreter that runs the endpoint ON THE SERVER (for an office installation the bundled one, `<INSTALL_ROOT>/current/python-runtime/<PATH>`, as `add-project` prints it), not the contributor's own; and give a host that resolves from the contributor's machine, not the server's own name for itself. This route needs the contributor's SSH key to reach the service account. If the server confines this contributor's key to the endpoint with the forced-command setup, the supplied configuration also sets `"forced_command": true`; state that the contributor must keep it exactly as supplied, since the confined key refuses the `--root` an unconfined client sends. An explicit local-transport configuration may be supplied for server terminals. No paths may depend on another worker's checkout. For operational helper scripts, explain how to obtain the matching kit version.

**Route B - web agent: the office route (My agents and the bridge extension), no shell.** FILL_IN the service address a worker's browser reaches, the project id, and which document the agent reads first (`GET <service address>/v1/docs/finding-work`). The owner or coordinator adds the agent on the project's **My agents** page and copies the set-up prompt into the agent's chat; that prompt writes `.orchestra/agent.json` and `.orchestra/AGENT.md` into the agent's own directory and keeps the agent's bearer secret in a curl config file in the owner's user profile, outside the directory and outside Git. The agent makes every call as `curl -K <credential file> <service address>/v1/...`. There is no client.py, no `python` setting and no SSH key on this route: a web agent needs no SSH key.

FILL_IN the complete client command prefix for Route A, with an explicit unique actor. It must support onboard, docs, brief, history, claims and file attachments. File attachments are read from the machine running client.py. For Route B, FILL_IN the service address and the credential file's location in the owner's profile instead; there is no client command prefix there.

## Delivery (required)

Fill in every FILL_IN in this section before installing the document. Workers follow it when they deliver a contribution; if it is absent or unclear, they ask the coordinator instead of guessing.

May workers push contribution branches to the project repository? FILL_IN (yes or no). Branch naming: FILL_IN (for example contrib/TASK-ID, one branch per task, created from the exact base commit named in the contribution). Never push the shared default/integration branch FILL_IN, never force-push any branch, and never merge, integrate or deploy from a contribution branch.

If pushing is not permitted, deliver a fallback bundle instead: create it in your own checkout with `git bundle create <name>.bundle <branch>`, copy it to FILL_IN (the operator-accessible host and directory workers may use; never another worker's directory or another project's inbox), and record it in the contribution payload as {"kind":"bundle","path":"FILL_IN:<name>.bundle","sha256":"<64 hex>"} using the SHA256 of the exact bytes copied. Name the exact 40-character commit and base commit in every contribution.

Leave the contribution awaiting review. Only the reviewer accepts or merges it; a contributor never closes, approves or integrates their own contribution.

## Project coordination rules

FILL_IN project-specific constraints, conflicting historical instructions to supersede, and required review/merge-slot rules. Preserve existing claims and permissions. Reference stable jobs/decisions if helpful; retrieve mutable task state from Beads.

If a repository, branch, data dependency or credential is unavailable, report the exact missing prerequisite. Do not fall back to another contributor's working directory or create another coordination writer.
