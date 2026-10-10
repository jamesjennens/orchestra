# Installation and recovery

## Install on Linux

Use an ordinary service account with a writable home, Python 3.10+, user systemd, and HTTPS access to GitHub release assets. Copy this kit to `/home/beads/beads-team-kit` in this example. Each deployment needs its own root, port and service name. Installation refuses unmanaged binaries or an existing service with the same name.

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime install --port 13317 --unit beads-team.service
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime add-project example
```

`add-project` initializes the project, records the standard native configuration, provisions the project's merge slot (idempotently) and performs the initial backup. A project whose slot is missing refuses `merge-check`, `merge-acquire` and `merge-release` with an error naming the `merge-create` coordination operation, which is also the manual repair for a project created before this behavior.

Project names use 2–24 lowercase letters/digits, starting with a letter. To use a project in the web interface (`http_service.py --backend endpoint`), a superuser then registers it there under the same name; the web service never creates canonical projects ([HTTP deployment](HTTP_DEPLOYMENT.md#projects-on-the-endpoint-backend)). After upgrading a web service, run its upgrade check once: `http_service.py --state <STATE> --list-unconfirmed-projects` lists project records made by an older kit that the service now refuses until a superuser confirms or archives them. Run these commands as the service account. An administrator should enable its user manager at boot and after logout:

```sh
sudo loginctl enable-linger beads
loginctl show-user beads -p Linger
systemctl --user is-active beads-team.service
```

The database listens on loopback only. Its generated credential is stored in the runtime's `deployment.private.json` (0600); runtime permissions are 0700. **The file is required**: since kittrial-5bb.156 every write through the endpoint reads it (and the password in it) before anything else, so a runtime without it, or with a file the endpoint's user cannot read, refuses every write with the endpoint's own line naming the file (over the web: 503 `server_configuration`). `prepare` writes it; a scratch runtime made by hand for a check needs one too. Contributors do not need the SQL password. Do not publish this directory or open the database port on the network. The installer configures metrics off for this deployment.

The kit runs `bd`, `dolt`, the HTTP service and the endpoint with `HOME` set to `<runtime>/home` and `BD_DISABLE_METRICS=1`, so Beads keeps its configuration inside the runtime and its usage metrics stay off; the kit writes nothing to the service account's own home directory. What that variable suppresses is bd's own telemetry, not anything the kit adds: with metrics on, bd 1.2.2 leaves a detached child that writes `$HOME/.config/bd/config.yaml` (`metrics: disabled: false`, upstream's event endpoint) and queues usage events under `$HOME/.beads/eventsData/` (`*.evtq` and `eventkit.lock`) just after a command such as `bd --version` returns. `bd metrics off` and `BD_DISABLE_METRICS=1` stop that child; the release tool sets the variable for its own run checks for the same reason (kittrial-5bb.166), and starts `dolt` there with `DOLT_DISABLE_EVENT_FLUSH=1` so that `dolt version` does not re-execute itself as a detached `dolt send-metrics` child (see docs/OFFICE_SERVICE.md; dolt's `metrics.disabled` global setting alone does not stop it). A runtime created before this change keeps working without a `home` directory: the environment variable alone keeps metrics off. Rollback note: an older kit reads Beads' configuration from the account's home directory instead, so before rolling back make sure `~/.config/bd/config.yaml` there has metrics disabled (`bd metrics off` as that account), or Beads turns its metrics on for a runtime that was created by this kit. Dolt's own global configuration is pinned to the runtime by `DOLT_ROOT_PATH` and is unaffected.

If initial installation fails, it stops the service and preserves files for inspection. Do not rerun by deleting the runtime: investigate the service journal first. Repeating installation of an already initialized deployment checks its pins, port and connectivity.

## Connect a contributor

Requirements governance is part of the complete backup sidecar and operation
journal set: `.requirements-governance.json`, its optional restore binding
`.requirements-governance-source.json`, and `.requirement-owner-requests/`.
New creation intents start simple; old creations and backups with no history
remain governed. Restore does not choose a mode. Preserve this set when moving
a project; use the [requirements snapshot command](REQUIREMENTS.md) for offline
owner-evidence export. Operator requirement-apply/backfill authority remains
unchanged in both modes.

**Rollback and restore of simple requirements.** The predecessor kit can restore
a backup of a project that has never had these sidecars. It refuses a backup
containing `.requirements-governance.json`, `.requirement-owner-requests/` or
`.requirements-governance-source.json` with `Invalid coordination backup path`.
Restore those backups with this kit or a newer compatible kit. A backup taken
by that predecessor omits these files: its restored project is governed, and
owner-accepted requirements cannot be read because their governance history is
missing. Keep the current kit's complete backup when rolling back; an older
kit's successful backup does not preserve owner requirement authority.
The optional `owner_decision` binding in an existing owner request receipt does
not change this predecessor restore limit. Current backups preserve and validate
the binding; an interrupted legacy acceptance without one remains unknown and
needs host operator reconciliation.

Configure the [server-owned project entry point](ONBOARDING.md) so new workers can start from an empty directory using a single SSH onboarding command. Its private instructions are included in coordination sidecar backups.

Install Python 3.10+ and OpenSSH on their machine. Configure an SSH alias `beads-team` for the server/service account with their own key; verify the server host key on first connection. Confirm an ordinary `ssh beads-team` works before using the noninteractive client.

Copy `client.example.json` to `client.local.json`, adjust the host and paths, then run. The
copied `"forced_command": false` is today's behaviour; a contributor whose key is confined
sets it to `true` (see [confine contributor keys](#confine-contributor-keys-with-a-forced-command)):

```sh
python client.py --config client.local.json --project example --actor alex/session1 -- ready --json
python client.py --config client.local.json --project example --actor alex/session1 -- refresh
python client.py --config client.local.json --project example --actor alex/session1 -- view
```

Keep local config outside committed project content or ignored. The client transports arguments and UTF-8 file contents as JSON over SSH. It does not copy source code or run builds. `--body-file`, `--design-file`, `--file` and `-f` read files on the contributor's machine. The endpoint exposes the routine issue commands; setup and maintenance use admin.py on the host.

Copy templates/READ_ME_FIRST.md and docs/WORKFLOW.md into each project repository, fill in project details, and add links to README and AGENTS.md. This makes the entry instructions discoverable by later agents without needing a pasted chat message.

## Confine contributor keys with a forced command

The endpoint (`endpoint.py`) is what enforces the kit's authority rules: the operator
allowlist on every host command, endpoint writes that always stay `unverified`, the
reserved comment prefixes, the HTTP actor-shape reservation, and the runtime `--root`.
Those rules bind a caller who cannot choose the remote command, and nothing else. The
shared service account is shell-trusted (see the README), so a contributor key without a
forced command can run `admin.py` or `bd` directly and every rule above holds only against
a cooperative caller. A forced-command entry makes the boundary real for that key.

`ssh_forced_command.py` is the `command=` value of a contributor key's `authorized_keys`
entry. It gives the key exactly one capability - running the configured endpoint - and
nothing else:

* only the endpoint path(s) the entry names can be selected; another program, `admin.py`,
  `bd` or a shell is refused;
* `--root` is fixed by the entry, so the caller cannot point the endpoint at another
  runtime, and no `--authority-store`, `--authority-lock` or `--require-authority` flag is
  ever passed;
* `SSH_ORIGINAL_COMMAND` is used only to select the endpoint; any other program, any flag
  and any extra argument is refused on stderr with status 2, and nothing runs.

Print the two exact lines for a public key with the helper:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime \
  authorized-keys --key-file ~/alex.pub
```

The contributor line is the confined entry; the operator line is the bare key, deliberately
unrestricted because the host commands need a shell. They are alternatives for different
keys: install exactly one entry per key, and grant the operator line only to an allowlisted
operator. The helper refuses a line that already carries options, a private key, a second
key line, or paths or an interpreter that cannot be quoted safely inside the entry. It
prints the interpreter as an absolute path with `-E -s` (ignore `PYTHON*` variables and the
user site directory; not `-I`, which would also drop the script's directory from
`sys.path`), and it refuses a `--python` outside the same bare-name-or-absolute-path
character class as `--root`, so `--python '$(touch${IFS}/tmp/canary)python3'` is refused
instead of printed into an entry the account shell would expand on every connection.

Confinement binds the key to the endpoint, not to an actor. A confined key still
self-declares its actor on every request, exactly as an unconfined one does; what the
forced command protects is the operator-gated and reserved operations, which a contributor
key could otherwise reach by running `admin.py` or `bd` directly.

### Bind a key to its projects

A confined key may name any project of the runtime. To bind it, print its line with the
projects it may use (kittrial-5bb.193; rule 1 of
[COORDINATORS_PER_PROJECT_DESIGN.md](COORDINATORS_PER_PROJECT_DESIGN.md)):

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime \
  authorized-keys --key-file ~/alex.pub --project alpha --project beta
```

The printed line carries `--project alpha --project beta` after `--endpoint`, and repeats
the names in the key comment (`orchestra-projects=alpha,beta`) so the file can be read by
eye; the binding is the arguments, never the comment. Only that line is printed: the
operator line is a shell, and a shell is every project. Each name must be a project of
this runtime. The client configuration does not change (`"forced_command": true`, as for
any confined key), and the project stays in the request.

What a bound key is answered: for one of its projects, exactly what any caller is
answered. For any other project, whatever the action (raw `bd`, `session register`,
reviews, the merge slot, lifecycle facts, views, everything the endpoint has), `ValueError:
Unknown/uninitialized project`, the answer for a project that does not exist: the key
cannot tell another project from none. Nothing of the other project is read first. The
three actions that exist only for the web service answer that they are available only to
the web service.

What it does not do: the key still names its own actor, any actor, inside its projects. Name
a principal as well (below) to confine the actor too. And it binds only a key whose ONLY line
in `authorized_keys` is the bound one. A key that also has an unrestricted or an unbound line
is not bound: replace that line. A line printed by an earlier release of the kit is served
by that release's wrapper, which knows no `--project` and refuses the line outright (its
own argument check), or, if the line names no project, binds nothing.

Read what is installed, without changing anything:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime authorized-keys-list
```

It reads `~/.ssh/authorized_keys` of the account that runs it (`--file` for another file)
and prints, for every line: its number, the key's type, fingerprint (as `ssh-keygen -l`
prints it) and comment, and its `kind`: `unrestricted` (no `command=`: the account's
shell, outside every rule of the kit), `confined` (the kit's forced command, any project),
`bound` (with its `projects`), `other-command` (a `command=` that is not the kit's
wrapper; said, not judged) or `unreadable`. For the kit's lines it also prints `other_kit`
(the wrapper or the endpoint the line names is not the installed kit's file: after an
upgrade of an office installation, a line that names `releases/<ID>` keeps running that
release), `names_release` (it is the installed kit today but names its release folder, so
it becomes `other_kit` at the next upgrade), `other_root`, `missing`, arguments the wrapper
does not know and projects that are not projects. `attention` lists the line numbers to
look at. `principal` is the principal named on the line (`lane:NAME` or `person:NAME`), or
null. The summary counts both kinds of binding: `bound` for every bound line and
`principal-bound` for the lines that name a principal; a line bound only to a principal is
`bound`, not `confined`. A repeated principal (`--principal` twice) and an ill-formed one
are under attention, because the wrapper refuses such a key.

### Bind a key to its principal

A confined key may name any actor. To bind it to a principal - a lane, in the
`lane:NAME` form (`person:NAME` is accepted too, and is a different principal) - name
the principal when the line is printed (kittrial-5bb.194; rule 2 of
[COORDINATORS_PER_PROJECT_DESIGN.md](COORDINATORS_PER_PROJECT_DESIGN.md)):

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime \
  authorized-keys --key-file ~/alex.pub --principal lane:orc-coord --project alpha
```

`--principal` and `--project` are independent: a key may be bound to projects, to a
principal, or to both. The printed line carries `--principal lane:orc-coord` after the
projects and repeats it in the key comment (`orchestra-principal=lane:orc-coord`); the
binding is the argument, never the comment. Only the confined contributor line is printed,
as with `--project`: an operator line has a shell and cannot be bound. A line that names a
principal twice is refused, as a project named twice is.

What a key bound to a principal is answered: a request whose actor the project's session
registry (`projects/PROJECT/.sessions.json`) gives to that principal is served exactly as
any caller is; every other action is refused before anything runs, whichever actor the
request names, with a sentence naming the principal and the actor. An actor that has no
entry has no principal and is not this key's. **The exception is a session registration**:
`session register` under a bound key is allowed and records the new actor under the key's
principal, so the new session can then act through that key. It is the only exception, and
only when the first argument is exactly `register`; a `session resume`, `session run` or
`session show` as another actor is refused like any other action. `session show` prints the
principal the actor belongs to (`principal`); on an installation that has never written an
owners map the key is omitted, so the answer is the one this kit gave before rule 2.

#### First call and pre-registration checks

Because a new lane owns no registered actors in the project initially, a bound key's
**first kit call must be `session register`**. No other call can precede registration or
be used as a pre-registration check:
1. `client.py` requires `--actor` for commands other than `session register`, refusing locally
   before any connection is attempted (`Supply a short contributor/session actor`).
2. If an actor is supplied, the endpoint's principal gate refuses the request before execution
   (`This key is bound to principal lane:NAME and may act only as actors that principal registered in this project`).
Consequently, a newly set-up lane cannot read served documentation (such as `docs sessions`
or `docs start`) through the kit client until *after* registration. The worker must read startup
instructions and registration guidelines directly from its local repository clone.

#### Checking a bound key without registering

To verify that an authorized_keys entry is installed and working without registering (or without making an actor):
- **On the server**: Run:
  ```sh
  python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime authorized-keys-list
  ```
  (pass `--file` only to inspect an authorized_keys file other than the account's own `~/.ssh/authorized_keys`). The command shows what each line is bound to (`projects` and `principal`) and puts under `attention` any lines that do not point at the installed kit (`other_kit`, `names_release`), or have a repeated or ill-formed binding (`principal_repeated`, `principal_ill_formed`, `project_repeated`), missing files, or unreadable lines.
- **On the client**: Test network and SSH authentication using plain SSH without the client wrapper:
  - With no command:
    ```sh
    ssh -T USER@HOST
    ```
    The forced-command wrapper answers on stderr with exit status 2:
    ```text
    ssh_forced_command: no endpoint selected: this key runs only the configured endpoint; a client with "forced_command": true sends its path
    ```
  - With a command:
    ```sh
    ssh USER@HOST exit
    ```
    The forced-command wrapper answers on stderr with exit status 2:
    ```text
    ssh_forced_command: this key may not run 'exit'
    ```
  Both exit status 2 refusals confirm that SSH authentication succeeded and the key is properly confined to the forced-command wrapper.

An actor that existed before the key was bound (an older coordinator, a legacy name with no
registration) keeps its name and its history if it is given to the principal once by the
writing host command:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime \
  adopt-actor alpha alex/session1 --principal lane:orc-coord --actor OPERATOR \
  --reason "the lane he coordinates in"
```

The command refuses a name that appears nowhere in the project (no session registration, no
owner entry and no tracker row names it), so the audit is not a place to invent an actor. An
ASSIGNEE on a tracker row counts as naming the actor, so a row assigned to a name makes that
name adoptable exactly as a row's author does; the check is a guard against a mistyped name,
not proof that the actor ever acted. A name on the deployment operator allowlist is not
exempt: until a session registration, an owner entry or a tracker row in the project names
it, a coordinator's listed name cannot be adopted there. It
refuses a name on the deployment operator allowlist that another principal owns in another
project: the one operator list must mean the same lane everywhere. It records in
`<runtime>/actor-adoptions.audit.json` who gave which actor to which principal, when and why
(`admin.py actor-adoptions [PROJECT]` prints it), and reports `previous` when it moved an
actor this project already gave to a different principal. It writes nothing when the project
already gives the actor to this principal.

**Moving an actor that another principal already owns is not the same plain command.** Once
a project's registry gives an actor to a principal, `adopt-actor` refuses to give it to
another unless the command names the owner it has now with `--from PRINCIPAL`; the audit
entry for such a change carries `"moved": true` beside `previous`. Without `--from` the
refusal changes nothing, so one mistyped actor cannot take a lane's identity and record away.
**Nothing removes an owner**: there is no command that takes an actor back to "no principal",
and a wrong owner is corrected with `adopt-actor ... --from`. The audit is a short history:
it keeps the newest 200 entries and drops the oldest silently; it is a record of recent
adoptions, not a complete ledger (the registry's owners map is the authority). A kill between
the command's two writes leaves an audit entry for a move that did not happen: the audit is
written first, so the registry can still give the actor to the principal it had, and running
the same command again appends a second identical entry. Read a `moved` entry against the
registry.

**What this section does not do.** Rule 2 binds only BOUND keys. A line with no
`--principal`, a key bound only to projects, and a line printed by an older release all act
as every actor until slice 4 (bound keys only), and a line of an older release stays outside
even then. A lane still passes its own work until rule 3 (slice 6): its actor may approve its
own contribution and take its own merge slot. A principal named with no `--project` reaches
every project of the installation by registering there first; `--principal` alone limits the
names a key may use, not the projects it can reach. And `operators add` does not look at the
owners maps: an actor adopted by two principals and listed afterwards belongs to both, in its
own project each. Adopt or move an actor before putting its name on the operator list.

`session register` under a bound key is unbounded, and every bound request parses the whole
registry: one lane can slow the others by registering in a loop (the registry was 16.6 kB
with 53 entries in the review's run, and 40 registrations took 35 s). Nothing here caps that;
slice 4's bound-keys-only rule and the operator list are what bound who may register.

Removing a coordinator whose lane is simply replaced: remove its `authorized_keys` line (its
principal binding goes with the key), and do not start with `operators remove
--confirm-revoke`, which makes every void, integration revert, retraction and proposal record
it authored stop counting. Take a name off the operator list only when that effect is what is
wanted.

**Downgrade limit.** The registry gains one key, `owners` (actor to principal). This kit
writes that key only when it is non-empty: an installation that configures nothing still
writes the registry exactly as before, and an older kit reads it. Once an actor is adopted,
or a session is registered under a bound key, the registry carries `owners`, and an older
kit's validator refuses a registry with the unknown key. That older kit then refuses
`session show`, `session resume`, `session register`, `session run start` and
`actor-standing` for the project, **and `admin.py backup PROJECT` fails for it with status
incomplete**; every key bound with `--principal` is refused (its wrapper does not know the
argument), while `bd` itself, `work` and `review` keep working. Before rolling back to a kit
without rule 2, remove that map from `projects/PROJECT/.sessions.json` first (and the binding
from the keys), or the older kit cannot read the registry, cannot back the project up, and
refuses every such key. The same applies to a coordination-sidecar backup taken after an
adoption. [OFFICE_SERVICE.md](OFFICE_SERVICE.md) repeats this beside "Binary pins on
rollback".

### Accept bound keys only

An installation can be set to accept bound keys only, so a line that leaves a key bound to
nothing - no project to reach, or no principal whose actors it may be - is refused by the forced
command before the endpoint runs (kittrial-5bb.196; slice 4 of
[COORDINATORS_PER_PROJECT_DESIGN.md](COORDINATORS_PER_PROJECT_DESIGN.md), and Migration steps 6
and 7):

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime \
  bound-keys-only on --actor OPERATOR
```

The command has three actions: `status` prints the setting, the last recorded change, whether the
two agree (`audit_agrees`, with a warning when an older kit or a hand edit changed one without the
other) and the lines that turning it on would refuse; `on` and `off` flip it. `audit_agrees` is
`null` with `audit_note` `no entry` when no flip was ever recorded: a value hand-set to on with no
audit entry does not read as agreeing, and `status` says so. Both `status` and
`on` read the authorized_keys file named by `--file` (default this account's
`~/.ssh/authorized_keys`) and report `would_be_refused`: the lines of this kit and this root that
the setting refuses, or would refuse while it is off. So the operator sees what `on` is about to
cut off before it happens, and `on` prints that list - after reading and building it and before it
writes the flip, so a slow or failing read of the keys file never leaves the setting on with
nothing printed. The keys file is read at most its first 1 MiB, and `would_be_refused` names at most
20 line numbers with `would_be_refused_more` (and an "and N more" in the note) saying how many more
there are, so a hand-written file cannot flood the terminal. It is operator-gated (`--actor` must be
on the deployment operator allowlist) and every flip is recorded in the deployment document's
`bound_keys_only_audit`, with the value it replaced, keeping the last 20 flips; a flip that changes
nothing writes nothing. `off` on a value that is not true or false is a real flip: it writes false,
so the bad value and its warning do not stay in the file.

The setting itself is one key, `bound_keys_only`, in `deployment.private.json`; absent or false is
off, so an installation that configures nothing behaves as it did in the ordinary case, and a fresh
install and one that turned the setting back off read identically. The reader reads at most the
first 1 MiB of that file and refuses a larger one, in the forced command and in `admin.py` alike:
the file is read on every connection, so a huge one must not be read whole.

With it on, a line that names no `--project` or no `--principal` is answered on stderr with exit
status 2, the missing part is named, and nothing is run - `ssh -T HOST` on such a key answers
this too. That message, like the non-bool warning, speaks of "the installation setting" and names
neither the settings file's server path nor the value found: stderr reaches the connecting
contributor. The operator's own `admin.py` commands name both. A line that names both is served
exactly as before, and `admin.py authorized-keys`
refuses to print a contributor line that the setting would refuse (`--role operator` still prints
the unrestricted line: a shell is outside this setting). What the setting does not touch: an
unrestricted key, another program's `command=`, a project's rules, and rules 1 and 2.

**What it does not do, said plainly.** A line printed before an upgrade names the wrapper and the
endpoint of the release it was printed from, by absolute path, and on an office installation that
path carries the release. Such a line runs THAT release's wrapper and endpoint - which know
nothing of this setting, or of rules 1 to 3 - against the live runtime, for as long as that
release's directory is there, and nothing in the new kit can refuse it. **Clean
`authorized_keys` by hand**: remove or reprint every line that points at another release. The
setting closes a forgotten line of the CURRENT kit; it does nothing about an older release's.
`admin.py authorized-keys-list` flags each line that points at a kit other than the installed one
(`other_kit`, `names_release`) and, while the setting is on, it also puts under `attention` every
line of THIS kit and THIS root that names no project or no principal, and its `bound_keys_only`
field says which setting it read. Its `would_be_refused` field names those lines whether the
setting is on or off, so it is the preview before `on`. The setting is decided by what the
wrapper's own command line decides: the wrapper FILE the line runs and its LAST `--root` (its
argparse takes the last when a line names it twice); an `--endpoint` argument that names another
kit's file does not move the line out of this kit's reach. A line that runs another kit's wrapper,
or whose last `--root` is another runtime, is never counted as refused: this setting does not reach
it (that line is served by the release or the root it names), and the note says so. A named line
the wrapper refuses for another reason (a bad or unknown argument, a binding named twice) is listed
as refused with a sentence saying the setting is not why.

`setup-status` reports the setting: the endpoint's read-only action carries a `bound_keys_only`
block (`enabled` true or false, and `null` with its reason when the setting cannot be read - a
damaged file, a path that is not a regular file, or one that nests too deeply - each with a
sentence). An endpoint older than this kit does not carry the block at all.

**Before an upgrade or a rollback.** The printed lines name the release, so after every upgrade
of an office installation print and install them again (`authorized-keys`), as Migration step 6
says. A kit older than this one does not know the setting at all: rolling back to it silently
stops refusing unbound lines although `bound_keys_only` is still true in
`deployment.private.json`. It reads that file otherwise as before, and when this kit is back the
setting applies again.

**A damaged or unreadable `deployment.private.json` refuses a confined key.** The wrapper cannot
tell whether the setting is on, so it refuses every call of a forced-command key (exit status 2,
saying the installation setting could not be read) until an operator repairs or removes the file;
nothing is run. The operator's own unrestricted key does not run the wrapper and still reaches a
shell, so this can never lock the operator out. A value that is neither true nor false is read as
off with a warning on stderr, as `review-writes` reads such a value for its own switch.

**Three exceptions to "an installation that configures nothing behaves exactly as it did".** All
three are deliberate, and all three are covered by the recovery:

* **A damaged `deployment.private.json` refuses every forced-command key even where the setting
  was never on.** The forced command cannot see whether the setting is on, so it refuses rather
  than serve unbound (a release before this one still answered `setup-status` here). **Recovery:**
  an operator repairs the file, or removes it - a missing file reads as off - and the confined
  keys work again; the operator's own unrestricted key reaches a shell throughout.
* **A line whose `--root` names a regular file is refused** (the wrapper's reader cannot look up
  `deployment.private.json` under a regular file). **Recovery:** correct that line's `--root` to
  the runtime directory and print it again with `admin.py authorized-keys`.
* **A VALID `deployment.private.json` larger than 1 MiB refuses every forced-command key even
  where the setting was never on.** Both readers read at most the first 1 MiB and refuse a larger
  file, because neither can tell whether the setting sits beyond the bound; so a valid but oversized
  file refuses a confined key although `bound_keys_only` was never set. **Recovery:** bring the
  file back under 1 MiB - the kit keeps a damaged audit list aside inside that file, which is the
  only thing in this kit that grows it - and the confined keys work again; the operator's own
  unrestricted key reaches a shell throughout.

### sshd settings the boundary needs

The forced command closes what the key can run; two sshd settings decide what the client can
put into the session *before* it runs:

* `PermitUserEnvironment no` (the default). With `yes`, a client can send environment
  variables through its own `~/.ssh/environment` or `SetEnv`.
* No `AcceptEnv` beyond locale variables: `AcceptEnv LANG LC_*`. With `AcceptEnv *`, a client
  `SetEnv LD_PRELOAD=...` or `SetEnv BASH_ENV=...` reaches the service account: `LD_PRELOAD`
  loads a library into the shell and the interpreter, and `BASH_ENV` runs a script in the
  shell - both act before any kit code executes, so the wrapper cannot close them. (The
  printed line starts the interpreter with `-E -s`, so a forwarded `PYTHONPATH` no longer
  runs a module there.) The
  wrapper does close the endpoint's own process: it execs the endpoint with a minimal,
  explicit environment (`PATH`, `HOME`, `LANG`/`LC_*`, and the variables the kit sets for the
  endpoint itself - none today), so a forwarded `PYTHONPATH`, `BASH_ENV`, `ENV`, `LD_PRELOAD`
  or `SSH_ORIGINAL_COMMAND` is not inherited there.

Check the effective configuration rather than the file (the running config may come from an
included file or a default):

```sh
sshd -T | grep -Ei 'permituserenvironment|acceptenv'
```

`permituserenvironment no` and an `acceptenv` line limited to `LANG`/`LC_*` (or no
`acceptenv` line at all) are what the confined setup assumes. The key line needs OpenSSH 7.2
or later on the server: an older sshd does not know `restrict` and refuses the key (it fails
closed). `restrict` in the key options
separately closes the user rc file (`~/.ssh/rc`), which sshd would otherwise run for the
session.

### What the options close

The contributor line carries the sshd options beside the forced command:

| Option | What it closes |
| --- | --- |
| `restrict` | pty, port/agent/X11 forwarding and the user rc file (`~/.ssh/rc`) in one word, and any capability a later OpenSSH adds is off until this list is edited |
| `no-pty` | no interactive terminal, so the key cannot type at a shell prompt |
| `no-port-forwarding` | no `-L`/`-R` port forwarding through the service account |
| `no-agent-forwarding` | the key cannot use its agent on the host to reach other accounts |
| `no-X11-forwarding` | no X11 channel |

### The client must know

The forced command refuses the `--root` an ordinary command line carries, so the
contributor whose key is confined sets `"forced_command": true` in their
`client.local.json`; the client then sends only the endpoint path. The `endpoint` value
there must be exactly the path printed in the entry (the helper prints both together, so
copy them as a pair); `root` must still be present, but the wrapper's fixed root wins, so a
stale value there can never move the endpoint to another runtime. Without
`"forced_command": true` the connection is refused, loudly, with nothing run. Absent or
`false` leaves today's command line byte-identical, so no existing client changes behaviour
until it opts in.

```json
{"host": "beads-team", "endpoint": "/home/beads/beads-team-kit/endpoint.py",
 "root": "/home/beads/beads-runtime", "forced_command": true}
```

One onboarding command changes: the bare `ssh beads-team python3 worker.py ... start` form
runs another program and is refused by a confined key. Register through the endpoint
instead (`python client.py --config client.local.json --project example -- session register
--name cline`), or keep a separate, deliberately unrestricted onboarding key for whoever
provisions workers.

### Migration for an existing deployment

Confining a key and setting that contributor's client flag are a coupled pair: the two change
together, per contributor. A confined entry without `"forced_command": true` makes the client
send a `--root` the wrapper refuses; `"forced_command": true` without the confined entry makes
the account shell try to execute the endpoint path as a command, which fails with
`Permission denied` (status 126) on every request. Neither half is documented as safe on its
own.

1. Keep one unrestricted operator key (the operator line the helper prints) for `admin.py`
   and the host commands; do not confine it.
2. For each contributor key, replace any existing unrestricted entry for that key with its
   confined line. sshd uses the first matching line, so a second entry is not a migration.
   Keep the session you edit `authorized_keys` in open, and verify the operator key in a
   second session before closing the one used to edit `authorized_keys`: a bad edit would
   otherwise lock out the only way back in.
3. Have each contributor add `"forced_command": true` to their `client.local.json` at the same
   time as their entry changes, then confirm `python client.py --config client.local.json
   --project example --actor <actor> -- ready --json` succeeds and that `ssh beads-team
   /path/admin.py --help` is refused for that key.
4. Rollback is per key: remove the `command=` entry (and the `"forced_command"` key) and the
   previous behaviour returns. Nothing in the runtime or the tracker changes.

Nothing here changes a live deployment by itself: the coordinator rolls it out with the
owner.

## Operator commands

A few coordination actions are host shell commands rather than contributor-client
actions. Run them as the service account on the coordination host; the client and
the endpoint do not expose them, and a worker or agent lane must not be asked to
run them. Their payload schemas and full semantics are in [native requirement
commands](REQUIREMENTS_INTEGRATION.md) (`requirement-*`) and [malformed structured
history](#malformed-structured-history) (`void-record`).

| Command | Purpose | Authority it needs |
| --- | --- | --- |
| `requirement-apply PROJECT --actor ACTOR --file record.json` | write an accepted requirement revision, or move an accepted record back to draft | the payload's `acceptance` object (named owners/approvers, policy, decision id, evidence) and the deployment operator allowlist |
| `requirement-backfill PROJECT --actor ACTOR --file backfill.json` | add the controlled requirement type/state labels to records created before this route | an `evidence` pointer for an entry that becomes `accepted`, and the deployment operator allowlist |
| `requirement-reconcile PROJECT --operation-id ID --actor ACTOR --disposition ...` | finish a requirement operation whose real write was uncertain | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `reference-apply PROJECT --actor OPERATOR --file acceptance.json` | accept a reference catalog entry: the payload names the newest draft `revision` and its `record_sha256`, and the command writes the next revision as accepted, after the F3 evidence (`operation: "draft"` with full content writes a direct accepted revision 1). An entry whose authority is an attestation is accepted only under the [extra rules below](#attested-reference-entries) | the deployment operator allowlist, checked before any write, and F3 evidence (`acceptance`: owners, approvers, policy, decision id, evidence) |
| `reference-apply PROJECT --actor OPERATOR --file batch.json` | accept a batch of reference entries under one F3 decision: the payload has `items` of `{key, revision, record_sha256}` (each the newest draft reviewed) in place of the single entry's fields. It behaves exactly as the `capability-apply` batch below: one receipt per item keyed `(operation_id, key)`, `accepted`, `already-accepted`, `refused` or `uncertain` per item, a refused item does not stop the others, the coordination lock is taken per item, at most 100 items, and re-running the same batch resumes it. Read the totals, not the exit code: see the `capability-apply` row | the deployment operator allowlist, checked before any write, and F3 evidence |
| `reference-reconcile PROJECT --operation-id ID --actor ACTOR --reason TEXT --disposition ...` | finish a reference operation whose real write was uncertain; `complete` needs `--issue-id` and refuses an anchor that has no live revision record, because its propose stopped or every record it held is voided (re-run the original `ref propose` with its `operation_id` first; if that payload is lost, use `anchor-release`) | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `capability-apply PROJECT --actor OPERATOR --file batch.json` | accept a batch of capabilities under one F3 decision: `items` of `{key, revision, record_sha256}` (each the newest draft reviewed). The command writes one acceptance record and one receipt per item, keyed `(operation_id, key)`, in list order. It reports `accepted`, `already-accepted`, `refused` or `uncertain` per item; an uncertain item stops the batch, and re-running the same batch resumes it. The command exits 0 when the batch ran, even if items were refused, so read the totals it prints: `accepted`, `refused`, `stopped` (an uncertain write stopped the batch) and `complete`, which is true only when every item was accepted. A changed list needs a new `operation_id`. The coordination lock is taken per item and released between items, with a 50 ms pause while it is free, so other writers wait behind at most one item; each item re-checks its `revision` and `record_sha256` under its own hold. An item takes about 3 seconds (two reads and four writes), so 100 items take 5 to 6 minutes: prefer batches of about 20. `operation: "draft"` with full content writes a direct accepted revision 1 | the deployment operator allowlist, checked before any write, and F3 evidence |
| `capability-retire PROJECT --actor OPERATOR --file retire.json` | supersede the newest revision of a key by a `successor` key, with evidence. The successor must exist, and a cycle is refused. A retired key refuses `revise` and acceptance. This is the only route that withdraws an accepted entry: the design has no demotion, so an accepted revision is never weakened or withdrawn in place (design section 4) | the deployment operator allowlist and F3 evidence |
| `capability-alias-reject PROJECT --actor OPERATOR --file reject.json` | reject a pending alias (`{schema_version, key, alias, reason}`); lookup then ignores it | the deployment operator allowlist |
| `capability-alias-propose PROJECT --actor OPERATOR --file alias.json` | propose an alias as a verified operator (`{schema_version, key, alias, evidence?}`). This is the only route that writes `identity: verified`; `capability propose-alias` through the endpoint always writes `unverified`, even for an operator's actor name | the deployment operator allowlist |
| `capability-verify PROJECT --actor ACTOR --file payloads.json` | record capability checks as **verified**. The file is what `capability check --repo . --payloads payloads.json` wrote at the commit being verified (one payload, or `{schema_version, items}` of up to 500). Each item is one capability and takes the coordination lock on its own; the result is `recorded`, `already-recorded` or `refused` per item, and re-running the file is safe. This is the only route that writes a verified check: `capability check --record` through the endpoint always writes an unverified report | the deployment operator allowlist or the `verifiers` list, both checked before any read |
| `operators list\|add\|remove [ACTOR] [--actor OPERATOR] [--reason TEXT] [--confirm-revoke] [--all-revoked]` | manage the deployment operator allowlist: who may run the operator-gated host commands. `add` refuses an HTTP account or agent id. `remove` needs `--confirm-revoke` and first names the voids, proposal dispositions and settings that change (capped at 5; `--all-revoked` names all). Every real change is recorded in `<runtime>/authority-changes.audit.json`, with `--actor`/`--reason` when they are given (see [the audited list changes](#the-operator-and-verifier-list-changes-are-audited)) | shell access to the coordination host; `deployment.private.json` is the only authority source |
| `verifiers list\|add\|remove [ACTOR] [--actor OPERATOR] [--reason TEXT] [--confirm-revoke]` | manage the deployment `verifiers` list: actors, other than operators, whose `capability-verify` records readers count as verified. The list is empty by default and grants nothing else. `remove` needs `--confirm-revoke`; the refusal names the capabilities whose verification would change. Every real change is recorded like `operators` (see [the audited list changes](#the-operator-and-verifier-list-changes-are-audited)) | shell access to the coordination host; `deployment.private.json` is the only authority source |
| `authority-changes` | read-only: the recorded changes of the operator and verifier lists - which list, the actor added or removed, the change, when, and the recorded `--actor`/`--reason` (`null` when the caller gave neither) | none: read-only |
| `review-writes status\|on\|off --actor OPERATOR` | read or set the per-installation switch that allows **writing** the new review-workflow record shapes (`withdraw`, `request-review`, `resolve-item`, `decline-review`, an item `severity`, a request-changes `summary`). Readers in this kit understand those shapes either way; with the switch off (the default) a write of one is refused before any native write. See [Review-workflow write switch](#review-workflow-write-switch) | the deployment operator allowlist, checked before any write; `deployment.private.json` is the only source (there is no environment fallback) |
| `checkpoint-provenance-writes status\|on\|off --actor OPERATOR` | read or set the per-installation switch that allows **writing** checkpoint provenance and direction dispositions (acknowledge, resolve, supersede). Readers in this kit understand them either way; with the switch off (the default) a checkpoint that asks for them is refused before any native write. See [Checkpoint provenance write switch](#checkpoint-provenance-write-switch) | the deployment operator allowlist, checked before any write; `deployment.private.json` is the only source |
| `bound-keys-only status\|on\|off --actor OPERATOR [--file AUTHORIZED_KEYS]` | read or set the per-installation setting that **accepts bound keys only**: with it on, the forced command of an `authorized_keys` line refuses a line that names no project or no principal, and `setup-status` reports it. `status` and `on` report `would_be_refused` (the lines of this kit and this root the setting refuses, or would refuse while it is off), read from `--file` or `~/.ssh/authorized_keys`. Off is the default and the absent key, so an installation that configures nothing behaves as before. See [Accept bound keys only](#accept-bound-keys-only) | the deployment operator allowlist, checked before any write; `deployment.private.json` is the only source |
| `proposal-review PROJECT --actor OPERATOR --file review.json` | record a coordinator disposition on a requirement proposal. The payload is `{schema_version, operation_id, key, previous, proposal_sha256, to_state, ...}`: `previous` is the `disposition_comment_id` and `proposal_sha256` the `sha256` that `proposal get` returned, so a stale read is refused before any write. `to_state` is `under-review` (the claim), `rejected` (with `reason`), `duplicate-of` (with `duplicate_of`), `needs-info` (with `question`), `escalated-to-owner` (with `escalation: {question, owner_identity, due_by}`) or `incorporated` (with `incorporation`, checked against the requirement record) | the deployment operator allowlist, checked before any read; the actor must be mapped to a person and must not be the submitter |
| `proposal-decide PROJECT --actor OPERATOR --file decision.json` | record the owner decision on an escalated proposal: `to_state` `approved` or `rejected` (with `reason`), and `decision: {decision_id}` naming an existing native decision issue. Never a requirement id | the allowlist; the decider must be a different person than the escalator and must not be the submitter |
| `proposal-settings PROJECT --actor OPERATOR [--map-actor ACTOR --to IDENTITY] [--namespace NAME --to IDENTITY] [--unmap-actor ACTOR] [--unmap-namespace NAME] [--add-decider IDENTITY] [--remove-decider IDENTITY]` | with no change, print the contribution settings; otherwise write the next settings record, composed from the current one and bound to its hash | the allowlist |
| `proposal-http-records PROJECT [--after UTC] [--before UTC]` | list the proposal revisions and dispositions whose native author has an HTTP account or agent id shape, with the tracker's creation time. Run it after upgrading to the kit that reserves those shapes and after rolling a rollback forward ([HTTP deployment](HTTP_DEPLOYMENT.md#requirement-proposals)) | none: read-only, no lock, no `--actor` |
| `proposal-reconcile PROJECT --operation-id ID ...` | finish a proposal submit, revise or disposition whose write was uncertain | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `capability-reconcile PROJECT --operation-id ID ...` | finish a capability operation whose write was uncertain (for a batch item, the id is `OPERATION_ID/KEY`). A transient native failure can leave a `pending` receipt with no native row behind it, and the same `operation_id` is then refused until it is cleared: run `capability-reconcile --disposition released`, then retry the original command | the deployment operator allowlist, checked before the receipt is read; then confirmation of the native record state |
| `record-reconcile PROJECT --kind requirement\|reference\|capability\|proposal ...` | the same reconcile for any record kind | as above |
| `reconcile-request PROJECT --request-id ID --actor OPERATOR --reason TEXT --disposition ...` | resolve a stuck coordination request receipt ([operational workflow](OPERATIONAL_WORKFLOW.md)) | the deployment operator allowlist, checked first; then its own actor-binding rules (`--any-actor`) |
| `finish-project PROJECT` | complete a project creation that `add-project` or the web interface started and that stopped half way, when the project was initialized: settings, backup target, merge slot and a backup, each safe to repeat. It prints the creation record. See [HTTP deployment](HTTP_DEPLOYMENT.md#creating-a-project-from-the-web-interface) | whoever runs commands on the host |
| `project-creations [--attention] [--usage]` | list the creation records in `project-creations/` as JSON; `--attention` lists only the ones that are running or that an operator must finish or remove, each with its command; `--usage` prints the number of project databases on the server and its limit | whoever runs commands on the host |
| `project-creations --set-server-limit N --actor OPERATOR` | set the most project databases this server may hold (default 20); see [The cost of many projects on one server](#the-cost-of-many-projects-on-one-server) | a listed operator |
| `remove-creation PROJECT --actor OPERATOR --reason TEXT` | remove a project creation that `add-project` or the web interface did not finish. Refuses a finished project, a name with no creation record and a running creation. An incomplete one is retired (nothing deleted, the name stays retired); a stalled one, for which nothing was made, loses only its record and its name is free. A record that cannot be read (`damaged`) is only set aside, as `project-creations/PROJECT.json.damaged-<UTC stamp>`; nothing else is touched | a listed operator |
| `retire-project PROJECT --actor OPERATOR --reason TEXT [--force]` | retire a partial or drill project: move `projects/PROJECT` to `retired/PROJECT-<UTC stamp>`. Nothing is deleted; see [Retiring a project](#retiring-a-project) | the deployment operator allowlist, checked first |
| `reference-misses-clear PROJECT` | delete the project's [reference lookup-miss log](#the-reference-lookup-miss-log). Same behaviour and output as `capability-misses-clear`, on `.reference-misses.json`, `.reference-misses.json.tmp` and `.reference-misses.lock` | none beyond the service account: it deletes telemetry only |
| `capability-misses-clear PROJECT` | delete the project's [capability lookup-miss log](#the-capability-lookup-miss-log). It prints what was removed (`finds`, `misses`, `phrases`), and in `repaired` any symlink, directory or unopenable lock file it removed from the three miss-log names (never following a link). It writes nothing to the tracker, takes no coordination lock and calls no `bd` | none beyond the service account: it deletes telemetry only, so there is no allowlist check and no `--actor` |
| `void-record PROJECT --actor OPERATOR --file void.json` | void a malformed or stale contribution-review record, or a malformed, foreign or conflicting reference or capability record ([below](#reference-and-capability-records)) | the deployment operator allowlist (`operators` in `deployment.private.json`) |
| `anchor-release PROJECT --kind reference\|capability --issue-id ID --actor OPERATOR --reason TEXT` | close a reference or capability anchor that holds no record and free its key, when the propose that created it cannot be re-run ([orphan anchors](#orphan-anchors)). With `--duplicate` (and, for an anchor that carries acceptance evidence, `--set-aside-evidence`) it releases a named anchor of a [duplicated key](#a-duplicated-record-key) although it holds well-formed records | the deployment operator allowlist, checked before any read |
| `handoff PROJECT --actor ACTOR --file handoff.json` | transfer a claim when the current owner cannot act | an owner decision/evidence pointer in the payload's `approval` |
| `set-guidance PROJECT --actor OPERATOR --file FILE` | install the project's [standing guidance](#standing-guidance) text (bounded plain text, 8000 bytes) and its audit record. A set with the same text repairs a missing or mismatched audit record (`repaired: true`) | the deployment operator allowlist, checked before any write |
| `guidance-status PROJECT --actor OPERATOR` | print who has acknowledged which guidance version, with the current text, the previous text, the history, `up_to_date`, `behind` and `stale`, and who cleared the guidance, when and which version (`clear_record`, `clears`) (the authoritative read; the endpoint's `guidance status` shows no guidance text) | the deployment operator allowlist |
| `clear-guidance PROJECT --actor OPERATOR` | remove `GUIDANCE.md` and `.guidance.json` and write the local `.guidance-clear.json` record (who, when, cleared version; shown by `guidance-status`); guidance then reads `present: false`. A record already there that is not valid is kept as `.guidance-clear.json.invalid.<UTC time>`, and the command says so. A symlinked `GUIDANCE.md` is refused and needs a manual delete | the deployment operator allowlist |
| `compact-guidance-acks PROJECT --actor OPERATOR` | drop acknowledgements for versions other than the current and previous one; the record keeps `acks_compacted_by`/`acks_compacted_at` (also kept across later sets) as the audit trail | the deployment operator allowlist |
| `adopt-actor PROJECT ACTOR --principal lane:NAME --actor OPERATOR --reason TEXT [--from lane:OWNER]` | give an existing actor to a principal (a lane) in one project's session registry, so a key bound to that principal may act as it. It refuses a name that appears nowhere in the project, and it refuses to move an actor another principal already owns unless `--from` names that owner. It refuses a name on the operator allowlist that another principal owns in another project, records the change in `<runtime>/actor-adoptions.audit.json` (with `moved` on a move), and writes nothing when the actor already belongs to this principal. See [Bind a key to its principal](#bind-a-key-to-its-principal) | the deployment operator allowlist (the `--actor`), checked before any write |
| `actor-adoptions [PROJECT]` | read the adoption audit: who gave which actor to which principal, when and why | none: read-only |

All five are shell-trusted: access to the service account's shell is the boundary.
`requirement-apply`, `requirement-backfill`, `void-record` and `anchor-release` also
check the deployment operator allowlist (`operators` in `deployment.private.json`), so a
deployment that configures no operators authorizes nobody and an unlisted
`--actor` is refused before any native or journal write. The allowlist is read
strictly: a shell-only `ORCHESTRA_OPERATORS` value that disagrees with the
deployment configuration is refused rather than honoured in one place and
ignored in the other. `operators add/remove/list` maintain that list (see the
removal and restore policy below). Before the `requirement-apply`/
`requirement-backfill` check ships, enrol every actor that accepts requirements
today (starting with `james`) with `admin.py operators add` on each installation
and confirm with `operators list`; an empty or short list is a deploy blocker,
not a warning. Use the identity of the person actually running the command as
`--actor`; the owner decision is named in the payload, never by reusing the
owner's actor.

### A confined coordinator runs the acceptance commands through the endpoint

Slice 3 of [COORDINATORS_PER_PROJECT_DESIGN.md](COORDINATORS_PER_PROJECT_DESIGN.md)
(kittrial-5bb.195). A coordinator that gave up the shell (a key bound to a principal) can run
the acceptance commands it needs without one, when **all** of these hold: the request arrives
over a key bound to a principal, the project's session registry gives the request's actor to
that principal, that actor is on the deployment operator allowlist (for `capability-verify`
the `verifiers` list is enough), and the project is one the key may name.

| Command | The host command it runs |
| --- | --- |
| `coordinator guidance-set --file FILE` | `admin.py set-guidance` |
| `coordinator guidance-clear` | `admin.py clear-guidance` |
| `coordinator guidance-status` | `admin.py guidance-status` (the authoritative read, with the text) |
| `coordinator reference-apply --file FILE` | `admin.py reference-apply` (one entry, or an `items` batch) |
| `coordinator capability-apply --file FILE` | `admin.py capability-apply` |
| `coordinator capability-verify --file FILE` | `admin.py capability-verify` |
| `coordinator proposal-review --file FILE` | `admin.py proposal-review` |
| `coordinator proposal-decide --file FILE` | `admin.py proposal-decide` |
| `coordinator handoff --file FILE` | `admin.py handoff` (the operator's transfer) |
| `coordinator set-onboarding --file FILE` | `admin.py set-onboarding` |

Each runs the same library call with the same payload as its host command, so the payload
schemas in the table above apply unchanged - except where the row below says the route carries
less. `--file` is a local file the client transports with the same attachment transport every
other write uses; the server never reads a path out of the request. `coordinator set-onboarding`
writes the document and answers with `changed` and with `set_by`, which **echoes** the actor the
server chose for the request: the route stores no attribution of its own (a request field called
`set_by` is ignored), so the installation's own log is the only trace of who set the text. A
previous `ONBOARDING.md` that cannot be read as UTF-8 text does not stop the write: it is treated
as changed, replaced, and the answer says so on stderr - the route used to answer a bare
`UnicodeDecodeError` with nothing written, so a damaged document could not be replaced from a
host without a shell (kittrial-5bb.238 item 1). Unlike the host command the route does **not**
probe the text for endpoint paths, because that probe resolves and reads every absolute `*.py`
path the text names - server files read out of the request - and the host
`admin.py set-onboarding` still warns the operator with a shell. A clear with nothing set
(`guidance-clear`) answers `changed: false` and writes nothing, so a retry whose answer was lost
is reconcilable without a shell.

**Which payload operations this surface carries.** `coordinator reference-apply` and
`coordinator capability-apply` carry exactly one payload `operation` value: `accept`, of a
draft that exists. Through this route a record is accepted only after somebody proposed it as a
draft (`ref propose` / `capability propose`), so every accepted record has a recorded proposal
before its acceptance: two steps, both attributed. The route does not check that the proposer and
the accepter differ; the same actor may do both. They do **not** carry
`draft`, the library's direct accepted revision 1, which creates a key that did not exist
already accepted on one party's own word - a per-project coordinator, possibly an agent,
accepting its own brand-new record in one step; a direct revision 1 is created by the
installation operator on the host (`admin.py reference-apply` / `admin.py capability-apply`).
They do **not** carry `retire` either, the only operation that withdraws an accepted entry: that
stays with the installation operator (`admin.py capability-retire`, above) - nor the contributor
operations `propose`/`revise`. `incorporated` is not an entry-apply operation at all: it is a
proposal disposition state, reached with `coordinator proposal-review` and
`coordinator proposal-decide`. Any other operation, in the single payload or anywhere in an
`items` batch, is refused before the library is called and nothing is written; a `draft`
anywhere in a batch refuses the whole batch, so a valid item beside it is not applied either. A
batch item carries no `operation` of its own, whatever the value, `accept` included: the batch
itself is the acceptance. A payload with no `operation` is the one place the two commands
differ, exactly as on the host: `reference-apply` defaults to `accept`, and `capability-apply`
is answered by the library ("operation must be one of ...") - a message that names the
operations the host carries, `retire` included, not the one this route does.

**What stays with the installation operator.** `proposal-settings`, `capability-retire`,
`capability-alias-propose`/`-reject`, `void-record`, `revert-record`, every `*-reconcile`,
`anchor-release`, `remove-creation`, `retire-project`, the backups, the switch commands
(`review-writes`, `checkpoint-provenance-writes`, `bound-keys-only`) and the operator and
verifier list commands are **not** reachable through this surface, for anybody: they remain
`admin.py` host commands.
The action refuses any name it does not know and names the ones it does.

**How the power is taken away.** The operator list is checked against the name the project's
session registry gives the key's principal, so removing the **actor** from the list
(`admin.py operators remove ACTOR --confirm-revoke`) refuses it at once, with no key change.
Removing the **key** means removing **every** `authorized_keys` line of that principal: a
principal that still has a second line still works, so one line removed is not a revocation.
An unbound key that names a listed actor is still refused by this action (it sends no
`--key-principal`), but, as before, it is not confined and can still write ordinary rows.

**The list is per installation, not per project.** A listed actor is a coordinator in **every**
project where its principal owns that actor name, and a key bound only to a principal (no
`--project`) reaches all of those projects. A confined coordinator therefore needs a key bound
to the projects it may serve (rule 1), not just a listed actor name.

**An installation that configures nothing is unchanged.** The action exists only for a key
bound to a principal: without `--key-principal` - every ordinary host loop and every key that
binds nothing - the endpoint refuses it before it reads a project file, takes a lock or calls
`bd`, and every other action behaves exactly as it did.

### The operator and verifier list changes are audited

`operators add|remove` and `verifiers add|remove` take `--actor OPERATOR` (the operator
making the change) and `--reason TEXT` (at most 400 characters). Every change that really
changes a list appends one entry - time, operator, list, actor, change, reason - to
`<runtime>/authority-changes.audit.json` beside `deployment.private.json` (schema version 1,
mode `0600`, the last `200` entries kept). The entry is written under the same deployment
lock as the list and **before** it, so a crash between the two leaves an entry with no
change rather than a change with no entry. When the cap makes room by dropping the oldest
entry, the change says so on stderr and what it recorded is carried in the baseline (below),
so a name is never lost from the trail. The read-only `authority-changes` command prints the
history; it counts and marks the entries that name no operator, prints the current operator
and verifier lists beside the entries, prints the baseline the trail begins with and any
damaged audit file kept beside the runtime, and replays the trail against the lists, saying
plainly (in the report's `replay.note` and on stderr) when the trail does not lead to the
lists. The recorded `--actor` is a record, not a grant: these commands still check no
allowlist, exactly as before (kittrial-5bb.192).

A call without the flags still works, because the office wrapper `coord.sh` runs the bare
`admin.py --root RT operators add ACTOR`. Such a call still records the change, with
`operator` and/or `reason` `null`, and prints one sentence on stderr naming exactly what to
add: the change is never silent, and the audit never pretends somebody was named. With one of
the two flags given, the sentence names the operator (or the reason) it does have and asks
only for the flag that is missing - a change by a named operator is not called unattributed.
A call that changes nothing (the name is already listed, or a remove of a name that is not
listed) records nothing, prints nothing and exits 0, exactly as the release before this audit.

**The baseline: every new history starts from a known state.** The first change that finds no
baseline in the file writes one beside the entries: a compact record - `at`, `operator`,
`reason` and `lists` - holding the operator and verifier lists exactly as they stood then, read
under the lock and before that change. So a history begins with a baseline when it is started
on an installation from before this kit, and again when a removal starts a fresh history after
a damaged audit. It is carried forward, never cut: when a change needs room in the 200-entry
cap, the entries dropped for room are folded into a fresh baseline first, so a name they
mentioned is still in the trail. (One compact record, not one entry per name: a baseline that
cost an entry for every name it holds could not fit inside a 200-*entry* cap on an installation
that lists many names, and the trail would stop being replayable exactly when it is needed.) The
baseline is what makes the reader's answer usable: a name the list holds that the trail never
mentions is then really a hand edit or a change made by a kit older than the baseline. A
history with no baseline at all - a file written by hand, or by a kit older than this one - is
still read, and the note says the trail is incomplete for these lists and may simply be older
than the lists. When a later change takes a baseline on such a history it says so: it holds the
lists as they stand at that change, not "as they stood when this history began"
(kittrial-5bb.229 finding 6). A list that cannot be read is recorded in the baseline as `null`,
labelled `UNKNOWN` (kittrial-5bb.229 rev-2 item 1): never as an empty list. An empty list there
would read every name the list really holds as one the trail never mentions, for good, and the
baseline's own `reason` says which list it is and that the trail is incomplete for it.

**Reading the answer: a script reads `replay.agrees`.** `authority-changes` exits 0 whether the
trail leads to the lists or not, and also when `deployment.private.json` cannot be read (then
`current_lists` and `replay` are `null` and the warning is on stderr). The JSON says it in
`replay.agrees`: `true` when the trail leads to the lists, `false` when it does not, and `null`
when there is no trail yet (no audit file at all: the reader then says there is no trail yet,
prints the lists and warns about nothing) or when the trail is INCOMPLETE for a list. `replay.state`
carries the same answer as `agrees`, `mismatch`, `no-trail`, or `incomplete`, and `replay.note` is
the sentence printed on stderr. Never a non-zero exit on a mismatch: every existing installation
would fail otherwise.
When one list ALONE cannot be read, `current_lists` holds `null` for it,
`replay.state` is `incomplete` and `replay.agrees` is `null`: the note says the trail is
incomplete for that list - and names it FIRST, before anything about the lists it could compare -
and the comparison this read cannot make is not made (kittrial-5bb.229 findings 2 and rev-2 item
2). The same state is reported when the BASELINE holds a list as `null` (UNKNOWN), which is what a
baseline records for a list that could not be read when that history began (rev-2 item 1): the
trail has no known state to replay that list from, even after the value is repaired. One rule
covers the audit and the lists both, and it is the same rule as for a damaged audit: a **removal**
is never refused for it, and only an **add** is. A removal that would start a new history while a
list cannot be read proceeds, and the baseline it writes records that list as `null` (UNKNOWN); an
add that would start one is refused, because a baseline must never hold an empty list for a list
that could not be read (rev-2 item 1). The placeholder flags are covered by the same rule: a
removal carrying the literal `--actor OPERATOR`/`--reason TEXT` the printed re-grant commands carry
is not refused - the placeholder is dropped, the entry is recorded without it, and one stderr
sentence says so - while an add carrying them is refused (rev-2 item 3).

**What the audit cannot see.** An entry holds no before/after of the list itself, so
`authority-changes` can only replay the trail from its baseline: a name the list holds whose
last recorded change is a remove, a name the trail adds that the list does not hold, and - with
a baseline - a listed name the trail never mentions (a hand edit of `deployment.private.json`,
or a list change made by a kit older than the baseline) are all reported as a trail that does
not lead to the lists, never silently. Nothing else in the kit detects a hand edit.

**A damaged audit, and the two kinds of change.** An `authority-changes.audit.json` this kit
cannot read - not JSON, empty, a list, `null`, another `schema_version` (including JSON `true`
or `1.0`), an unknown top-level key, an entry or a baseline with an unknown or missing field or
an `at` that is not a UTC stamp, nested past the guard, a BOM, non-UTF-8 bytes, `NaN`, or mode
`000` - is treated differently by the two. A **removal** (`operators remove NAME
--confirm-revoke`, `verifiers remove NAME --confirm-revoke`) is NEVER refused for it: the
damaged bytes are put beside the runtime FIRST, as a hard link or a copy, under the name
`authority-changes.audit.json.damaged-<UTC date-time>` (`.N` if that name is taken), and the
atomic write of the fresh history then replaces the audit path. The ONE rule for both the audit
and the lists: a removal is never refused for either, and only an add is - here, an **add**
(`operators add`, `verifiers add`, and a `restore-new --restore-operators`/`--restore-verifiers`
re-grant, which is an add) IS refused, with a sentence that names the file and the recovery:
move the damaged file aside by hand, then run the command again. That refusal is checked before
the lock is taken, so it costs nothing at all (no lock file, nothing written), and an add that
would change nothing is not refused at all. The sentence saying a fresh history starts is printed
only once that history has actually been written, so the kit never says it and then refuses
(kittrial-5bb.229 rev-2 item 1). Only a regular, non-symlink file
is a candidate or is listed: a symlink at a `.damaged-*` name, even a dangling one, is never
followed, never overwritten and never read as bytes the kit kept inside the runtime
(kittrial-5bb.229 finding 1). Where the filesystem has no hard links the copy is written to a
temporary name first and renamed into place, opened `O_CREAT|O_EXCL|O_NOFOLLOW`, so a kill
inside the copy never leaves a partial file under a `.damaged-*` name (finding 4); `O_EXCL` is the
flag that does the work - it already refuses a taken name, a symlink included - and `O_NOFOLLOW` is
kept beside it only because it says the intent and costs nothing (rev-2 item 4). The path is therefore never absent,
not even for an instant: a kill, or a write that fails, between the two leaves the
damaged file exactly where it was plus one extra name, and running the command again sets the
same bytes aside again - the name already holding them is reused, found by `samefile` or by
comparing the BYTES (not the size: a same-size file holding something else is not the kept copy,
rev-2 item 4) - so even without hard links the attempts do not pile up copies. One sentence on stderr says
so, and the fresh history's first record - its baseline, or the removal when the lists are empty
- names the file kept. The `mv` command every refusal prints carries a real,
current stamp, not a placeholder, so following it twice cannot overwrite the first kept file. A
path that is not a regular file at all - a directory, a fifo or a symlink - is refused for every
command, because the kit only keeps a damaged regular FILE beside the runtime by itself.

**The `.damaged-*` files are never removed.** Nothing in the kit deletes one, ever: they
accumulate beside the runtime as the record of what the trail could no longer read, and the
reader lists them (in its JSON, under `damaged_files`, and in the note when there are any). Move
one away, or archive it, yourself once you have read it. Two things beside them ARE cleaned up,
because they are interrupted writes rather than kept records: `.authority-changes.audit.json.XXXXXXXX`,
the copy `atomic_private_write` leaves if it is killed before its rename, and
`.authority-changes.audit.json.damaged-<stamp>.tmp-<16 hex>`, the copy of a damaged audit's bytes
left if the process is killed inside that copy. The next write under the deployment lock removes
both and says so on stderr (kittrial-5bb.229 rev-2 item 4).

`--actor` and `--reason` may be given at most once; a repeat is refused instead of recording the
last value silently. Abbreviations are OFF for these two commands, so `--act` and `--reas` are
refused; the abbreviations that existed before these commands took `--actor` are kept as
explicit aliases: `--a` and `--all` mean `--all-revoked`, and `--confirm` means
`--confirm-revoke`. Neither command takes the recording flags on `list`: they would change
nothing and be ignored.

The audit is runtime-level and, like `actor-adoptions.audit.json`, is never part of a project's
coordination backup.

**Re-grants are recorded too.** `restore-new SRC DST --restore-operators`/`--restore-verifiers`
re-adds the names the backup records that this host no longer lists, and every name it re-grants
gets one entry in the same audit, written under the same deployment lock and before the
configuration. `operator` is `restore-new`'s `--actor` (null when it was not given) and the
recorded reason names `restore-new` and the source project, then the `--reason` sentence.
`restore-new` itself takes `--actor OPERATOR` and `--reason TEXT` for these two flags, checked
before the restore starts (the reason must leave room for the `restore-new SRC: ` prefix inside
the 400-character ceiling); they are refused when neither flag is given, and a repeat of either
is refused. Like `operators` and `verifiers`, `restore-new` does not abbreviate ANY of its flags:
`--act`, `--reas`, `--restore-op` and `--without-c` are refused by the parser with exit status 2.
The spelled-out names are unaffected, and every use in this document and in the kit's own calls
is spelled out (`--restore-operators`, `--restore-verifiers`, `--without-coordination`). A
re-grant nobody was named for
records a null operator and prints the same one stderr sentence the four list commands print, so
it is never silently unattributed. A damaged audit refuses the re-grant the same way it refuses
an add: the restore is still complete, and it exits 3 with the warning and the commands to
re-grant by hand - those commands now carry `--actor`/`--reason` (with what the restore was
given where it had it), so following them does not leave the unattributed entry the warning says
to avoid. Where the restore was given no `--actor`/`--reason`, the commands carry the literal
placeholders `OPERATOR` and `TEXT` and the warning says to replace them: the re-grant is an ADD,
and running one unchanged is refused, so the audit never records an operator named OPERATOR
(kittrial-5bb.229 finding 3). A REMOVAL carrying the same literal placeholders is not refused -
a removal is never harder with the flags than without (rev-2 item 3): the placeholder is dropped,
the entry is recorded without it, and one stderr sentence says exactly what was recorded. What is
NOT covered: a hand edit of `deployment.private.json`, and a list change made
by an older kit running on the same runtime - both change the lists with no entry, and only the
reader's replay reports the gap.

### Standing guidance

The standing guidance channel (kittrial-5bb.99) stores one operator-set instruction
per project in `projects/PROJECT/GUIDANCE.md` plus its audit record
`projects/PROJECT/.guidance.json`. The version is the SHA-256 of the exact text
bytes, computed by every reader. Only `admin.py set-guidance` writes it (operator
allowlist, checked before any file is read). The endpoint can read it and record an
actor's own acknowledgement of the exact version it names; it cannot write it, and
every other guidance subcommand is refused. The endpoint's `guidance status` shows
versions and actor names but no guidance text; `admin.py guidance-status` is the
authoritative operator read and adds the current text, the previous text and the
history.

* A hand edit of `GUIDANCE.md`, or a crash between the two writes, is reported as an
  **unbound** record. Every reader says the same thing (`guidance get`,
  `guidance version` and the block in `brief`, `work` and resume): `present: true`,
  `version: null`, `set_by: null`, `unbound: true`, `meta_version` (the version the
  audit record names), a `warning` and a `next_action` saying the guidance is being
  repaired by the operator; `guidance get` returns `text: null`. No reader hands out
  the hash of the withheld text, and nobody can acknowledge it.
  Text without a setter is never delivered to a worker and never followed. Setting
  the same text again repairs the record and prints `repaired`; the generation the
  old audit record named is kept in `history`, so `get --since` still knows it. A
  `GUIDANCE.md` that is a symlink is refused (readers and `clear-guidance` do not
  follow it): that state needs a manual delete of the link.
* **A mismatched or unreadable guidance pair degrades its project's backup instead
  of failing it.** `backup --all` still backs up every other project and still
  completes the tracker backup of the affected one, leaving the bad guidance pair
  out of the sidecar; the run records that project `complete` with a `degraded`
  message (and prints it), and the entry stays in `backup-status`. The process exits
  0 because the tracker backup is complete and usable - the daily timer must not
  report a broken backup for a two-file instruction fault - and the operator repairs
  the pair with a same-text `set-guidance` and takes a fresh backup. `restore-new` on
  a backup with no guidance pair restores the tracker with the project reading
  `present: false` (no guidance), never a mismatched pair.
  A degraded run does not read like a clean one (kittrial-5bb.105): the summary line
  counts and names the degraded projects (`Backed up 3 of 3 project(s), 1 degraded
  (NAME); ...`), a one-project `backup` prints `complete but degraded`,
  `backup-status` prints the count and each message on stderr (stdout stays the JSON
  record), `backup-copy` names each degraded project it copies, and `restore-new`
  says that what the degraded entry names was not in the backup and was not restored.
  `backup-status --require-complete` still passes, so the timer is unaffected;
  `backup-status --require-clean` and `backup-copy --require-clean` refuse. Use
  `--require-clean` as the backup check before a release.
* Acknowledgement requires the version the caller read, given as
  `--version VERSION`; the endpoint refuses a bare positional version and refuses a
  stale version **without naming the current one**, so a caller must run
  `guidance get` first. **Acks are unauthenticated:** actors are self-declared, any
  actor the endpoint accepts may ack for itself, and a caller can name another
  actor. A flood can fill the bounded table and evict a real lane's ack; that fails
  safe because an evicted lane re-reads and re-acks. The table is bounded (500): a
  new lane evicts the oldest entry rather than being refused, acks for versions
  other than the current and previous one are dropped at every set, and
  `compact-guidance-acks` drops stale ones on demand (its
  `acks_compacted_by`/`acks_compacted_at` audit is kept across later sets). An
  acknowledgement does not prove a read.
* Guidance is bounded (8000 bytes) plain text. C0, C1, bidi, word-joiner, BOM and Unicode tag (`U+E0000`-`U+E007F`) characters are refused, and so are the other characters that render as nothing or as a blank (kittrial-5bb.105): variation selectors (`U+FE00`-`U+FE0F`, `U+E0100`-`U+E01EF`, and the Mongolian `U+180B`-`U+180D`, `U+180F`), the Hangul fillers (`U+115F`, `U+1160`, `U+3164`, `U+FFA0`), the Khitan filler `U+16FE4`, `U+034F`, `U+17B4`, `U+17B5`, `U+2800`, `U+FFFC`, and every format character (Unicode category Cf: `U+061C`, `U+180E`, `U+FFF9`-`U+FFFB`, the musical, shorthand and Egyptian format controls and the rest), so the rule does not depend on a list. The only format characters allowed are the visible ones that are part of real text, and only directly before a letter or digit of their own script (kittrial-5bb.121: a terminal often draws them with zero width, so one inside a Latin word could split a keyword without showing; all thirteen are prepended marks, written in front of the number or word they span): the Arabic number signs, end of ayah, pound and piastre marks and supertitle sign (`U+0600`-`U+0605`, `U+06DD`, `U+0890`, `U+0891`, `U+08E2`) before an Arabic letter or digit, the Syriac abbreviation mark `U+070F` before a Syriac letter, and the Kaithi number signs (`U+110BD`, `U+110CD`) before a Kaithi letter or digit or a Devanagari digit (`U+0966`-`U+096F`; Kaithi has no digits of its own and Unicode's ScriptExtensions.txt lists those digits for Kaithi); and ZWNJ/ZWJ under the rule below. Only the FOLLOWING character counts, and it must be of Unicode category L* or N* inside that script's code-point ranges: a mark at the end of the text or before a space, another mark, a combining mark, tatweel `U+0640`, an Arabic presentation form (`U+FB50`-`U+FDFF`, `U+FE70`-`U+FEFF`), punctuation or an unassigned code point is refused. Arabic letters assigned or changed after Unicode 13.0 (`U+0870`-`U+089F`, `U+08B5`, `U+08C8`-`U+08FF`) do not count either, so that every supported Python gives the same answer. One case still passes, by decision (kittrial-5bb.124): a mark directly before an Arabic letter or digit that sits inside a Latin word (`ig` + `U+0600` + `U+0661` + `nore`). The mark is followed by a base of its own script, and that base is visible, so the word visibly does not read as the keyword; the rule is about marks that split a word invisibly. The answer is the same on Python 3.10 to 3.13 even though their Unicode tables differ (13.0 to 15.1): `U+0890` and `U+0891` are unassigned in 3.10 and Cf from 3.11, and the Egyptian format controls `U+13439`-`U+1343F` are unassigned in 3.10 and Cf from 3.12, so the allowed marks, their scripts and those Egyptian controls are listed explicitly in the kit rather than read from the interpreter's tables. Blank characters that render as a visible space are accepted on purpose: the no-break spaces `U+00A0` and `U+202F`, `U+2000`-`U+200A`, `U+205F`, the ideographic space `U+3000` and the Ogham space mark `U+1680`. They do not hide text, they are common in pasted French and CJK text, and an acknowledgement names the exact version, so two texts that differ only in such a space are never confused for one another. The same check runs when the text is read, so a hand-edited file that carries one reads `unreadable` until an operator sets clean text. One `set-guidance` repairs any file that cannot be read as guidance (a refused character, over the limit, not UTF-8): `guidance-status` (and the endpoint's `guidance status`) answers in that state instead of failing: `unreadable: true`, `unreadable_reason` naming the character or the limit, `audit_record` (the version, setter and time of the last generation an operator set), the acknowledgement table and the `repair` sentence, never the unreadable text. The set replaces the file, prints that it did, answers `replaced_unreadable: true`, copies nothing of the unreadable file into the record, and keeps the history and the acknowledgement rules of any other set. (Before kittrial-5bb.105 that set was refused with the old file's error and the repair was `clear-guidance`, which drops both.) ZWNJ and ZWJ are allowed only between two letters or combining marks of one script that uses them (Arabic, including Persian and Urdu; Syriac; Mongolian; N'Ko; Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam, Sinhala); anywhere else, including inside a Latin word and in an emoji sequence, they are refused. An emoji that needs `U+FE0F` is therefore refused: check the text with `guidance-status` before upgrading a project whose guidance holds one.
* `clear-guidance` removes both files, writes the small local
  `.guidance-clear.json` audit record (who cleared it, when, the cleared version,
  bounded to the last 10 clears) and guidance reads `present: false`.
  `guidance-status` (host) and the endpoint's `guidance status` show it: `clears`
  (who, when, which version; never text) and `clear_record` (`absent`, `ok` or
  `invalid`). A file at that path that is not a valid record this kit wrote (a hand
  edit, a symlink) reads `invalid` with a `clear_warning`, and the next clear keeps it
  as `.guidance-clear.json.invalid.<UTC time>` (each one under its own name, listed in
  `clear_records_kept_aside`) and starts a new record instead of replacing it
  silently. The record is not yet written into the coordination backup: this kit's
  restore accepts and validates it in a sidecar, but the kit before it refuses a whole
  restore on a sidecar path it does not know, so `backup` starts carrying it only
  once every installation runs a kit that accepts it. Until then a restored project
  has no clear record; the removed guidance record itself stays in the project's most
  recent coordination backup, if one was taken.

**Every upgrade and rollback: restart the web service in the same step as the files (kittrial-5bb.156).** Where the web interface runs (`http_service.py --backend endpoint`), the service is a long-running process and the endpoint is started anew for each request. Replacing the kit's files therefore changes the endpoint at once and the service only when it is restarted, and a service of the other kit left running applies its own rules to the new endpoint's answers (seen: it registered a half-made project whose creation record is damaged, which the new service refuses). Replace the files and restart the service together; see docs/HTTP_DEPLOYMENT.md.

**Upgrading to this kit, and rolling back from it (kittrial-5bb.105).** The plain-text
rule is checked when guidance is read as well as when it is set, so a change of rule
has an effect in both directions.

* **Before upgrading, check each installation's guidance.** Guidance that was valid
  before and holds a character this kit refuses (for example an emoji that carries
  U+FE0F, or any other invisible format character) reads `unreadable` from every reader
  after the upgrade, with `attention` raised and the text withheld, until an operator
  sets clean text. Run, for each project, on the kit you are about to deploy:

  ```sh
  python3 /path/to/new-kit/admin.py --root /home/beads/beads-runtime guidance-status PROJECT --actor OPERATOR
  ```

  It answers `unreadable: true` with the character named in `unreadable_reason` if the
  current text would be refused; one `set-guidance` with clean text repairs it, before
  or after the upgrade. A refused character in the PREVIOUS text is harmless: the
  record stays valid, acknowledgements, compaction and backup work, and that previous
  text is withheld from `get --since` and from `guidance-status`
  (`previous_text_withheld: true`).
* **Rolling back.** This kit accepts some text the kit before it refuses: a zero-width
  joiner or non-joiner after a combining mark (the common Indic use after a virama) is
  accepted here and reads `unreadable` there, and that project's backup is recorded
  degraded there. **Check for it before every rollback.** For each project, on the
  coordination host:

  ```sh
  python3 -c "import sys,unicodedata as u;t=open(sys.argv[1],encoding='utf-8').read();print([i for i,c in enumerate(t) if c in '\u200c\u200d' and i and u.category(t[i-1]).startswith('M')] or 'none')" /home/beads/beads-runtime/projects/PROJECT/GUIDANCE.md
  ```

  It prints `none`, or the positions of a joiner that follows a combining mark. If it
  prints positions, set text without them, or clear the guidance, on this kit before
  rolling back.

**Upgrading to, and rolling back from, kittrial-5bb.121.**

* **Upgrading.** A visible format character (the list above) that is not directly
  followed by a letter or digit of its own script - for example an Arabic number sign
  before ASCII digits, at the end of a word, or before a combining mark or tatweel - was
  accepted before and reads `unreadable` after the upgrade. Run `guidance-status` with
  the new kit on each project before upgrading, as above.
* **A same-text repair keeps the original setter.** A set with the same text that
  rewrites a record failing the strict check (the check acknowledge, compaction and
  backup apply) used to replace `set_by` and `set_at` with the repairing operator and
  time, and the original setter was kept nowhere. It now keeps `set_by` and `set_at` in
  `.guidance.json` and records the repair in a separate local file beside it,
  `.guidance-repair.json` (`schema_version`, the repaired generation's `version`,
  `set_by` and `set_at`, and `repaired_by`, `repaired_at`). `guidance get`/`state`,
  `brief` (JSON and the text `Guidance:` line) and `guidance-status` show
  `repaired_by`/`repaired_at` from that file only while it names the record's current
  version, setter and time and the record is intact; a file for another generation or of
  any other shape is ignored. The next set of different text, and `clear-guidance`,
  remove it. The file is local: `backup` does not carry it, so a restored project shows
  the original setter without the repair note.
  The file is also unauthenticated: anyone who can write the project directory can
  write one. So it is read only up to 4096 bytes, never through a symlink, and shown
  only when the repairer it names is a configured operator (`deployment.private.json`
  `operators`); a well-formed file naming anyone else, or a project outside a deployment
  root, shows no repair (kittrial-5bb.124). Treat a repair note as a hint, and the audit
  record's setter and history as the record. Three consequences (kittrial-5bb.125):
  a genuine repair stops being shown once its operator is removed from the operator
  list; nothing is shown at all when no operators are configured; and a file planted by
  someone who can write the project directory, naming a listed operator, is shown. A
  repair whose repairer is no longer listed is hidden rather than shown as "repaired by
  NAME (no longer a listed operator)": a reader cannot tell it from a planted file
  naming any unlisted name, so marking it would show those too. If the audit path is a symlink, a
  directory or any other non-regular file, the same-text set refuses before it changes
  anything (`Nothing was changed: ...`); remove it and set again. If the audit write
  itself fails after the record was repaired, the set succeeds and prints that the
  repair audit could not be written.
* **Rolling back below kittrial-5bb.121.** Nothing to do for the repair audit. The
  record keeps exactly the fields the older kit validates, so `guidance ack`,
  `compact-guidance-acks`, `backup` and `restore-new` work there unchanged; the older kit
  never reads `.guidance-repair.json`. A set there that rewrites the record (new text,
  or its own same-text repair) changes the version or the setter and leaves the file
  behind, where this kit then ignores it.

**Rollback gap and the exact step.** An older kit (at or before
`dca96b9d`) validates the coordination sidecar against a fixed path set that does
not include `GUIDANCE.md` or `.guidance.json`, so `restore-new` by that kit fails
with `Invalid coordination backup path` on a backup taken by this kit while
guidance is set. A backup taken by the older kit during a rollback succeeds but
silently omits guidance. To roll back:

1. While still on this kit, run `admin.py clear-guidance PROJECT --actor OPERATOR`
   (or delete `projects/PROJECT/GUIDANCE.md` and `projects/PROJECT/.guidance.json`
   by hand), then take the backup (`admin.py backup PROJECT`). Keep a copy of the
   two files if the guidance text and acks are needed later.
2. Restore that backup with the older kit's `restore-new`. Guidance on the restored
   project reads `present: false`.
3. After rolling forward again, re-run `admin.py set-guidance` to reinstall the
   text. The previous history and acknowledgements are not recovered
   automatically from the cleared files.

### Review-workflow write switch

kittrial-5bb.94 adds review-workflow records (`withdraw`, `request-review`,
`resolve-item`, `decline-review`, an item `severity`, a request-changes `summary`)
that a kit built before that change refuses: it meets an unknown `operation` value
or an unexpected field inside a task's `Kind: contribution-review-v1` chain, fails
closed on that task with `Malformed contribution-review history; operator
reconciliation required`, and refuses every review write on it. The kit that ships
them is therefore staged in two steps, and this kit is step one.

* **Readers understand the new shapes unconditionally.** `review`, `brief`, `work`
  and `history` parse and project them whatever the switch below says.
* **Writers are off by default.** `deployment.private.json` gains one boolean,
  `review_workflow_writes`; absent or `false` means **OFF**, and a value that is not
  a boolean (a hand-set `"yes"`, say) is **read as OFF with a warning** rather than
  refused, so a malformed value cannot make `work` or `review` fail for every actor
  while `brief` still answers. With it off, a review write of any new shape is
  refused before any native write, and a legacy request-changes item (`{id, text}`,
  no severity, no summary) still works. An exact retry of an operation id already in
  the chain still reconciles, so records written while it was on stay recoverable.
* **The coordinator turns it on** once the rollback target is a kit that reads the
  new shapes:

```sh
python3 admin.py review-writes status --actor OPERATOR     # current value (default: false)
python3 admin.py review-writes on  --actor OPERATOR        # allow writing the new shapes
python3 admin.py review-writes off --actor OPERATOR        # back to refusing them
```

`--actor` must be on the deployment operator allowlist (`operators` in
`deployment.private.json`), checked before any write. There is deliberately no
`ORCHESTRA_*` environment fallback: the file is the single source, and the endpoint
supplies the value to the review write path, so a contributor cannot set it.
`off` removes the key, so a deployment that never turned it on and one that turned
it back off read identically.

Every flip also appends one entry to a short **append-only history** in
`review-writes.audit.json` beside the deployment file, under an exclusive
deployment lock (kittrial-5bb.110 item 3). Each entry names who set the switch,
when, and the value it replaced; the history keeps the last 20 flips, and a flip
that does not change the value appends nothing instead of overwriting the record.
`review-writes status` prints `audit` (the last entry), `audit_history` and
`audit_agrees`, and **warns when the switch and the last recorded entry disagree**
- what an older kit, which does not know the audit file, or a hand edit leaves
behind. A **damaged** audit file - not JSON, not an object, an unknown schema, or
malformed entries - is reported the same way and reads `audit_agrees: false`; the
next flip does not silently overwrite it. Before writing the fresh history the flip
renames the damaged bytes aside, in the same directory, as
`review-writes.audit.json.damaged-<UTC date-time>` (`.N` on a collision) and says so
in its warning. The audit file is read through the same bounded-nesting JSON guard as
every other caller-written file, so a deeply nested one is refused cleanly instead of
crashing `status` with a `RecursionError`.

**Deployment order and rollback.** Deploy this kit with the switch off, verify the
reads (`review`, `brief`, `work`, `history`), and leave it off for as long as a
rollback to a pre-kittrial-5bb.94 kit must stay possible: no chain this kit writes
can then contain a shape that kit refuses. Once the rollback target is a kit that
reads the new shapes, run `review-writes on`. After that, rolling back below this
kit leaves every task that carries a new shape unreadable (the same fail-closed
hazard as the `assignee_at_approval` snapshot): reconcile with
`admin.py void-record` on each affected record, or stay forward. Turning the switch
off again does not remove records already written; it only stops new ones.

### Checkpoint provenance write switch

kittrial-5bb.1 lets a checkpoint record which activity it incorporated (per-entry
provenance) and lets the assignee record an explicit disposition for another actor's
direction: acknowledged, resolved or superseded. A kit built before that change does
not read those records, so writing them is staged like the review-workflow shapes:
every kit from `orchestra-06f7807-20261005i` on **reads** them, and writing them is
off until an operator turns it on. [BRIEFINGS.md](BRIEFINGS.md) describes what workers
see in each state; this section is the operator's procedure.

```sh
python3 admin.py checkpoint-provenance-writes status --actor OPERATOR   # current value (default: off)
python3 admin.py checkpoint-provenance-writes on  --actor OPERATOR      # allow the new records
python3 admin.py checkpoint-provenance-writes off --actor OPERATOR      # stop new ones (see one-way below)
```

`--actor` must be on the deployment operator allowlist. Every `on`/`off` appends who,
when, the previous and the new value to `checkpoint_provenance_audit` in
`deployment.private.json`, in the same atomic write as the switch. An `on` when it is
already on, or an `off` when it is already off, writes nothing and answers `changed:
false`, as `review-writes` does (kittrial-5bb.136; before, it appended an entry).

**One lock for every change to `deployment.private.json`.** Both switches,
`operators add|remove`, `verifiers add|remove`, `project-creations --set-server-limit`
and the `--restore-operators` /
`--restore-verifiers` merges of `restore-new` take the deployment lock
(`.review-writes.lock`) across their whole read-modify-write and re-read the file under
it, so two changes made at the same instant, from any two of these commands, never lose
one another (kittrial-5bb.131, kittrial-5bb.136). A change that finds the lock held
waits up to 10 seconds; these changes take milliseconds, so a holder that keeps it longer
is stuck, and the command then refuses with `Nothing was changed: another change to
deployment.private.json still holds its lock ...` instead of waiting for good. Run it
again; if it repeats, find the holder (`fuser RUNTIME/.review-writes.lock`). A killed
holder releases the lock with its process. Reads (`status`, `operators list`, every
endpoint read) never take the lock.

Two changes in behaviour come with this (kittrial-5bb.136, kittrial-5bb.142):

- **`review-writes on|off` now exits 1 after 10 seconds** with the refusal above when
  another change holds the lock. Before kittrial-5bb.136 it waited without bound. A
  script that ran it while another change could be stuck should check the exit status
  and run it again.
- **A kit before kittrial-5bb.136 takes no lock for `operators add|remove` or
  `verifiers add|remove`** (its switches do take it). While two kit versions write the
  same `deployment.private.json` (during an upgrade, or a second checkout pointed at the
  same runtime), a change made by the older kit at the same instant as another change
  can still be lost. Upgrade every kit that writes the file before relying on this, or
  make such changes from one kit at a time, and check `operators list` / `verifiers
  list` afterwards.
- **An older writer can fail outright.** A writer from a kit before kittrial-5bb.136
  (no lock) that is paused between writing its temporary copy and renaming it can have
  that copy removed by a newer writer's cleanup (below). Its rename then fails with a
  `FileNotFoundError` traceback and exit status 1. `deployment.private.json` stays valid
  and keeps the newer change; the older command's change is not made, so run it again
  (from the newer kit).

**Temporary copies.** Every change writes the new file to a temporary copy beside it
(`.deployment.private.json.XXXXXXXX`, created `0600`) and renames that copy over the
file. A writer killed between the two leaves the copy behind, and it is a full copy of
the configuration, Dolt password included. The next change that takes the lock removes
every such copy and says so on stderr (`Removed N temporary cop(ies) of
deployment.private.json left by an interrupted write: ...`); reads and refused changes
leave them. A kit before kittrial-5bb.142 never removes them: delete any by hand while
no change to the file is running.

**A damaged audit.** If the audit key has been hand-edited into something that is not
a list of entries of the shape the switch writes (`actor`, `at`, `action` on/off,
`previous` and `enabled` true/false, and nothing else), `status` still answers,
reading it as an empty history with a warning (`audit_readable: false`). The next
`on`/`off` that changes the value keeps the damaged value aside in the same file under
`checkpoint_provenance_audit_damaged_<UTC stamp>` (`_2`, `_3` on a collision, never
overwriting) and starts a fresh list, as `review-writes` keeps a damaged audit file
aside. A kept value stays in the file for good and is read and rewritten with it by
every later change; once you have looked at it (or copied it elsewhere), you may delete
a `checkpoint_provenance_audit_damaged_*` key from `deployment.private.json` by hand. Do
that while no change to the file is running, and keep the file's permissions (`0600`).

**Before turning it on, check:**

1. **Every installation that could become a rollback target, or that could receive a
   restored backup of a project from here, runs a reader kit** - any release from
   `orchestra-06f7807-20261005i` on. A project's checkpoint records travel in its
   native backup, so `restore-new` on an older kit would meet records it cannot read.
2. `checkpoint-provenance-writes status` on each installation reads `false` and
   `audit_readable: true` (or you have looked at the warning and accept a fresh
   history).
3. Workers are told (below), so a refusal or a direction that stays outstanding is
   not a surprise.

**Order across installations.** The switch is per installation; there is no global
one. Upgrade every installation to a reader kit first and leave all switches off;
verify `brief`, `work --mine` and `checkpoint TASK --directions` on each. Then turn
the switch on one installation at a time, starting with the one whose projects you
can most easily reconcile, and check one checkpoint there (`brief` shows its
provenance; a disposition is accepted) before the next. Never turn it on where a
project might later be restored onto a pre-reader kit.

**What changes for workers when it is on:**

* The assignee can acknowledge, resolve or supersede another actor's direction in a
  checkpoint's `directions`; they persist across later checkpoints.
* A plain checkpoint no longer clears outstanding directions: a direction stays on
  `brief` and `work --mine` until it is explicitly resolved or superseded.
  (With the switch off, the next checkpoint by anyone advances the legacy baseline.)
* A checkpoint by someone who is not the assignee no longer clears the assignee's
  directions or moves their cutoff.

**It is one-way per task.** Once a task holds a checkpoint record in the new shape,
turning the switch off again does not convert it: no kit writes a further checkpoint
on that task until the switch is back on (a legacy write is refused before mutation
rather than hiding outstanding directions), and a pre-reader kit still cannot read the
task. `off` only stops new tasks from starting to use the new records. Plan to stay on.

**If a rollback is needed after it is on.** Roll back only to a reader kit (any release
from `orchestra-06f7807-20261005i` on): it reads every record the switch let workers
write, and it honours the switch in `deployment.private.json`, so nothing needs
changing. Turning the switch off first does not prepare a deeper rollback: records
already written stay, and the tasks holding them refuse further checkpoints while it
is off (one-way, above). Going below a reader kit leaves those tasks unreadable there.
If that cannot be avoided, keep the rollback short, do not write checkpoints on those
tasks while it lasts, and return to a reader kit before relying on their briefs; there
is no command that converts the records back.

**Telling workers.** Set standing guidance on each project (`admin.py set-guidance
PROJECT --actor OPERATOR --file FILE`), which every worker reads at the start of a run,
with a short note such as: "Checkpoint directions are on: acknowledge or resolve other
people's instructions in your checkpoint's `directions` (see `checkpoint TASK
--directions`); a plain checkpoint no longer clears them." Point them at
[BRIEFINGS.md](BRIEFINGS.md) for the field shapes.

### Rollback of the release/liveness change

Rolling the deployment back below the kit that reads the additive `live` dimension
is **fail-open for liveness**: the older kit ignores the unknown `live` events
entirely and keeps reporting a task the environment was rolled back out of as
`deployed=passed` at its old release, with `deployed_delivery` still naming it,
until the kit is rolled forward. Its release selection likewise cannot tell that
the current environment no longer carries a task. Nothing is lost or rewritten (the
`live` events, the `lifecycle-scope` events and the deployed evidence all stay in
the native history), and no review chain is refused or needs `admin.py
void-record`: the dimension is additive for readers. The same limit applies to a
restore of pre-change data, which has no `live` facts at all. Roll forward to
recover the per-environment reading; do not "repair" it by deleting `live` events.

Validate a payload before writing. Each command is fail-closed and refuses before
any native write, and the same validator can be run with no native read or write
from the kit directory:

```sh
python3 -c "import json,sys,requirement_records as r; r.validate_payload(json.load(open(sys.argv[1])), operator=True)" record.json
python3 -c "import json,sys,requirement_records as r; r.validate_backfill(json.load(open(sys.argv[1])))" backfill.json
python3 -c "import json,sys,recovery as r; p=json.load(open(sys.argv[1])); r.validate(p,p['task'])" void.json
python3 -c "import json,sys,handoff as h; h.validate(json.load(open(sys.argv[1])))" handoff.json
```

The validators check payload shape only; each command rechecks native state
(record existence, revision ledger, key uniqueness, the allowlist) and refuses
before writing. There is no `--validate-only` flag today. The `requirement-apply`
line uses the operator form because the contributor form (default
`operator=False`) refuses an operator payload that accepts a first revision
(`operation: "draft"` with `acceptance_state: "accepted"`), while `operator=True`
additionally runs the F3 acceptance check.

Requirement records created before `requirement_records.py` (for example by
`create-child`) can be untyped, or typed `draft` although accepted; label them
with `requirement-backfill`. An accepted backfill needs an `evidence` pointer and
is refused when the newest revision comment says `draft`; accept those through
`requirement-apply` with F3 evidence, which writes a new accepted revision. A
record with no revision comments at all can instead get an accepted revision 1
via `operation: "draft"` with `acceptance_state: "accepted"` and an `acceptance`
object (operator only), so the revision number can match a published manifest.

## Backups

Run after meaningful work and before maintenance:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup example
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup example second
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup --all
```

This invokes Beads' native Dolt backup into `runtime/backups/example`. The native step is Dolt's own backup synchronization through the SQL client (`CALL DOLT_BACKUP('sync', ...)` over the loopback connection, bounded by an explicit 30-minute ceiling), not `bd backup sync`, whose fixed client read timeout of about ten seconds a large database cannot meet on a busy or stalled server; a project that carries no Dolt server metadata has no SQL coordinates and keeps the `bd backup sync` path. The password travels in the environment, never on a command line. One or more project names, or `--all` for every initialized project of the runtime, run in a single invocation; each project is backed up exactly as the single-project form does, one failing project does not stop the others, and the command exits non-zero when any targeted project's backup pair is not complete. The sync client runs in its own session/process group. A normal `SIGTERM` or Ctrl-C is turned into an error inside the critical section, so the kit runs its cleanup and then stops that whole process group before the backup lock is released: the `dolt` client and anything it spawned cannot keep writing `backups/PROJECT` after the lock is gone, the previous complete pair is kept, and the run exits non-zero. A `kill -9` (or OOM kill) of `admin.py` itself can run no cleanup at all, so that client can still finish writing `backups/PROJECT` after the lock is released — re-run `backup` for that project before relying on that generation, and prefer a normal stop. Same-host backups protect against some mistakes, not loss of the server. Arrange an ordinary scheduled, encrypted off-machine copy of completed backups using your existing backup system; `backup-copy` below is the kit's reference for the pair selection. The kit does not install a backup timer. Preserve the kit/version pins and a protected copy of deployment configuration separately; JSONL views are useful exports but are not a substitute for the native backup.

Restore drills deliberately create a new project:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime restore-new example examplerestore
```

It refuses a populated destination, restores status and comments, and retains original issue IDs. The native restore is Dolt's own restore through the SQL client (`CALL DOLT_BACKUP('restore', '--force', 'file://.../backups/SOURCE', 'DEST')` over the loopback connection, bounded by the same explicit 30-minute ceiling as `backup`), not `bd backup restore`, which has the same fixed client read timeout of about ten seconds as `bd backup sync`: a 588 MB backup failed under `bd backup restore` at exactly 10 s (`i/o timeout`, `invalid connection`) and takes about a minute through the SQL client. The client runs in its own session/process group with the password in the environment, never on a command line; a normal `SIGTERM`, Ctrl-C or the ceiling stops that whole process group before the source's backup lock is released. Because bd does not run the restore, the kit then performs the one step `bd backup restore --force` would have performed itself: it writes the restored database's `_project_id` into the destination's `.beads/metadata.json` (every other key is kept), so bd accepts the restored project instead of refusing it with `PROJECT IDENTITY MISMATCH`; a backup that records no project identity leaves the file unchanged, as bd does. The command prints `Restored backups/SOURCE into DEST through the Dolt SQL client in N s.` and, when the identity changed, the identity it adopted. A destination with no Dolt server metadata in `.beads/metadata.json` has no SQL coordinates and keeps the `bd backup restore` path, as `backup` keeps `bd backup sync`. The native restore also carries the source project's recorded backup target, so `.beads/dolt-backup.json` and the restored `dolt_backups` row would still name `backups/SOURCE`; `restore-new` re-points the destination at `backups/DEST` immediately after the restore, and `backup` refuses (before any native command) a project whose recorded target is not its own `backups/<name>`, naming the recorded URL and the expected directory, so a clone that was never re-pointed cannot overwrite the source project's backup directory. A clone restored before that re-point existed (or whose re-point failed) is repaired in place with `admin.py backup-repoint DEST`, which runs `bd backup init backups/DEST` under the kit environment and verifies both `.beads/dolt-backup.json` and the `dolt_backups` row before reporting success; do not run a bare `bd backup init` (it fails without the kit environment, `Error 1045 Access denied for user root`) and do not re-run `restore-new` onto an existing name (it is refused and would discard the clone). Re-pointing moves no data: if the clone was ever backed up while still mis-pointed, the SOURCE project's `backups/SOURCE` may hold the clone's data, so back the SOURCE project up again before relying on that backup. Inspect records and comments before any cutover. Checkpoint activity cursors name the project they were taken in, and `restore-new` gives the project a new name. From kittrial-5bb.131 a kit compares a retained checkpoint's cursor under the project it names, so a restored task whose content has not changed reads as current, as it already did on `work --mine`, and real activity after the restore is still reported as newer activity. Because current no longer prompts for it, `brief` says where such a checkpoint was taken (kittrial-5bb.136): `checkpoint.taken_in_project` names the source project, the text form adds `Checkpoint taken in project: NAME`, and the next action starts with `CHECKPOINT FROM PROJECT NAME:` asking you to confirm the history before relying on it. The name comes from text a checkpoint writer supplied, so it is shown only when it is a project name (2-24 lowercase letters and digits, beginning with a letter); anything else shows as `(unrecognised project name)` (kittrial-5bb.142). A kit before kittrial-5bb.131 makes every retained checkpoint read as newer activity after a rename (STALE CHECKPOINT in `brief`), even when native task bytes are unchanged; that is a scope change, not proof of a new comment. On such a kit, reconcile the restored history and write a fresh checkpoint under the destination name before trusting its next action. Cursors are written the same way by every kit, so mixed kits read each other's checkpoints. Do not run both copies as live coordination trackers. A complete host-loss recovery requires reinstalling the pinned kit on a replacement host, placing the saved backup and its coordination sidecar under its backups directory (with timestamps preserved, see below), using restore-new, verifying it, then updating client project/host settings. If `restore-new` refuses because no copy of the coordination sidecar can be used, first look for an intact copy of the pair, for example in your off-machine copies. Run `backup-authority SOURCE` to see what each copy says. Use `--without-coordination` only when the native tracker data alone is acceptable, then reconcile sessions, handoffs, requests and merge context by hand. No manual restore through the SQL client or edit of `.beads/metadata.json` is needed at any project size: `restore-new` does both. Re-grant operators and verifiers deliberately afterwards (the restore reports them as NOT restored), and take a fresh `backup` of the restored project before relying on it. A separate-deployment drill exercises backup transfer; actual replacement-host outage recovery remains an operator exercise.

### Coordination journals and interrupted recovery

Session registrations in `.sessions.json` are included in the coordination sidecar, alongside the private project onboarding entry point. Restore with a kit version supporting these records. Preserve original actor IDs for resumed workers; do not create a second live authority from a restored registry.

**Record journals and the shared slice 0.** The reference catalog, requirements-gathering and capability-index designs each keep an idempotency journal: `.reference-requests/`, `.proposal-requests/` and `.capability-requests/`. This kit writes none of them, but it treats them as a tolerant reader:
- **Backup:** it backs them up when they exist.
- **Validation:** it validates every receipt with the frozen schema (a 64-hex `sha256`, a known `status`, and optional `actor`, `id`, `revision` and `acceptance`; unknown keys are kept). A malformed receipt refuses the backup before the native sync, and refuses the restore before its first write.
- **Restore:** it restores them with the project.

Restore compatibility, exactly:
- **Backups containing the journals:** a backup with any of the three journal paths restores only with `admin.py` at this kit or later. It can run from a checkout.
- **Older kits:** any kit older than this one refuses the whole restore of such a backup, before writing anything.
- **Older backups:** this kit restores backups made by older kits normally. Those backups carry none of the journals, so at most idempotency receipts are missing.

This kit is therefore the oldest one a deployment may roll back to once any of those records exist.

**Open items, owner questions and decisions: slice 0 (kittrial-5bb.126).** This release adds the names of `docs/OPEN_ITEMS_DECISIONS_DESIGN.md` and nothing that writes them. It changes behaviour in exactly two ways:
- **Raw record comments:** a raw `bd comments add` whose body starts `Kind: open-item-v`, `Kind: item-resolution-v`, `Kind: owner-answer-v` or `Kind: coordinator-decision-v` (any version) is refused, like every other record prefix. Such comments, if any exist, are hidden from the shared surfaces; an item anchor (a row labelled `open-item` that carries an item, resolution or answer record) is hidden; a decision issue stays visible and only its record comment is hidden.
- **Labels:** every `open-item:` label is reserved: no contributor adds, removes or replaces one on any row. The exact label `open-item` stays an ordinary label.

It also names two journals, `.open-item-requests/` (a receipt journal, validated and counted as a reservation like the three above) and `.owner-answers/` (the host-issued journal, validated by its own entry validator like `.integration-reverts/`, with the fixed entry shape of the design's §11.2.1). No kit writes either yet, and a backup collects each only when its directory exists, so a backup taken by this kit is restorable by the previous kit exactly as before.

Rollback and restore, exactly:
- **Older kits cannot restore such a backup at all.** Once a backup carries either journal, every kit before this one:
  - refuses its `restore-new` with `ValueError: Invalid coordination backup path`, and leaves nothing behind;
  - refuses `restore-new --without-coordination` of it too, so not even the native data can be restored with an older kit;
  - takes its own backup of a project that has the journals without error and **silently leaves both journals out**, so that backup no longer carries them.

  The way out is to restore with this kit or a newer one (`admin.py` from a checkout is enough). Nothing creates either journal until open-item writes are turned on in a later release, so with writes off none of this can arise.
- **This kit:** a malformed `.owner-answers/` entry or `.open-item-requests/` receipt refuses the backup before the native sync and the restore before its first write.
- **The four kinds are not void targets yet:** `void-record` refuses them as an unsupported target kind, as before, until the release with their reader.

At deploy time, list the projects that already use an `open-item` label: `python3 admin.py --root RUNTIME open-item-label-check [PROJECT...]`. It reads each initialized project once (no lock, no write), prints JSON naming every row carrying `open-item` or an `open-item:` label, and exits 1 when any project uses one or could not be read. A project whose `.beads/metadata.json` does not record the Dolt server coordinates is reported unreadable and bd is not run for it: bd would otherwise create an embedded database inside the project and list nothing. A project that does must choose another label for its own use before open-item writes are turned on for it; the later release that adds the switch refuses it.

**Requirement proposals: triage runs on the host.** A contributor submits a proposal through the client (`proposal submit`). Everything that rests on operator authority is a host command, because over SSH the actor is self-declared: `proposal-review`, `proposal-decide` and `proposal-settings` above. A stored settings record counts only when its native author is on the operator allowlist, and a stored disposition when its author is on the allowlist or is an HTTP account (the web service wrote it for a member with `reviews.approve`, see [HTTP deployment](HTTP_DEPLOYMENT.md#requirement-proposals)); removing an operator makes their dispositions inert (the proposal reads its earlier state) and re-adding them restores it.

- **Session-start step: map the coordinator before its first disposition.** The no-self rules compare people, not actor strings, so an unmapped actor is refused with "map this actor first". A new coordinator session brings a new `session-<uuid>` actor: after it registers, run `admin.py proposal-settings PROJECT --actor OPERATOR --map-actor session-<uuid> --to person:<name>`. To map a person once for all their sessions, use `--namespace <name> --to person:<name>`: it matches every session registered under `<name>`, `<name>/...` or `<name>-...` (see the name with `session show ACTOR`). A host operator who has no session, such as `james`, is matched by a namespace equal to that actor name.
- **An owner decision needs two people.** The decider must be a different person than the coordinator who escalated, and neither may be the proposal's submitter. A project with one allowlisted operator cannot both escalate and decide over SSH.
- **Deciders.** `--add-decider IDENTITY` lists who an escalation may name. With none configured, an escalation may name any durable identity.
- **Incorporating.** After the owner approves, draft the requirement revision through the ordinary `requirement` action, accept it with `requirement-apply` when it is ready, then record `incorporated` with `proposal-review`. A draft incorporation is allowed; `work` counts it as `incorporated_unaccepted` until the requirement is accepted.
- **Attribution is not authentication.** `identity: verified` on a proposal means the native author of every revision maps to its `person:` submitter, or wrote it through the web service as its `account:` submitter. Nothing is authorized by it. Only the submitter revises: an actor that does not resolve to a `person:` submitter is refused, whatever `submitter` its payload repeats; an unmapped contributor can still revise a proposal whose every revision they wrote. A proposal with an `account:` submitter is written and revised only through the web service.
- **`proposal mine --submitter IDENTITY` is a declared query over SSH**: it lists every proposal that names the identity, verified or not, and each item says which. On the web, "my contributions" lists verified proposals only.
- **An operator with a web account** maps the operator actor to the account: `proposal-settings PROJECT --actor OPERATOR --namespace OPERATOR --to account:usr_<id>`. Otherwise the no-self rules see two people. The mapping does nothing else: the operator actor still cannot revise the account's proposals or submit proposals that read as the account's. A namespace shaped like an account or agent id (`usr_...`, `agent_...`) is refused, and `operators add` refuses such an id.
- **Coordinator text is readable over SSH.** A reason, a question and an escalation question are filtered on `proposal get` and `list` by the declared actor, and `proposal mine --submitter IDENTITY` returns them to whoever names that identity. Both are self-declared over SSH, so anyone with endpoint access can read them. Do not put secrets in them.
- **Nobody reviews their own proposal.** The check compares the reviewer with the declared submitter and with the actor that wrote revision 1, so submitting under another person's name does not open a self-review.
- **Only a real settings record makes a settings anchor.** A task that merely carries the label `contribution-settings` is ignored by every reader and by `proposal-settings`.
- **Incorporating against a draft that was accepted later.** Link the revision the proposal landed in. If that content has since been accepted, record `acceptance_state: accepted` with the `acceptance_decision_id` of the revision that accepted it. If a later revision changed the content, the link reads `draft` and `work` counts it as `incorporated_unaccepted`: link the newer revision in a new proposal's incorporation instead.
- **Removing an operator reverses what they recorded.** `operators remove` (below) prints, before it acts, the proposals whose state changes and the projects whose contribution settings change.
  - A proposal the removed operator decided reads its earlier trusted state again, and no new disposition can be recorded on it while the ledger and the trusted state disagree.
  - If the removed operator wrote the first settings record, every later record in that chain stops counting: the actor map and the deciders read empty and triage stops. Enter them again with `proposal-settings`; the new record starts a fresh chain on the same anchor.
  - Re-adding the operator restores both. There is no other repair yet: `void-record` accepts reference and capability records (kittrial-5bb.74), but not proposal or settings records. Until then the submitter can submit a new proposal that supersedes the stuck one.
- **After a restore,** re-check `operators list` and `proposal-settings` before recording anything: both the allowlist and the actor map decide what counts.

**Acceptance evidence trusts the comment's native author.** A reference entry reads
accepted when its acceptance evidence comment was written by an actor on the operator
allowlist. Over SSH a comment's author is the actor the caller declared, so what stops
a contributor writing such a comment is the reserved-prefix guard (the shared slice 0),
not the author field.

- **Residual risk.** A `Kind: reference-acceptance-v1` comment written on a kit older
  than the shared slice 0, or during a rollback below it, under an operator's actor
  name, would read as a real acceptance. The same applies to the other record kinds.
- **Why acceptance is not bound to the host receipt.** Reading an accepted entry must
  not depend on a local journal: a restore may lose `.reference-requests/`, and the
  evidence comment has to remain the authority.
- **Before the first use of the catalog on an installation,** and after any rollback
  below the shared slice 0, scan each project for record comments that no writer of
  this kit made. Save `bd export --all` for the project as `export.jsonl`, then run:

  ```sh
  python3 -c "import json,sys;K=('Kind: reference-','Kind: requirement-proposal-','Kind: proposal-disposition-','Kind: contribution-settings-','Kind: capability-');[print(r['id'],c.get('id'),c.get('author'),c['text'].splitlines()[0]) for r in map(json.loads,sys.stdin) for c in r.get('comments') or [] if isinstance(c.get('text'),str) and c['text'].startswith(K)]" < export.jsonl
  ```

  It prints nothing on a project that has never used the catalog. Any line it prints
  on such a project is a comment to investigate before you rely on the catalog. A
  malformed one can be voided ([reference and capability records](#reference-and-capability-records)).
  A well-formed one reads like a real record, so `void-record` refuses it: supersede it
  with a newer accepted revision, or retire the key.
  The coordinator ran this scan on all seven live projects on 2026-10-02 and found none.

A row is hidden as a record anchor only when it carries one of the labels `reference`, `proposal`, `contribution-settings` or `capability` **and** a v1 record comment of the same family. A project's own task that merely uses one of those words as a label (for example jjbp-j03.20's `proposal`) stays visible and editable. The endpoint also refuses a raw `bd close`, `bd reopen` or `bd update --status` aimed at a record anchor (kittrial-5bb.92): an anchor's status belongs to its record operations, and a status change through the raw path is refused before the native write.

**The `verifiers` list.** The deployment configuration may carry a `verifiers` list beside `operators`. It is a second, narrow deployment-wide authority: an actor on it may run `admin.py capability-verify`, and readers then count that actor's capability checks as `verified`. It grants nothing else. Keep it empty unless a host-side verifier other than an operator exists; the coordinator's integration step verifies as an operator.

- **One authority source.** `deployment.private.json` is the only source. A shell `ORCHESTRA_VERIFIERS` that disagrees with the file is refused by `verifiers add`, `verifiers remove` and `capability-verify`; it is never read as authority. Entries are actor identities (letters, digits, `_`, `.`, `-`), so an `account:` value can never be a verifier.
- **Revocation.** `verifiers remove ACTOR` requires `--confirm-revoke`. Afterwards every check that actor recorded reads `reported`, and drift that only their passes had cleared reappears. Nothing is deleted: re-adding the actor restores the reading. An actor who is also an operator stays trusted.
- **Backup and restore.** Each project's coordination sidecar records the list beside `operators`, for information. `restore-new` never re-grants it on its own. When the backup records verifiers this host does not list, the restore prints them, says they were **NOT** restored, and their checks read `reported`. Re-grant one with `verifiers add ACTOR`, or pass `--restore-verifiers` to re-establish the whole recorded list. `--restore-operators` does not re-grant verifiers.
- **One caller can fill the failing-report pool.** Every contributor posting through the endpoint is `unverified` until SSH actors are bound to people (kittrial-5bb.68), so they share one pool of 5 open failing reports per project. One caller can fill it, and then every other contributor's failing report is refused, naming the cap, until those failures are cleared. To clear them, an operator or listed verifier records a passing check at an integrated commit for each drifted capability (`capability check --payloads`, then `capability-verify`); `capability list` shows which ones read `drifted`. If the reports are noise, that pass is still the way to clear them, because a report is never deleted.
- **Integration step.** At the integrated commit, in a clean checkout: `capability check --repo . --payloads payloads.json`, then on the coordination host `admin.py capability-verify PROJECT --actor OPERATOR --file payloads.json`. The payloads file is created private (mode 0600) and is never written through a symbolic link. Drift clears only on such a verified pass at a commit the project's lifecycle evidence records as integrated; a reverted integration does not count. **The payload file is generated from the records, not kept by hand:** with no `--key` the client writes one payload for every accepted or draft capability the endpoint holds **whose check the checkout can decide** — a record whose pointers are all `unknown` gets none and reads `not-recordable` — so a record added since the last release is covered with no edit. The release is not finished until `capability-verify` passes for every accepted entry; a failing **draft** does not fail the release, but it is listed in the release record with its key, the pointers that did not resolve and an owner. A delivery carries its capability proposal payload as a file named in its contribution summary, and the coordinator writes it into the index only after the delivery is integrated (kittrial-5bb.179).
- **Rolling back below this kit.** An older kit keeps every record, hides them as before and simply reports no `verification`. It never rewrites or removes `views/CAPABILITIES.md`, so after a rollback delete `projects/PROJECT/views/CAPABILITIES.md` by hand; otherwise the last page rendered stays readable through `view` with its old export stamp.

`add-project` initializes the project, provisions its merge slot (idempotently) and performs an initial backup, so a freshly provisioned project can run `merge-create`/`merge-check`/`merge-acquire` without a manual slot setup. A project whose slot is missing refuses `merge-check`/`merge-acquire`/`merge-release` with an error naming the `merge-create` operation, which is the manual repair. `backup` captures both the native backup directory and `backups/PROJECT.coordination.json`. The sidecar preserves pending child-request reservations and merge context outside Dolt. Keep this pair together. A pending marker is written before synchronization and becomes complete only after native sync succeeds; restore refuses an incomplete sidecar.

**A project without a healthy merge slot (kittrial-5bb.202).** Three ways of making a project can leave it without one: a plain `bd init` (a tracker the kit did not provision), a `restore-new` whose source database predates the slot (the native restore replaces the clone's rows), and a project made before the slot was provisioned when `merge-create` was never run. The kit closes the ones it controls: `add-project`, `finish-project` and the web creation path provision the slot, and `restore-new` provisions it idempotently after the native restore and after the coordination sidecar and the operation journal are restored, so a failure of that step leaves only the slot to mend (kittrial-5bb.202 rev-3). For the rest, the read-only `admin.py --root RUNTIME merge-slot-report` lists every initialized project whose slot is missing, damaged or cannot be read, and the web set-up page lists a missing or damaged slot as a step. Both name the `merge-create` coordination operation, which is the repair. What issuing a worker credential answers depends on the case, exactly: a **missing** slot -- rows that came back without the slot row, or a readable tracker with **no rows at all** (a plain `bd init`, whose `bd export --all` exits 0 and prints nothing) -- answers 503 `merge_slot_missing` and names `merge-create`, never the transient sentence; a **damaged** slot is not a refusal at all (the row is there, so issuing a credential still answers 201, as on main, and the report and the set-up step list it so `merge-create` can repair it); and a tracker that **cannot be read** (its database dropped, its Dolt server stopped, `bd` failing, or no server coordinates recorded) keeps the transient 503 `unavailable`, "try again shortly", which a retry can mend.

**A sidecar that exists but cannot be used (kittrial-5bb.152).** `restore-new` decides from the coordination sidecar before it creates anything:

- **A copy is usable** (the canonical sidecar, else the last-complete copy described next): it restores from it.
- **No copy exists at all** (a legacy backup): it restores exactly as before. It says outstanding requests and merge context need reconciling.
- **A copy exists but none can be used:** it refuses with exit status 1 and `Incomplete coordination backup: no copy of the coordination sidecar of backup SOURCE can be used (...)`. Each copy is named with why it cannot be used: pending, not JSON, not a schema-1 sidecar, a directory, a FIFO or device, unreadable, or a symlink. This includes the case where the canonical sidecar is missing and only a damaged last-complete copy remains. Before kittrial-5bb.152 that case was restored as legacy: exit 0, with no sessions, handoffs, requests, merge context or recorded operators, and nothing said so.
- **`--without-coordination`** restores such a backup's native tracker data alone, as the refusal suggests. Its operation-journal snapshot is restored as before when there is one.
  - Its last lines say what was NOT restored: the coordination records, and the operators and verifiers the backup records (never compared or re-granted). They also say whether the operation journal was restored.
  - The flag is refused for a backup whose sidecar is usable (it would drop restorable data).
  - It cannot be combined with `--restore-operators` or `--restore-verifiers`.
  - On a truly legacy backup (no sidecar copy at all) it changes nothing, and its last line says so: there was nothing to leave out (kittrial-5bb.157).

**A directory where a sidecar copy belongs (kittrial-5bb.157).** `backup` refuses before it writes anything, and leaves the directory and the pair as they are. This applies to `backups/PROJECT.coordination.json` and `backups/PROJECT.coordination.last-complete.json` alike. The message names the directory and says to move it out of `backups/` and run `backup` again. It is not a file this kit writes, so the kit never replaces or deletes it: it may hold something an operator put there. Before kittrial-5bb.157 the run ended with a raw `Is a directory` error.

`backup-authority` reaches the same answer, refusal or legacy outcome as `restore-new` for every combination of the two copies. The last complete sidecar is also kept as `backups/PROJECT.coordination.last-complete.json` before that marker replaces it, and is put back if the run fails or is interrupted, so one failed run never destroys the previous restorable pair; `restore-new` serves the canonical sidecar when it is complete and falls back to that durable copy when it is not. The operation-journal snapshot (`backups/PROJECT.http-operations.sqlite3`) is staged during the run and promoted only after the native sync and the new complete sidecar are durable, so the snapshot a restore replays always belongs to the same generation as the Dolt native backup and the complete sidecar: a run that fails after an acknowledged write leaves the previous snapshot in place, and the un-synced operation is not replayed from a restored journal. When `restore-new` cannot use the canonical sidecar it prints that it is using the durable last-complete copy and that the restored journal snapshot belongs to that generation; if a promotion after a successful sync fails, the new complete sidecar is kept rather than rolled back beside the new native directory. The complete sidecar also records a stat-only manifest (relative path, size and mtime) of the native backup directory as that generation finished, and `restore-new` recomputes it before any coordination or journal write: when the directory no longer matches, it prints a loud WARNING that the restored Dolt may hold effects whose receipts the restored journal does not have (an interrupted or killed run can leave the native directory partly rewritten while the restore serves the previous complete pair) and to take a fresh backup before relying on that pair. A sidecar written before the manifest existed records none, and the restore says the check could not be performed instead of calling the pair clean. Backup and restore serialize access to the pair, and backup excludes contributor writes through the endpoint. Direct operator/native writes bypass these locks and must be paused for backup. The completed sidecar also records the deployment operator allowlist, so a restore can report recorded authority the destination host does not list. `restore-new` does **not** apply it: re-granting an operator is deployment-wide authority and stays an explicit decision (`--restore-operators`, or `operators add OPERATOR`), so a stale backup cannot silently reverse a revocation. See [Operator removal and restore policy](#operator-removal-and-restore-policy).

Copy a completed, quiescent backup pair off-machine using your normal encrypted backup system. Do not copy it during the next sync. Preserve the pair together with its `backups/PROJECT.http-operations.sqlite3` journal snapshot, so a restored project's acknowledged operations are not re-executed. Preserve file timestamps on the way out and on the way back (`cp -a`, `rsync -a`, or an archive that keeps mtimes): the complete sidecar's stat-only manifest keys on each file's `mtime_ns`, so a copy-back that resets timestamps makes `restore-new` warn that the native directory no longer matches its generation even though the bytes are intact. This is not an atomic transaction across arbitrary filesystem copies; take a filesystem snapshot or hold the project's `backups/PROJECT.lock` while copying (the `backup-copy` helper below does this for you). Legacy backups without a sidecar warn that outstanding requests/merge context require reconciliation.

Every run of `backup` also writes `runtime/backups/backup-status.json`: a schema-versioned, sorted-key record of the run with one entry per project, naming whether that project's pair (its `backups/PROJECT` native directory plus its `complete` `backups/PROJECT.coordination.json` sidecar) is complete, the UTC time it completed, and the reason when it is not. The per-project pair state is re-read from the files, so a run that fails or is skipped — and even a "successful" call whose sidecar is still `pending` — is recorded as **not** complete rather than assumed complete. A named or single-project run updates its own entries and carries the other projects' last known state forward instead of erasing it; `generated_at` and `scope` always describe that run, so a carried-forward entry is never presented as its result. Record scope is explicit: `"scope":"all"` means the run covered every project initialized at that moment, `"named"` means it covered only the projects listed. A named run refuses a name that is not an initialized project before it backs up or records anything (`backup NOSUCHPROJECT` leaves the record unchanged), and no run carries forward an entry for a name that is not an initialized project, so an entry an older kit recorded for a mistyped name is dropped by the next run.

Read that record on the host:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-status
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-status --require-complete
```

`--require-clean` is the stricter gate: everything `--require-complete` checks, and it also exits non-zero when any project is recorded `degraded` (complete, but missing something it should carry, today a guidance pair), naming each. Use it before a release; keep `--require-complete` for the timer and the daily copy, which a degraded project must not fail. `backup-copy DEST --require-clean` refuses in the same case; without the option `backup-copy` copies a degraded project and names it.

`--require-complete` exits non-zero unless the last run used `--all` and every project initialized in the runtime is listed as complete with its pair still complete on disk, naming each project that is missing, not complete in the record, absent from the record (a project added after the last run), or no longer complete on disk. Use it as the gate in front of the off-machine copy, so a project that is absent from the schedule or whose pair is half written is noticed instead of being silently omitted.

The kit also ships a generic reference helper for the copy itself:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-copy /srv/off-machine/beads-runtime
```

`backup-copy DEST` applies exactly the `--require-complete` gate and refuses, naming what is missing, when any initialized project is not complete. It then copies each project's last complete pair — the native backup directory as `DEST/PROJECT`, the complete sidecar as `DEST/PROJECT.coordination.json`, and, when the project has one, its operation-journal snapshot as `DEST/PROJECT.http-operations.sqlite3` (the same shape a runtime's `backups/` directory has, so the copy can be copied back and restored) — and copies the record that gated the copy. Each project's `backups/PROJECT.lock` is held for that project's whole staging, while its coordination lock is held only long enough to re-check the pair and copy the small sidecar and journal snapshot; the long native copy runs after the coordination lock is released, so copying a large database does not block endpoint writes on it (both commands take the two locks in the same order, so no deadlock is possible). Files are staged in a per-run directory and then moved into place by rename rather than merged, so a concurrent `backup --all` cannot yield a mixed native directory beside a complete sidecar and stale destination files do not survive. Each project's native directory is also checked against the `mtime_ns`-keyed manifest its complete sidecar records, before and after that project's copy: a directory that no longer matches — a killed or interrupted run can leave it partly rewritten — is refused rather than copied, a directory that changes while it is being copied is refused, and a sidecar that records no manifest is reported as unverifiable instead of being called clean. The completeness record is published only after every project is in place: a failure exits non-zero with a clear error, removes the staging directory and leaves no completeness record, so the destination never looks complete when it is not. It prints exactly what it copied, is credential-free and never touches a unit, timer or schedule. The scheduled, encrypted off-machine system, its encryption, retention and destination, remain the operator's: this helper is the reference the operator can gate and schedule, not a replacement for that system, and it cannot see whether a copy actually left the host.

If restore is interrupted, the new destination may exist with only part of the restore completed. When the native step of `restore-new` fails, reaches its ceiling or is stopped (`SIGTERM`/Ctrl-C), the kit stops the restore client's process group, prints `restore-new did not complete: ...` naming the cause and the partial project, and exits non-zero without the re-point, the coordination sidecar, the journals or the operation journal: the destination then exists with an initialized but partial database (its `.beads/metadata.json` still holds its own fresh identity, so bd may refuse it). A `kill -9` of `admin.py` itself runs no cleanup, so the client may still be writing the destination's database for a while. Preserve it for inspection; retry recovery into another unused destination. Do not delete the source or force reuse of the partially restored target. While a destination left by a stopped or timed-out restore remains in the runtime, a `backup` of it fails (`backup 'default' not found`), so expect `backup --all`, `backup-status --require-complete` and `backup-copy` to report the runtime incomplete until it is dealt with; the source project's own backup pair is not touched. Retire such a project with `retire-project` ([Retiring a project](#retiring-a-project)); recovery drills are still better run in a separate runtime than in a live one. When the native step fails on a damaged backup or a refused password instead, the destination is an empty, working project rather than a partial one, and the notice says so; retiring it needs no flag. When the restore stops before `add-project` initialized the destination, the notice says the directory exists but is not a working project. A `SIGTERM` during the identity step that follows the native restore is reported like a stop during the restore itself. Every way the restore can stop after the destination starts to exist prints the notice, naming the step: a failure, `SIGTERM` or Ctrl-C in the `add-project` step, in the native restore, or in the re-point and coordination step. If the destination's directory has disappeared by then, the notice says that instead. Ctrl-C ends with exit status 130, and a refusal ends with one line (`ValueError: ...`), not a traceback. Only the kit's own refusals are shortened: any other error keeps its traceback. A `deployment.private.json` or `--file` payload that is not valid JSON is a refusal that names the file. Verify pending reservations, comments, lifecycle events, baselines and slot context before switching clients.

### The cost of many projects on one server

With pinned bd 1.2.2 and Dolt 2.2.0, catalog checks on writes get slower as the
server holds more project databases. The [measured investigation](../reports/BD_DATABASE_SCALING.md)
includes actual bd SQL, repeated synthetic timings, fixture limits and raw samples.
At 1, 20 and 50 databases the native create medians were 0.338, 0.909 and 2.126 s;
native list/show stayed near 0.2 s for the measured issue history. These values are
not guarantees for other schemas, concurrent loads or arbitrary read histories.

- **Captured cause.** Each measured native write and merge-slot command issued two
  filtered `INFORMATION_SCHEMA.COLUMNS` checks. Pinned add-project samples issued
  73 such checks, in addition to other SQL. The count is captured, not inferred.
  The predicates name the selected schema, but pinned Dolt still builds the catalog
  across databases. See the report for elapsed query times and exact sources.
- **Keep the current pin.** Beads 1.3.1 replaces two cursor-column probes but
  adds TABLES probes and expands migrated schemas. The available evidence does
  not show a cost reduction; add-project was about 1.6 times slower. The kit also
  fails compatibility checks with 1.3.1. Migration prevents 1.2.2 from using that
  database, reads included, and has no data downgrade. See the report for the
  measurements, independent review findings and upgrade requirements.
- **Names and the limit.** `admin.py project-creations --usage` and the operator
  setup-status view report `project_databases` used/limit. The default limit is 20;
  a listed operator can record another value with
  `admin.py project-creations --set-server-limit N --actor OPERATOR`.
  The count comes from names under `projects/`, retired directories and creations
  holding names, including damaged records. It is not a server-catalog query;
  an unrepresented database can be missed, while a reservation may precede one.
- **Enforcement.** The cap stops web creation only. Operator `add-project` remains
  permitted above it.
  Archived/retired records do not remove the database or its catalog cost. The kit
  never drops a database. Twelve visible projects can therefore exhaust 20 names.
- **Creation time.** Pinned add-project took about 7 s with one existing database,
  23 to 26 s at 20 and 62 s at 50 in the synthetic measurements. One creation
  runs at a time on a server, so at 20 to 50 databases it can keep every other
  creation refused as busy for about 25 to 60 seconds, longer as the server fills.
  Web creation can take up to the service's `--create-timeout` (900 seconds).
- **Planning.** Retain 20 for these pins: measured create at 20 is about 2.7 times
  the one-database cost. Compare the filesystem count with an operator-authorized
  catalog count and passive operation timings; count errors are not zero usage.
- **Release and backup.** The report's 25-target synthetic releases and backups at
  50/100 databases are single observations, not a repeated curve or deployment
  guarantee. Backup sends no catalog checks; its growth follows project count.

### Retiring a project

`admin.py retire-project PROJECT --actor OPERATOR --reason TEXT` takes a project out of the runtime without deleting anything. It is for a project a stopped `restore-new` left behind, or a drill project you no longer want backed up. It is also how an operator removes a project creation that `add-project` or the web interface started and that stopped half way (with `--force` when the half-made project cannot be read): the creation record is then marked removed, the creator's place is free again, and the name stays retired.

What it does:
- It moves `projects/PROJECT` to `retired/PROJECT-<UTC stamp>` with one rename, under the project's backup lock and coordination lock.
- The project is then not initialized. `backup --all`, `backup-status --require-complete` and `backup-copy` stop counting it, and the endpoint answers "Unknown/uninitialized project" for it.
- `backups/PROJECT`, every other project's backups and the Dolt database are not touched. To undo, move the directory back.
- Both steps are written to `retired/journal.jsonl`: the intent before the move and the result after it, each with the actor, the reason, what the checks found and whether `--force` was used.
- **What holds a retired name is the directory, not the journal.** A name is refused for a new project (`add-project`, `restore-new`, a creation from the web interface) because `retired/NAME-<UTC stamp>` exists; that list of directories is the only thing read. `retired/journal.jsonl` is an audit trail for people: the kit appends to it and never reads it, so a broken or missing journal unprotects no name and stops nothing. Do not rename or remove a `retired/NAME-<UTC stamp>` directory: its database is still on the server, and without the directory the name could be used again and would adopt it.

`--force` is needed when the project looks like a working tracker, holds the merge slot, or has pending reservations. The checks fail closed: what cannot be read is treated as the dangerous answer. The refusal names what it found:
- **It looks like a working tracker:** bd reads the project and it holds issues, or metadata is absent while the same-named server database holds issues. The state of its backups does not matter; a healthy project whose last backup failed is still a healthy project.
- **Nothing could be checked:** the Dolt server (or bd itself) could not be reached, or the server did not answer the probe within 15 seconds (a frozen server does not make the command hang with its locks held). If a same-named database exists but its issue count cannot be read, that is also treated as unknown. The project may be a healthy tracker, so this needs `--force` too. Start the server and retry instead when you can.
- **Its `.beads/metadata.json` is there but unusable:** it cannot be opened, is not JSON, or does not record the Dolt server. The kit cannot tell what the project is, so this needs `--force`. bd is never run on such a project: without the server coordinates bd would fall back to an embedded database, create `.beads/embeddeddolt` inside the project and report it as empty.
- **The merge slot** is held, or bd reads the project but could not read its slot.
- **Reservations:** pending receipts in its request journals, or receipts that could not be read.

A project a stopped restore left behind needs no flag: the server answers and bd rejects the project. An empty project that bd reads (what a restore that failed before writing leaves) needs no flag either. A directory that was never initialized (no `.beads/metadata.json`: what a stop during the `add-project` step leaves) needs no flag when no same-named server database exists or that database contains no issues.

"bd rejects the project" is any refusal by bd while the server answers. By design that also covers a project whose recorded `project_id` or `dolt_database` was changed by hand so that it no longer matches its database: bd refuses it exactly as it refuses a partial restore, the kit cannot tell the two apart, and it retires with no flag. Nothing is deleted, and moving the directory back undoes it.

`retire-project` is refused, with or without `--force`, while a `restore-new` into that name is running: the restore holds `backups/PROJECT.restore.lock` for its whole run. Wait for it to finish or stop it first.

If the move itself fails (for example `retired/` is on another filesystem), nothing is changed, the journal gets a `failed` line after the `intent` line, and the message says so. `retired/` must be a real directory: while it is a symlink, `retire-project`, `add-project` and `restore-new` all refuse, because retired names could not be checked.

Afterwards:
- **The name stays reserved.** `add-project PROJECT` and `restore-new SOURCE PROJECT` refuse a retired name and print the retired entry, because the database of that name is still on the server and a new project would adopt it.
- **The gate passes at once.** `backup-status --require-complete` ignores the entry the last run recorded for the retired project. The record's own `status` stays `incomplete` until the next `backup --all`, which drops the retired entry; a health line built on that status recovers then.
- **Retired names are visible.** `backup-status` prints them in a `retired` list (the key appears only when there are any), and `backup-copy` prints them and does not copy them.
- **The web interface.** The command cannot see the web service's state. If the project is registered there, archive it in the web interface first; otherwise its task pages answer the endpoint's "Unknown/uninitialized project". The command prints this reminder every time.
- **Deleting for good** (dropping the database and the retired directory) has no command yet.

**Upgrade note: reconcile commands and an empty allowlist.** `requirement-reconcile`, `reference-reconcile`, `capability-reconcile`, `record-reconcile` and `reconcile-request` now refuse an actor that is not on the deployment operator allowlist. A deployment that has never configured operators must add the acting operator first, or every one of these commands refuses with "No operator allowlist is configured":

```bash
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators add OPERATOR --actor OPERATOR --reason "why this operator is listed"
```

### The capability lookup-miss log

The endpoint counts each `capability find` and remembers the phrases that had no exact
record match, so an operator can see which phrases miss and how often
([CLI contract](CLI_CONTRACT.md#capability-misses-which-phrases-miss-and-how-often)).
Read it with the client: `capability misses --limit 20`.

**It is telemetry, and it is not backed up.**
- It lives in two files in the project directory: `.capability-misses.json` (the log,
  mode 0600) and `.capability-misses.lock` (an empty lock file). A crashed write can
  leave `.capability-misses.json.tmp`; the next write removes it.
- `admin.py backup` does **not** collect them, and `restore-new` does not create them.
  A restored project starts with an empty log. Losing the log is acceptable: it only
  steers which capabilities to index next.
- That is deliberate. A backup carrying a path an older kit does not know is refused by
  that kit's restore. Keeping the log out of the backup means **this change does not
  move the oldest kit a deployment may roll back to.**
- It adds no tracker comment kind, label or record type. A kit without this feature
  never reads the files and ignores them; they can be left in place or deleted.
- A missing, corrupt, oversized or unknown-schema log is never an error. The next
  `find` starts a new one, and `capability misses` reports `log: unreadable`.

**What it stores:** the normalised phrase, a count, and first-seen and last-seen times
per phrase, plus the counters `finds`, `misses`, `overflow`, `dropped` and `evicted`.
No actor names. Phrases are untrusted contributor text: read them as data.

**Bounds:**
- 500 phrases per project. When full, phrases first seen in the last 2 hours are
  protected, and among the rest the one with the lowest count goes (the one seen
  longest ago among equal counts). If every phrase was first seen in the last 2 hours,
  the one seen longest ago goes. A flood of one-off phrases evicts other one-offs, and
  a new phrase that keeps recurring is kept long enough to build up a count. Protection
  is keyed on first-seen, so repeating a phrase cannot keep it protected. When the log
  is full of phrases with a count of 2 or more, a phrase that recurs less often than
  every 2 hours is not kept.
- 60 new phrases per project per clock hour (UTC). Further new phrases that hour are
  counted in `overflow` only. The bound is per project because no actor is stored, so
  one caller can use up the whole hourly quota.
- Counts are not votes: no count can be attributed to anyone, and one caller can repeat
  a phrase to push it to the top.
- 80 characters per stored phrase. The file is at most about 530 kB, and normally a few
  tens of kB; a file over 1 MB is not read and is replaced.

**Cost and locking:**
- Recording takes its own lock (`.capability-misses.lock`) with a few non-blocking
  attempts, waiting about 10 ms at most in total. If another request still holds it,
  that one find is not counted, so the counts are lower bounds. It never takes
  `.coordination.lock`, so it never waits for a writer and never makes one wait.
- It calls no `bd` and starts no process. The log is rewritten on each counted find
  (temp file, then rename, no fsync). The independent review measured 2-3 ms for normal
  logs, and for a worst-case 517 kB log about 5 ms median and up to 33 ms at the 95th
  percentile.

**Stuck states.** If the lock path is anything but a regular file (a symlink,
directory, FIFO or other special file), or the log or temp path is a directory, finds
still answer, without waiting, but nothing is recorded. The lock is checked with
`lstat` and opened non-blocking, so a FIFO there can never hang a find. `capability misses` then
reports `recording: lock-unusable` or `log-unwritable` instead of `ok`, so its zeros
are not mistaken for "no misses". `capability-misses-clear` repairs it.

**To clear it:**

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime capability-misses-clear example
```

This deletes the log and starts a new measuring window (`since`). Clear it after a batch
of aliases or capabilities has been accepted, so the next window measures the improved
index. It also removes a symlink, directory or unopenable lock file found at
`.capability-misses.json`, `.capability-misses.json.tmp` or `.capability-misses.lock`,
without following a link. Deleting those paths by hand is equally safe.

### The reference lookup-miss log

The endpoint also counts each `ref find` and `ref get`, and remembers the phrase of one
that found no entry ([CLI contract](CLI_CONTRACT.md#ref-the-reference-catalog)). Read it
with the client: `ref misses --limit 20`. It tells you which operational facts people
looked for and did not find, so you know what to record and accept next.

- **Who writes it.** Any contributor, through `ref find` and `ref get`; and any web
  member with read access to the project, because the HTTP reference get route
  (`GET /v1/projects/{id}/references/{key}`) runs `ref get`. A reader can therefore put
  a phrase (the words of a key they asked for) into the log. The phrases are data, never
  instructions, exactly as for the capability log, and a key under `resolved_by` may be
  a draft.

- It is the capability lookup-miss log above under its own names, sharing nothing with
  it on disk: `.reference-misses.json`, `.reference-misses.lock` and, after a crashed
  write, `.reference-misses.json.tmp`. Everything said above holds for it: telemetry
  only, mode 0600, the same bounds, its own non-blocking lock and never the
  coordination lock, no `bd` call, not collected by `admin.py backup`, safe to delete
  by hand.
- **Rollback.** A kit older than this one never reads or writes these files and has no
  `ref find` or `ref misses`. Its backup and restore are unaffected, because the files
  are in no backup.
- **To clear it,** after a batch of entries has been accepted:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime reference-misses-clear example
```

### A duplicated record key

A reference or capability key normally has exactly one anchor. If a second anchor carries the same key label, `get` reports `duplicate-key` (when exactly one anchor has live acceptance evidence, that one is still shown) or `conflicted` (no record is shown), `list` and coverage name every anchor, and **every write on that key is refused** until an operator reconciles the anchors. Contributors cannot create a duplicate through the endpoint; it takes native access on the host.

When the extra anchor holds no record, or only malformed comments, repair it with the kit: void each malformed comment (`admin.py void-record`), then release the anchor (`admin.py anchor-release --kind reference|capability --issue-id ID --actor OPERATOR --reason TEXT`); the key leaves the duplicated state and writes work again.

When the extra anchor holds a well-formed record, a void cannot repair it: a void never withdraws a well-formed record the entry reads, and that includes acceptance evidence. Release the wrong anchor by name instead. First read every anchor (`capability get KEY` or `ref get KEY` names them, and `history` shows each one) and decide which is the genuine one. Then, on the host:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime anchor-release PROJECT --kind capability --issue-id PROJECT-FORGED --actor OPERATOR --reason 'forged beside PROJECT-GENUINE' --duplicate
```

`--duplicate` releases the NAMED anchor only. Every refusal comes before the first write:

- The actor is not on the deployment operator allowlist (checked before any read).
- The row is not an anchor of that kind, was already released, or carries more than one key label.
- The key is not duplicated. The anchors are counted exactly as every write counts them, so the last anchor of a key is never released this way.
- The named anchor holds a record this kit cannot read (a newer record version).
- The named anchor holds a readable record and no remaining anchor does. "Readable" is what a reader would present: an anchor that is record-less, whose records are all voided, or that reads `malformed`, `unsupported` or incomplete does not count, even when its revisions themselves parse (one other bad comment on an anchor makes readers refuse the whole entry). An accepted, superseded or drifted entry counts. A `draft-only` entry counts when it shows a proposed revision, or when its acceptance is inert because its operator was removed (`acceptance-inert`); it does not count when its record says accepted and there is no acceptance evidence at all (`accepted-without-evidence`), because it then shows nothing to revise from. A key is never left with nothing readable: deal with the other anchor first (release it; void its malformed records first if the plain mode asks for that), which leaves the readable one as the key's only anchor.
- The named anchor carries acceptance evidence that no void names, **live or inert**. It needs the extra flag `--set-aside-evidence`. That covers the anchor readers currently select: among duplicates only live acceptance evidence selects an anchor. Inert evidence (its operator was removed from the allowlist) counts, because it would read live again if that operator were re-added.
- The named anchor reads `malformed` and carries live acceptance evidence. It may be the key's accepted record behind one bad comment: void that comment first (`admin.py void-record`), then run the release again.
- The named anchor reads an accepted record and no remaining anchor reads one. The key never loses its only accepted record; release the other anchor instead. Live acceptance evidence on a remaining anchor is not enough: an operator accept that stopped after writing its evidence and before the accepted revision leaves evidence on a draft.

What a release does, in order: it settles the pending receipt the row's `request:` label names, closes the row if it is open, adds one audit comment as the operator, then removes the state, `request:` and `request-content:` labels and, last, the key label. The type label and every comment stay, so nothing is deleted and the row stays hidden from `work`. Each step is skipped when already done, so a re-run after an uncertain write finishes the release.

The audit comment names the operator, the reason, the key, the anchors that remain, which anchor readers selected before and select now, every record set aside (revision and hash), every piece of acceptance evidence set aside (revision, record hash, operator, decision id, live or inert), and the count of other records on the anchor (capability aliases and verifications) that stop counting for the key. The printed JSON carries the same facts: `remaining`, `selected_before`, `selected_after`, `records`, `evidence_set_aside`, `stop_counting`.

A released row is no longer an anchor of any key and is never read for one again. **Evidence set aside stays set aside**: re-adding the operator who recorded it does not bring it back, and the key reads exactly as it did after the release. The release cannot be undone through the kit. Aliases and verifications on the released anchor stop counting; record them again on the remaining anchor if they are still wanted. Take a backup afterwards.

Which anchor to name, by shape:

| The key's anchors | The safe sequence |
| --- | --- |
| a genuine draft and a record-less anchor (orphan) | plain `anchor-release` of the orphan |
| a genuine draft and a forged draft | `--duplicate` on the forged one |
| two accepted anchors, both with live evidence | `--duplicate --set-aside-evidence` on the forged one |
| an accepted anchor and a draft whose accept stopped after its evidence | `--duplicate --set-aside-evidence` on that draft |
| a forged accepted anchor whose evidence is inert (or by an unlisted author) and a genuine draft | `--duplicate --set-aside-evidence` on the forged one |
| a genuine entry whose acceptance is inert (its operator was removed) and a forged draft | `--duplicate` on the forged draft; the genuine entry keeps its evidence and reads accepted again if the operator is re-added |
| two anchors whose acceptance is inert | `--duplicate --set-aside-evidence` on the forged one: its inert evidence is set aside for good. If you cannot tell which is genuine from the records, re-add the operator(s) first so the entries read as they were accepted, decide, then release with the same command; or use the host procedure below |
| a genuine draft and a forged anchor whose record says accepted but carries no acceptance evidence (it reads `draft-only` with `accepted-without-evidence`) | `--duplicate` on the forged one; no flag is needed, it has no evidence. The genuine draft is not released beside it: the forged anchor shows nothing a contributor could revise from |
| an anchor that reads `malformed` beside a readable one | void the malformed comment, or `--duplicate` on the malformed anchor when it is the forged one and carries no live evidence |

Three shapes have no clean kit path:

- **A forged accepted anchor with LIVE evidence beside a genuine draft.** The forged anchor holds the key's only accepted record, so it is not released. Release the genuine draft with `--duplicate` (the forged anchor is readable, so this is allowed), then revise and accept the right content on top of the anchor that remains.
- **A forged anchor that holds a record version this kit does not read (`reference-entry-v4`, `capability-entry-v2`), a `reference-entry-v3` record with an authority type this kit does not know, or an unknown record kind of the family.** This kit cannot judge it, so it refuses to release it, and it refuses to release the genuine anchor beside it (nothing readable would remain). Use the host procedure below, or a kit that reads the record. A `reference-entry-v2` record, or a v3 record with a `decision` authority, is one this kit reads: a forged anchor holding one is judged like any other and can be released with `--duplicate`. A kit older than that authority type cannot judge it.
- **Two anchors that both say accepted and carry no acceptance evidence.** Neither can be genuine: the kit writes the evidence before an accepted revision, so there is no entry to protect and nothing a contributor could revise from. `--duplicate` is refused on both (no remaining anchor holds a readable record), and the refusal's advice does not apply. Use the host procedure below on both anchors, then propose the entry again.

When two anchors both have live acceptance evidence, the key reads `conflicted` and neither is selected. Decide which is wrong, with the project owner if it is not obvious, and release that one with `--duplicate --set-aside-evidence`; the other becomes the selected anchor, and the output shows it. Do not try to void the acceptance evidence first: that void is refused.

On an older kit (rollback), a released anchor that still holds records is not read for its key either, but that kit lists it as a `malformed` entry with no key in `coverage` until the kit is rolled forward. A project backup taken with duplicates restores them as they were, and the release works on the restored project.

Last resort, when the command refuses a case you have decided by other means: on the host, as an operator, remove the type, key and state labels from the wrong anchor with the native tool under the kit environment, for example for a capability:

```sh
bd update PROJECT-FORGED --remove-label capability --remove-label capability-key:KEY-SLUG --remove-label capability:accepted
```

Use the labels the forged anchor actually carries (`bd show PROJECT-FORGED --json`). The stripped issue stays in the database, closed, with its comments, as evidence; it no longer counts as an anchor, and writes on the key work again. Record what you did and why on the genuine anchor's project, and take a backup afterwards.

### Deeply nested JSON

**Known limits of this section (coordinator note at integration, from the independent
review of kittrial-5bb.108; tracked as kittrial-5bb.111).** The guard covers record
comments and caller requests. It does not yet cover three host-written inputs: issue
metadata set with the native tool (`bd update --metadata`), the project's `.sessions.json`,
and the `--file` payloads of `admin.py` commands. JSON nested thousands of levels deep in
the first two still makes `work` and `brief` fail for the project, on this kit as on older
ones. The list below of what fails on an older kit is also incomplete: proposal, proposal
disposition, contribution-settings and requirement-revision comments, and a lifecycle
record stored as a state reason (which the listing command does not print, because it reads
only comments), fail reads there too.

Resetting issue metadata with `bd update ISSUE --metadata "{}"` merges into existing metadata (a no-op); clearing it requires `bd update ISSUE --unset-metadata KEY`, which removes the key and both kits recover.

The kit refuses JSON nested more than 64 levels deep wherever it parses text that
somebody else wrote: a record comment, a `--file` attachment, a payload argument, a
request to the endpoint, an HTTP request body, a cursor. The deepest JSON the kit itself
writes nests 4 levels.

- **A record comment** nested deeper (which only a host write can produce: the record
  prefixes are refused for contributors) reads as a malformed record of its kind. It
  fails only its own entry or task: `work`, `brief`, `ref`, `capability` and `proposal`
  reads carry on for everyone, the entry is named in `coverage`, and `void-record`
  repairs a reference or capability record as it does any malformed one. Before this,
  one such comment made the reads exit with `RecursionError`, made `work` exit 124 for
  every actor of the project, and made `void-record` fail the same way.
- **A void does not repair the project for an older kit.** The voided comment stays in
  the tracker, and a kit before this one parses a record comment before it applies a
  void, so it fails on that project exactly as it did before the void. Measured on the
  kit before this one, one comment at a time: an over-deep reference or capability
  record fails that catalog's reads and `work` for every actor; an over-deep review,
  integration-revert or record-void comment fails `work` for every actor and the
  task's `brief` and `review`; an over-deep checkpoint fails only that task's `brief`;
  the other record kinds did not fail a read. A project with no such comment reads
  identically on both kits.
- **Before rolling such a project back to an older kit, remove the comments.** The
  native tool has no command that deletes a comment, so this is a direct delete in the
  tracker database, on the host, as an operator, under the kit environment, in the
  project's directory. Take a backup first (`admin.py backup PROJECT`); the backup is
  the only copy of what you remove. List the over-deep record comments with the kit's
  own scan (comment id, then task id):

  ```sh
  bd sql "SELECT id, issue_id, text FROM comments WHERE text LIKE 'Kind: %'" --json \
    | python3 -c "import json,sys; sys.path.insert(0,sys.argv[1]); import record_json as r; [print(c['id'], c['issue_id']) for c in json.loads(sys.stdin.read()) if r.nesting(c['text'].partition(chr(10))[2]) > r.NESTING_MAX]" /home/beads/beads-team-kit
  ```

  Remove each one, then run the listing again; it must print nothing:

  ```sh
  bd sql "DELETE FROM comments WHERE id = 'COMMENT-ID'"
  ```

  Both kits then read the project (checked on a runtime for every record kind, with
  `work`, `brief`, `review`, `history`, the catalog lists and a backup afterwards). A
  void record that named a removed comment stays and is harmless. Record what you
  removed and why on the project, and take a backup afterwards. On this kit and later
  ones the delete is not needed: `void-record` is the repair.
- **A request** nested deeper is refused with `JSON nested too deeply (more than 64
  levels)`: exit 2 over SSH, and HTTP 422 `Request body is not valid JSON` from the web
  service. Nothing is written and no operation is reserved. Before this, some routes
  answered exit 124 "outcome unknown" although nothing had been written, and the web
  service answered HTTP 500.
- **Why a fixed bound.** The interpreter's own limit is about a thousand levels and
  depends on the Python version and on how deep the call stack already is, so the same
  text could parse in a writer and fail in a reader. The bound is checked by counting
  brackets in one pass before any parse, so it gives the same answer everywhere and costs
  time in proportion to the text, however malformed the text is (about half a second for
  the largest request the endpoint takes, 2 MB).

### Malformed structured history

A comment that claims a reserved machine format (`Kind: contribution-review-v1`, `Kind: task-checkpoint-v1`) but fails validation makes `brief`, `review` and `refresh` fail for that task. Repair it on the host; never edit or delete rows in the native database.

Read the incident first: `brief PROJECT-TASK` fails naming the offending comment id, and `history PROJECT-TASK` returns its exact bytes. Configure the operator allowlist once per deployment, then build a void payload and submit it with the host command:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators add OPERATOR --actor OPERATOR --reason "why this operator is listed"
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators list
```

```json
{"schema_version":1,"operation":"void-record","operation_id":"void-1","task":"PROJECT-TASK",
 "target":"COMMENT-ID","target_kind":"contribution-review","target_sha256":"SHA256-OF-ORIGINAL",
 "original":"EXACT ORIGINAL COMMENT TEXT","reason":"WHY THIS RECORD IS VOID","disposition":"void"}
```

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime void-record PROJECT --actor OPERATOR --file void.json
```

`target_sha256` is the SHA-256 of `original` encoded as UTF-8, so the record proves which bytes were reconciled. Operator authority is server-side configuration, never the payload's own `operator` string: the deployment allowlist (`operators` in `deployment.private.json`) is the single authority source, the endpoint supplies it to every read and to `refresh`/`render`, and reads require the void's stored native author to be on it as well as to match the name in the payload. `ORCHESTRA_OPERATORS` is not an authority source; a host command refuses when it is set to a set that differs from the deployment configuration, instead of authorizing a write the endpoint would ignore. `void-record` refuses any actor outside the allowlist; a deployment that configures no operators authorizes nobody. A contributor therefore cannot make a void valid by naming their own actor as the operator, so a forged or self-authored void is inert even if the generic raw-write guard is bypassed. Void records are append-only and are written only by this host command; the record-void prefix is reserved, so any raw `comments add` write of one is refused on the contributor endpoint, exactly like a raw review or handoff record. The guard parses the `comments` request structurally: it resolves `add` past leading flags, refuses an attachment transported before `add`, and refuses the operator-only `-a`/`--author` spelling, so the reviewer's `["comments","@attachment:a","add",TASK,"-a","ops-james"]` forged-authorship request fails closed with zero native writes. A malformed, stale, foreign-authored or unconfigured-author void comment is ignored and reported rather than applied. Nothing is deleted: the original comment, the void record and its actor, timestamp and reason all stay in native history, in `history` reads and in rendered pages, and native backup plus `restore-new` preserve both. Reads expose applied voids in `review.recoveries`. Reads also require a void to follow its target in native order, so a void recorded before the record it targets cannot apply.

A void may not target a record that is part of the contribution history the surviving records currently form, so recovery cannot suppress a current revision or approval. If a surviving record's `previous` names a voided comment, reads keep failing closed and name that record; void it explicitly as well, or deliver a revision that repairs the chain. A directly written void that would suppress the surviving history is inert: reads stay healthy, the void is listed in `review.recoveries` with `applied: false` and its refusal reason, and a warning names it. A void is a repair of a broken history, so a void applied after the latest approval invalidates that approval: the task returns to `awaiting-review` and a fresh approval is required before integration. Re-running the identical void payload is idempotent.

#### Reference and capability records

A comment that claims a reference or capability record kind but fails its schema makes
only its own entry read `malformed`: `ref` and `capability` reads, `work` and `brief`
keep working and name the anchor in `coverage`, but a `revise` of that key is refused
until the record is repaired. `void-record` repairs it (kittrial-5bb.74). The payload is
the one above, with the anchor as `task` and one of these as `target_kind`:
`reference-entry`, `reference-acceptance`, `capability-entry`, `capability-acceptance`,
`capability-verification` or `capability-alias`. `ref get KEY` (or `capability get KEY`)
gives the anchor as `native_id`; on the host, read the comment's `id` and exact `text`
from `bd export --all`, as in the scan above.

- **Same command, same authority.** The host command, the allowlist check before any
  read, the payload binding, the idempotent retry and the refusal of a reused
  operation ID or a second void of one target are the review void's. Reads trust a void
  by the same rule (`recovery.records`). Like a contribution-review void it is one
  appended native comment and writes no host journal: only the integration-revert
  retraction is journaled, because it must prove that the host issued the revert.
- **What a void applies to.** A void applies only to a comment the entry cannot read:
  one that fails its kind's schema (a BOM or CRLF lookalike of a v1 prefix included),
  belongs to another anchor or key, or holds a revision, or the acceptance evidence for
  a revision, that an **earlier** comment of the same kind already holds. Of a
  conflicting or duplicated pair, only the later one can be voided: the writer never
  writes a second holder, so the earliest is the only one it can have written, and a
  void cannot itself be voided. Acceptance evidence is live only when its stored native
  author is a live configured operator AND matches the record's own `operator` - the same
  rule the readers use (`keyed_entries.evidence_is_live`); anything else is inert. Inert
  evidence is not a holder of the place, so it is the voidable record and the operator's
  live evidence beside it is the protected one (kittrial-5bb.92 review
  `plant-protected-by-void-rule`; the void rule uses the reader's definition, review
  `void-rule-live-definition`). A well-formed record the entry
  reads is refused at write and ignored on read. A void repairs history and never
  withdraws a decision: to replace an entry, revise it and accept the new revision, or
  retire the key.
- **Removing and re-adding an accepting operator.** `operators remove` makes that
  operator's evidence inert: the entry reads `draft-only` with `acceptance_inert`, and
  their sole evidence can be voided by another operator while they are removed. Re-adding
  the operator makes the evidence live again, so the void names a well-formed record the
  entry reads: it stops applying, the evidence returns, and the read carries a
  `void-refused` warning. If, while the first operator is removed, a second operator
  accepts the same revision with a DIFFERENT decision, the shown acceptance is the second
  operator's; re-adding the first flips the shown acceptance back to theirs (earliest in
  native order) and later accepts are refused with `conflicting acceptance evidence for
  one revision` - the way out is to void the later evidence. `requirement-backfill` reads
  the same live filter, so a backfill by another operator while the first author is
  removed writes a second evidence comment (kittrial-5bb.92 review
  `void-rule-live-definition`).
- **The earliest-holder cross-check is a consistency check, not a security boundary.**
  The earliest holder is established by **both** bd's native (stored `created_at`) order
  and comment-id (UUIDv7) order. An edit of `created_at` alone is visible because the two
  orders then disagree. Someone who can write the native database can rewrite the comment
  `id` too, make the orders agree, and leave the legitimate record as the later - and so
  voidable - holder. It raises the cost of a forgery and surfaces a partial one; it does
  not close the hole and a repaired ledger is not proof of authorship
  (kittrial-5bb.92 review `order-check-limits-and-rollback`).
- **When the two orders disagree.** The history is conflicted, **no** void of that place
  applies, and the kit has no command that repairs it: both holders stay. The operator
  stops, reads the anchor natively (`bd show ANCHOR --include-comments`), decides which
  record is genuine, and repairs the stored order from a native backup or with the native
  tooling (recording the reason on the project), then re-reads. Do not guess, and do not
  void either holder to force a state the ledger cannot justify.
- **What readers show.** The entry reads as if the voided comment were absent, and its
  `warnings` carry `record-voided`. A void written around the host command is reported
  instead: `void-invalid` (malformed, stale, out of order, or not written by a
  configured operator) or `void-refused` (it names a record the entry reads, the
  earliest holder of a revision, or a record of another kind). The anchor stays hidden on every surface, because all its
  comments are kept.
- **Writes agree.** The endpoint gives contributor writes the allowlist, so `ref
  revise` and `capability revise` see the same entry `get` shows once the void applies.
- **Revocation.** After `operators remove`, that operator's voids stop applying here too
  and the entry reads `malformed` again; re-adding the operator restores the repair.
  Without `--confirm-revoke`, `operators remove` counts the operator's voids of
  reference and capability records that apply today and names the entries whose
  reading changes (for example `example/reference calendar.trading draft-only ->
  malformed`). Each list is capped at five entries with `(+N more)`; add
  `--all-revoked` to the same command to name every affected entry
  (kittrial-5bb.92).
- **Reconcile agrees.** `reference-reconcile` and `capability-reconcile --disposition
  complete` read the allowlist too, and refuse an anchor whose every record is voided,
  as they refuse one whose propose never posted its record.
- **Rolling back below this kit.** An older kit's reference and capability readers do
  not read voids at all, so a repaired entry reads `malformed` again there (only that
  entry). The anchor stays hidden. A review read of the anchor, if one reaches it, lists
  the void among ignored operator void comments. An older `void-record` refuses these
  target kinds before any write. An older kit also counts every acceptance evidence
  record with no author rule, so on an entry where this kit wrote the operator's own
  evidence beside an inert plant for one revision, the older kit refuses any further
  accept with `conflicting acceptance evidence for one revision` and cannot void the
  plant (it still reads the plant as a holder). Reconcile that entry natively or restore
  from a backup; rolling forward to this kit restores the repair with nothing to clean
  up (kittrial-5bb.92 review `order-check-limits-and-rollback`). No sidecar path is
  added, so backups restore on either kit, and rolling forward restores the repair with
  nothing to clean up.
- **Limits.** A record of a version this kit does not read (`Kind: reference-entry-v4`,
  `Kind: capability-entry-v2`) or of an unknown kind of the family cannot be a void
  target: the target must claim a prefix this kit reads for the declared kind (v1, and
  v2 and v3 of `reference-entry`), exactly or through the BOM/CRLF view the readers use.
  It stays `unsupported` (or `malformed`) until a kit that reads it handles it. A
  `reference-entry-v3` record whose authority type this kit does not know is not a void
  target either ([below](#reference-entries-that-point-at-a-decision)).

#### Attested reference entries
A reference entry whose authority is an `attestation` records an operational fact: who
observed or stated it, when, on what basis and how ([CLI contract](CLI_CONTRACT.md#ref-the-reference-catalog)).
The kit cannot check such a fact against anything, so the acceptance carries the
weight.

- **Who writes what.** Any contributor may propose or revise an attested draft (`ref
  propose`, `ref revise`). Only `admin.py reference-apply` accepts one. Until then
  every read marks it: `state: draft-only`, `authority_kind: attested`,
  `authority_accepted: false` and an `authority_note` that starts `NOT ACCEPTED.`
- **What the operator must show.** On top of the ordinary acceptance rules, an attested
  revision is accepted only when all of these hold. Each refusal is made before any
  write.
  1. `acceptance.evidence` is one line saying what you did to check the fact: you
     re-ran the check (command, host, date), or you confirmed the statement with the
     person named in `by`. The kit checks that the line is there, not what it says.
  2. `acceptance.decision_id` names an existing issue of type `decision` or labelled
     `decision`. The kit checks only that: the issue may be closed, and it may be one a
     contributor created or an ordinary task a contributor labelled `decision`. Whether
     it is a real decision is yours to check (the known limit tracked as kittrial-5bb.87).
  3. For an `owner-statement`, the identity in `by` is one of `acceptance.owners`,
     written the same way.
  4. `review_by` is in the future and at most 6 months after `observed`.
  5. `observed` is not in the future and not more than 6 months old. An older
     observation is refused: check the fact again and revise the entry first.

  "Today" in rules 4 and 5 is the server's UTC date. East of UTC, a local date can be a
  day ahead of it until 00:00 UTC, and an `observed` of that date is refused as later
  than today; the refusal names the UTC date.
- **Past its review date.** An accepted attested entry whose `review_by` has passed
  still reads `state: accepted`, with `due: expired`, like any entry. Its
  `authority_note` then says `PAST ITS REVIEW DATE`: check the fact again and revise or
  re-accept the entry.
- **Many at once.** Put the reviewed drafts in one batch (`items`) under one decision.
  An item that fails a rule above is reported `refused` with its reason, and the others
  are still accepted.
- **Revising an older draft to an attestation.** A draft written with a `repo-path`
  authority and no commit cannot be accepted. Revise it with `ref revise`, giving the
  same content and an `attestation` authority, then accept that revision.
- **A rule nobody observed.** A working rule that nobody observed and the owner did not
  state is neither basis. Record it as a decision issue and give the entry a `decision`
  authority that points at it ([below](#reference-entries-that-point-at-a-decision)).
- **Record version and rollback.** An attested revision is stored as
  `Kind: reference-entry-v2`; every other revision stays v1. A kit older than this one:
  - does not serve an entry that holds a v2 revision. Such an entry reads
    `unsupported` when its anchor also holds a v1 record: an earlier v1 revision, or
    acceptance evidence, which stays v1, so every accepted attested entry reads this
    way. An attested draft that was never accepted and has no v1 revision reads as an
    anchor with no record yet, and `ref get` of it is refused, naming the key. Both
    are named in `coverage`, and every other entry reads as before;
  - loses a whole entry to one contributor's draft. Any contributor can revise an
    accepted v1 entry with an attestation; that draft is a v2 revision on the same
    anchor, so the older kit reads the WHOLE entry `unsupported`, its accepted v1 record
    included, until roll-forward. No operator step is needed to cause it. This kit keeps
    serving the accepted record and shows the draft as `proposed`;
  - cannot write to such an entry, void its v2 records or release its anchor, and
    refuses a new propose of its key;
  - backs up and restores a project that holds v2 records exactly as before: the
    records are native comments, and no sidecar path is added.

  So after a rollback the attested facts are not available until the kit is rolled
  forward, and no record is lost or rewritten in between. One thing can change: the
  older kit does not treat a never-accepted attested draft's anchor as a record anchor,
  so there a contributor can reopen, claim and close that row and add a plain note to
  it. Its status and assignee can therefore differ after roll-forward; its labels and
  records cannot (label replacement and a raw v2 comment are refused). Do not "repair"
  such an entry with the older kit.

#### Reference entries that point at a decision
A reference entry whose authority is `{"type": "decision", "id": ISSUE}` records a rule
the project set for itself: how a release is made, what runs first. Nobody observed it
and the owner did not state it, so it is not an attestation; the decision issue is where
it was set ([CLI contract](CLI_CONTRACT.md#ref-the-reference-catalog)).

- **Who writes what.** Any contributor may propose or revise such a draft; the id must
  name an existing issue of type `decision` or labelled `decision`. Only `admin.py
  reference-apply` accepts one, singly or in a batch. Until then every read marks it
  `state: draft-only`, `authority_kind: decision`, with a note starting `NOT ACCEPTED.`
- **What the operator must show.** On top of the ordinary acceptance rules (`review_by`
  set, in the future and at most 24 months ahead), each checked before any write:
  1. `authority.id` still names an existing decision issue.
  2. `acceptance.decision_id` names an existing decision issue. It may be the same
     issue as `authority.id`.
  3. `acceptance.evidence` is one line.
- **What the kit does not check, and why.** It checks that the issue exists and is a
  decision, and nothing about who wrote it or whether it is closed. Over SSH an actor
  name is self-declared: a contributor can create a decision issue, close it, and do
  both under an operator's name. A rule requiring "closed" or "created by an operator"
  would look like a control without being one. The control is that only an allowlisted
  operator accepts, on the host route: read the decision before you accept an entry
  that points at it. The limit is tracked as kittrial-5bb.87 and kittrial-5bb.106.
- **If the decision issue goes away.** A read never looks the issue up again, so an
  accepted entry keeps reading `accepted` if its decision issue is later deleted or
  loses its type. The next `ref revise` that keeps that id, and the next accept of a
  draft that carries it, are refused, naming the id. Revise the entry to point at the
  decision that sets the rule now, then accept that revision.
- **Revising an older draft.** A draft written with a `repo-path` authority and no
  commit cannot be accepted. Revise it with `ref revise`, giving the same content and a
  `decision` authority, then accept that revision.
- **Record version and rollback.** A decision revision is stored as
  `Kind: reference-entry-v3`. Attested revisions stay v2 and the rest v1. A kit older
  than this one behaves towards a v3 revision exactly as a kit older than the
  attestation authority behaves towards a v2 revision
  ([above](#attested-reference-entries)): it does not serve the entry (`unsupported`
  when the anchor also holds a record it reads, an anchor with no record yet
  otherwise); one contributor's v3 draft makes it read a whole accepted entry
  `unsupported`; it cannot write to, void or release such an entry; and its backup and
  restore are unaffected. Nothing is lost; roll forward to read them.
- **No further version for a new authority type.** Version 3 is the last one an
  authority type needs. A v3 revision whose authority type this kit does not know (one a
  later kit added) makes its entry read `unsupported`, with the warning `authority type
  NAME is newer than this kit`. It is not malformed: it cannot be a void target, the
  entry takes no write, and its anchor cannot be released.
  - **Only a record that is valid everywhere else counts.** The comment must start with
    the exact `Kind: reference-entry-v3` line (a BOM or CRLF variant does not count) and
    be the canonical bytes of a v3 record whose every field outside `authority`
    validates, content hash included, with an `authority` object whose `type` this kit
    does not know and which is shaped like a type name: lowercase letters, digits and
    hyphens, starting with a letter, at most 32 characters. `Decision`, `repo-path ` with
    a trailing space, a blank, a control character or markup is not a later kit's type,
    and reads malformed. The record's `key` must also belong to the anchor it is on. A
    later kit may put anything inside `authority`; it may not change v3 anywhere else.
  - **Everything else is malformed, and a void repairs it:** a stray or hand-typed
    comment, a typo in the type of a broken record, a wrong hash, a BOM or CRLF variant.
    So a line of garbage on a genuine entry never freezes it.
  - **What to do with one.** If a later kit wrote it, install that kit. The warning
    cannot tell such a record from one built by hand on the host to look like it, and
    for a made-up type no kit will ever read it. If you have established that it is not
    a later kit's record, this kit has no command for it: use the last-resort host
    procedure under [a duplicated record key](#a-duplicated-record-key) (strip the
    anchor's labels with the native tool, then propose the entry again), and record what
    you did.

#### Orphan anchors

`ref propose` and `capability propose` create the entry's anchor, close it and post
revision 1 inside one locked operation. If that write stops in between, the anchor holds
no record: readers report it as an incomplete anchor, every other operation is refused
the key, and if it stopped before the close the row is open and claimable in `work`. Only
the same operation's retry, with the identical payload, finishes it. When that payload is
lost, release the anchor:

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime anchor-release PROJECT --kind reference --issue-id ANCHOR --actor OPERATOR --reason 'original payload lost'
```

- It checks the operator allowlist before any read. It refuses a row that is not an
  anchor of that kind, or that still holds a live record. An anchor whose every record an
  applied void names holds none, so the same command frees it. An anchor that holds a
  well-formed record is released only as a named duplicate of its key, with
  `--duplicate` ([a duplicated record key](#a-duplicated-record-key)).
- It marks the pending receipt that the row's `request:` label names as `released`, with
  the audit, so that operation ID is settled.
- It closes the row if it is open, adds one plain audit comment, then removes the state,
  `request:` and `request-content:` labels and, last, the lookup label, which frees the
  key. The type label stays, so an anchor with voided records stays hidden, and a row
  that never held a record reads as the ordinary closed task it already was.
- Each step is skipped when it is already done, so a re-run after an uncertain write
  finishes the release. Once the lookup label is gone the row is no longer an anchor, and
  a re-run is refused.
- It is a host command only. Over SSH the endpoint actor is self-declared, so nothing
  that rests on the operator allowlist is offered there.

On an older kit a released row holds no key either, and it is never claimable:
- a row that never held a record is the ordinary closed task it is on this kit;
- an anchor whose records were voided stays hidden as a task, but that kit does not
  read voids, so its catalog lists it as a `malformed` entry (with no key) in `coverage`
  and counts it as `malformed` in the `work` attention block until the kit is rolled
  forward.

#### Operator removal and restore policy

`admin.py operators remove OPERATOR` is a revocation, not a cleanup: voids that operator authored stop applying on read, the incident is reported as unreconciled again, and re-adding the operator restores those dispositions. The same holds for the requirement-proposal dispositions, owner decisions and contribution settings they recorded; the refusal names the proposals and settings that change (see "Requirement proposals" above). Every affected list in that refusal is capped at five entries with `(+N more)`; `operators remove OPERATOR --all-revoked` names all of them instead. Because that destroys recorded dispositions, `remove` refuses unless `--confirm-revoke` acknowledges the consequence.

The allowlist is deployment configuration rather than a native Beads object, so each project's complete coordination sidecar records the allowlist in force at backup time. **A restore does not re-grant it by default.** The allowlist is authority for *every* project on the deployment, so a backup taken before `operators remove ACTOR --confirm-revoke` would otherwise silently restore that actor's authority deployment-wide — including for projects the backup has nothing to do with. `restore-new` therefore restores the native records, the coordination sidecar and the operation journal, but leaves the deployment allowlist untouched. When the backup records operators this host does not list, the command prints them by name, states that they were **NOT** restored, and explains that void records they authored stay inert on the restored project until authority is granted again. Nothing is deleted: the void comments and their `original` bytes are intact, and they apply again as soon as the actor is deliberately re-added.

To re-establish the recorded authority, re-grant it explicitly, one actor at a time:

```
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators add OPERATOR --actor OPERATOR --reason "why this operator is re-granted"
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime operators list
```

Or re-run the whole restore with `--restore-operators` when the entire allowlist recorded in the backup is intended to be in force again, for example on a replacement host:

```
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime restore-new example examplerestore --restore-operators --actor OPERATOR --reason "replacement host, re-granting the recorded allowlist"
```

The `--actor`/`--reason` on `restore-new` are recorded on every re-grant the restore makes in the authority-changes audit, exactly as on `operators add`/`verifiers add`; a re-grant without them records a null operator and prints the sentence saying so (kittrial-5bb.192). `--restore-operators` is additive only (it never removes an entry) and prints exactly which entries it re-granted. `--restore-operators` and `--restore-verifiers` are the last step of `restore-new`, after the coordination files and the operation-journal snapshot are restored (kittrial-5bb.142). They are the only step that takes the deployment lock, and both lists are re-granted under one wait for it (kittrial-5bb.144). The restore still completes, but nothing is re-granted, in two cases: another change holds the lock past its 10-second wait, or `authority-changes.audit.json` is damaged (a re-grant is an add, and an add is refused while the audit is damaged; move the file aside first, then run the printed commands):

- **Exit status 3** means the restore is complete, but the authority asked for was not re-granted (kittrial-5bb.144; kittrial-5bb.142 exited 0). Status 0 means restored, including every re-grant asked for; 1 means failed. A script running `restore-new ... --restore-operators && next-step` therefore stops at status 3. `restore-new --help` states the codes.
- **The warning is the last thing the restore prints**, on stderr, after `Restored only into the newly created project` and anything else on stdout. It names both lists (`WARNING: the restore is complete, but deployment authority the backup records was NOT re-granted: operators (--restore-operators): ...; verifiers (--restore-verifiers): ...`) and the cause (the lock, or the damaged audit and how to move it aside). It then gives one exact, shell-quoted `admin.py --root ROOT operators add ACTOR --actor OPERATOR --reason TEXT` (or `verifiers add`) command per entry - with the operator and reason the restore was given where it had them, so following them does not leave the unattributed entry the audit warns about - and the `backup-authority` command below, and ends with `restore-new exits 3: ...`.

Run the printed commands; do not repeat the restore, which is refused because the destination now exists. Before kittrial-5bb.142 such a refusal ended `restore-new` with exit status 1 and a half-restored destination: the operation journal and the other list were not restored either.

A failure in the operation-journal step (after the destination exists) still ends `restore-new` with the failure notice and status 1, and re-grants nothing, because the authority step comes after it. A backup with no coordination sidecar records no operators or verifiers: with either flag, `restore-new` says so and re-grants nothing.

**What a backup records, afterwards.** `operators list`, `verifiers list` and `backup-status` show only this installation. To compare a backup with it, at any time and without changing anything, run:

```
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime backup-authority PROJECT
```

It prints JSON naming the sidecar it read: the canonical one, else the durable last-complete copy, the same one `restore-new` uses; `null` when there is none. For `operators` and for `verifiers` it gives `recorded` (what the backup records), `listed_here` (what this installation lists now) and `not_listed_here` (what `--restore-operators` / `--restore-verifiers`, or `operators add` / `verifiers add`, would re-grant). It takes no lock and writes nothing. PROJECT is the backup's project name, the SOURCE of `restore-new`.

It tells three cases apart (kittrial-5bb.145):

- **No such backup.** When `backups/PROJECT` does not exist, it refuses with `No such backup: backups/PROJECT does not exist` and exit status 1. A mistyped name no longer reads as a backup that records nobody. An invalid project name is refused as a name first.
- **Damaged sidecar.** When a sidecar copy exists but neither the canonical sidecar nor the last-complete copy is usable, it refuses with exit status 1. The message names each copy and why it cannot be used (not valid JSON, not a schema-1 sidecar, a status other than `complete`, a symlink, unreadable), and says the lists cannot be trusted. When the canonical copy is unusable but the last-complete copy is fine, it answers from the last-complete copy and lists the copy it passed over, and why, under `unusable`. It always answers from exactly the copy `restore-new` would restore from (kittrial-5bb.150):
  - A good canonical copy is used even when the last-complete copy is a symlink; the symlink is listed under `unusable`.
  - A symlink that `restore-new` would reach and refuse makes `backup-authority` refuse too.
  - A FIFO or device in place of a sidecar copy is reported as not a regular file at once; reading it never blocks.
- **No sidecar at all.** A legacy backup answers `sidecar: null`, empty lists and a `note` saying it records no operators or verifiers, with exit status 0.

Passing `--restore-operators` is an explicit authorization decision: after a revocation, do not pass it "to make the restore look complete" — a revoked operator stays revoked until an operator re-adds them by name. The native backup preserves the original comment and the void comment; the sidecar preserves the authority reads would need to apply it, so a restore still preserves both the original and its disposition, with the authority decision left where it belongs: with the deployment operator.

### Optional scheduled backup

For an externally supervised, unprivileged foreground service and its scheduler
commands, see [the office service runbook](OFFICE_SERVICE.md). The user-systemd
timer below applies to deployments that use a user systemd manager.

Edit `templates/beads-backup.service` for the installation paths, then copy it and `templates/beads-backup.timer` into the service account's `~/.config/systemd/user/`. Its `ExecStart` uses `backup --all`, so the one timer covers every project initialized in that runtime, including projects added later, and every run writes the `backup-status.json` record described above. Enable with `systemctl --user daemon-reload` and `systemctl --user enable --now beads-backup.timer`. Check `systemctl --user list-timers`, the service journal, and `backup-status --require-complete`; lingering must already be enabled for unattended operation. The timer performs same-host backup only. Configure off-machine copying, its completeness gate and retention separately; the timer does not copy anything off the host. These templates do not replace an existing team's backup schedule, and an existing installation that already runs a long-sync wrapper for this runtime keeps it. Once this kit's native step is deployed, that wrapper's monkeypatched `run_bd` and its `--timeout` ceiling are dead code: `admin.py backup` performs the native step itself through the Dolt SQL client (bounded by the 30-minute ceiling), and the wrapper's `dolt-backup-state.json` marker is no longer written or read by the kit.

`add-project` reads every installed `~/.config/systemd/user/beads-*backup*.service` unit for the account — `beads-backup.service` is only one of the names a deployment may use — and reports factually which units it read. It states that a `backup --all` unit covers every project; a recognised long-sync wrapper for this runtime is reported as covering only the projects its `--project` arguments name (a wrapper that names no project is not coverage of anything), because a project added later needs another wrapper line. Where no unit covers the project it prints, each under its own label, the shell command that runs a backup of every project now (`PYTHON KIT/admin.py --root ROOT backup --all`, as the service account pastes it), the `ExecStart` line for a schedule (a line of the unit file, not a shell command, with the unit directory it goes in) and the check to run afterwards (kittrial-5bb.200: it used to print the `ExecStart` line alone, which pasted into a shell answers `Permission denied`). It prints the exact `ExecStart` to add when a unit that names projects individually does not cover every project (a project list is replaced with the durable form; named projects and `--all` are never combined in one command) and never steers an operator off an existing wrapper. An absent or unreadable unit is reported as no coverage rather than assumed fine, and a unit that runs the backup through `sh -c` is deliberately still reported as not backing up the runtime: only a recognised `admin.py` or wrapper `ExecStart` can be attributed to this runtime with certainty, and a wrong "already covered" answer could leave a project silently off the schedule, so the conservative direction is the safe one (it can prompt a double-check, never hide a gap). It reads the unit files only — it never edits, installs or enables a unit — and systemd drop-ins (`*.service.d/*.conf`) are not inspected, so the report is about the unit files themselves and not about a drop-in override.

### Local and Windows clients

Use `client.local.example.json` for explicit Linux same-host execution, under an account with access to the runtime. Use `beads.cmd` on Windows or `sh beads.sh` on POSIX from any directory. The wrappers take the same required config/project/actor arguments as client.py; no PowerShell execution-policy change is required. See [operational examples](OPERATIONAL_WORKFLOW.md).

Installed kit provenance is read only from the adjacent non-executable `provenance.json`
manifest. A clean archive may commit the explicit placeholder source identity
`unknown`; it must not commit the SHA of an earlier revision. A release build generates
the manifest outside the source archive after selecting the exact archive revision:
`{"schema_version":1,"component":"orchestra-kit","version":"...","source_commit":"<40 lowercase hex>","build_id":"<immutable build id>","files":{"VERSION":"<sha256>","client.py":"<sha256>","version.py":"<sha256>"}}`.
The `files` map binds the identity to the packaged bytes (excluding the manifest
itself). Installation copies the manifest and those files as one artifact and validates
every digest before use. Only `unknown` or a complete 40-character lowercase source
commit is accepted. Missing, malformed, stale, or mismatched manifests report
`source unknown`; the client never imports adjacent Python modules, consults ambient
`ORCHESTRA_SOURCE_COMMIT`, or infers identity from a parent Git checkout.

## Maintenance

```sh
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime service status
journalctl --user -u beads-team.service -n 50
python3 /home/beads/beads-team-kit/admin.py --root /home/beads/beads-runtime service restart
```

If a client times out, inspect the task before retrying a mutation: the write may have committed. Do not replace a lost response with an assumed failure.

To retire the service, back up first, then `systemctl --user disable --now beads-team.service`. Preserve the runtime until recovery is no longer needed. Remove only the exact unit installed for that deployment and run `systemctl --user daemon-reload`. Linger is an account-wide setting: leave it enabled if other user services need it.

## Repeat validation

Local checks: `python -m unittest discover -s tests -v`.

Create two disposable projects and run the integration suite with an unused restore destination:

```sh
python tests/integration.py --config client.local.json --project testone --other-project testtwo --restore-project testrestore --output reports/local-integration.json
```

This restarts the configured server and creates synthetic records. Run it only on a disposable deployment, never a live team server. It checks two independent SSH clients, concurrent claims/comments, project separation, rendered corrections, restart and backup restoration.
