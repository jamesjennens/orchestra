# Office HTTP transport and project access design

**Status:** design proposal for review; not an implementation, deployment plan, or
accepted BRD baseline.

This document defines a bounded security and product boundary for an office
deployment of Orchestra. It addresses browser users and command-line workers
without weakening the existing SSH/local transport. Examples use synthetic
names and tokens. No real credentials, private project history, or office
hosting assumptions belong here.

## 1. Goals and non-goals

The design should let an ordinary user use a browser or an authenticated
client without SSH access or access to a shared home directory. It should
preserve canonical task, claim, checkpoint, history, review, lifecycle and
backup semantics. A server-side authorization decision, not a hidden UI
control, must protect every read and mutation.

The first design assumes local accounts and passwords, a future seam for an
external identity provider, a global superuser, and project memberships with
`owner`, `contributor` and `viewer` roles. It does **not** select an office
domain, SSO provider, network policy, public exposure, cloud vendor, email
service, Git permission model, or deployment topology. Those are unresolved
authority and operations decisions, not defaults to smuggle into an
implementation.

The HTTP service is an authenticated adapter to the canonical coordination
service, not a replacement for repository permissions, Git hosting, TLS
termination, operating-system isolation, or deployment approval.

## 2. Threat model and trust boundaries

### Assets

- Project names, task descriptions, comments, attachments, review evidence,
  actor attribution and audit records.
- Account password verifiers, browser sessions, worker credentials, revocation
  state, backup keys and service configuration.
- Cross-project confidentiality and mutation authority, especially membership,
  ownership and superuser administration.

### Adversaries and assumptions

The threat model includes an unauthenticated internet or office-network caller,
an authenticated user attempting cross-project access or privilege escalation,
stolen/expired browser or worker credentials, replayed requests, malicious
attachments, a compromised browser, and a buggy or misconfigured reverse proxy.
It also includes an operator accidentally exposing a database or backup.

The first deployment trusts the host operator, the canonical database process,
the TLS/reverse-proxy boundary and the configured secret store. It does not
trust client-supplied actor strings, project identifiers, role claims, hidden
frontend routes, or request ordering. A superuser is an administrative
authority, not an emergency bypass that should be shared by workers.

Compromise of the host, canonical database account, backup encryption key,
reverse proxy, or active superuser is outside the service's ability to repair;
the recovery plan must detect, revoke, rotate and restore from a known-good
copy. Denial of service and traffic analysis are deployment concerns, with
rate limits and monitoring as mitigations rather than guarantees.

### Required invariants

1. Every request has one server-derived authenticated principal or is rejected.
2. The effective actor/run identity is mapped from authenticated credentials;
   a submitted actor label is metadata and may never grant authority.
3. Every project-scoped read and write is authorized against current
   membership, not a stale client claim.
4. Role changes, disablement, revocation and project deletion take effect
   before the next authorized operation.
5. Audit entries do not contain passwords, bearer values, session cookies, or
   full attachment contents.
6. Errors do not reveal whether another project, user, task or identifier
   exists unless the caller is authorized to know it.

## 3. TLS and request boundary

TLS terminates at an operator-managed reverse proxy or at the service itself;
the choice is a deployment decision, but plaintext must never be accepted on a
production-facing listener. The proxy must validate its certificate chain,
redirect or reject HTTP, set a bounded request body size, preserve only the
documented forwarding headers, and send a trusted request ID. The application
must ignore client-supplied `X-Forwarded-*` values unless the connection came
from a configured proxy.

Use modern TLS defaults supplied by the maintained proxy, disable obsolete
protocols/ciphers, enable HSTS only after the HTTPS hostname is stable, and
document certificate renewal and clock requirements. Internal proxy-to-service
traffic should use a loopback or protected socket; if it crosses hosts, use
authenticated TLS or an equivalent protected channel. The service binds only
to the configured interface and never exposes the database listener.

Responses set `Cache-Control: no-store` for authenticated data and credentials.
Cookies use `Secure`, `HttpOnly`, an explicit `SameSite` policy and a narrow
path/domain. CORS is disabled unless a separately approved browser origin list
is configured; `*` is not valid for credentialed requests.

## 4. Accounts, sessions and worker credentials

### Local human accounts

Accounts have stable opaque user IDs, display names, disabled state, timestamps,
and a password verifier. Store passwords only with a current memory-hard
password hashing scheme such as Argon2id (or the selected platform equivalent)
with a documented cost policy and upgrade-on-login path. Never log or persist
the password, reset secret, session cookie or bearer token.

The first bootstrap must require an out-of-band operator action that creates
the initial superuser with a one-time password setup or equivalent. There is no
shared/default password. Login has uniform failure responses, per-account and
per-source throttling, bounded attempts, and audit events for success, failure,
disablement, reset and logout. An administrator reset invalidates existing
sessions and records the acting administrator; disabling an account preserves
historical attribution while denying new work.

Browser login creates a server-side session with a random, high-entropy opaque
identifier. Store only a hash of the identifier, principal, issued/expiry times,
last-use metadata and revocation state. Rotate the session after login and
privilege changes; expire idle and absolute lifetimes; revoke on logout,
disablement, password reset and explicit administrator action. State-changing
browser requests require a synchronizer token or a documented same-site
double-submit CSRF defense. Do not put session values in URLs or
`localStorage`.

### Worker/API credentials

A human account may issue a named, project-scoped worker credential through an
authorized API operation. The secret is displayed once, stored server-side
only as a verifier or token hash, and carries an ID, owner user ID, allowed
project(s), allowed operation scope, creation/last-use/expiry timestamps and
revocation state. Prefer short-lived access tokens obtained from a separately
authenticated session; if a long-lived credential is required for unattended
work, require an explicit expiry and rotation.

Bearer credentials travel only in `Authorization: Bearer ...` over TLS. Never
accept them in query strings, referers, browser bundles or logs. Revocation
checks the credential record on every request; a signed self-contained token
alone is insufficient because immediate revocation is required. A key ID may
be logged, but not the secret or a reversible token.

The credential's server-side subject determines the authenticated user. A
requested `actor` such as `worker/example-run-7` is retained only as
attribution after validation and binding to that subject and credential. It
cannot impersonate another user, authorize a role change, or escape the
credential's project/scope. Run IDs remain distinct from stable human
ownership and are not leases.

### Future external authentication

Keep a provider-neutral identity binding table: local user ID, provider name,
provider subject, binding state and audit metadata. External login may map to an
existing account only through an explicit, reviewed linking or invitation
operation. Do not auto-link on an email string. The first implementation must
not pretend that SSO, MFA, recovery email or group synchronization exists.

## 5. Project isolation and authority

Projects are opaque server identifiers with a separate membership relation.
Every query, export, attachment fetch, audit read and mutation carries an
authorization filter before data retrieval. Do not use user-controlled project
paths or a global search that can disclose names, counts, existence, history or
identifiers from unauthorized projects. Storage paths are generated from
validated opaque IDs, not project names.

The minimum action matrix is:

| Operation | Superuser | Project owner | Contributor | Viewer |
| --- | --- | --- | --- | --- |
| List/open an authorized project | all | own | member | member |
| Read tasks, history, decisions, checkpoints and permitted attachments | all | own | member | member |
| Create a project | policy-controlled | no | no | no |
| Invite/remove members and assign contributor/viewer | all | own | no | no |
| Assign/remove project owner | all, subject to safeguard | policy-controlled, subject to safeguard | no | no |
| Create/update jobs, tasks, feedback and checkpoints | all | own | member, per operation | no |
| Claim work as the authenticated worker subject | all | own | own permitted credential | no |
| Submit/revise a contribution or review request | all | own | member, per workflow | no |
| Read project audit | all | own | no | no |
| Disable users, reset accounts, manage all projects | yes | no | no | no |

“All” is still subject to audit and destructive-operation confirmation. Project
role never grants Git merge, repository administration, deployment or host
access. A project creator becomes an owner only after the configured creation
policy permits it. Prevent removal or disabling of the final active
superuser, and prevent a project from having no active owner unless an
authorized superuser performs an explicit recovery operation. Role assignment
must be checked against the assigner's current role and cannot accept a
client-supplied role claim.

The unresolved policy choices are whether ordinary authenticated users may
create projects, whether project owners may add owners, whether a superuser
may be delegated, and whether viewer access includes attachments and audit
records. Implementations must require an explicit selected policy for each.

## 6. Minimal API and optional browser product

The minimal API is JSON over HTTPS and reuses existing canonical operation
semantics. It should expose only bounded operations, with stable error codes,
request IDs and pagination cursors:

```http
POST /v1/sessions
Content-Type: application/json

{"username":"alex@example.invalid","password":"<not-recorded>"}
```

```json
{"session":{"expires_at":"<timestamp>","csrf_required":true},
 "user":{"id":"usr_opaque_01","display_name":"Alex"}}
```

```http
GET /v1/projects/proj_opaque_01/tasks?limit=50&cursor=<opaque>
Authorization: Bearer <worker-secret>
```

```http
POST /v1/projects/proj_opaque_01/tasks/task_opaque_07/checkpoints
Idempotency-Key: req_01H... 
Authorization: Bearer <worker-secret>
Content-Type: application/json

{"checkpoint":{ "...": "canonical checkpoint payload" }}
```

The server derives the principal and permitted project from the credential.
An optional `actor` field is checked only for the credential's registered
attribution namespace; a mismatch is rejected rather than trusted. Mutating
responses include the canonical operation ID/result, not a success-shaped
fallback after an uncertain write. Use `401` for absent/invalid credentials,
`403` for authenticated but unauthorized actions, `404` for an inaccessible
resource where existence must not leak, `409` for stale state or idempotency
conflict, `413` for bounded payload violations, `429` for throttling, and
`5xx` only for an operation whose commit status is explicitly unknown.

The optional browser product uses the same API and server authorization. It
provides sign-in, project creation/joining, membership administration, task
briefs/current checkpoints, paginated history, contribution/review queues,
feedback and artifact links. Reading never acknowledges work. The UI is not
the security boundary, does not store long-lived bearer secrets, and must
surface stale/conflict/error states. A polished visual design and accessibility
criteria belong to the follow-up web-interface task, not this transport design.

## 7. Attachments, replay and idempotency

Request bodies, individual attachments, total request size, count, filename
length, decompressed size and accepted media types are bounded at the proxy
and application layers. Store attachments outside source control under
server-generated names, bind metadata to the authorized project/task, compute
content hashes while streaming, and reject traversal, symlinks, ambiguous
encodings and archive bombs. Download authorization is checked independently;
the original filename is display metadata only.

Every mutation that can be retried accepts an idempotency key scoped to
authenticated principal, project and operation route. Store the request hash,
canonical result or explicit in-progress/unknown state, expiry and audit
pointer under a uniqueness constraint. The same key with different content
returns a conflict. A repeated exact request returns the original canonical
result; it must not duplicate a task, comment, attachment registration or
role change. Idempotency does not make a non-transactional external side effect
exactly once, so the response and reconciliation protocol must expose unknown
outcomes.

Pagination cursors are opaque, bounded and bound to principal/project/query
scope. Do not accept client offsets as authorization or use timestamps alone
where stable tie handling is required. Rate-limit login, token creation,
attachment upload, exports and expensive history queries separately.

## 8. Audit and privacy

Append audit events for login outcomes, session/token issuance and revocation,
account/role/project changes, authorization failures, attachment operations,
canonical mutations, idempotency reuse/conflict, backup/restore and operator
recovery. Each event includes server time, request ID, authenticated user ID
or anonymous result, credential ID where safe, project/resource IDs only when
authorized, action, outcome and a redacted reason. Actor/run attribution is
additional metadata, never the identity field.

Audit storage is append-only to ordinary service callers, access-controlled,
retained under an explicit policy, and included in protected backup scope.
Define redaction, export and incident-review procedures. Do not log request
bodies, Authorization headers, cookies, passwords, reset values, CSRF values,
attachment contents or raw query strings containing secrets.

## 9. Deployment, backup, recovery and SSH compatibility

The service runs under a dedicated least-privilege account with a private
runtime root, separate secret/config paths, a loopback or protected service
socket, resource limits and health/readiness checks that do not disclose
credentials. The deployment package must document reverse-proxy setup,
certificate renewal, secret provisioning, clock synchronization, log
retention, upgrades, rollback and shutdown. Public listeners are not enabled
by this design task.

Backups contain the canonical native database plus coordination sidecar,
authentication/session/token revocation state, project membership, audit data
and the private onboarding/configuration material required for recovery.
Encrypt off-machine copies, restrict key access, define retention, and never
copy live credentials into source. A restore drill uses a new isolated
deployment, verifies hashes, revocation state, memberships, audit continuity
and pending operations, then performs an explicit cutover. Rotate all
deployment secrets and invalidate sessions/tokens if compromise is suspected.

SSH remains a supported explicit transport for operators and migrations. It
continues to use the existing actor/configuration contract and does not gain
HTTP roles implicitly. Local same-host transport remains explicit and does not
fall back to HTTP or SSH. During migration, do not expose the database or
shared runtime path to ordinary browser users.

## 10. Phased migration

1. **Design and decision:** accept this boundary, select authority policies,
   hosting/TLS constraints, retention and recovery owners, and create linked
   implementation briefs. Keep the current trusted SSH deployment unchanged.
2. **Disposable service:** implement the minimal API/auth package behind a
   private TLS test endpoint; validate project isolation, actor binding,
   revocation, idempotency, limits, audit and recovery with synthetic data.
3. **Pilot bridge:** create explicit local accounts and project memberships.
   Register worker credentials per project and map existing SSH actor/run
   records to stable user IDs as attribution metadata. Do not infer identity
   from actor spelling or silently merge people. Keep SSH available for
   operators and provide a documented rollback to it.
4. **Office rollout:** provision TLS and accounts through the selected
   operator-controlled process, run a backup/restore drill, invite users,
   monitor audit/rate-limit signals, and migrate one disposable/project
   cohort at a time. Revoke bridge credentials after verification.
5. **Browser UI:** only after the API/auth contract is reviewed, implement the
   required multi-user workflow against the same authorization boundary.

No migration step changes the draft BRD in place. Requirement changes,
authority decisions and implementation acceptance are separate canonical
records; previous snapshots and SSH evidence remain reproducible.

## 11. Test and review strategy

Tests should be disposable and synthetic:

- Unit tests for password-hash policy, session/token expiry and revocation,
  role/action decisions, actor binding, project filters, error redaction and
  canonical idempotency keys.
- HTTP contract tests for unauthenticated access, cross-project reads/writes,
  role escalation, final-owner/superuser safeguards, CSRF/cookie behavior,
  stale cursors, replay, conflicting idempotency keys, pagination and clean
  JSON errors.
- Fuzz/property tests for paths, filenames, JSON limits, Unicode, compressed
  attachments and malformed forwarding/auth headers.
- Disposable integration tests for two projects and multiple accounts,
  concurrent claim/comment/review operations, uncertain responses followed by
  exact retries, credential revocation, restart and backup/restore.
- TLS/proxy checks for plaintext rejection, forwarding-header trust, secure
  cookies, cache controls, body limits and request-ID propagation.
- Browser acceptance tests, in the later UI task, for create/join, membership,
  task/review/checkpoint flows, keyboard navigation and no data leakage.
- Operational review of secret provisioning, log redaction, restore cutover,
  rollback and incident credential rotation. No live office service is a
  valid test fixture.

Review must separately label design acceptance, implementation, tests,
integration, deployment and live verification. This document claims none of
those lifecycle facts merely because examples or proposed tests exist.

## 12. Open decisions before implementation

- Which named authorities may create projects, assign owners, approve
  memberships and operate the global superuser?
- Are ordinary users allowed to create projects, and is invitation required?
- Which project roles may read attachments, audit records and private feedback?
- What are the session/token lifetimes, MFA expectations and account-recovery
  authority for the initial office?
- Which TLS termination, hostname, network exposure, proxy trust and
  certificate-renewal policy is approved?
- What deployment owner, backup key owner, retention period and recovery
  objective apply?
- Which external identity provider, if any, will be linked later, and who may
  bind an external subject to an existing local account?
- Which exact canonical endpoint/client surfaces are in the first HTTP slice,
  and which remain SSH-only for operators?

Until these are answered in canonical decisions, `.19` and `.20` must treat
the corresponding behavior as blocked design input rather than inventing a
security policy.
