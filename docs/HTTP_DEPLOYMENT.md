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
sends `Cache-Control: no-store` on every response.

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

## 8. Backup, restore and rollback

Back up, in one coordinated snapshot:

1. the canonical coordination database and its sidecar,
2. this service's `--state` document (accounts, membership, revocation state,
   audit and idempotency records).

Encrypt off-machine copies, restrict the key to the backup owner, and define
retention before rollout. Restore drill on an **isolated** deployment:

```sh
sudo systemctl stop orchestra-http
sudo -u <SERVICE_USER> install -m 0600 <RESTORED_STATE> <RUNTIME_ROOT>/http-state.json
sudo -u <SERVICE_USER> python3 -m json.tool <RUNTIME_ROOT>/http-state.json > /dev/null
sudo systemctl start orchestra-http
curl -fsS http://127.0.0.1:8443/healthz          # {"status":"ok"}
```

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
  expired. A tombstone is removed only when its age exceeds `JOURNAL_TOMBSTONE_SECONDS`
  (default 30 days) measured against the trusted clock; a still-live tombstone is never
  dropped to satisfy the byte or count budget.
* **high_water** — a non-decreasing wall-clock mark. Reclaim, compaction and the
  replay/expiry decision refuse to act while the clock is more than
  `JOURNAL_MAX_SKEW_SECONDS` ahead of it, and the mark is never ratcheted toward a
  jumped-forward clock, so a forward jump can neither reclaim nor expire an identity
  whose receipt window is still open. `trusted_now()` (the same clock rows are stamped
  with) is used on the read path too, so during a +8 day jump a 60-second-old committed
  receipt replays and a live uncertain reservation reports `124` instead of both being
  refused as expired.

An exact retry inside its window replays the committed response or reports `124`
uncertainty; outside it (or after reclaim) it is refused as expired with `rc=2`. It is
never re-executed.

**Retention is by time only.** A committed receipt is never compacted, deleted or
otherwise evicted while its window is open, whatever the store's size; the byte budget
(`MAX_JOURNAL_BYTES`) and the tombstone bound (`JOURNAL_TOMBSTONE_LIMIT`) are
**reported**, never enforced by eviction, and accumulated tombstones never block a
write. The one admission bound is the live-identity count (`JOURNAL_LIMIT`): when the
store genuinely cannot hold one more live identity the mutation fails closed with `124`,
the transaction is rolled back so the pre-existing store is untouched, and no effect is
attempted. `stats()` reports `live_bytes`, `tombstone_bytes`, `bytes`, `limit_bytes`
(`MAX_JOURNAL_BYTES`), `over_bytes`/`over_tombstones`/`over_limit`, `journal_mode` and
`running_totals_match`; `stats()['file_bytes']` is the database file's size on disk. The
store keeps `live_count`/`tombstone_count`/`total_bytes` running totals in `meta`,
updated inside the same transaction as every row write, so a keyed operation never scans
the table and latency does not grow with the store's size. Each row also carries the
directed `actor`, the canonical `route` and the precomputed `replay_until`/`expires_at`
timestamps.

The HTTP service's own idempotency receipts and committed canonical results live in a
second SQLite store beside the service state document (`<state>.records.sqlite3`,
`http_auth.RecordStore`, WAL, time-only retention). They used to live inside the
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

# accept the current clock as the high-water mark after a real clock correction
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> --reset-high-water

# explicit override after reconciling canonical state: remove a still-live identity
sudo -u <SERVICE_USER> python3 admin.py --root <RUNTIME_ROOT> journal <PROJECT> \
    --prune-before <EPOCH_SECONDS>
```

`--reset-high-water` is the recovery for a genuine correction (an NTP fix, a restored
host): while the persisted mark is more than `JOURNAL_MAX_SKEW_SECONDS` behind the real
clock reclaim stays refused, so a forward jump cannot drop a live receipt, and this flag
explicitly accepts the corrected clock. It removes no identity by itself.

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

Until these are recorded, this service is a disposable local validation build,
not an office deployment.
