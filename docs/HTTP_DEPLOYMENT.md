# Office HTTP service: installation, TLS and recovery runbook

**Status:** runbook for the disposable `.19` implementation. It describes a
deployment shape but does not authorize one. No office hostname, certificate,
secret store, network policy or backup key is selected here; the office owner
supplies them. Nothing in this document was executed against a live host.

The service is `http_service.py` (wire protocol and authorization), backed by
`http_auth.py` (accounts, sessions, credentials, membership, audit, idempotency).
Both use the Python standard library only. `http_client.py` is the worker/CLI
transport; the existing SSH/local `client.py` transport is unchanged and remains
the operator path.

## 1. What the service is and is not

It is an authenticated adapter to canonical coordination data: browser sessions
and project-scoped worker credentials gate every route, and organization is by
project membership (`owner`, `contributor`, `viewer`) with a global superuser.

It is **not** a replacement for repository permissions, TLS termination, host
isolation, or the canonical database's own access controls. It never grants Git
merge, deployment or host authority.

## 2. Components and paths

| Item | Value |
| --- | --- |
| Service | `http_service.py` (stdlib `ThreadingHTTPServer`) |
| Auth core | `http_auth.py` |
| Client | `http_client.py` |
| Canonical binding | `--backend endpoint` (default) via `EndpointBackend` -> `endpoint.py`; `--backend inprocess` is a disposable local check only |
| Private state | one JSON document at `--state` (e.g. `<RUNTIME_ROOT>/http-state.json`) |
| Python | 3.10 or newer; no third-party packages |
| Listener | loopback by default (`127.0.0.1:8443`), fronted by the reverse proxy |

The state document contains only password verifiers, hashed session/credential
tokens, membership, idempotency records and audit events. It must live outside
source control on a private path owned by the service account.

The `endpoint` backend runs the canonical `endpoint.py` client as the service
account and is the only binding that touches canonical coordination data. The
HTTP layer never reads or writes canonical storage directly. The `inprocess`
backend keeps canonical records inside the service state and exists solely for
disposable local validation; it is not a deployment backend.

## 3. Provisioning a host (placeholders)

```sh
# 1. dedicated least-privilege account and private runtime root
sudo useradd --system --home <RUNTIME_ROOT> --shell /usr/sbin/nologin <SERVICE_USER>
sudo install -d -m 0700 -o <SERVICE_USER> -g <SERVICE_USER> <RUNTIME_ROOT>
sudo install -d -m 0700 -o <SERVICE_USER> -g <SERVICE_USER> <RUNTIME_ROOT>/secrets

# 2. the kit (pinned revision; no packages to install)
sudo -u <SERVICE_USER> git clone <KIT_REPO_URL> <RUNTIME_ROOT>/kit
cd <RUNTIME_ROOT>/kit && git checkout <PINNED_COMMIT>

# 3. one-time superuser bootstrap; there is no default or shared password
sudo -u <SERVICE_USER> python3 http_service.py \
  --state <RUNTIME_ROOT>/http-state.json --bootstrap-user <ADMIN_USERNAME>
# the operator types the password at the prompt; it is never echoed or logged
```

Bootstrap refuses to run once any account exists, so it cannot silently reset a
live deployment. Create ordinary accounts through the API and hand each user a
single-use reset value; only redemption changes the verifier.

## 4. Service unit

```ini
# /etc/systemd/system/orchestra-http.service  (placeholders, not installed here)
[Unit]
Description=Orchestra authenticated HTTP service
After=network-online.target

[Service]
User=<SERVICE_USER>
Group=<SERVICE_USER>
WorkingDirectory=<RUNTIME_ROOT>/kit
ExecStart=/usr/bin/python3 http_service.py \
  --state <RUNTIME_ROOT>/http-state.json \
  --host 127.0.0.1 --port 8443 \
  --backend endpoint \
  --endpoint <RUNTIME_ROOT>/kit/endpoint.py \
  --root <RUNTIME_ROOT> \
  --trusted-proxy 127.0.0.1
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=<RUNTIME_ROOT>
UMask=0077

[Install]
WantedBy=multi-user.target
```

`--trusted-proxy ADDR` (repeatable, address or CIDR) names the peers whose
forwarded headers the service will believe. A reverse proxy on the same host is
`--trusted-proxy 127.0.0.1`. Enable it **only** for the configured proxy address:
forwarded headers from any other peer are ignored, so a client cannot spoof the
throttle key or claim `https`.

That last point is what makes browser cookies safe under the documented TLS
deployment. TLS terminates at the proxy, so the loopback connection to the
service is plaintext and is **not** treated as secure by itself. The service marks
the session cookie `Secure` only when the request arrived over TLS at the service
or when a trusted peer sent `X-Forwarded-Proto: https`. A plaintext loopback
request, or an `X-Forwarded-Proto` header from an untrusted peer, does not earn a
`Secure` cookie. The proxy must set `X-Forwarded-Proto https` (see section 5).

TLS may also terminate at the service itself with
`--cert <CERT_PATH> --key <KEY_PATH>` (TLS 1.2 minimum). The service refuses a
plaintext non-loopback listener unless `--allow-plaintext-non-loopback` is passed
explicitly for a disposable test.

## 5. Reverse proxy (example shape)

```nginx
# placeholders only; the office owner chooses the real hostname and certificate
server {
    listen 443 ssl;
    server_name <HOSTNAME>;
    ssl_certificate     <CERT_PATH>;
    ssl_certificate_key <KEY_PATH>;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 300k;          # matches the service body bound

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
}
```

The proxy must reject or redirect plaintext, validate its certificate chain,
enforce a body limit, and preserve only the documented forwarding headers. It
should set `X-Forwarded-Proto https` and the service must name the proxy address
with `--trusted-proxy` for that header to be believed (section 4); otherwise
browser session cookies are issued without `Secure`. Do not add a wildcard CORS
origin: credentialed requests require an explicit origin list and the service
sends `Cache-Control: no-store` on every API response.

### Web interface on the same origin

The service also serves the browser interface from the kit's `web/` directory at
`/`, on the same origin as `/v1`, so the session cookie stays `SameSite=Strict` and
no CORS is needed. Proxy `/` as above; nothing else is required. Static serving is
anonymous `GET`/`HEAD` only, from a fixed allowlist (`index.html`, `css/`, `js/`,
`js/views/`, `img/`; `.html .css .js .svg .png .ico`), with no directory listing.
The raw path is checked before decoding, so dot and percent-encoded traversal never
match, and a symlink that resolves outside `web/` (or onto an excluded file) is
refused. `prototype.html`, `js/prototype.js`, `js/mock.js` and `web/data/` are never
served. Static responses carry `Content-Security-Policy: default-src 'self';
script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self';
frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, `nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and `Cache-Control:
no-cache` with an `ETag`, so a redeploy is picked up on the next load.
`--web-root DIR` serves a different copy; `--no-web` turns the interface off and
leaves the JSON API unchanged.

Slice 1 of the interface covers sign-in, projects and members, tasks, claims,
contribution review and feedback. Requirements, decisions and records are hidden
from its navigation (the service has no routes for them yet) and a direct link
shows "not available on this server".

## 6. Worker clients

```sh
export ORCHESTRA_HTTP_URL=https://<HOSTNAME>
export ORCHESTRA_PASSWORD='...'          # read from the environment, never an argv
python3 http_client.py login --username <USERNAME>
# the client prints the session token to stdout; keep it out of shell history/logs
python3 http_client.py --token "$TOKEN" call GET /v1/projects
```

A project owner or superuser issues a project-scoped credential once; the secret
is shown only in that `201` response. For unattended workers, store it in the
operator's secret store and use it as a bearer token. An exact retry of a lost
issuance returns `200` metadata with `secret_available:false`; it never re-delivers
the secret. Revoke and reissue instead.

A credential is bound to one project and to its `scopes` list (`read`, `tasks`,
`checkpoints`, `reviews`, `feedback`). It may always read the project it is scoped
to; a write route requires the matching scope. No credential can administer
accounts or projects, issue or revoke credentials, or approve a review, whatever
the role of the account that issued it. Issuing a credential never lends the
issuer's authority to it.

Mutating calls accept an `Idempotency-Key`. On an uncertain `503` the client
raises `UncertainOutcome` carrying the key: retry the identical request with that
key to reconcile. Never retry an uncertain mutation with a new key.

**Retry contract.** An exact retry sent no more than 29 days
(`JOURNAL_RETRY_HORIZON_SECONDS`, the 30-day tombstone horizon minus the 24 h skew
allowance) after the original attempt replays the recorded result, reports
uncertainty or is refused as expired; it is never re-executed, **as long as the total
uncredited forward clock error stays below 24 h** (`JOURNAL_MAX_SKEW_SECONDS`). A
forward step of 24 h or less is not suspect and not credited, and such steps add up:
three false +23 h steps with no correction re-execute retries about 28.4-29.1 days
old. Keep the host clock NTP-disciplined; after correcting a clock that ran ahead, run
`admin.py journal <PROJECT> --reset-high-water`. Tombstones already aged out during the
error are not restored. An older retry is unsupported: its tombstone may have aged out
and the effect may run again. Reconcile canonical state and use a new key instead.

Service-local routes (credential issue, account and project create, membership
changes) are not backed by the canonical journal: their exact-retry window is the HTTP
idempotency record's (`IDEMPOTENCY_TTL_SECONDS`, 24 h). That record expires on the same
confirmed timeline (see the record store in section 8), so a clock jump never shortens
it; a retry after the window is treated as a new request.

### Personal agents

An agent is a personal identity owned by one user; it pulls work over the same REST
API instead of reading HTML. Create one with `POST /v1/agents` (session authority),
giving `name`, an optional free-text `working_directory` hint for the owner's own
machine, `tool`, `machine`, `notes` and the `projects` to grant. The response carries
the agent record, the setup payload and the credential secret **once**; an exact
idempotent retry returns `200` with `secret_available:false` and never re-delivers it.

The setup payload writes `.orchestra/agent.json` with the server URL, the agent id and
the project ids and no secret, and `.orchestra/` stays out of Git. The secret belongs in
a per-agent curl config file in the user profile (`%USERPROFILE%\.orchestra-agent-<name>.curlrc` on Windows, `~/.orchestra-agent-<name>.curlrc` on macOS/Linux, mode 600) holding one line `header = "Authorization: Bearer <secret>"`; every call hands curl that file with `-K`. An environment variable is only a secondary fallback (`ORCHESTRA_AGENT_SECRET`).
This is the owner's practical update of 2026-09-29, which supersedes the "VS Code secret
storage or the OS credential store first" wording of owner decision 9 on
kittrial-5bb.22: an agent running in VS Code cannot read those stores, while curl reads
the file itself. `<name>` is the agent name as a slug (lowercase letters, digits and
hyphens, at most 40 characters; `agent_slug()`), and the setup payload returns the exact
names as `secret_file`. Because two agents of one owner share a user profile, the
service refuses (`409`, naming the clashing agent and the file) to create or rename an
agent whose file name would equal that of another agent of the same owner: "Build bot",
"build-bot", "Build_Bot" and "Build.bot" are one file, as are names that agree in their
first 40 characters. A name must contain a letter or digit, so the generic
`.orchestra-agent-agent.curlrc` never arises for a new agent. Different owners may use
the same name. Clashes already present in older state are left alone and stay readable;
rename one of them (to a name that does not clash) before setting up its folder. Windows steps come first:

1. Open Notepad and paste the one line with the secret shown once.
2. File > Save As, "Save as type: All files (*.*)", file name
   `%USERPROFILE%\.orchestra-agent-<name>.curlrc` (Windows file dialogs expand
   `%USERPROFILE%`; no `.txt`). On macOS/Linux save `~/.orchestra-agent-<name>.curlrc`
   and `chmod 600` it.
3. Test it: `curl.exe -fsS -K "$env:USERPROFILE\.orchestra-agent-<name>.curlrc"
   <server>/v1/agents/me` in PowerShell, or `curl -fsS -K ~/.orchestra-agent-<name>.curlrc
   <server>/v1/agents/me`. Success prints the agent's JSON; `401` means a wrong secret or
   header line; curl's "cannot read config from ..." (exit code 26) means a wrong file
   name or a `.txt` extension.

The web interface's setup dialog shows these steps with Copy buttons (the line with the
real secret only in the dialog that has just issued it), and `.orchestra/AGENT.md` and
the setup prompt name the agent's exact file and use `curl.exe -K`/`curl -K` for every
call. The setup snippet and the resume prompt never show the secret on a command line: a
literal `export` would persist in shell history and the process list, and curl reads the
header from it with `-K`, so no shell expansion ever puts the secret in the
process list either. A lost secret is replaced, never re-shown: `POST
/v1/agents/{id}/credentials` issues a new one (the old credential works until revoked
with `POST /v1/agents/{id}/credentials/{credential}/revoke`). The
agent then calls `GET /v1/agents/me` and `GET /v1/agents/me/next` with its credential as a
bearer token (`Authorization: Bearer <agent-secret>`); every other project route works as
before, capped at the owner's live role and the agent's granted projects. A grant may
only name a project the owner can open, including when a superuser edits somebody
else's agent (`404` otherwise). Set `--public-url https://<HOSTNAME>` so the setup and
resume snippets carry the real address. Attention is computed at read time only: the
service runs no scheduled job, poller or timer, and the owner resumes the agent
manually.

Attention re-authorizes every granted project with the same live check the task routes
use before it reads anything, so a project the caller can no longer open (the owner
was removed, or the grant was narrowed) is skipped instead of leaked. Each project is
read once per request through one full task snapshot, so an agent's own task is found
however deep it sorts and the request costs one `endpoint.py` / `bd list --all`
invocation per project whatever the project's size (the page-sized cap is applied to
the in-memory snapshot); only the *claimable* suggestions meet the page-sized cap, and
`truncated` reports that suggestion list.

A project owner or admin governs which agents may work in their project without
holding `agents.manage` and without seeing anything the agent's own owner keeps
private. `GET /v1/projects/{id}/agents` lists the agents whose grant names the project
(id, name, owner, `last_seen_at`, `enabled` only, never `working_directory` or
`working_directory_hidden`), `GET /v1/projects/{id}/agents/{agent}` returns one of
them, and `DELETE /v1/projects/{id}/agents/{agent}` removes the project from that
agent's grant with an audit record naming the project and the acting user. An agent that
is not granted the project is reported exactly like a nonexistent id: both routes answer
`404` with the same `Agent not found` message, so a project owner cannot probe the wider
registry. Removal
takes effect on the agent's next request: the credential is refused (`403`/`404`) on
that project's routes and the project drops out of `/v1/agents/me/next`. It revokes
nothing else - the agent is not disabled, keeps its other projects and credentials,
and its own owner keeps every other control.

The removal is therefore **not sticky**, and that is the owner's settled decision rather than
an open question. It drops one project from the grant; it does not mark the agent as barred
from the project. The agent's own owner still administers the agent and can add that project
back immediately while they are still a member of it - the same live grant checks apply as
for any other grant, and the project owner gets no notification beyond the audit record. On
2026-09-29 the owner resolved whether a project owner may block a re-grant (kittrial-5bb.46
comment `01a0eea4-48b0-7045-855b-28299dfe7143`, "leave as is"): the revoke **stays
non-sticky**, the agent's owner may re-grant while still a project member, and **no blocking
state is added**. The control for an untrusted member is removing that member from the
project, not barring the agent from being re-granted.

Revoke an agent credential, disable the agent, disable the owner or remove the
owner's project membership and the agent stops on its next request. The
`working_directory` hint is returned only to the owner or a superuser. The browser
screens that display the directory and the copyable resume prompt are
kittrial-5bb.20 and consume these JSON responses; they are not part of this service
build.

## 7. Secrets, rotation and redaction

- Password verifiers are stored with memory-hard `scrypt`; plaintext passwords,
  reset values, session cookies and bearer tokens are never stored or logged.
- TLS keys, the password used at bootstrap and issued worker secrets live in
  `<RUNTIME_ROOT>/secrets` or the operator's secret store, never in Git.
- Rotate deployment secrets and revoke all sessions/credentials if compromise is
  suspected. Disabling an account immediately revokes its sessions and credentials.
- Audit events record time, request id, authenticated user id, project, action,
  outcome and a short redacted reason. They never contain request bodies,
  `Authorization` headers, cookies, passwords, reset values or attachment content.

### Clock jumps and time-based expiry

Every place that ages or deletes by time, and what an accepted clock jump does to it:

| Item | Clock | Can a jump re-execute an effect? | Security direction |
|---|---|---|---|
| Journal tombstones (`http_authority`) | confirmed timeline (`aged_from`, `jump_credit`) | No (within the retry contract) | n/a |
| Journal receipt reclaim, uncertain expiry | raw, only when not suspect | No: worst case `rc=2` refusal | n/a |
| HTTP idempotency records and committed results (`RecordStore`) | confirmed timeline (`expires_confirmed`) | No: a retry replays or is refused | n/a |
| Sessions (idle and absolute), worker credentials, reset tokens (`http_auth`) | monotone `max(raw now, RecordStore high_water)` **and** raw real lifetime (`issued_raw` / `last_used_raw`) | No | A forward jump expires existing ones **early** (safe; log in again or re-issue). A backward step, including one that corrects a forward jump, can never revive an item that has already expired: `high_water` never decreases and every auth observation persists it, even a rejection. Because the floor stays ahead until the raw clock passes it, each item also carries its raw issuance (and raw last use) stamp and expires when **either** clock reaches its lifetime, so a pinned floor cannot stretch a real session idle/absolute, credential or reset lifetime |
| Auth clock observation (`RecordStore.monotonic_now`) | raw, persisted as `high_water` | No | Not an expiry by itself. Every observation that advances `high_water` is one record-store write transaction, so each authenticated request performs at least one (accepted at office scale; see the cost note) |
| Login throttle window (in memory) | raw | No | A forward jump clears it early (a few extra attempts); a backward step keeps it longer (safe) |
| Audit log (`AUDIT_LIMIT`) | count-bounded; timestamps only | No | none |

**Auth expiry uses two clocks.** Every auth decision (login, session/credential
authentication, idle refresh, credential issue, reset issue/redeem, capability checks and
the canonical endpoint's authority descriptor) compares against `max(now, high_water)`
from the service's record store **and** against the item's real age on the raw host clock.
`high_water` is the largest raw clock any observation has seen; it never decreases, and
the observation is persisted even when the decision rejects the request, so a restart
cannot lose the floor. `trusted_now` is deliberately **not** used for auth expiry: while
the clock is suspect it is held near the anchor so a record inside its real window can
still replay, which would keep an expired auth item alive.

An item is expired when **either** clock says so:

* the monotone deadline (`expires_at`, `idle_expires`, `absolute_expires`) is reached.
  This is what stops a backward step from reviving an item, and it is never dropped; or
* its real lifetime has elapsed: `raw - issued_raw >= lifetime`, and for session idle
  `raw - last_used_raw >= session_idle`. `issued_raw` and `last_used_raw` are stored
  beside the monotone stamps for exactly this second comparison.

The raw half is what stops a **forward** jump that is later corrected from stretching a
lifetime. While `high_water` is pinned ahead of the corrected clock every newly issued
session, worker credential and reset value is stamped on that floor, so its monotone
deadline is late by the jump; its real TTL/idle window still expires it on time. An item
issued *during* the error window (when the raw clock itself was wrong) carries that wrong
raw stamp, so its raw age is understated by the jump as well: those items must be revoked
or the floor reset, which is why the operator remedy below covers both.

After correcting a clock that ran ahead, reset the service record store's own floor:

```sh
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> \
    record-store --state <RUNTIME_ROOT>/http-state.json --reset-high-water
```

That sets `high_water` to the corrected clock and clears suspicion while keeping
`jump_credit`, so sessions, worker credentials and reset values issued while the floor was
pinned stop being stamped on it (the same command without `--reset-high-water` only
reports, and prints the record store's clock state — `stats()`, the same shape as
`journal`). **Revoking items issued during the error window is not enough**: it does not
cover items issued *after* the correction (a new login, worker credential or reset), which
are the ones stamped on the pinned floor and stretched by the jump - the clock must be
reset. For completeness also revoke the sessions, worker credentials and reset tokens
issued *during* the error window, and run the operation-journal equivalent
`python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> --reset-high-water` for each
project.

`record-store` requires both the state document and its record store to exist and opens
them without creating anything: a mistyped `--state` path is refused with a non-zero exit
and a message naming the missing path, and it never creates the directory or an empty
`<state>.records.sqlite3`. (It used to create both and report success, leaving the real
store pinned.) The record store is created by the HTTP service itself.

A **backward** clock step is not symmetric, and the bound below is what the monotone floor
can and cannot promise. Because `high_water` never decreases, an item that is still live
when the host clock steps back by `B` is not revoked — but its remaining lifetime is
extended by up to `B`. The monotone clock stays frozen at the pre-step floor until the raw
clock catches up, and the item's raw issue/last-use stamp makes its real age look `B`
smaller, so both halves of the expiry check move later by the same `B`. Measured on the
monotone-clock review probe: a 12 h session used every 20 min died at real +18 h after a 6 h
backward step, i.e. 6 h after its 12 h absolute lifetime. A clock that keeps stepping
backward — a bad time source, a VM snapshot restore, or an operator correcting in the wrong
direction — can therefore keep a still-live session, worker credential or reset value alive
for as long as each step stays ahead of its idle or absolute deadline, and a use during the
backward window restarts the idle window from an earlier raw stamp. Two things bound the
exposure: a backward step never *revives* an item that has already expired on either clock,
and once the raw clock is correct and stable every item expires at its real lifetime. With
no trustworthy monotonic time source the kit cannot beat that step-sized bound; if the host
clock is known to be unstable, revoke sessions and worker credentials and re-issue them once
it is stable. `--reset-high-water` is the remedy for a corrected **forward** jump, not for a
backward step.

Correcting a clock that ran **behind** (a forward step) extends nothing: it clears the
login throttle window early and expires sessions and credentials that were already past
their lifetime. The login throttle window (in memory) is a rate limit, not an item expiry,
so it stays on the raw clock — a backward step only lengthens it, and a forward step clears
it early.

**Cost note (accepted at office scale).** Persisting the monotone floor means every auth
observation that advances `high_water` is a record-store write transaction, and one
authenticated request observes at least once (a session/credential authenticate plus the
authority check). That is acceptable for a single office service with a per-process lock;
a materially higher request rate would need the floor held in memory under the same
single-writer discipline, with the same persisted-on-rejection rule.

## 8. Backup, restore and rollback

Back up, in one coordinated snapshot:

1. the canonical coordination database and its sidecar, **and each project's
   operation store** `<PROJECT>/.http-operations.sqlite3` — `admin.py backup PROJECT`
   captures all three together (Dolt via `bd backup`, the coordination sidecar, and a
   consistent `sqlite3` backup-API snapshot of the operation store in
   `<RUNTIME_ROOT>/backups/<PROJECT>.http-operations.sqlite3`, converted to
   `journal_mode=DELETE` so it is one self-contained file with no `-wal`/`-shm`),
2. this service's `--state` document (accounts, membership, revocation state and
   audit) **and its record store** `<state>.records.sqlite3` (HTTP idempotency
   receipts and committed results).

The two SQLite stores run in WAL mode, so never copy the bare `.sqlite3` file of a
live store with `cp`: committed pages may still be in the `-wal` file. Take a
consistent copy with the SQLite backup API instead — `admin.py backup` does this for
the operation store; for the record store (or any store taken outside `admin.py`),
stop the service or use:

```sh
sudo -u <SERVICE_USER> python3 -c "import sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
    <RUNTIME_ROOT>/http-state.json.records.sqlite3 <BACKUP_DIR>/http-state.json.records.sqlite3
```

Take the state document and its record store in the same service stop (or back to
back, state document first) so they describe the same moment. Encrypt off-machine
copies, restrict the key to the backup owner, and define retention before rollout.
Restore drill on an **isolated** deployment:

```sh
sudo systemctl stop orchestra-http
sudo -u <SERVICE_USER> install -m 0600 <RESTORED_STATE> <RUNTIME_ROOT>/http-state.json
sudo -u <SERVICE_USER> install -m 0600 <RESTORED_STATE>.records.sqlite3 \
    <RUNTIME_ROOT>/http-state.json.records.sqlite3
sudo -u <SERVICE_USER> rm -f <RUNTIME_ROOT>/http-state.json.records.sqlite3-wal \
    <RUNTIME_ROOT>/http-state.json.records.sqlite3-shm
sudo -u <SERVICE_USER> python3 -m json.tool <RUNTIME_ROOT>/http-state.json > /dev/null
sudo -u <SERVICE_USER> python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA quick_check').fetchone()[0])" \
    <RUNTIME_ROOT>/http-state.json.records.sqlite3      # ok
# a project, including its operation store, is restored into a NEW project name:
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> restore-new <PROJECT> <NEW_PROJECT>
sudo systemctl start orchestra-http
curl -fsS http://127.0.0.1:8443/healthz          # {"status":"ok"}
```

`restore-new` validates the operation-store snapshot first (read-only open,
`PRAGMA quick_check`, the `operations` and `meta` tables) and refuses a corrupt
snapshot before it creates the destination project, runs the Dolt restore or writes
any coordination file. The restored store replaces any stale `-wal`/`-shm` sidecars.
Without the record store the service starts with an empty receipt set (a lost-response
retry then falls through to the operation store, which still replays or refuses it).

After restore, verify hashes, revocation state, memberships and audit continuity,
then cut over explicitly. Rollback is the previous pinned kit revision plus its
matching state snapshot; restore both together. A state document whose
`schema_version` is not understood fails closed at startup rather than guessing.

### Operation journal recovery (idempotency receipts)

Each project keeps its idempotency receipts in `<PROJECT>/.http-operations.sqlite3`: a
SQLite database (stdlib `sqlite3`, WAL mode, one transaction per keyed mutation) with
one indexed row per identity. Revision 7 replaced the older whole-document
`<PROJECT>/.http-operations.json`, whose read/modify/rewrite cost grew with the journal
and whose byte budget could drop a live record.

The legacy document is imported **in the same transaction that creates the schema**,
guarded by a persisted `meta['legacy_migrated']` marker that is checked on every open.
A crash between schema creation and the import therefore leaves either the untouched
`.http-operations.json` (the transaction rolled back) or a fully migrated store; the
next open finishes the migration rather than silently ignoring the still-present JSON.
Only after the commit succeeds is the document renamed to
`.http-operations.json.migrated` (kept for audit; the import happens once).

The store keeps the same three concepts:

* **live entries** — a *committed* receipt keeps its replayable response for
  `JOURNAL_COMMITTED_RETENTION_SECONDS` (default 1 day); an *uncertain* reservation
  (`in_progress`/`unknown`) stays live for `JOURNAL_RETENTION_SECONDS` (default 7 days)
  because it may correspond to a committed native write whose outcome was never
  observed.
* **tombstones** — every reclaimed identity becomes a compact row (`state='expired'`:
  operation id, request hash, principal, times) that keeps refusing an exact retry as
  expired. A tombstone ages on the *confirmed timeline*: it records
  `aged_from = reclaimed_at - jump_credit` and is removed only when
  `aged_from < now - jump_credit - JOURNAL_TOMBSTONE_SECONDS` (default 30 days), i.e.
  when its age excluding every suspect forward step credited since it was reclaimed
  passes the horizon. A clock jump therefore never ages a tombstone out (an accepted
  jump extends retention by its length; an idle gap of more than 24 h, such as a
  weekend, extends it by the gap), and a still-live tombstone is never dropped to
  satisfy the byte or count budget.
* **trusted clock** — `meta` holds `high_water` (the largest raw clock any write has
  seen; it never decreases), `suspect`, `anchor`, `suspect_since` and `jump_credit` (a
  non-decreasing total of every suspect forward step). Every write observes the raw
  clock: a step of more than `JOURNAL_MAX_SKEW_SECONDS` (24 h) since `high_water` makes
  the store *suspect* and is added to `jump_credit` (the first such step records
  `anchor = high_water`; a further big step restarts `suspect_since` but keeps the
  anchor); once the raw clock has run for `JOURNAL_SUSPECT_SETTLE_SECONDS` (1 h) after
  the step the next write clears it; a backward step is never suspect and never
  credited. The trusted clock is `now`, or while suspect
  `clamp(anchor + (now - suspect_since), anchor, anchor + 24 h)` — only the time
  elapsed since the step counts — and decides every replay/expiry question on the read
  and write paths (the HTTP record store uses the same rule). Rows are stamped with the
  raw clock. Receipt reclaim runs only while the store is not suspect, against the raw
  clock; tombstone deletion also waits for a non-suspect store and uses the confirmed
  timeline above.

  *Effect.* After an idle weekend the first write is suspect for one hour, with the
  trusted clock held near the anchor, so an old receipt may still replay instead of
  being refused (harmless); then normal operation resumes without an operator. **While
  consecutive writes stay more than 24 h apart the store stays suspect**: reclaim
  pauses and old receipts keep replaying until two writes fall within 24 h and an hour
  passes. During a genuine forward jump (for example +8 days) a 60-second-old receipt
  replays and an uncertain reservation keeps reporting `124`.

  *Residual risk.* A jump that persists for longer than the settle hour is accepted:
  receipts still inside their real window can then be compacted to tombstones, so an
  exact retry is refused as expired (reconcile and use a fresh `operation_id`); no
  tombstone inside the retry horizon is deleted and nothing is re-executed. **A jump
  that is later corrected keeps the store suspect for about the length of the jump**
  (`high_water` is left in the future; the trusted clock stays at the anchor and
  reclaim pauses), and a repeat of the same jump would not be detected again, until
  `--reset-high-water` (below). A clock step of 24 h or less is not suspect and not
  credited, which is why the client retry contract (section 6) is the horizon minus
  24 h and holds only while the total uncredited forward clock error stays below 24 h.

  *Sparse projects.* A project whose writes are always more than 24 h apart credits
  every gap, so its `jump_credit` grows at about real time and its few tombstones
  effectively never age out. This is safe (one small row per identity) and ends as soon
  as writes fall within 24 h of each other.

An exact retry inside its window replays the committed response or reports `124`
uncertainty; outside it (or after reclaim) it is refused as expired with `rc=2`. It is
never re-executed.

**Retention is by time only.** A committed receipt is never compacted, deleted or
otherwise evicted while its window is open, whatever the store's size; the byte budget
(`MAX_JOURNAL_BYTES`) and the advisory tombstone size (`JOURNAL_TOMBSTONE_LIMIT`) are
**reported**, never enforced by eviction; the tombstone count never stops reclaim and
never blocks a write (tombstones age out by time only). The one admission bound is the live-identity count (`JOURNAL_LIMIT`): when the
store genuinely cannot hold one more live identity the mutation fails closed with `124`,
the transaction is rolled back so the pre-existing store is untouched, and no effect is
attempted. `stats()` reports `live_bytes`, `tombstone_bytes`, `bytes`, `limit_bytes`
(`MAX_JOURNAL_BYTES`), `over_bytes`/`over_tombstones`/`over_limit`, `journal_mode` and
`running_totals_match`, plus the clock state `high_water`, `suspect`, `anchor`,
`suspect_since`, `clock_skewed` (= `suspect`) and `trusted_now` as of the call
(`clock_persisted` is the stored row); `stats()['file_bytes']` is the database file's
size on disk. The
store keeps `live_count`/`tombstone_count`/`total_bytes` running totals in `meta`,
updated inside the same transaction as every row write, so a keyed operation never scans
the table and latency does not grow with the store's size. Tombstone ageing is a
range scan on the `(state, reclaimed_at)` index, and schema upgrades (columns,
backfill, indexes, clock state; schema 5 in this revision) run once, atomically, on the
first open by the new code, never on every open. Each row also carries the
directed `actor`, the canonical `route` and the precomputed `replay_until`/`expires_at`
timestamps.

The HTTP service's own idempotency receipts and committed canonical results live in a
second SQLite store beside the service state document (`<state>.records.sqlite3`,
`http_auth.RecordStore`, WAL, time-only retention). It keeps its own trusted-clock state
and `jump_credit` in its own `meta` table (the same rules as the journal, mirrored, not
shared rows), stores each record's expiry on the confirmed timeline
(`expires_confirmed = expires_at - jump_credit` at write time), and treats a record as
expired, or deletes it, only when `expires_confirmed < now - jump_credit` (clamped
trusted clock while suspect; no deletion while suspect). A clock jump therefore never
ages an HTTP idempotency record or committed result out early. An older record store is
upgraded atomically on open (`expires_confirmed = expires_at`, credit 0). They used to live inside the
`http.json` state document, where the result map evicted by count/bytes and the
idempotency map grew without bound; `http.json` no longer grows with the number of keyed
operations and no keyed operation rewrites it. Back up the two files together: the
state document and the records store.

An operator intervenes for inspection, for a stuck unknown identity, to shorten a
window, or after a genuine clock correction:

```sh
# inspect: per-state counts, tombstones, expired/reclaimable, live/tombstone bytes,
# configured bounds, over-budget flags, journal_mode and file bytes
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT>

# compact closed receipt windows now (safe; reclaim writes tombstones, never drops)
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> --reclaim-expired

# override either window for one inspection/compaction (seconds)
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> \
    --retention 604800 --committed-retention 86400 --reclaim-expired

# after correcting a wrong clock: high_water = now and clear suspicion
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> --reset-high-water

# the same recovery for the service record store's monotone auth floor (keeps jump_credit);
# without the flag it only reports the clock state and the state/record-store paths. Both
# paths must already exist: a typo is refused non-zero and creates nothing
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> record-store \
    --state <RUNTIME_ROOT>/http-state.json --reset-high-water

# explicit override after reconciling canonical state: remove a still-live identity
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> \
    --prune-before <EPOCH_SECONDS>
```

`--reset-high-water` sets `high_water` to the current clock and clears suspicion; it
never reduces `jump_credit`, so tombstones keep ageing on the confirmed timeline. The
`record-store --reset-high-water` command applies exactly the same rule to the HTTP
record store's monotone auth clock (`<state>.records.sqlite3`), keeping its own
`jump_credit`, and is the remedy that stops auth items issued after a corrected forward
jump from being stamped on the pinned floor. It refuses a state document or record store
that does not already exist (non-zero, creating nothing), so a mistyped path cannot
silently reset a fabricated store instead of the real one. A backward step needs no
reset: it extends a still-live item by up to the step size (the bound in the auth-clock
section above), and shortening that window means revoking the affected sessions or
credentials, not resetting the floor. No
operator action is needed after an idle gap (suspicion settles by itself after an
hour). Use it after correcting a clock that had jumped forward: it re-arms jump
detection and ends the capped-expiry period at once. It removes no identity by itself.
Older stores are upgraded on first open in one transaction. The credit always
starts at 0: schema 4 and 5 stores were never deployed, so no step before the upgrade
is credited. A rev7/rev8 store whose
`high_water` is more than 24 h old starts suspect (`anchor = high_water`) and settles an
hour later like any idle gap. Schema 6 (this revision; from a rev9 schema-5 store or
older) adds `jump_credit = 0`, the `aged_from` column (set to `reclaimed_at` for
existing tombstones) and the `(state, aged_from)` index, which replaces
`(state, reclaimed_at)`; a crash during the upgrade leaves the previous schema intact.

**Upgrade note: the route is part of the operation hash.** From this revision the HTTP
service sends the canonical route with every keyed mutation, so the journal row
records it and `operation_hash` binds it. A mutation journaled by an earlier revision
over HTTP (route recorded as empty), whose result is not in the service record store
(an uncertain first attempt), and retried after the upgrade inside its replay window, is
refused as `Operation identity reused with a different request` (`rc=2`,
HTTP `400`) rather than replayed; it is never re-executed. Upgrade while no HTTP
mutation is awaiting a retry, or reconcile such a retry through a canonical read.

`--prune-before` is the only operation that removes a record without a tombstone, so it
is the only one that can make an exact retry repeat its effect. Reconcile the canonical
task/comment state first, then prune that identity, then let the client issue a fresh
`operation_id`. Every journal command runs under the project coordination lock, so it
cannot interleave with a live mutation; it is a local operator action and is never
exposed over HTTP.

If a project's live identity count exceeds `JOURNAL_LIMIT` inside the receipt windows,
the mutation fails closed with `124` rather than dropping a live identity. The remedies
are to let the windows close (a closed window is compacted to a tombstone and stops
counting as live), to `--prune-before` after reconciliation, or to raise the reviewed
`JOURNAL_LIMIT`. A large byte total is *reported* (`over_bytes`) rather than relieved by
eviction; size is the operator's signal to raise the reviewed `MAX_JOURNAL_BYTES`, move
the store to a larger volume, or shorten the committed window. The store is one local
file inside the project directory: `admin.py backup PROJECT` takes a consistent
`sqlite3.Connection.backup()` snapshot of it (under the same backup lock as the
coordination sidecar) into `<RUNTIME_ROOT>/backups/<PROJECT>.http-operations.sqlite3`,
and `admin.py restore-new PROJECT DESTINATION` restores that snapshot into the newly
created project. `bd backup` itself covers Dolt only, so the snapshot is what makes the
claim in this section true.

## 9. SSH compatibility

SSH and local transports keep their existing actor/config contract and gain no
HTTP roles. HTTP principals are never inferred from an actor string; mapping
legacy actor-owned claims to a stable user id is an explicit migration action in
the pilot phase, not part of this service.

## 10. Known limitations of this disposable build

- The canonical binding is implemented for the full route surface:
  `EndpointBackend` provides `invoke`, `list_tasks`, `get_task`, `task_history` and
  `list_feedback`, maps task mutations onto `bd create/update` and
  checkpoints/reviews onto the canonical structured `checkpoint`/`review` actions,
  and re-checks authority when it writes the durable idempotency receipt. A
  disposable subprocess test (`tests/test_http_review_fixes.py`) exercises the HTTP
  surface through this seam against durable file-backed canonical state, including
  uncertain-write reconciliation after a restart. Confirming the exact `bd` argv
  against a live pinned runtime still needs a POSIX host with `bd`; this
  workstation has neither.
- **Explicitly unresolved routes.** The jobs alias and artifact-content routes are
  not implemented. `POST`/`GET /v1/projects/{id}/feedback` fail closed with
  `501 not_implemented` on the canonical backend because the dedicated feedback
  stream ships with `kittrial-5bb.13`; the service never substitutes its own store
  for canonical feedback. Attachment uploads are validated and bounded but only
  their metadata and digest are retained, so **UI readiness is not claimed**.
- The personal-agent registry, one-time agent credential and the agent REST
  contract (`/v1/agents`, `/v1/agents/me`, `/v1/agents/me/next`, and the
  project-scoped `GET /v1/projects/{id}/agents`,
  `GET /v1/projects/{id}/agents/{agent}` and
  `DELETE /v1/projects/{id}/agents/{agent}`) are implemented and tested. The
  browser screens that render them are kittrial-5bb.20 (served at `/`, see
  section 5).
- The web interface reads `GET /v1/projects/{id}/members`,
  `GET /v1/projects/{id}/worker-credentials` (metadata only),
  `GET /v1/projects/{id}/tasks/{task}/brief`, `GET /v1/projects/{id}/queue`,
  `GET /v1/me/work` and `GET /v1/accounts/lookup?username=&project=`. Over the
  canonical binding the brief maps the canonical `brief --json` read and the queue
  pages the canonical `work` action (bounded; `complete: false` past the bound).
  Canonical task rows from `bd list` carry no review state, so the task list merges
  in the review state from that same bounded `work` projection (one queue read per
  request, shared with the queue). A closed task the projection no longer lists, or
  a row past its bound, has `review_state: null` and no next action, and the list
  reports `review_states_complete: false`; it never guesses "claim" or "deliver" for
  work that may be under review.
- **`GET /v1/me/work` cost.** On the canonical binding each project costs one bounded
  `work` walk (up to 10 subprocess reads of 100 rows), for at most 50 of the caller's
  projects. The same principal's read of a project is reused for 20 seconds
  (`EndpointBackend.READ_CACHE_SECONDS`, in memory, bounded to 2048 entries), so
  repeated page loads do not re-export every project. A principal's own successful
  write drops their cached reads of that project, so their action shows at once.
  Authority is never cached:
  each project is re-authorized on every request, so a removed member loses it at
  once; only the task data may be up to 20 seconds old on My work. The queue, brief
  and task list always read fresh.
- **Agent prompts on My work.** `GET /v1/me/work` also returns `agent_prompts`: one
  copyable prompt per agent the caller owns, built by `agent_prompts.py` from the same
  queue data, limited to the projects that agent is granted, grouped by project and
  tailored by the caller's live role there
  (approvers: reviews and re-reviews with revision, commit and contribution id,
  approved-but-not-integrated, blocked, unclaimed P0/P1 and stale claims, i.e. claimed
  with no recorded activity for 72 hours or more; workers: changes requested with the
  pending request item ids where the backend knows them, their claimed and delivered
  tasks, claimable tasks; viewers: a read-only status summary that tells the agent not
  to change anything). At most 25 items, then "and N more". Every prompt carries its
  snapshot time, tells the agent to fetch its own list first (`/v1/agents/me/next`
  with `curl -K` and its per-agent file) and, for items that concern the owner rather
  than the agent, to compare with `/v1/projects/<id>/queue` and
  `/v1/projects/<id>/tasks?status=active`; to report every difference before acting;
  to re-check each item's brief before acting; and it states that titles are labels
  written by other people. Titles and names appear only as quoted labels (control
  characters and line breaks removed, every quote-like character, typographic and
  fullwidth included, turned into an apostrophe, at most 60 characters); ids are passed
  through only if they look like ids. The server address in a prompt is `--public-url`,
  or the `<ORCHESTRA_SERVER_URL>` placeholder when none is set; it never comes from the
  request's `Host` header. No prompt contains or asks for a secret. On the
  canonical binding the `work` projection gives counts but not pending request ids or
  review times, so those lines say "read the task brief" and "wait time unknown", and
  the blocked class is empty (see `endpoint-blocked-signal`).
- **Review respond step (slice 2).** Canonically a requested change stays open until
  the contributor records a `respond` resolution for it, even after a newer revision
  arrives. The web interface cannot record responses yet; the task page says the
  requests are still open instead of implying the new revision resolved them.
  Contributors respond with their worker tools. (The disposable in-process backend
  has no respond record and treats a newer revision as resolving the requests.)
- **Account lookup residual risk.** `GET /v1/accounts/lookup` answers exact usernames
  for a project administrator, and any account may create a project and so become
  one; an account holder can therefore still test whether a given username exists.
  Mitigations: exact match only (no prefix or search), the same 404 for missing,
  partial and disabled accounts, at most 20 lookups per principal per 10 minutes
  (`429 rate_limited` beyond that; in memory, reset on restart) and an audit event
  per authorized lookup on that project (`accounts.lookup`, outcome
  `found`/`not_found`/`throttled`, reason `username_hmac=<16 hex>`: HMAC-SHA256 of
  the lower-cased name under a per-deployment key, never the name itself). The key
  (`lookup_audit_key`) is generated once into the private service state and is never
  logged or returned; it is backed up and restored with that state, and deleting it
  only makes earlier digests incomparable with later ones. Operators who need stronger guarantees
  should disable self-service project creation (section 12 of the design) so only
  real project owners can look names up.
- **Known limitation `endpoint-blocked-signal`.** `blocked` attention for an agent is
  derived from the in-process canonical checkpoint view (`backend.state['checkpoints']`).
  The canonical endpoint binding does not mirror checkpoints into service state, so
  over `--backend endpoint` that one signal is **absent rather than wrong** while
  changes-requested, claimable and awaiting-review are unaffected. Mirroring
  checkpoints into the endpoint backend is deliberately not attempted in this
  revision (it is the open checkpoint item `endpoint-blocked-signal`).
- Administrative audit coverage is partial: login outcomes, authorization
  denials and every successful idempotent mutation are recorded; a per-field
  before/after administrative trail is not implemented.
- The state store is a single JSON document guarded by one in-process lock, written
  with a unique temporary and an atomic replace. Run exactly one service process;
  multi-process or multi-host scale-out is out of scope and would need a
  transactional store. The store lock is also the authority/revocation boundary: a
  revocation cannot interleave between an authority check and the mutation it
  authorizes.
- The final-owner invariant is enforced for every caller, including a superuser.
  Recovery is explicit and accepted: assign `owner` to another member first, then
  demote, remove or disable the previous sole owner. Review approval is owner-only;
  a contributor cannot approve, and a worker credential never can.

## 11. Unresolved office decisions (owner, before rollout)

1. Hostname, certificate operator and renewal process.
2. Reverse-proxy trust boundary and whether the proxy terminates TLS or the
   service does.
3. Secret-store location and backup-key owner.
4. Retention period and recovery objectives for audit, state and backups.
5. Whether to bind an external identity provider (SSO) or MFA, and when.
6. Network exposure and any approved browser origin list.
7. The personal-agent design decisions (checkpoint item `owner-decisions-pending`)
   were settled by the owner on 2026-09-27 (comment
   `01a0e2f2-569c-7d03-94ee-36b8dd61940a`): decisions 1, 2, 3, 5, 6, 7 and 8 keep
   the coordinator proposal as implemented; decision 4 (project owners/admins list
   and revoke agents in their project) and decision 9 (recommend VS Code secret
   storage or the OS credential store first, environment variable only as a
   documented fallback, never a literal `export` of the secret) are implemented in
   this revision. On 2026-09-29 the owner updated decision 9 in practice: the secret
   lives in a per-agent curl config file in the user profile, used with `curl -K`
   (section 6, "Personal agents"); the rest of decision 9 stands. The `owner-decisions-pending` checkpoint item is therefore
   resolved; the disposable-build caveat below still stands until rollout decisions
   1-6 in this list are recorded.

Until these are recorded, this service is a disposable local validation build,
not an office deployment.
