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

A project owner or superuser may issue a named, project-scoped worker
credential through an authorized API operation. The secret is displayed once, stored server-side
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

### Provisional local lifecycle defaults

For the first implementation, use configurable defaults of a 30-minute
browser idle timeout, 12-hour absolute browser session lifetime, and session
rotation on login, password reset, disablement and privilege change. Logout
revokes the current session immediately. Password reset is an authenticated
superuser operation in the initial local deployment: it generates a
single-use random reset value delivered only through the operator's approved
out-of-band channel, expires it after 30 minutes, stores only its hash, and
revokes all sessions and worker credentials owned by the reset account.
There is no email dependency.

Worker credentials default to project scope, the minimum selected operation
scope, 30-day expiry and explicit owner/superuser revocation. Creation returns
the secret once; the server stores only a verifier. A future deployment may
shorten or extend these values by configuration, but expiry and revocation
checks remain mandatory. SSO and MFA can later replace or strengthen login
without changing project authorization or actor binding; neither blocks the
first local implementation.

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
| Create a project | yes, or authenticated user when enabled | no | no | no |
| Invite/remove members and assign contributor/viewer | all | own | no | no |
| Assign/remove project owner | all, subject to safeguard | policy-controlled, subject to safeguard | no | no |
| Create/update jobs, tasks, feedback and checkpoints | all | own | member, per operation | no |
| Claim work as the authenticated worker subject | all | own | own permitted credential | no |
| Submit/revise a contribution or review request | all | own | member, per workflow | no |
| Issue/revoke worker credentials | all | own project | no | no |
| Read project audit | all | own | no | no |
| Disable users, reset accounts, manage all projects | yes | no | no | no |

“All” is still subject to audit and destructive-operation confirmation. Project
role never grants Git merge, repository administration, deployment or host
access. The coordinator-proposed initial defaults are configurable service
policy, not owner-approved BRD text:

- Authenticated users may create private projects; the creator becomes the
 first owner. A deployment may disable self-service creation, in which case
 only a superuser creates projects and assigns the first owner.
- Owners may assign contributor/viewer membership, but only a superuser may
 assign or remove project owners. The final active project owner cannot be
 removed or disabled without an explicit superuser recovery action.
- Exactly one or more protected superusers exist. The final active superuser
 cannot be disabled, demoted or deleted through the ordinary API.
- Members may read tasks, history, decisions, checkpoints, feedback and
 ordinary permitted attachments. Viewer access excludes audit records,
 membership administration and mutations.
- Project owners and superusers may read project audit records. Private
 feedback follows project membership, not the personal “My work” filter.
- Superusers can administer every project; no cross-project search or
 identifier disclosure is implied for ordinary members.

Role assignment must be checked against the assigner's current role and cannot
accept a client-supplied role claim. These defaults may be configured before
implementation tests, but changing them requires a reviewed policy decision
and corresponding authorization tests.

## 6. Minimal API and required browser workflow

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

The browser workflow is required product scope, not an optional GUI. It uses
the same API and server authorization for sign-in, project creation/joining,
membership administration, task briefs/current checkpoints, paginated history,
contribution/review queues, feedback and artifact links. Reading never
acknowledges work. The UI is not the security boundary, does not store
long-lived bearer secrets, and must surface stale/conflict/error states. The
follow-up web-interface task owns visual design, accessibility and the
end-to-end browser walkthrough; this document fixes its security and API
contract.

### Bounded route and operation contract

The following is the first bounded surface. “Backend mapping” means the
existing canonical operation whose authorization wrapper must run before the
call; it does not authorize direct database access. All mutation rows require
the authenticated principal, current membership check, and a request ID.

| Route/operation | Principal and role | Preconditions | Idempotency and response/errors | Backend mapping |
| --- | --- | --- | --- | --- |
| `POST /v1/accounts`, `GET /v1/accounts` | Superuser; list is superuser-only | Create requires unique username and initial disabled/unset password; list is administrative | Create key scoped to principal + route (not payload); `201` account without a password, `403/409`; list `200` redacts secrets | Account create/list and audit |
| `POST /v1/sessions`, `DELETE /v1/sessions/current` | Anonymous login; authenticated logout | Login uses local account; logout uses current session | Login is not idempotent; `201` session or uniform `401`; logout exact session returns `204` | Local account verifier; session create/revoke |
| `POST /v1/accounts/{id}/password`, `POST /v1/accounts/{id}/reset`, `POST /v1/accounts/{id}/reset/redeem`, `POST /v1/accounts/{id}/disable` | Password change: account owner or superuser; reset issue/disable: superuser; reset redeem: reset recipient | Change requires current password or superuser; issue creates a pending single-use reset; redeem requires unexpired reset value and new password | Change/redeem key scoped to principal + account + operation; reset issue key scoped to admin + account; `204/200`, `401/403/409`; reset value is never returned by API | Password verifier update; reset-token consume; account disable and session/token revocation |
| `POST /v1/projects`, `GET /v1/projects`, `POST /v1/projects/{id}/archive` | Authenticated user; create default enabled; list members only; archive owner/superuser | Create validates unique metadata; archive requires active owner and confirmation | Create/archive key scoped to principal + route + payload; `201/200`, `403/409` | Project create/list and archive state transition |
| `PUT /v1/projects/{id}/members/{user}`, `DELETE .../members/{user}` | Owner or superuser | Assigner is current owner; cannot remove final active owner | Key scoped to principal + project + target + payload; `200/204`, `403/409` | Membership add/remove and role transition |
| `POST /v1/projects/{id}/worker-credentials`, `POST .../worker-credentials/{credential}/revoke` | Owner or superuser issues; owner of credential may revoke own | Project membership current; requested scope subset of issuer scope | Issue key scoped to principal + project + route; request hash detects payload conflicts; `201` secret once, exact uncertain retry `200` metadata only; revoke key scoped to principal + project + credential, `204`; `403/409` | Credential registry and revocation |
| `POST /v1/projects/{id}/jobs`, `PATCH /v1/projects/{id}/jobs/{job}`, `POST /v1/projects/{id}/tasks`, `PATCH /v1/projects/{id}/tasks/{task}` | Owner/contributor for create/update according to project policy; viewer denied | Membership current; update carries resource version; task parent/job must be in same project | Key scoped to principal + project + route + client operation ID; `201/200`, `403/409` | Canonical job/task create/update |
| `GET /v1/projects/{id}/tasks`, `GET .../tasks/{task}` | Project member; viewer may read | Membership checked before query and cursor validation | Opaque cursor bound to principal/project/query; `200`, `401/403/404` | Canonical task/list/show and history views |
| `POST /v1/projects/{id}/tasks/{task}/claim` | Contributor/owner or bound worker credential | Task open/claimable and actor binding matches credential | Key scoped to principal + project + task + claim context; `200`, `403/409` | Existing atomic claim operation |
| `POST /v1/projects/{id}/tasks/{task}/reviews` | Contributor submits; project owner requests or approves | Current contribution and review chain are read under same authorization check | Key scoped to principal + task + contribution + operation; `201`, `403/409` | Structured contribution/review protocol |
| `POST /v1/projects/{id}/tasks/{task}/checkpoints` | Contributor/owner; viewer denied | Checkpoint previous/cursor is current; task membership remains valid | Key scoped to principal + task + previous + payload; `201`, `403/409` | Canonical checkpoint append |
| `GET /v1/projects/{id}/tasks/{task}/history` | Project member; audit is separate | Cursor is bound to authorized task/project snapshot | Opaque cursor; `200`, `403/404/409` | Bounded history/activity read |
| `POST /v1/projects/{id}/feedback`, `GET /v1/projects/{id}/feedback` | Contributor/owner may add; project member may read per default policy | Source task/version and evidence are bounded; membership current | Add key scoped to principal + project + source + payload; `201`; list cursor; `403/409` | Feedback append/list stream |

All successful mutation responses contain the canonical operation or record
identifier and committed state. `401` means no valid authenticated principal,
`403` means the principal lacks authority, `404` hides inaccessible existence,
`409` covers stale state, final-admin safeguards and idempotency conflicts,
`413` covers limits, `422` covers invalid structured payloads, and `429`
covers throttling. A timeout or `5xx` never fabricates success: the client
must reconcile with the same idempotency key or canonical read.

Membership and revocation race handling is transactional at the service
boundary: authorization reads the account/session/credential, membership,
role and resource version under one database transaction or equivalent
serializable check; the mutation records the checked authorization version.
Revocation/disablement commits invalidate future operations and waits for
in-flight transactions to commit or abort before reporting completion. A
request that loses the race returns `401`, `403` or `409` and cannot complete
the canonical mutation.

Existing actor-owned claims are mapped by an explicit migration table from
legacy actor string to stable user ID and, where applicable, credential ID.
The first authenticated operation must prove possession of the mapped
credential; spelling, display name, SSH key comment and actor string alone are
not proof. Ambiguous or unmapped claims remain attributed historical records
and require an explicit owner-authorized handoff before a new principal acts.

## 7. Attachments, replay and idempotency

Request bodies, individual attachments, total request size, count, filename
length, decompressed size and accepted media types are bounded at the proxy
and application layers. Store attachments outside source control under
server-generated names, bind metadata to the authorized project/task, compute
content hashes while streaming, and reject traversal, symlinks, ambiguous
encodings and archive bombs. Download authorization is checked independently;
the original filename is display metadata only.

Every mutation that can be retried accepts an idempotency key scoped to
authenticated principal, project and operation route. The key namespace does
not include the request payload: store the canonical request hash separately,
canonical result or explicit in-progress/unknown state, expiry and audit
pointer under a uniqueness constraint. and the same key with different content returns a conflict. A repeated exact
request returns the original canonical
result; it must not duplicate a task, comment, attachment registration or
role change. Idempotency does not make a non-transactional external side effect
exactly once, so the response and reconciliation protocol must expose unknown
outcomes.

Credential issuance is the deliberate exception to replaying a secret. The
issuance idempotency record stores the credential ID, request hash, status and
secret-delivery state, but never the bearer secret. If the `201` response is
lost, an exact retry returns `200` with the credential ID and
`secret_available:false`; it never creates a second secret and never exposes
the original secret after the one-time delivery window. The caller then uses
`POST .../worker-credentials/{id}/revoke` with the same authenticated owner
or superuser, reconciles the revoked state, and issues a new credential with a
new idempotency key. A failed or uncertain issuance is therefore explicitly
reconciled, not treated as a usable credential. Revoke has its own idempotency
record and returns `204` for the committed revoked state; it does not replay
or return an issuance secret.

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

### Implementation contract versus rollout configuration

The `.19` implementation may proceed against a disposable local configuration
once the route matrix, defaults above, transaction/revocation rules and
contract tests are reviewed. It must use placeholders for hostname,
certificate paths, proxy address, secret-store location, retention and
recovery owner. Those are office rollout configuration, not prerequisites for
implementing or testing the service.

The office deployment owner later selects the real hostname, certificate
operator, network exposure, backup key owner, retention period, recovery
objective and incident contacts. A future SSO provider and MFA policy are
integration choices; they are not prerequisites for the first local
username/password implementation. The service must retain an identity-provider
seam and be explicit when an office rollout has not selected one.

## 10. Phased migration and exit criteria

1. **Design and contract review:** review this boundary, route matrix,
   configurable defaults, threat model and test plan. Keep the current
   trusted SSH deployment unchanged. Exit when the coordinator records review
   disposition and the implementation briefs reference this design.
2. **`.19` disposable service:** implement the minimal API/auth package behind
   a private TLS test endpoint using synthetic local configuration. Exit when
   contract tests demonstrate local login/logout/reset/disable, project
   isolation, actor binding, revocation races, idempotent retries, bounded
   attachments, audit redaction, restart and restore; no office listener is
   required.
3. **`.20` required browser workflow:** build sign-in, create/join, project
   membership, task/claim, checkpoint/history, feedback and contribution/review
   screens against the reviewed API. Exit when a fresh synthetic user and
   second worker complete the browser walkthrough, unauthorized project data
   stays hidden, stale/retry errors are visible, and accessibility checks pass.
4. **Pilot bridge:** create explicit local accounts and project memberships.
   Register worker credentials per project and map existing SSH actor/run
   records to stable user IDs as attribution metadata. Do not infer identity
   from actor spelling or silently merge people. Exit when one disposable
   project cohort passes backup/restore and rollback, with SSH still available
   for operators.
5. **Office rollout:** the deployment owner selects hostname, TLS, proxy,
   secret store, retention, recovery and optional external authentication.
   Run a backup/restore drill, invite users, monitor audit/rate-limit signals,
   and migrate one approved cohort at a time. Exit only after explicit
   operational approval and live verification; this design grants neither.

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

## 12. Open decisions before implementation and rollout

The following are genuine coordinator/owner decisions, with the recommended
default already usable for disposable implementation:

- **Project creation:** recommend authenticated creation with creator as owner;
  effect of disabling it is that only a superuser can create and assign the
  first owner.
- **Owner delegation:** recommend only superusers assign/remove owners; effect
  is that project owners administer contributor/viewer membership only.
- **Visibility:** recommend members read private feedback and permitted
  attachments, while only owners/superusers read audit; effect is narrower
  disclosure for viewers and contributors.
- **Recovery authority:** recommend superuser-only local reset with the
  provisional 30-minute reset value and immediate credential/session
  revocation; effect is an operator-controlled, no-email bootstrap.
- **Office rollout:** select the hostname, TLS termination/certificate
  operator, proxy trust and network exposure; effect is deployment-specific
  configuration, not a change to the API contract.
- **Operations:** assign deployment and backup-key owners, retention and
  recovery objectives; effect is required before office rollout/live
  verification, not before disposable implementation.
- **External identity:** select whether and when to bind an SSO provider or
  MFA; effect is a future authentication enhancement, not a prerequisite for
  local accounts.

The first implementation is blocked only on review of this contract and its
configurable defaults. `.19` and `.20` must not claim deployment authority;
office configuration, SSO/MFA and live verification remain separate decisions.

The required fresh-user walkthrough is therefore concrete: a superuser creates
an account and the user sets a password through the one-time reset redemption;
the authenticated user creates a project when creation is enabled (otherwise
the superuser creates it and assigns membership); the owner adds a second
member, creates a job and task, and issues a project-scoped worker credential;
the worker claims the task, appends a checkpoint and submits a contribution;
the owner reviews it and both users can read the permitted history and
feedback. Every transition is backed by the routes above, current role checks,
and canonical backend records. A password reset issuance alone does not
complete onboarding: only redemption by the recipient changes the verifier.
