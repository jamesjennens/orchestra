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
has no embedded Dolt, and the kit only uses server mode.** The one place this
shows is in the kit's own tests: `tests/test_bd_label_aliases.py` makes its
tracker with a bare `bd init` (embedded mode), so its three tests are skipped
when the suite is run with `ORCHESTRA_BD_BIN` set to the static bd; the same
three tests pass with it against a tracker the kit made in server mode.

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
  the target's binaries.
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
`previous`. A repeated release ID is refused; install a new immutable ID.

```sh
<INSTALLER_PYTHON> office_release.py install --archive <RELEASE_TARBALL> \
  --sha256 <RELEASE_SHA256> --install-root <INSTALL_ROOT>
<INSTALLER_PYTHON> office_release.py verify --install-root <INSTALL_ROOT>
<INSTALLER_PYTHON> office_release.py rollback --install-root <INSTALL_ROOT>
```

Stop the supervised process before switching releases, then start it from the
new `current` link. The installer and rollback command print a restart reminder;
do not let an old process serve requests after switching. Each running child
uses paths pinned to the release that started it, and restarting makes the new
release active. Rollback switches code and interpreter; it never rolls back
the mutable runtime. Preserve a verified backup before any upgrade whose state
format changes. The support team's release record should include artifact
digest, source commit, UAT verification output and the previous release ID.

## Prepare a private runtime

Use the bundled interpreter, with `<PYTHON>` denoting
`<INSTALL_ROOT>/current/python-runtime/<PATH_INSIDE_PYTHON_ARCHIVE>` and `<KIT>`
denoting `<INSTALL_ROOT>/current/kit`. `prepare` copies the release's digest
pinned Dolt and Beads archives into `<RUNTIME_ROOT>/bin`, creates a private
database configuration and initializes nonsecret Dolt settings. It never
starts a systemd unit. The first `run` sets the fresh Dolt root password; if
stopped between that change and the next check, the next `run` verifies the
stored password and continues. Keep `deployment.private.json` private.

**git must be on the service's PATH.** bd runs `git` when it initializes a
project, so `admin.py add-project` needs it, and so does the account that
starts `office_service.py run`. The host's packaged git is not required: any
git on PATH will do (`export PATH=<DIR_WITH_GIT>:$PATH` in the shell or the
scheduler's entry that starts the service and runs the admin commands). git
2.21.0 is known to work: the coordinator's install rehearsal on AlmaLinux 8
ran add-project, backup and health with it.

```sh
<PYTHON> <KIT>/office_service.py prepare --root <RUNTIME_ROOT> --db-port <DB_PORT>
```

Create an operator-owned mode-0600 JSON file outside source control:

```json
{"schema_version":1,"http_host":"127.0.0.1","http_state":"<RUNTIME_ROOT>/http-state.json","trusted_proxies":["127.0.0.1"],"public_url":"https://<APPROVED_HOST>"}
```

By default the HTTP listener is loopback only, and a separately approved
reverse proxy terminates TLS. The next section has the other two ways to serve
it. Bootstrap the first HTTP superuser through the documented
`http_service.py --bootstrap-user` prompt under the bundled interpreter;
the password is entered interactively. The `endpoint` backend is selected by
the supervisor and operates against this runtime's canonical project data.
Start the service once, then run `<PYTHON> <KIT>/admin.py --root <RUNTIME_ROOT>
add-project <PROJECT>` before scheduling `backup --all`. A new empty runtime
has no initialized project to back up. `add-project` prints a worker client
endpoint under that release's exact path. Regenerate each worker's client
configuration after install or rollback if its endpoint still points to an
older release.

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
  interface for up to 30 seconds, and `health` reads `web=down` meanwhile; a
  client that keeps opening them keeps it stopped. The service has no
  per-address limit: where that matters, put it behind the approved proxy or
  a firewall rule and use the loopback shape.
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
