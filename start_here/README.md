# Start a project or worker

This is the short path into an Orchestra-coordinated project. Beads is the canonical
coordination service; a repository checkout is not a second coordination database.
Project provisioning and worker registration are separate operations.

The intended office path is a web interface with per-person and per-agent identities
on a service-account-hosted server; each agent identity is owned by a person. That
route is still in progress. The SSH/private-config instructions below and the fleet
prompt are current interim or home-use options, not the recommended office setup.

## Choose the path

- **Starting a project:** the repository owner fills in
  [`templates/READ_ME_FIRST.md`](../templates/READ_ME_FIRST.md), links the working
  agreement from `README.md` and `AGENTS.md`, and asks the service operator to create
  the canonical Beads project and install its private onboarding entry based on
  [`templates/PROJECT_ONBOARDING.md`](../templates/PROJECT_ONBOARDING.md). The owner
  or operator must provide service access and the installation-specific bootstrap
  command; follow the [server onboarding instructions](../docs/ONBOARDING.md) and
  [`docs/OPERATIONS.md`](../docs/OPERATIONS.md). Do not run operator commands or
  initialize another database without that authority.
- **Joining an existing project:** obtain its connection instructions from the
  project owner or the server's `docs project` and `docs start` responses. Use those
  instructions for the repository URL, current client/helper, private client
  configuration and project name. They are installation-specific and must not be
  copied into this repository.

`worker.py start` or `session register` creates a worker identity; it does not create
a project. Register once, save the returned actor privately, and resume that same
actor on later runs. Never use `start` to replace an interrupted worker.

## From an empty directory

1. Obtain service access and the owner/operator-provided bootstrap command before
   proceeding. Create a dedicated local working directory for this worker and
   project before starting it; never launch from another worker's directory or
   another project's checkout. Keep one directory/checkout per actor. Save the full
   actor and the project-specific client configuration privately in that directory
   (outside Git or locally ignored), and never commit or share them. For a **new
   worker only** on an SSH deployment, the bootstrap command has this form; replace
   every placeholder with the approved installation values. A returning worker
   uses its existing directory and skips `start`:

   ```sh
   ssh REPLACE_WITH_APPROVED_SSH_ALIAS python3 REPLACE_WITH_INSTALLED_KIT_PATH/worker.py --root REPLACE_WITH_COORDINATION_RUNTIME_PATH --project REPLACE_PROJECT start --name REPLACE_READABLE_NAME
   ```

   Run the bootstrap from the new worker's own local working directory. The server
   returns the allocated actor and the server-owned project entry. Save the exact
   actor and request ID privately. Use the installed client, endpoint and
   `client.local.json` values supplied by that project entry; do not reuse or infer
   configuration from another project. The server-owned [onboarding
   instructions](../docs/ONBOARDING.md) explain how to fetch the **installed**
   client/helper set. Check `onboard` for client/kit version parity. If onboarding
   fails after registration, keep the actor and retry `onboard`; do not register
   again.
2. Clone the repository into your own directory using the project-provided URL and
   authentication method. Do not use another contributor's checkout or virtual
   environment.
3. Read the clone's `AGENTS.md`, `README.md`, project entry point and working
   agreement. The server's `docs NAME` command serves a fixed catalog; if a requested
   document is not in that catalog, read its repository-relative path from your own
   clone. A missing catalog entry is not permission to guess a remote path.
4. Create an environment in this checkout using the repository's supported Python
   version (3.10+ for this kit). For example:

   ```powershell
   python -m venv .venv
   $python = '.\.venv\Scripts\python.exe'
   & $python --version
   ```

   ```sh
   python3 -m venv .venv
   python=.venv/bin/python
   "$python" --version
   ```

   Install only dependencies documented by that repository's lockfile or setup
   instructions. The Orchestra client itself uses the Python standard library; do
   not install packages or borrow another checkout's `.venv` without a project
   requirement.
5. Fetch before selecting a base, then record the exact commit and check your own
   checkout:

   ```sh
   git status --short --branch
   git fetch origin
   git rev-parse origin/REPLACE_BASE_BRANCH
   git switch --no-track --create task/REPLACE_TASK_ID origin/REPLACE_BASE_BRANCH
   git rev-parse HEAD
   git status --short --branch
   ```

   Use the task's agreed base and branch policy; do not assume `main` or discard
   existing work. The new branch must not track the base branch. When authorized to
   publish the contribution branch, push it explicitly:

   ```sh
   git push --set-upstream origin task/REPLACE_TASK_ID
   ```

   Verify required imports and checks from this checkout's own interpreter in the
   same execution context where you will work.
6. A new worker must choose exactly one registration route: run the owner-provided
   bootstrap from step 1 once, or register through the **installed client path**
   using the interpreter created in step 4. Do not do both. If registering through
   the client:

   ```powershell
   & $python 'REPLACE_WITH_INSTALLED_CLIENT_PATH' --config 'REPLACE_WITH_PRIVATE_CONFIG_PATH' --project REPLACE_PROJECT -- session register --name REPLACE_READABLE_NAME
   ```

   ```sh
   "$python" REPLACE_WITH_INSTALLED_CLIENT_PATH --config REPLACE_WITH_PRIVATE_CONFIG_PATH --project REPLACE_PROJECT -- session register --name REPLACE_READABLE_NAME
   ```

   Save the returned actor and request ID privately. For an existing worker, skip
   registration and resume the saved actor before selecting unrelated work:

   ```powershell
   & $python 'REPLACE_WITH_INSTALLED_CLIENT_PATH' --config 'REPLACE_WITH_PRIVATE_CONFIG_PATH' --project REPLACE_PROJECT --actor REPLACE_ACTOR -- session resume
   & $python 'REPLACE_WITH_INSTALLED_CLIENT_PATH' --config 'REPLACE_WITH_PRIVATE_CONFIG_PATH' --project REPLACE_PROJECT --actor REPLACE_ACTOR -- work --mine
   ```

   ```sh
   "$python" REPLACE_WITH_INSTALLED_CLIENT_PATH --config REPLACE_WITH_PRIVATE_CONFIG_PATH --project REPLACE_PROJECT --actor REPLACE_ACTOR -- session resume
   "$python" REPLACE_WITH_INSTALLED_CLIENT_PATH --config REPLACE_WITH_PRIVATE_CONFIG_PATH --project REPLACE_PROJECT --actor REPLACE_ACTOR -- work --mine
   ```

   Use the actor returned by registration, not the readable name. Registration
   prints a request ID before sending; if the response is uncertain, retry with that
   same request ID and readable name rather than registering again. A returning
   worker uses only its saved actor; check its requested revisions before other
   claims. Do not run `client.py` from the project clone unless that repository is
   the Orchestra kit itself.
7. Read the task, dependencies, history and current owners. Recheck overlapping
   files/interfaces. Choose one unheld task from `ready --json`, claim it atomically,
   register the complete plan and pass any required acknowledgement or launch gate
   before editing. Do not claim another task until this one has been delivered.
   Follow the linked
   [`worker guide`](../docs/WORKER_GUIDE.md) and
   [`review workflow`](../docs/REVIEWS.md).

## Short delivery sequence

`read current state -> fetch and record base -> preflight and compare impacts -> claim
-> register/verify plan -> implement and test -> deliver exact commit -> await review`.
Record tested behavior and limitations. A delivered branch is not an approved or
integrated change. Contribution-branch push permission, merge permission, deployment
permission and live verification are separate; follow the project owner's policy.
Keep the task open while awaiting review unless its acceptance explicitly says
otherwise.

After delivery, a loop-capable harness may continue only when the project and
harness explicitly permit it: at the configured interval, check review feedback on
your own tasks with `work --mine` first, then check `ready --json` for one next
claimable task. Stop at the deadline or when you have no actionable open or
pending-review task and `ready --json` has no unheld task; do not wait on work held
by other actors or keep polling an empty queue. In office/person-started mode, do
not poll or loop: finish one bounded turn and end with a one-line status for the
person.

## Machine-specific settings are examples, not defaults

The client configuration shape can look like this, but every value is supplied by
the project owner/operator. This example contains placeholders only:

```json
{
  "host": "REPLACE_WITH_APPROVED_SSH_ALIAS",
  "endpoint": "REPLACE_WITH_INSTALLED_ENDPOINT_PATH",
  "root": "REPLACE_WITH_COORDINATION_RUNTIME_PATH"
}
```

Never commit populated settings, credentials, local paths or private project history.
If Git reports a Windows Schannel/TLS backend problem, an approved Git for Windows
installation may support a one-command OpenSSL backend override:

```powershell
git -c http.sslBackend=openssl clone REPLACE_WITH_REPOSITORY_URL
```

This does **not** disable certificate verification. If the backend is unavailable or
the verified connection still fails, use the organization's approved proxy/CA setup
or ask its administrator. Never set `http.sslVerify=false` or bypass host-key checks.

## Project feedback

Use the project feedback stream documented by server-served `docs operations`
(`feedback add --file` and `feedback list`) when the installed service supports it.
If an older service lacks that action, ask the coordinator for an explicitly agreed
interim parent/job target; do not attach feedback to an unrelated child or claim a
dedicated stream exists. Feedback and its evidence may be private: never copy it
into public Git.

Before removing existing recurring instructions, use the generic
[reminder/handover migration worksheet](MIGRATION.md). It includes a before/after
fixture and coverage mapping; it does not change live project records.

## Role prompts

Copy the relevant template and fill its placeholders:
[coordinator](../templates/COORDINATOR_PROMPT.md),
[worker](../templates/WORKER_PROMPT.md),
[fleet](../templates/FLEET_PROMPT.md) or
[subagent](../templates/SUBAGENT_PROMPT.md). These are task prompts, not new
authorization or role-enforcement mechanisms. Before using a copied guide or
prompt, replace every marked value in the prompt you intend to run. Scan the
instantiated prompt text for leftover `REPLACE_` placeholders and stop until none
remain; do not scan these generic source templates, which intentionally contain
placeholders. For example: `rg -n 'REPLACE_' filled-prompt.txt`.
