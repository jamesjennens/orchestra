# Server-owned worker onboarding

Start a worker in an empty directory. It needs SSH access to the configured service account, not access to another contributor's checkout. Give it the installation's bootstrap command (replace paths, host, project and actor):

```sh
ssh beads-team python3 /home/beads/orchestra/worker.py --root /home/beads/beads-runtime --project example start --name cline
```

`worker.py start` allocates a durable actor and prints it with onboarding instructions. Run it for a new worker from that worker's dedicated local working directory; use one directory and checkout per actor, never another worker's directory or another project's checkout. Save the full actor privately in that directory alongside the exact client configuration supplied by the server-owned project entry, outside Git or in local ignored state. Use the installed client and printed project-specific endpoint/configuration; do not copy settings from another project. If the server-owned entry says the contributor key is confined to the endpoint, that configuration also carries `"forced_command": true`; keep it exactly as supplied, because the confined key refuses the `--root` an unconfined client sends (see [installation and recovery](OPERATIONS.md#confine-contributor-keys-with-a-forced-command)). The readable name need not be unique. See [session registration and retries](SESSIONS.md). `onboard` and `docs [NAME]` remain read-only for existing actors. Bootstrap uses the installed endpoint locally and cannot claim tasks or write task records. Normal contributions use `client.py` and their configured project endpoint, including any project-specific guard. SSH access retains the existing trusted-team security model; a confined contributor key narrows its reach to the endpoint without binding the key to an actor, which still self-declares.

Git access to the code host comes first. Before claiming any task, the worker must be able to fetch from and push to the project's repository: an SSH key or a token added to the person's OWN account on the code host (GitHub, Bitbucket, GitLab, or the office's own host), with write access granted by the project owner. Check it first: `ssh -T git@github.com` (GitHub), `ssh -T git@bitbucket.org` (Bitbucket), `ssh -T git@gitlab.com` (GitLab), or `ssh -T git@<office-host>` for an office host - the answer names the account; and `git ls-remote <repository URL>` - it lists the refs, so a checkout without access fails here rather than in the middle of the work. A token is used in place of a key where the host or the office prefers one (for example over HTTPS with the host's credential helper). This is access to the code host, not to the Orchestra server: the Orchestra service is reached by the client configuration or the agent credential, and having one says nothing about the other. An agent that does not have git access records a checkpoint and claims nothing.

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

## Host-created projects and web registration

`admin.py add-project NAME` creates the canonical project on the coordination host but
does not make it appear on the site. A superuser must register the existing
name from **Projects → Register a project**. The registering person becomes the owner
and can add the intended owner, then step down. Host creation does not auto-register a
project. See [projects on the endpoint backend](HTTP_DEPLOYMENT.md#projects-on-the-endpoint-backend)
for the web flow.

Host operator commands - accepted requirement revisions, backfills, operator voids and operator handoffs - are not part of worker onboarding. See the [start guide](../start_here/README.md) and [installation and recovery](OPERATIONS.md) before running one or handing it to the owner or host operator.

The project entry point should explicitly supersede obsolete checkout-local coordination paths where necessary, while retaining repository build rules and current claims. Repository README/AGENTS.md can point to `onboard`; keep a minimal connection command there or in the initial worker prompt. The client/server connection information is the irreducible bootstrap input.

## Private config and the platform credential store

`setup_assistant.py` writes the private `client.local.json`-shaped config that `client.py` reads (`transport`, `host`, `endpoint`, `root`, `python`; `client.py` also reads the optional `forced_command` boolean, which this tool does not set) and, only when asked, stores a secret in the operating system's credential store instead of in a file:

For a contributor key confined to the endpoint, pass `--forced-command` when setting up its SSH client config. This writes `"forced_command": true`, so the client sends only the configured endpoint; keep the option paired with that key's server-side `authorized_keys` forced command. Omit it for ordinary SSH keys.

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
assume the checkout already exists. Git access to the code host comes first. Before
claiming any task, the worker must be able to fetch from and push to the project's
repository: an SSH key or a token added to the person's OWN account on the code host
(GitHub, Bitbucket, GitLab, or the office's own host), with write access granted by the
project owner. Check it first: ssh -T git@github.com (GitHub), ssh -T git@bitbucket.org
(Bitbucket), ssh -T git@gitlab.com (GitLab), or ssh -T git@<office-host> for an office
host - the answer names the account; and git ls-remote <repository URL> - it lists the
refs, so a checkout without access fails here rather than in the middle of the work. A
token is used in place of a key where the host or the office prefers one (for example
over HTTPS with the host's credential helper). This is access to the code host, not to
the Orchestra server: the Orchestra service is reached by the client configuration or
the agent credential, and having one says nothing about the other. An agent that does
not have git access records a checkpoint and claims nothing. After successful start,
save its actor, request ID and exact client configuration privately, then clone the
repository into this worker's own checkout. If onboarding fails after registration,
retain the actor and retry onboarding; do not run start again. Returning workers fill
Actor, Client/config and Checkout from private saved state and skip registration.

If the coordinator explicitly supplied a task, use that task. Otherwise inspect
task ownership and dependencies, then select one unheld task from `ready --json`.
A task is held by another actor if it is assigned to that actor or has an
in-progress claim belonging to that actor; unheld means neither applies. Claim it
atomically and register/verify your plan before implementation. Do not claim
another task until this one is delivered. The Delivery section is supplied by the
separate onboarding-template dependency (.36); if it is absent or unclear in the
server-owned entry, ask the coordinator rather than guessing.

A project may be set so that only its coordinators assign work (`claiming: assigned`).
Then `ready`, `work --claimable`, the agent's `next` list and `wait` offer no
unassigned task, and a claim of one is refused with one sentence naming the setting. A
task already assigned to you can still be started; an empty listing carries "Only the
project's coordinators assign work here; nothing is assigned to you yet." Do not
claim an unassigned task in that project: wait for the coordinator to assign it.

Run ready/work through the installed client, not as bare shell commands. Example
after replacing the client, config, project and actor with the printed values:

```sh
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- ready --json
python orchestra-client.py --config client.local.json --project PROJECT --actor ACTOR -- work --mine
```

Only if the harness and project explicitly permit a loop, after delivery check
every REPLACE_INTERVAL_MINUTES minutes for review requests on your own tasks first,
then check `ready --json` for one next unheld task. Continue when a task is
available; do not stop just because the previous task was delivered. Stop at
REPLACE_DEADLINE or, after checking both sources, when you have no actionable open
or pending-review task and `ready` has no unheld task. Use the full
client-prefixed command examples above; do not run bare `work --mine` or
`ready --json` as shell commands. Do not wait on work held by other actors. In
office/person-started mode, do not start a poll or a loop of your own; end each turn
with a one-line status for the person. Only the worker's own user authorizes checking
again, by asking, within what the project's standing guidance allows. A task awaiting
review or integration is not a reason to stop checking until your user's deadline, or the
project's limit; that is exactly when checking matters. A harness that cannot wait or
schedule tells its user so
in one line instead of refusing. Do not depend on another worker's checkout or start
another coordination database.
```

For workers already connected, use the client `onboard` command in that prompt. The longer [worker prompt](../templates/WORKER_PROMPT.md) remains a reference, but no longer needs to be pasted into every new session.

## Windows: VS Code terminals hang or show no output

On Windows, VS Code terminals that hang, pile up, or give an agent no command output back are usually caused by an old PSReadLine module: Windows PowerShell 5.1 ships PSReadLine 2.0.0, and upgrading it to 2.4.5 fixes the problem. Everything below runs in your own account, without administrator rights, and either you or your agent can run it.

1. Check the versions:

   ```powershell
   (Get-Module PSReadLine -ListAvailable).Version
   $PSVersionTable.PSVersion
   ```

2. If PSReadLine is below 2.4, allow per-user module installs and install it. Setting the execution policy for your own account is a normal per-user step, and it is what lets the install work:

   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   Install-Module PSReadLine -Scope CurrentUser -Force -SkipPublisherCheck
   ```

   If `Get-ExecutionPolicy -List` shows MachinePolicy or UserPolicy set, the command will not take effect; ask IT.

3. If `Install-Module` fails (an old PowerShellGet, or no access to the PowerShell Gallery), install it by hand. Download the `.nupkg` from `https://www.powershellgallery.com/packages/PSReadLine`, run `Unblock-File` on it, rename it to `.zip` and extract it. Put the extracted files in a folder named `PSReadLine\2.4.5` inside the first path of `$env:PSModulePath` (your per-user modules folder; it may be under OneDrive if your Documents folder is redirected there). The extra package files (`[Content_Types].xml`, `_rels`, `package`) can be deleted. Then restart VS Code.

4. Check again with the commands from step 1: PSReadLine should now report 2.4.5 (any 2.4 or later is fine). Open a new VS Code terminal to confirm that it responds.

## Web personal-agent setup wizard

For personal agents created on the website (`My agents` or `#/setup`, [design note](ONBOARDING_DESIGN.md) slice 4, `kittrial-5bb.329`; one action per screen since `kittrial-5bb.348`), setup runs as a wizard showing ONE action per screen: a short title, at most two short sentences, one button, choice or command with its Copy button, and `Back` / `Next`, with any explanation behind "More about this":

- **Your computer, first:** the operating system (auto-detected `Windows (PowerShell)`, `macOS`, `Linux`) and what the agent runs in (`kittrial-5bb.456`): VS Code with **Orchestra Bridge 0.14 or later**, VS Code with an older Bridge or none yet, or **No Bridge: Claude Code, Codex, a command-line agent** (the longer path below without the VS Code screen); then what the agent does (worker first).
- **With Orchestra Bridge 0.14 or later** the path is short: save your secret (the lines to paste, including the server address, so the extension's Set up finds the server itself), restrict the file on macOS/Linux, run `Orchestra Bridge: Set up` in the agent's folder (it checks the certificate, tests the connection, writes `.orchestra/agent.json` and `.orchestra/AGENT.md`, and compares `working_directory`, updatable via `PATCH /v1/agents/me`), then paste the resume prompt.
- **With an older extension, or curl only**, the longer path adds: save and move the certificate (when the service has its own), the rewrite command that writes the certificate's real path, the self-check, and **Save to agent folder…** with the agent's folder path printed beside it.
- **Without a secret** (opened from the agent's card) the secret screen says the page cannot show the secret again, that issuing adds a NEW credential (the others keep working until revoked) and how many work now, and has the **Issue a new secret** button right there; the new secret appears on that screen at once, with the count of working credentials and **Revoke the N older credentials** (a confirmation; only this agent's own credentials, through the existing per-credential revoke). Right after creation the screen also says the secret need not be saved at once. The secret is never stored or pasted into a chat; an agent that is sent one is told not to use or save it. Errors raised while the wizard is open are shown inside it, on the current screen.
- **Behind a command-line proxy:** every curl the wizard gives carries `--noproxy HOST --max-time 20`, and the self-check says in one sentence when a proxy probably answered instead of Orchestra.
- `Next` on the test and VS Code screens checks `GET /v1/agents/{id}` (`last_seen_at`) to confirm the agent reached Orchestra. While the last screen is open and visible, a 3-second check watches for the agent's first connection and stops on success.

A web project's coordinator must not give its worker CLI-only kit commands: a worker with a web credential has no kit and no command line. It reaches Orchestra only through the Orchestra Bridge tools (`#orchestraNext`, `#orchestraBrief`, `#orchestraHistory`, `#orchestraClaim`, `#orchestraCheckpoint`, `#orchestraReview`, `#orchestraRelease`) and the HTTP routes in its `.orchestra/AGENT.md`. For task text and the latest checkpoint with all open items, name `#orchestraBrief` or `GET /v1/projects/PROJECT/tasks/TASK/brief`; for comments, `#orchestraHistory` or `GET /v1/projects/PROJECT/tasks/TASK/history`; for its own work, `#orchestraNext` or `GET /v1/agents/me/next`. Never tell it to install the kit.
