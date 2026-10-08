# Foreground office service and offline releases

This is the unprivileged, externally supervised deployment shape. The support
team's scheduler starts one foreground `office_service.py run` process and sends
SIGTERM to stop it. The process starts its own Dolt and authenticated HTTP/API
children, writes their output under `--logs`, and exits nonzero if either child
fails. It holds an exclusive lock under `--root`; a second start is refused.
Linux kills the children if the supervisor itself is SIGKILLed, and a subsequent
start uses the same durable runtime. This does not make an interrupted backup a
completed backup: inspect `backup-status` and run a fresh backup after such a
stop. No root or user systemd manager is required.

Use separate private paths for the installation, mutable runtime and logs.
Coordination records, HTTP identities and backup snapshots are private data,
not source code. The release tree contains code and public binaries only. The
operator supplies actual account names, paths, proxy, certificates, contact
route, schedule and artifact repository in the local service request; none
belong in this repository. Test on UAT first and obtain its verification output
before proposing a production rollout.

## Build and install an offline release

Build on a trusted Linux build host with Python 3.10+, the exact source commit,
the bd and dolt archives pinned by `versions.json` (see below for which bd), and a relocatable Python 3.10+ Linux
x86-64 `install_only` archive verified against its chosen upstream digest. For
RHEL 8, choose a build that supports glibc 2.28 or earlier. Pass its exact
relative executable path inside the archive. `build` verifies all three input
digests, packages the source from `git archive` rather than the working tree,
and writes exact commit provenance plus a SHA-256 for the finished artifact.

```sh
python3 tools/office_release.py build --repo <SOURCE_CHECKOUT> \
  --commit <FULL_COMMIT> --build-id <IMMUTABLE_RELEASE_ID> \
  --python-archive <PINNED_PYTHON_TARBALL> --python-sha256 <PINNED_SHA256> \
  --python-executable <PATH_INSIDE_PYTHON_ARCHIVE> \
  --bd-archive <PINNED_BD_TARBALL> --dolt-archive <PINNED_DOLT_TARBALL> \
  --output <RELEASE_TARBALL>
```

### What each bundled binary needs, and which bd to bundle

A release bundles three programs, and each must be able to start on the target host:

| Program | What it needs |
|---|---|
| the bundled Python | whatever the chosen build needs; for RHEL 8 choose one for glibc 2.28 or earlier, as above |
| dolt (the `dolt` entry of `versions.json`) | nothing: it is statically linked |
| bd, the `bd` entry of `versions.json` (upstream's release archive) | **glibc 2.34 or later.** It cannot start on RHEL 8 (glibc 2.28): the loader says `version 'GLIBC_2.34' not found` |
| bd, the `bd_static` entry of `versions.json` | nothing: it is statically linked. **Bundle this one for RHEL 8**, or for any host whose glibc is older than 2.34 |

`bd_static` is the same bd version built from the same source commit without cgo
(`CGO_ENABLED=0`). Upstream publishes no static build of this version, so the kit
builds it: `tools/build_bd_static.sh WORKDIR` (it needs docker, the Go image by
digest and the Go modules, each checked; the compile runs with the network off).
The archive is `WORKDIR/out/beads_1.2.2_linux_amd64_static.tar.gz`; two runs give
the same bytes, and its SHA-256 must equal the `bd_static` entry, which also
records the image digest, the source commit and the flags. It has no download
URL: hand the archive to the build with `--bd-archive`, as the Python archive
is handed over.

What the static build lacks, and why that does not matter here. cgo is what bd
uses for its embedded Dolt engine, for `bd federation`, and for ten `bd doctor`
checks that open the database directly. Upstream's source says of a build
without cgo: "only server mode is supported; embedded mode returns an error
directing the user to server mode". This kit only ever runs bd against its own
Dolt server (`bd init --server --external`) and uses neither `bd doctor` nor
`bd federation`. The kit's real use of bd was run with both binaries and gave
the same answers (kittrial-5bb.161). In one sentence: **the `bd_static` build
has no embedded Dolt, and the kit only uses server mode.** The kit's own tests
follow that: `tests/test_bd_label_aliases.py` starts a scratch Dolt SQL server
from the `dolt` binary beside bd and makes its tracker with
`bd init --server --external`, so its three tests run with either binary; they
skip, with that reason, only when no `dolt` is beside bd.

`build` accepts either pinned bd archive for `--bd-archive`, says which entry it
is, and records it in the manifest (`pins`). Name the target's glibc and the
build refuses a release that could not start there:

```sh
python3 tools/office_release.py build ... --bd-archive <BD_STATIC_TARBALL>   --dolt-archive <PINNED_DOLT_TARBALL> --target-glibc 2.28 --output <RELEASE_TARBALL>
```

- With `--target-glibc`, a bundled Python, bd or dolt that needs a newer glibc
  stops the build: `bd needs glibc 2.34; the target has glibc 2.28. It could not
  start there. Bundle a build of it for the target: for bd, the archive
  versions.json pins as bd_static`. What each needs is read from the file itself
  and is recorded in the manifest (`binaries`), whether or not a target is named.
- `build` also starts bd and dolt once on the build host (`bd --version`,
  `dolt version`). `--no-run-check` skips that, for a build host that cannot run
  the target's binaries. The check only starts each program and reads what it
  says: it does not identify the program, so a shell script named `bd` that
  prints a version would pass it. The digest pin in `versions.json` is what
  guarantees which bd is bundled (`pin_of` refuses an archive that is neither
  pinned entry), not the run check. Neither program is expected to leave nothing
  behind, so each is started with its own metrics-off switch (kittrial-5bb.166).
  bd is started with `BD_DISABLE_METRICS=1`, the guard the runtime sets
  (`admin.environment`), because it otherwise leaves a detached child that queues
  its usage-metrics event kit (`$HOME/.config/bd/config.yaml`,
  `$HOME/.beads/eventsData/`) just after `bd --version` returns. dolt is started
  with `DOLT_DISABLE_EVENT_FLUSH=1`, which stops the detached `dolt send-metrics`
  child it re-executes after `dolt version` returns; without that variable dolt
  still makes that child even when `metrics.disabled` is set in its global config
  (measured on koopa with `strace -f -e trace=execve`, 1 exec without the
  variable and 0 with it). `dolt version` itself still writes its global config,
  a version-check file and an event lock under `$HOME/.dolt/`
  (`config_global.json`, `version_check.txt`, `disable_version_check.txt`,
  `eventsData/dolt.lock`). Those late writes used to make the run check's scratch
  directory fail to remove itself (`[Errno 39] Directory not empty`, about one
  build in three, kittrial-5bb.166), so the scratch is also removed with errors
  ignored and retried until it stays gone; no `office-release-check-*` directory
  is left in TMPDIR.
- `install` starts the bundled Python, bd and dolt on the target itself before it
  switches `current`. One that cannot start refuses the install and nothing is
  switched; the refusal names the program, what the loader said, what the file
  needs and the host's glibc. `verify` repeats the check on the installed release
  and prints which bd it carries.
- `prepare` installs the bundled bd into a NEW runtime. A runtime that already
  has a bd keeps it, whichever pinned entry it is: `prepare` never swaps one for
  the other under an installation that is in use.
- `tools/office_verify.py` reports, as `bd`, which pinned entry the runtime's bd
  is and whether it starts on the host.

Publish the artifact, its SHA-256, this installation tool and the source commit
through the approved artifact route. On RHEL 8, run the installer with
`/usr/libexec/platform-python` (Python 3.6); `python3` may not be installed.
The installer needs only that standard library; the service uses the bundled
interpreter. `install` verifies the artifact digest and inner archives, extracts
to `releases/<ID>`, checks the bundled interpreter version and that bd and dolt
start on this host, then switches
`current` by atomic symlink rename. It preserves the prior release as
`previous`. A release ID names one build for good: another build under an ID
that is installed is refused ("Release ID already installed, and what is
installed under it is not this archive"); give it an ID of its own. The SAME
release installed again is not unpacked a second time: see "Going forward again
after a rollback" below.

```sh
<INSTALLER_PYTHON> office_release.py install --archive <RELEASE_TARBALL> \
  --sha256 <RELEASE_SHA256> --install-root <INSTALL_ROOT>
<INSTALLER_PYTHON> office_release.py verify --install-root <INSTALL_ROOT>
<INSTALLER_PYTHON> office_release.py rollback --install-root <INSTALL_ROOT>
<INSTALLER_PYTHON> office_release.py activate --install-root <INSTALL_ROOT> --release <ID>
```

Stop the supervised process before switching releases, then start it from the
new `current` link. The installer and rollback command print a restart reminder;
do not let an old process serve requests after switching. Each running child
uses paths pinned to the release that started it, and restarting makes the new
release active. Rollback switches code and interpreter; it never rolls back
the mutable runtime. Preserve a verified backup before any upgrade whose state
format changes. The support team's release record should include artifact
digest, source commit, UAT verification output and the previous release ID.

**Going forward again after a rollback.** `rollback` makes `previous` the current
release and keeps the one it left as `previous`; that release stays installed
under `releases/<ID>`. To return to it, or to any other release that is still
installed:

- `install` its archive again, exactly as the first time. The archive is checked
  against `--sha256`; the installed release must be that archive (its stored
  manifest is the archive's, byte for byte: the same build ID, source and
  interpreter digests); the bundled interpreter, bd and dolt are started from the
  installed release as a first install starts them; then `current` is switched to
  it and what was current becomes `previous`. Nothing is unpacked or written under
  `releases/`. It prints `Current release is now <ID> (it was installed already;
  its manifest is this archive's)` and the restart reminder. If the release is
  the current one already, it says so and changes nothing.
- `activate --release <ID>` when the archive is no longer at hand: the same
  start checks and the same switch, by the folder name under `releases/`. It
  says that the release was not compared with an archive.

`rollback` itself prints which of the two to use for the release it has just
left. (Before kittrial-5bb.189 `install` answered `Release ID already installed`
for it, and the only way forward was a second `rollback`, which swaps the two
links back but which nothing mentioned, or a new build of the same source under
another ID.) What these do not check: an installed release is not hashed file by
file, because nothing but its manifest was stored to compare with; a release
whose files were changed after it was installed is caught only if its interpreter,
bd or dolt no longer start. `activate` compares nothing but the build ID the
stored manifest states with the folder name: a `source_commit` edited in an
installed manifest is what `verify` then prints as the release's source. Only
`install` of the archive compares the whole manifest. A stored manifest the tool
cannot read (not JSON, not an object, no build ID or interpreter named) is
refused by `activate` and `verify` with a sentence that names the file.

A switch is two links, `previous` and then `current`, and a run can be killed
between them. Nothing that is served has changed by then: `current` still names
the old release, and running the same command again completes the switch. Until
then `previous` names the current release too, and `rollback` refuses: `previous
and current both name releases/<ID>: a switch was interrupted between its two
links`, with the installed releases and what to run (`activate --release <ID>`,
or `install` of the archive, for the release that should be current). A
`rollback` killed between its own two links has gone back and left the same
state; a second `rollback` is refused the same way. As with every switch: stop
the supervised process first and start it from the new `current`; `add-project`'s printed paths name a
release where there is no `install/current` link and must be printed again.

Binary pins on rollback. Roll back only to a release that knows both pinned bd
archives. A release built before kittrial-5bb.161 knows only the `bd` (upstream)
pin: its `prepare` compares an existing runtime bd with that one pin, so a
runtime carrying `bd_static` (installed by a newer release) stops with
`Binary/pin mismatch; use a new deployment root for upgrade: <RUNTIME>/bin/bd`
on the next `prepare` after the rollback. Nothing is replaced and the runtime is
otherwise untouched; a new deployment root is the documented way forward. That
release's installer is readable as `git show 87a356c^1:bootstrap.py`. A rollback
that stays within releases that know both pins has no such step.

Rules 1 and 2 on rollback (kittrial-5bb.194). A rollback to a kit built before
kittrial-5bb.194 also rolls back the session registry's `owners` map and the
`--principal` key binding. The older kit's session validator refuses a registry that
carries `owners`, so for a project where an actor was adopted or a session was registered
under a bound key it refuses `session show`, `session resume`, `session register`,
`session run start` and `actor-standing`, and **`admin.py backup PROJECT` fails for that
project with status incomplete**. A key whose line carries `--principal` is refused on
every request (the older wrapper does not know the argument), while `bd` itself, `work`
and `review` keep working. The way forward is the one OPERATIONS.md ("Bind a key to its
principal", Downgrade limit) documents: remove the `owners` key from
`projects/PROJECT/.sessions.json` by hand and reprint the keys without `--principal`
before the rollback, or restore with a kit that knows rule 2. Decide this before rolling
back an installation that has used rule 2, exactly as for the binary pins above.

## Prepare a private runtime

Use the bundled interpreter, with `<PYTHON>` denoting
`<INSTALL_ROOT>/current/python-runtime/<PATH_INSIDE_PYTHON_ARCHIVE>` and `<KIT>`
denoting `<INSTALL_ROOT>/current/kit`. `prepare` copies the release's digest
pinned Dolt and Beads archives into `<RUNTIME_ROOT>/bin`, creates a private
database configuration and initializes nonsecret Dolt settings. It never
starts a systemd unit. It runs the installed `bd --version` on both a fresh and
an existing runtime, so a bundled bd that cannot start (for example one built
against a newer glibc) fails `prepare` with the loader's sentence instead of
being reported as prepared. The first `run` sets the fresh Dolt root password;
if stopped between that change and the next check, the next `run` verifies the
stored password and continues. Keep `deployment.private.json` private.

**git must be on the service's PATH.** bd runs `git` when it initializes a
project, so `admin.py add-project` needs it, and so does the account that
starts `office_service.py run`. `add-project` (and the web project-creation
action, which runs the same work) checks for git **before** creating anything
and refuses with a plain sentence when it is missing, so a host without git
gets that sentence instead of a half-made `projects/NAME`. Nothing is removed
after that check: a creation that fails later leaves `projects/NAME` exactly
as the failure left it, for an operator to finish or remove. `add-project` keeps the
same `project-creations/NAME.json` record the web creation path keeps, so
`finish-project` and `remove-creation` act on a stopped CLI creation too, and a second
`add-project` that meets the leftover names those two commands instead of only saying
"Project already exists" (kittrial-5bb.176).
The host's packaged git is not required: any git on PATH will do
(`export PATH=<DIR_WITH_GIT>:$PATH` in the shell or the scheduler's entry
that starts the service and runs the admin commands). git 2.21.0 is known to
work: the coordinator's install rehearsal on AlmaLinux 8 ran add-project,
backup and health with it.

```sh
<PYTHON> <KIT>/office_service.py prepare --root <RUNTIME_ROOT> --db-port <DB_PORT>
```

Create an operator-owned mode-0600 JSON file outside source control:

```json
{"schema_version":1,"http_host":"127.0.0.1","http_state":"<RUNTIME_ROOT>/http-state.json","trusted_proxies":["127.0.0.1"],"public_url":"https://<APPROVED_HOST>"}
```

By default the HTTP listener is loopback only, and a separately approved
reverse proxy terminates TLS. The next section has the other two ways to serve
it. With no proxy yet, the supported first-install shape is an SSH tunnel to
that loopback listener:

```sh
ssh -N -L <LOCAL_PORT>:127.0.0.1:<HTTP_LOOPBACK_PORT> <HOST>
```

and open `http://localhost:<LOCAL_PORT>`. `localhost` is a browser secure
context, so the interface works over plain HTTP there; set `public_url` to that
`http://localhost:<LOCAL_PORT>` URL. What does not work is a secure-context-only
feature reached over plain HTTP on a host name rather than `localhost`, such as
the save-to-folder button; give those a real TLS endpoint.

Bootstrap the first HTTP superuser while the service is **stopped**. A running
service holds its state in memory and writes it back, so an account created
underneath it is lost. The command needs `--root <RUNTIME_ROOT>`, takes the
runtime lock and refuses while a service holds it:

```sh
<PYTHON> <KIT>/http_service.py --bootstrap-user <NAME> \
  --state <RUNTIME_ROOT>/http-state.json --root <RUNTIME_ROOT>
```

`--root` must name an existing runtime directory; a path that is not one is
refused with one sentence, not a traceback. The guard is only as good as
`--root`: it locks the root you name, so naming a different root while a
service holds the real one gets past the lock and the new account is lost as
before. The password is entered interactively. The `endpoint` backend is
selected by
the supervisor and operates against this runtime's canonical project data.
Start the service once, then run `<PYTHON> <KIT>/admin.py --root <RUNTIME_ROOT>
add-project <PROJECT>` before scheduling `backup --all`. A new empty runtime
has no initialized project to back up. `add-project` prints the worker client
configuration, the bootstrap command and a second, local-transport example for
an agent that runs on the server itself. Where the installation has an
`<INSTALL_ROOT>/current` link, the endpoint and the interpreter in those lines
are printed through it, so an upgrade needs no change to a configuration printed
by this release or later. An installation with no `current` link (the live kits
keep `<BASE>/kit -> <BASE>/releases/<ID>`) prints the release spelling instead,
and says so: print that configuration again after an upgrade or it keeps running
the release that printed it. In that
configuration `python` names the interpreter that runs the endpoint on this
server when a worker on another machine reaches it over SSH (and the same
interpreter for the local transport): the bundled one. `host` is the worker's
to fill in and must be a name or address the worker's machine can reach; this
server's own host name may not resolve from the worker's network. (The
service's `public_url` is where the web interface is published, which need not
be where SSH lands, so the kit does not guess the host from it.) A
project made on the host is not on the web yet: nothing of it appears in the web
interface until a superuser registers it there (New project, with this name, or
`POST /v1/projects` without `create`).

**What must be regenerated after an upgrade.** A client configuration or
`authorized_keys` line printed by a release *before* this one names
`releases/<ID>` rather than `current`. The old folder stays on the host, so
those clients keep running the kit of that release against the new runtime while
the service runs the new one. After `install` or `rollback`, for every worker
whose configuration or key line names a `releases/<ID>` path:

- change `endpoint` to `<INSTALL_ROOT>/current/kit/endpoint.py` and `python` to
  the bundled interpreter, `<INSTALL_ROOT>/current/python-runtime/<PATH>` (the
  `add-project` output of this release has both);
- re-run `admin.py --root <RUNTIME_ROOT> authorized-keys --key-file <KEY.pub>`
  and replace that contributor's `authorized_keys` entry with the printed line.
  A confined key's `--endpoint` is compared as one token against the `endpoint`
  in that contributor's client config, so those two change together.

A configuration or key line that already names `<INSTALL_ROOT>/current/...`
needs nothing: the link moves with the upgrade. A bare `python3` in an old
client config is the other half of the same problem: on RHEL 8 it is
platform-python 3.6, which cannot run the endpoint, and a host with no `python3`
on PATH fails outright, so name the bundled interpreter instead. Since
kittrial-5bb.191 an interpreter older than 3.10 is told so before anything is
imported: every program of the kit that needs 3.10 prints one line
(`endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8
(/usr/bin/python3). Nothing was carried out. ...` and what to do) and exits 2,
where the endpoint used to die in a regular-expression traceback: `endpoint.py`,
`client.py`, `admin.py`, `office_service.py`, `http_service.py`,
`http_client.py`, `lifecycle.py`, `coordination.py`, `capabilities.py`,
`requirement_records.py`, `setup_assistant.py`, `worker.py`, `worker_gate.py`
and `tools/office_verify.py`. That includes `office_service.py health`, which
happened to run under 3.6. `tools/office_release.py` and
`ssh_forced_command.py` have no such check: they run under the host's
Python 3.6 on purpose. The client shows the endpoint's line as it is, without
its usual "outcome may be uncertain"; with a confined key (`"forced_command":
true`) the interpreter is the one in the `authorized_keys` line, so the client
says that the line must be printed again (`admin.py authorized-keys`) instead
of pointing at its own configuration.

The client half of those lines is a change in this kit's `client.py`: it runs the
interpreter the config names, where older clients ran a literal `python3` on the
server and ignored the `python` key. A worker therefore needs the `client.py` of
this release or later for a printed `python` to take effect; an older client with
the new config still runs `python3` on the server, which on a host without one
fails with `python3: command not found` (observed after a rollback,
kittrial-5bb.182 item 5).

## How the web interface is reached: three shapes

The private office JSON decides. Loopback is the default, and a file that sets
none of the settings below behaves as it always did.

| Shape | Private JSON | People open | Prefer it when |
| --- | --- | --- | --- |
| **HTTPS on an open port** | `"http_host": "<ADDRESS>", "cert": "<CERT.pem>", "key": "<KEY.pem>"` | `https://<ADDRESS>:<PORT>` | the host has a port open to the office network and no proxy. The first choice of the three for that case. |
| **Loopback, behind a proxy or a tunnel** (the default) | `"http_host": "127.0.0.1"` or nothing | the proxy's address, or `http://localhost:<PORT>` through `ssh -L <PORT>:127.0.0.1:<PORT> <HOST>` | an approved reverse proxy terminates TLS, or only a few people use it and each can tunnel. |
| **Plain HTTP on an open port** | `"http_host": "<ADDRESS>", "allow_plaintext_on_network": true` | `http://<ADDRESS>:<PORT>` | never for long: a stopgap on a network you trust, until a certificate is made. |

- `http_host` is the address the service binds: an address of the host, or its
  name. `0.0.0.0` (or `::`) binds every address; the service then cannot name
  itself, so `public_url` must be set to the address people use. Otherwise
  `public_url` defaults to `https://<ADDRESS>:<PORT>` (or `http://...` for
  plain HTTP); it is what agent prompts and set-up texts carry, so it must be
  an address the agents' machines can reach.
- An address other than loopback without `cert` and `key` is refused, unless
  the file says `"allow_plaintext_on_network": true`. The refusal names the
  setting. With it the service starts and prints, at every start, on standard
  error and in `http.log`: `WARNING: serving plain HTTP on <ADDRESS>:<PORT>.
  Passwords and session cookies cross the network unencrypted. Use cert and
  key for HTTPS.` The setting together with loopback, or together with a
  certificate, is refused: it would do nothing there.
- Which ports are open, and to whom, is the host's firewall and the network's
  business. The service listens where it is told; it does not restrict who may
  connect beyond its own log-in.

**A self-signed certificate** is enough for the HTTPS shape. Make one on the
host with its own openssl, naming the address (or the name) people will type,
and keep the key readable by the service account only:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout <KEY.pem> -out <CERT.pem> -subj "/CN=orchestra-office" \
  -addext "subjectAltName=IP:<ADDRESS>"        # or DNS:<HOST_NAME>
chmod 600 <KEY.pem>
```

(`-addext` needs OpenSSL 1.1.1, which RHEL 8 has.) What people then see: the
browser does not know who signed the certificate, so on the first visit it
shows a warning page ("Your connection is not private" or "Warning: Potential
Security Risk Ahead"); they choose to continue, once per browser, or the
certificate is added to the machines' trusted certificates. After that the
connection is encrypted like any HTTPS connection; what a self-signed
certificate does not give is proof to a first-time visitor that this is the
right server. A client such as `curl`, or an agent's tool, must be given the
certificate file (`curl --cacert <CERT.pem>`) or it refuses the connection, as
it should. Replace the certificate before it expires (`-days`), then restart.

**What does not work over plain HTTP at a host name or address.** Browsers give
some features only to a secure context (HTTPS, or `localhost`):

- the button that saves an agent's set-up files straight into a folder is not
  offered; the files are shown to copy instead;
- copying to the clipboard falls back to the older method, which works in
  current browsers but may be removed by them;
- the session cookie is not marked `Secure`, and the password and that cookie
  cross the network readable by anyone on the path.

Log-in, the session cookie, the request-forgery check on every change, and
issuing an agent's credential work the same in all three shapes. Through an SSH
tunnel the page is opened as `http://localhost:<PORT>`, which the browser
treats as a secure context, so nothing above is lost there.

`health` and `tools/office_verify.py` ask the listener the file describes when
they are given it (`--config <PRIVATE_OFFICE_JSON>`): the configured address,
over HTTPS when a certificate is set. That probe does not verify the
certificate: it is the service asking its own listener, on the host, whether it
answers, and a self-signed certificate must not make it read as down. Without
`--config` they ask `http://127.0.0.1:<PORT>`, which is right for the loopback
shape only. The verification record's `listener` says which shape is in use
(`loopback`, `https` or `plain-http-on-network`), the host and the public URL.

**Connections that say nothing.** On an open port anybody who can reach it can
open a connection and send nothing: a port scanner, a browser's spare
connection, a laptop that left half way through a TLS handshake. The web
service bounds what such a connection costs, in all three shapes:

- Each connection has its own thread, and the TLS handshake is done there. A
  connection that has not finished its handshake delays nobody else.
- The service waits **30 seconds** for a client each time it needs something
  from it: to finish the handshake, to send a whole request (the line and
  headers; then the body), to take a response; an idle keep-alive connection
  is kept as long. Then the connection is closed, whatever it had sent so far
  (a client that sends a byte now and then is closed too). The time the
  service itself takes over a request does not count.
- At most **200** connections are served at once. One more is closed at once,
  and the log says so the first time (`connections: the limit of 200 open
  connections was reached ...`). So 200 silent connections stop the web
  interface for up to 30 seconds, and `health` reads `web=down` meanwhile.
- Of those, at most **100** are from one client address. One more from it is
  closed at once, before a thread or a handshake is spent on it, and the log
  says so (`connections: 'ADDRESS' has 100 connections open, the limit for
  one address; further ones from it are closed at once`), at most one such
  line a minute; the next one says how many were not shown. So one address
  that keeps reopening silent connections holds 100 places and no more, and
  every other address is served meanwhile (measured: other clients answered
  70 times of 70; without the limit, 0 of 70). An IPv6 address is counted
  with its whole /64. What it does not stop, measured: **two addresses
  acting together can take all 200** (115 silent connections each: other
  clients answered 0 of 30); and the clients of an address that is at its
  limit, honest ones included, are closed with it until it lets go.
  `"connections_per_address": N` in the service configuration changes the
  number (1 to 200). It is half and not less because people who share an
  address share it: an office behind one router is ONE address. A browser
  uses up to six connections while a page loads and lets them go within 30
  seconds; the page keeps none open; the worker client uses one for the
  time of a command. So 100 is about sixteen browsers loading at the same
  moment. A lower number protects better against one address and reaches an
  office's own people sooner; a higher one the other way round, and each
  silent connection costs the service a thread (with 100 held by one address
  the web process peaked at about 240 MB with a new state document).
- **Behind the approved proxy** every connection comes from the proxy, so an
  address named in `trusted_proxies` is not limited as an address. The limit
  is then on requests: one forwarded address (the last `X-Forwarded-For`
  element, the one the proxy itself added) has at most 100 requests being
  served at once, and one more is answered `503 busy` with `Retry-After: 1`
  before anything is carried out. Silent connections are then held at the
  proxy and never reach the service, so bounding those per address is the
  proxy's business (for nginx: `limit_conn`). A proxy in front that is NOT
  named in `trusted_proxies` makes all its clients one address with 100
  connections between them: name it.
- **Through the SSH tunnel** (or any client on the service's own host)
  everybody arrives from 127.0.0.1 and cannot be told apart. With
  `trusted_proxies: ["127.0.0.1"]`, as in the configuration shown above,
  that address is not limited at all for connections; without it, it is one
  address with 100. Either way the people behind it are NOT one client for
  logging in (below).
- A handshake that fails is one line in the http log (`tls: 'ADDRESS': 'TLS
  handshake not completed: ...'`), at most one such line every 5 seconds; the
  next one says how many were not shown. A browser that has not accepted a
  self-signed certificate shows there. A port scan does not fill the log.
- Where the process cannot start a thread before the 200 are reached (a memory
  or thread limit set on it), the connection is closed in the same way and the
  log says so once (`connections: no thread could be started ...`).
- A request is its whole body: one that ends before its `Content-Length` is
  not carried out and gets no answer.
- A legitimate request is not cut for taking long to ANSWER: the 30 seconds
  run only while the service waits for the client. An endpoint action that
  takes minutes is answered. What can be cut is a client so slow that sending
  its request (at most 256 KiB) or taking a response takes more than 30
  seconds.

**Log-in attempts need no credentials, so their cost is bounded.** A password
check takes 16 MiB of memory for about a tenth of a second, and they are made
one at a time. Three bounds keep a flood of attempts, with any user names, from
taking the memory of the host or the log-in of everybody else:

- Every check is computed in one thread of the web process, so the memory of
  one check is what the checks keep, whatever the number of connections.
  (Before, 190 attempts at the same moment left 3 GB resident: each
  connection's thread kept its own 16 MiB. That was the C allocator, not the
  checks running at once.)
- At most **16 log-ins are in flight at once** (one being checked, the others
  waiting their turn). One more is answered at once with 503 `busy`,
  `Retry-After: 5`, "Too many people are logging in at this moment. Try again
  in a few seconds.", whatever the user name: nothing is checked, nothing is
  counted against the name, and no count is cleared.
- Of those 16 places **one client address holds at most 4** (an IPv6 address
  with its /64; behind the approved proxy the forwarded address). A log-in
  over its address's share is not turned away at once: **it waits up to 2
  seconds for one of that address's places**, and only then is answered 503
  in the same way, with "Too many log-ins from your address are being checked
  at this moment. Try again in a few seconds." At most 12 log-ins of one
  address wait like that at once (one more is refused at once), so an address
  parks at most 12 of the service's threads however many connections it has.
  Parked log-ins have no bound of their own beyond those 12 per address: many
  addresses can each park 12, and what bounds them in all is the 200
  connections. A log-in that wakes when every one of the 16 places is taken is
  answered like anybody else ("Too many people are logging in at this moment").
  `"logins_per_address": N` in the service configuration changes the share
  (1 to 16); the 2 seconds and the 12 are fixed.

**People who share an address.** An office behind one router is one client
address. Its people logging in together at nine in the morning each need a
tenth of a second of checking; with the wait all of them get in: ten at the
same moment from one address, all ten in at the first try, the slowest
answered after about half a second (about two seconds with the audit log
full). Without the wait six of the ten would be told to try again.
**Through the SSH tunnel**, and for anything else that arrives from the
service's own host or from a trusted proxy that forwards no address,
everybody comes from the same address and nobody can be told apart: such a
log-in has no share per address at all and is held to the 16 places only.
Ten at the same moment through the tunnel: all ten in, with
`trusted_proxies: ["127.0.0.1"]` and without it. The price, said plainly:
somebody who floods log-ins THROUGH the tunnel keeps everybody out for as
long as they do (measured: not in within a minute); they already have a
log-in on the server.

Measured over HTTPS with the real service in a container of its own (the
release's Python 3.12; made-up user names sent in a loop, a new connection
each time, 45 seconds each), while a person at another address logs in with
the right password and somebody already logged in reads a page once a second:

| Who floods | The person gets in after | A logged-in read, median | Web process, peak |
|---|---|---|---|
| one address, 20 / 40 / 100 clients | 0.3 / 0.4 / 0.7 s | 0.4 / 0.8 / 1.0 s | 100 / 138 / 238 MB |
| the same with the audit log full (2.8 MB state) | 4.4 / 1.3 / 1.5 s | 1.3 / 1.7 / 1.7 s | 224 / 338 / 590 MB |
| two addresses together, 20 clients each | 1.3 s (2.7 s) | 1.4 s (2.9 s) | |
| three addresses together | 2.2 s (3.7 s) | 2.3 s (4.8 s) | |
| four addresses together | not within a minute | 3.4 s (5.6 s) | |

(In brackets: with the audit log full. Before any of this, with the audit log
full and one address flooding with 100 clients: not in within a minute, and a
peak of 2.5 GB.) The process starts at about 45 MB (57 MB with the audit log
full).

What this does not do, measured in the same runs:

- **Four addresses acting together take all 16 places** and keep everybody
  else out of logging in. Three do not.
- **A person at the flooding address itself** shares its four places with the
  flood: they got in after half a minute to a minute (five to ten tries), not
  at once.
- **Ten people at ANOTHER address during a 100-client flood** from one: eight
  in at the first try and two told to try again (seven and three once), the
  slowest answer after 3 to 6 seconds: the checks are slower under the flood
  and two of the ten wait longer than the 2 seconds.
- **Somebody who is already logged in is slowed down**, as the table shows,
  but far less than a flood without the wait would slow them: a refused
  attempt now holds its connection for 2 seconds, which slows the flood
  itself (with a full audit log every checked failed log-in is also audited
  and rewrites the whole state document under the state lock).
- **Memory is not flat under a sustained flood.** The checks keep 16 MiB; the
  rest of the peak is the state document being written again for every
  checked attempt, in the thread of each connection, and it stays resident
  afterwards (445 MB and 723 MB after all the runs above).
- The lockout per user name and address is unchanged.

## External scheduler commands

The scheduler should set a private working directory, pass the log and runtime
paths below, and treat a nonzero exit as a failed service. A child crash makes
the supervisor exit nonzero; the external scheduler must restart it. It may run 24x7 or
follow the approved day schedule. Send SIGTERM and allow at least the selected
`--stop-seconds` plus scheduler overhead before SIGKILL; the default child
deadline is five seconds, shared with time reserved for Dolt; a child that
requires SIGKILL makes the supervisor exit nonzero. The supervisor tightens the
log directory to mode 0700 only when this run newly created it and the service
account owns it; a pre-existing `--logs` directory (for example one owned by a
supervisor account that collects logs as another user or group) is left with the
mode and ownership it already has, and no chmod there can stop startup. Log files
are always opened mode 0600 with `O_NOFOLLOW`, so their confidentiality does not
depend on the directory mode. The external scheduler
owns log rotation, retention, and the log directory's ownership and mode. On a
SIGKILL restart, verify backup state before
depending on a possibly interrupted native backup.

```sh
<PYTHON> <KIT>/office_service.py run --root <RUNTIME_ROOT> \
  --logs <LOG_DIR> --config <PRIVATE_OFFICE_JSON> --port <HTTP_LOOPBACK_PORT> \
  --stop-seconds 5
```

Schedule these separate commands while the service is up:

| Cadence | Command | Required result |
| --- | --- | --- |
| Daily, after startup | `<PYTHON> <KIT>/admin.py --root <RUNTIME_ROOT> backup --all` | Exit 0; durable long-sync backup and status file. |
| Frequent | `<PYTHON> <KIT>/office_service.py health --root <RUNTIME_ROOT> --port <HTTP_LOOPBACK_PORT> --config <PRIVATE_OFFICE_JSON>` | Exit 0 and one line with version, DB, web and latest backup status/time. |
| After successful backup, optional | `<PYTHON> <KIT>/admin.py --root <RUNTIME_ROOT> backup-copy <OFF_MACHINE_STAGING>` | Exit 0 before the approved encrypted off-box transfer. |

`health` exits nonzero if the database or web listener is down, if a backup
failed, or if no completed backup is recorded. It reports a missing backup as
`unknown`, never as success. `backup --all` writes `backup-status.json` and
preserves the previous complete pair on failure. `backup-copy` gates on all
initialized projects having complete pairs. The external scheduler owns the
timer, encryption, retention and alert destination.

Collect a read-only, machine-readable UAT record after startup and backup:

```sh
<PYTHON> <KIT>/tools/office_verify.py --install-root <INSTALL_ROOT> \
  --root <RUNTIME_ROOT> --port <HTTP_LOOPBACK_PORT> --config <PRIVATE_OFFICE_JSON>
```

It checks that the installed source provenance matches the release manifest,
reports the bundled Python and host libc, and includes the health result and
exit code. It reads the runtime but changes no records or service state.

For recovery, stop the service, inspect the last backup status and sidecar,
follow `docs/OPERATIONS.md` for `restore-new` into a **disposable** project,
compare exports, and only then make an explicit operator decision on the live
runtime. Keep the native directory, coordination sidecar and operation journal
snapshot together. Do not test restore against a live installation.

## UAT verification record

Have the service-account operator return: OS and glibc version, release ID and
source commit from `verify`, artifact SHA-256, first and second start results,
`health` before and after the daily backup, SIGTERM stop duration, forced
SIGKILL/restart outcome, disposable restore/export comparison, install/rollback
round trip, and any skipped check with its reason. The coordinator reviews this
record and the support runbook before a production release. Contact and
escalation names belong in the private support ticket.
