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

# 3. one-time superuser bootstrap; there is no default or shared password.
#    Run it while the service is stopped: it takes the runtime lock and refuses
#    while a service holds that root. --root must name an existing runtime
#    directory (a path that is not one is refused with one sentence).
sudo -u <SERVICE_USER> python3 http_service.py \
  --state <RUNTIME_ROOT>/http-state.json --root <RUNTIME_ROOT> \
  --bootstrap-user <ADMIN_USERNAME>
# the operator types the password at the prompt; it is never echoed or logged
```

Bootstrap refuses to run once any account exists, so it cannot silently reset a
live deployment. Create ordinary accounts through the API and hand each user a
single-use reset value; only redemption changes the verifier.

The guard is only as good as `--root`: it locks the root you name, so naming a
different root while a service holds the real one gets past the lock and the new
account is lost as before.

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

`--connections-per-address N` (default 100; 1 to 200) is how many of the
service's 200 connections one client address may have open at once; one more
from it is closed at once. An IPv6 address is counted with its /64. A peer named
by `--trusted-proxy` is not limited as an address, because every client behind
it arrives from it: for the requests it forwards, N is how many requests of one
forwarded address are served at once, and one more is answered `503 busy` with
`Retry-After: 1` and `Connection: close` before its route begins. A forwarded
header from any other peer does not change which address a connection is
counted for.

`--logins-per-address N` (default 4; 1 to 16) is how many of the 16 log-ins in
flight one client address may have. A log-in over it waits up to 2 seconds for
one of its address's places (at most 12 of one address wait at once) and is then
answered `503 busy` with `Retry-After: 5`. A log-in that arrives from the
service's own host, or from a trusted proxy that forwarded no address (an SSH
tunnel), is not held to a share: everybody arrives from that address. See
docs/OFFICE_SERVICE.md ("Connections that say nothing" and the paragraphs about
log-ins) for what the limits stop, what they do not, and the measurements.

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

**Feedback is not built on the endpoint backend**, which is the backend of every
real installation: reading and sending it (`GET` and `POST
/v1/projects/{id}/feedback`) answer `501 not_implemented`, nothing is kept and no
idempotency key is held. The Feedback page says so ("Feedback is not available on
this server") and shows no form. Until it is built, what somebody found goes on the
task it concerns or to the people who run the project; a worker with the client has
`feedback` over SSH.

### Projects on the endpoint backend

With `--backend endpoint`, a project's tasks live in a canonical Beads project under
the runtime root, and only an operator creates one, on the coordination host. The web
service never creates it. A project is therefore made in two steps:

1. On the host, as the service account: `admin.py --root <RUNTIME_ROOT> add-project
   NAME` (2-24 lowercase letters or digits, starting with a letter).
2. In the web interface, a **superuser** registers it: Projects, "Register a project",
   canonical project name `NAME` and an optional display name. Through the API, this is
   `POST /v1/projects` with `{"project_id": "NAME", "name": ...}`.

The rules:
- Registration is superuser-only. Anyone else gets the same 403 whatever name they send,
  so the check that the canonical project exists cannot be used to probe for names.
- The project id is the canonical name. One canonical project is registered once; a
  second registration is refused with 409, naming the existing record (archived or not).
  An exact retry of a successful registration with the same `Idempotency-Key` replays
  its 201.
- A few names are reserved for the server's own routes and cannot be registered:
  `unconfirmed` is the upgrade check below (`GET /v1/projects/unconfirmed`). Registering
  one is refused with 422 before the host is asked, because the record could never be
  read back under its own name.
- Registration writes `registered_by` (the superuser who registered it) on the record.
  That mark is what makes the mapping count as superuser-backed, so it keeps working even
  if that superuser is later demoted. A record made by an older kit has no such mark; a
  confirmation backfills it, and see the limit on that below.
- If no initialized canonical project `NAME` exists, the request is refused with 422 and
  nothing is stored.
- Registering makes the superuser the project's only member (owner). Add members
  afterwards on its Members page.
- `GET /v1/sessions/current` reports `project_create`: `register` for a superuser,
  `operator-only` for anyone else. The Projects page shows the register form, or only the
  explanation.

**Records from before this rule.** An older kit let any account "create" a project from
the web interface. That wrote only an HTTP record with a `proj_...` id, which can never
match a canonical project, so its task pages failed. Such a record is now listed with
`usable: false` and a reason, the UI marks it "Not usable" with no task links, and its
task routes answer 409 instead of an endpoint error. Nothing is deleted. Archive each one
(the Archive button, or `POST /v1/projects/{id}/archive`), then create and register the
real project as above.

**Records with a canonical id from before this rule (the upgrade check).** Before
registration was limited to superusers, any account could create a record whose id was a
canonical project name and so become owner of that canonical project. Such a record is
served only when something shows a superuser stood behind it: the register route wrote it,
a superuser confirmed it, or its creator is a superuser today. Otherwise it is
**unconfirmed**: listed `usable: false` with `needs_confirmation: true`, and refused like
a `proj_...` record until a superuser decides. After every upgrade to this kit or later:

1. On the host, list the records the service will not serve. This reads the state file
   only, so it is safe beside a running service:
   `python3 http_service.py --state <STATE> --list-unconfirmed-projects`
   A superuser sees the same list in the web interface (Projects, "Projects to confirm
   or archive") and at `GET /v1/projects/unconfirmed`. Each entry names who created the
   record and every current member with their role. An empty list means nothing to do.
2. For each `unconfirmed` entry, a superuser either:
   - **confirms** it (`POST /v1/projects/{id}/confirm`, or Confirm on the page, which
     shows the creator and the members first). The canonical project must exist on the
     host. The members keep their access, so review them; the confirmation is recorded
     on the record (`confirmed_by`, `confirmed_at`) and in the audit log
     (`projects.confirm`), and it backfills `registered_by` when the record has no such
     mark, so the mapping stays superuser-backed if the confirming superuser is later
     demoted; or
   - **archives** it (`POST /v1/projects/{id}/archive`).
3. Archive each `no-canonical` entry (a `proj_...` id).

**The backfill's limit (a known exposure, not closed).** A confirmation backfills
`registered_by` only for a record that has no such mark, and a record can only be
confirmed while the backend will not serve it. A record whose creator is still a
superuser is *usable*, so the confirm route refuses it with 409 "This project needs no
confirmation" and the backfill **cannot** be applied yet. Demote that creator first (or
wait until nothing else makes the mapping superuser-backed) and the read turns unusable,
which is exactly when confirm works and stamps `registered_by` beside `confirmed_by`.
Until then such a record stays usable only for as long as someone is a superuser behind
it: demoting the creator without confirming first leaves it unusable, and the confirmation
is then available. There is deliberately no route that stamps the mark on a record the
service is already serving.

**What an unusable record still allows.** While a record is unconfirmed or has no
canonical project:
- Reads of the record, its members and its credentials work. Its task, review and queue
  routes answer 409 before any endpoint call. Usability is judged live on every read, not
  from the short read cache, so a record that becomes unusable (for example its creator is
  demoted) stops being served at once.
- Nothing that grants or extends access is accepted (409): adding a member or changing a
  role, issuing a worker credential, a new agent grant, or a new credential for an agent
  that already holds a grant on the record.
- Everything that removes access works: removing a member, revoking a worker credential,
  revoking an agent's grant, archiving. Cleaning up a suspicious record never requires
  confirming it first.
- The refusal is given only to a caller who may touch the thing being changed. On the agent
  routes an account that may not administer the agent gets the ordinary 404 `Agent not
  found` (and `Project not found` for a project its owner cannot see), never the
  unusable-record sentence, so the refusal cannot be used to probe for agent ids, record
  ids or their owners' memberships.
- **An archived unusable record** is a special case of the refusal: it cannot be confirmed
  (the confirm route refuses an archived record), so the 409 says so and names the route
  that works — removing the access being added (an agent grant, a membership, a
  credential). An agent that still holds a grant on an archived record therefore gets a new
  credential as soon as its owner removes that grant, instead of being told to confirm or
  archive a record that is already archived.

With `--backend inprocess` (disposable local validation) everything is service-local, so
"New project" still creates the project directly (`project_create: create`), and no record
is ever unusable or listed for confirmation.

### Setting a project up

Registering a project makes it exist; it does not make it ready. The web interface has
a page for what is left, **Set up** in the project's menu (`#/p/NAME/setup`), which an
owner is taken to straight after registering and which stays reachable from the project
(a line on the project page says how many steps are left while any is). It is for the
project's owners and for superusers; anyone else gets `403`. Through the API it is
`GET /v1/projects/{id}/setup`.

Seven steps. Each has a state (`done`, `todo`, `optional`, `unknown`, `unavailable`,
`not-applicable`), who can do it, and either where it is done in the web interface or
the exact command for the server. `remaining` counts the `todo` ones.

| Step | Read from | Who does it |
| --- | --- | --- |
| Members added | the project's members; `optional` while the caller is the only one | an owner (a superuser to add another owner) |
| Repository location recorded | the project's `repository` field | an owner, on the setup page |
| A first task defined | the task list (the project's merge slot row is not a task) | any member who may write tasks |
| A personal agent granted the project | the enabled agents granted this project | each member, for their own agent |
| Guidance set | the host | an operator: `admin.py set-guidance NAME --actor OPERATOR --file FILE` |
| Onboarding set | the host | an operator: `admin.py set-onboarding NAME --file FILE` |
| Covered by a scheduled backup | the host | an operator, with the `ExecStart` line shown |

- **Who can do it holds for one person and for three.** Each step names a role, not a
  person. On an installation where one person is operator, superuser and owner, they do
  every step; where those are three people, the page tells the owner which steps are
  theirs and gives them the command to send to the operator for the rest.
- **The page runs nothing on the server.** The host steps show a command to copy; the
  web interface does not run it. One host step can also be done on the page: an owner
  may write the project's onboarding text there (see "Onboarding text from the setup
  page" below). Guidance is not settable from the web interface.
- **A step the server could not check is counted separately.** `remaining` counts the
  steps that are to do; `unchecked` counts the ones whose state could not be read. The
  page says "1 step could not be checked" and never "nothing is left" over such a step.
- **The host steps come through one read-only endpoint action, `setup-status`**, so the
  web service still reaches the host only through `endpoint.py`. It returns states with
  a version or a time and never guidance or onboarding text: guidance `set`, `not-set`,
  `unbound` or `unreadable` (the last two read as "to do", with the reason); onboarding
  `set` or `not-set`; the backup schedule `covered`, `not-covered` or `unknown` (from
  the installed `beads-*backup*.service` unit files; drop-ins are not inspected; with
  `unknown` a `reason` of `no-account-home` or `unreadable`), the
  line that covers every project, and how the last backup run recorded the project.
  `endpoint.py` answers the action for any actor it accepts; `client.py` has no command
  for it.
- **Where the backup units are looked for.** The supervised service runs with `HOME`
  pointed into the runtime (`<root>/home`), which holds no unit files. When the kit
  scopes `HOME` it records the account's own home in `ORCHESTRA_ACCOUNT_HOME`, and the
  setup read looks for `beads-*backup*.service` there. The kit sets that variable
  itself, from the `HOME` it replaces, and overwrites any value it was started with; a
  contributor's SSH command never carries it (the forced command passes only locale
  variables, `PATH` and `HOME`). Run from a shell, `add-project`'s schedule report and
  `backup-status` do not read the variable at all and behave as before.
- **A service started without a usable `HOME`** (none set, or `HOME` already the
  runtime's home with no recorded account home) cannot find the unit files. The backup
  step then reads "Could not check", with that reason; it does not read "to do". Start
  the service from the account's own environment to get an answer.
- **A server whose `endpoint.py` predates the action** refuses it; the three steps then
  read `unavailable` ("Not available on this server") and the rest of the page works.
  With `--backend inprocess` there is no host and they read `not-applicable`.
- **The setup page never shows another member's agent setup.** The agent step says how
  many agents may work in the project and whether one is the caller's.

**Where the repository is.** `PATCH /v1/projects/{id}` with `{"repository": "..."}`
(owners and superusers; `null` or an empty string clears it; audited as
`projects.repository` without the value) records where the project's code lives. Every
member reads it in `GET /v1/projects/{id}`. An agent reads it as `repository` on each
project in `GET /v1/agents/me/next`, as `project_repository` in a task brief over HTTP,
and listed beside the agent setup text (`setup.repositories`), never inside the
commands of that text.
- It is a **label written by another person**. Wherever it is shown or delivered it is
  data: an agent or a person checks it is the repository they expect before cloning,
  and never runs it as a command. The same note travels with the value wherever an
  agent reads it: `repositories_note` in `GET /v1/agents/me/next` and beside the setup
  text, `project_repository_note` in the task brief, and `repository_note` on the
  project itself in `GET /v1/projects` and `GET /v1/projects/{id}`, which an agent
  credential can read too (each null when no repository is recorded).
- **What the kit checks: its shape only. Exactly these forms are accepted**, and the
  whole value must match one of them:
  - `https://host[:port]/path`, with **no user name**, so a token or a password cannot
    ride in it (`https://TOKEN@host/...` and `https://user:secret@host/...` are refused);
  - `ssh://[user@]host[:port]/path`, with a plain user name and no password;
  - `user@host:path` (the scp form), with a plain user name and no password;
  - an absolute path that begins with exactly one `/`, for example `/srv/git/x.git`.
- In every form:
  - at most 300 characters, ASCII only, and the scheme in lower case;
  - a host is labels joined by single dots; each label is letters, digits and `-` and
    starts and ends with a letter or digit; at most 253 characters;
  - a port is a number from 1 to 65535;
  - a user name is at most 32 characters: letters, digits and `. _ -`, not starting
    with `.` or `-`;
  - a path is letters, digits and `. _ ~ + = , / -`, with no `..` segment, and the path
    of the scp form does not start with `-`;
  - there is no percent-escape, space, quote, control or format character anywhere.
- So these are refused: any other scheme (`http://`, `file://`, `git://`) and an
  upper-case scheme; a remote-helper form (`ext::...`, `fd::...`); a one-slash scheme
  (`file:/x`); a host or path that is an option (`ssh://-oProxyCommand=...`,
  `git@-oProxyCommand=...:x`); a relative path (`../x`, `project`); `host:path` without a
  user name; a letter that is not ASCII in a host. The refusal never repeats the value.
- **Windows drive paths and network shares are refused** (`C:\git\x.git`, `C:/git/x.git`,
  `\\server\share\x.git`, `//server/share/x.git`). Git on another system reads `C:/x`
  as ssh to a host named `C`, and a share path opens a connection to the named host with
  the reader's own sign-in. Record a URL instead.
- **The kit does not judge where a host points.** `localhost`, an IP address and a
  link-local address are hosts like any other.
- **A token cannot be told from a name.** A user name or a path segment may be an
  access token and the rule cannot know. When one begins like a well-known token
  (`ghp_`, `github_pat_`, `glpat-`, `xoxb-`, `sk-`, `AKIA` and a few more) the value is
  still accepted, and the project carries `repository_warning`, which the setup page
  shows to the owner in red: every member and agent can read the value, so replace it
  and revoke the token. The list of beginnings is short and does not claim to be complete.
- **A value stored under an earlier rule is checked when it is read.** Nothing is
  rewritten. A stored value that no longer passes is not shown as the repository and is
  not delivered to agents: `repository` reads null, `repository_needs_attention` is true,
  and the setup step reads "to do" and says the recorded location no longer fits. An
  owner records it again.
- **What the kit does not check:** that the repository exists, that anyone can reach
  it, or that an agent's clone points at it.
- **The SSH brief does not carry it.** The field lives in this service's `--state`
  document, not in the canonical project, so `brief TASK` over SSH has no such field.
- It is part of the `--state` document, so the backup of that document in section 8
  covers it. A kit before this one reads a state file that has the field: its project
  routes return the record as it is stored, so the field still appears in them, and
  that kit has no route that changes it and no page that shows it.

### Creating a project from the web interface

An operator creates a project on the host (`admin.py add-project NAME`) and a superuser
registers it. A named account can also be allowed to do both in one step.

**The grant.** A superuser allows an account to create projects, with a limit on how
many it may have at one time: `PUT /v1/accounts/{id}/project-grant` with
`{"limit": N}` (1 to 100; 5 when left out), `DELETE` to take it away. Session authority
only. Each change is audited as `accounts.project-grant` with the superuser, the account
and the limit. The People page has a panel "Who may create projects".
- A superuser may always create, without a limit.
- **An agent or worker credential never may**, whoever owns it: the capability is
  refused for every credential.
- **The limit counts** the projects that account created that are not archived, plus any
  name it holds on the host through a creation that has not finished. Handing a project
  to another owner does not free a place. Archiving it does.
- **The server has a limit of its own**, set by an operator (20 unless changed): the
  number of project databases it holds. It counts every database, also those of archived
  and retired projects and of creations that did not finish, because none is ever
  dropped. So an account that creates, archives and creates again stays within its own
  limit and still fills the server. At the server's limit a creation is refused with
  "This server is at its limit of projects, so no new one can be created. Ask an operator
  of the server to raise the limit or to make room." The answer gives no numbers: they
  would say how many projects other people have. A superuser sees the numbers on the
  Projects page, in `GET /v1/project-creations` (`server`) and on a project's setup page.
  Why there is a limit: see OPERATIONS.md, "The cost of many projects on one server".
- **What the page is told matches what the endpoint will answer.** `project_host_create`
  in `GET /v1/sessions/current` counts the names the account holds on the host (`held`),
  and its `reason` is `limit` (the account's own) or `server-limit`.

**Creating.** `POST /v1/projects` with `{"project_id": NAME, "name": "...", "create": true}`.
Without `"create": true` the route registers an existing project, as before.
- The name follows the host rule: 2 to 24 lowercase letters or digits, starting with a
  letter.
- The service asks the host through one endpoint action, `create-project`, which runs
  the same code as `add-project`: database, settings, backup target, merge slot and a
  first backup.
- **It takes from a few seconds to a few minutes**: about six seconds on a server with
  no other project and about one second more for each project database the server
  already holds (measured; see OPERATIONS.md). The action has its own timeout,
  `--create-timeout` (900 seconds), separate from `--endpoint-timeout`.
- **Nothing else waits for it.** The creation is done in three steps: the request is
  reserved under the service's authority lock (the grant, both limits and the name are
  checked, and the name is held from that moment), the project is initialized with that
  lock released, and the result is confirmed under the lock again. While a project is
  being created every other request is served as usual: reads, task writes in any
  project, grants, sign-ins.
- **One creation at a time.** A second creation while one runs answers 503 `busy` at
  once, with `Retry-After: 60` and "Another project is being created on this server. Try
  again in a minute." Nothing was done; the same request can be sent again.
- **If the grant is revoked or the account disabled while the project is being made**,
  the request is refused (403) and the project stays on the host, complete and not
  registered. A superuser sees it on the Projects page as "Made, not registered" and
  either registers it (New project, with that name) or has an operator retire it. The
  same holds if the server is over its limit when the work ends.
- **The project appears in the web interface only after the host reports it created**,
  first backup included. The creator is its only member, as owner, and is sent to the
  setup page. Only a superuser adds a second owner.
- The audit record `projects.host-create` names the account, the limit of its grant and
  the count the creation brings it to.
- **Refusals.** An account without the grant gets the same 403 as on the register route,
  whatever name it sends, so the answer cannot be used to learn which names exist. An
  account with the grant may learn that a name is not available: one sentence, the same
  for a name that is taken, held by another creation, or retired. A refusal leaves
  nothing behind: no directory, no database, no record.
- **Where the backup schedule names projects one by one**, the new project is not
  covered until an operator adds it. The setup page says so and shows the line.

**Who can call the host action.** `create-project` (and the list and onboarding actions
below) is accepted by `endpoint.py` only when the process was started with the web
service's authority arguments and the request carries the live-authority descriptor of a
signed-in account. The endpoint then checks the grant and the limit itself, against the
live authority store and under its lock, and the limit once more under the host's own
creation lock.
- **Where contributor keys are confined to the forced command** (`ssh_forced_command.py`),
  an SSH caller cannot reach the action: the forced command drops the authority
  arguments, and the endpoint refuses the action without them.
- **Where a key is not confined**, its holder has a shell as the service account and can
  already run `admin.py add-project`. The action gives such a caller nothing new, and
  nothing here stops them.

**When a creation stops half way.** The host keeps one record per creation in
`<root>/project-creations/NAME.json`: the intent is written before the first write and
the result after the last.
- **Nothing was made yet** (no directory, or an empty one): the request answers 409
  "The project could not be created and nothing was made. Try again; if it fails again,
  ask an operator of the server." The same request can be sent again. The cause is not
  in the answer (it may name paths and commands of the host); the operator finds it in
  `<root>/project-creations/last-failure.txt` (the newest failure, the step it happened
  at, and the four before it under `earlier`).
- **No answer to a person carries host text** (kittrial-5bb.143). A creation answers a
  refusal with one of its own sentences and nothing else, whatever went wrong on the
  host: a record that cannot be read, a directory that cannot be written, a journal
  that is not a database. The sentence is chosen by what is on the host, not from the
  error:
  - a creation record of that name that cannot be read: "Project name NAME is not
    available: choose another name". The name is held until an operator has set the
    record aside (below). No request can adopt or resume such a record, whatever it
    claims.
  - the project was made and the last step failed (the operation journal
    `project-creations/operations.sqlite3` could not be used): "Project NAME was made
    on the server, but it could not be registered in the web interface. Ask an operator
    of the server to look at it. When that is repaired, create it again with the same
    name: nothing is made twice, it is only registered. Do not create it under another
    name." It is listed for a superuser as made and not registered. After the repair
    the same person sends the creation again and it is registered.
  - the project was made and the last step could not get its lock in time: 503 `busy`
    with `Retry-After`, "Project NAME was made on the server; registering it had to
    wait for a lock. Send the same request again in a moment: nothing is made twice."
    (not the endpoint's general "Nothing was done", which would not be true here).
    Sent again, the request registers it.
  - The web service applies the same rule on its side: it puts the endpoint's line in
    its 409 only when the line is, whole, one of those sentences. Anything else
    becomes "The project could not be created, or was only partly made. Ask an
    operator of the server to look before you try again.", and the line goes to the
    service's log (`create-project NAME answered a line that is not a creation
    sentence: ...`). This also covers an endpoint older than the rule.
- **A refused follow-on base arrives whole** (kittrial-5bb.158). A canonical refusal's last
  line is handed on up to 200 characters (6000 for a checkpoint, which lists every
  problem). The follow-on base refusal of the review action is longer, and cut at 200 it
  lost its reason, the recorder's name and what an operator must do. When the line is
  exactly that sentence (`review_workflow.BASE_REFUSAL`: the kit's own words, hexadecimal
  commit ids, a recorder name of a constrained shape) it is handed on whole, up to 1500
  characters, and it is the `message` of the 422 as well as the `detail`. No other line
  gets the larger limit, so a caller's own text is never echoed back at that length.
- **What a caller without the web service reads** (kittrial-5bb.149, .158). Over SSH the
  endpoint's own lines arrive as they are: its busy line names the lock file it waited
  for, and its refusal when `deployment.private.json` cannot be read names that file.
  That caller is somebody who was given the endpoint. The web service does not pass the
  busy line on (below). A configuration file that cannot be read is answered 503 with no
  path from the release that contains kittrial-5bb.156; before it, a member's request is
  answered 422 with the endpoint's line.
- **Busy, on every route** (kittrial-5bb.149). When the endpoint could not get a lock in
  time it answers return code 75 with a line that names the lock file. The service does
  not pass that line on. A read answers 503 `busy` with `Retry-After` and the service's
  own sentence, "The server is busy and this request was not completed. Send it again in
  a moment."; the endpoint's line, which says which lock, is in the service's log
  (`busy: the endpoint answered return code 75 for ACTION: ...`). A write that the
  service holds an operation identity for keeps its more careful answer, 503
  `uncertain` ("reconcile with the same idempotency key"), which names no path either.
  A creation passes on only its own two busy sentences (another creation is running;
  the project is made and registering it had to wait).
- **A write the service cannot answer for: "cannot say"** (kittrial-5bb.149, .156). The
  service saves its own state under a lock it shares with the endpoint. When a wait for
  that lock runs out after a route began, a write is answered 503 `uncertain`, with no
  `Retry-After`: the request may have been carried out (a creation was made and
  registered, and only the save could not be done). Which sentence depends on the
  request:
  - with an `Idempotency-Key`: "The server was busy and cannot say whether this request
    was carried out. Look before you repeat it, or send it again with the same
    idempotency key." Sending it again with the same key is safe: it is answered with
    what happened.
  - without one: "... Look before you repeat it: sent again without an idempotency key,
    it may be carried out twice." A task create repeated blindly is a second task. The
    web interface always sends a key.
  - the same rule for the other uncertain answer of a write ("The operation may have
    committed; reconcile with the same idempotency key" or, without a key, "The
    operation may have committed. Look before you repeat it: ...").
  - a log-in (`POST /v1/sessions`) is answered 503 `busy` instead ("not completed. Send
    it again in a moment."): a session that was made and not answered is one nobody
    holds. So is any write whose wait ran out before its route began.
- **A refusal is answered as it is while the lock is held** (kittrial-5bb.156). The audit
  entry of a refused write is saved without a long wait (5 seconds the first time, then
  no wait at all until a save succeeds). When it cannot be saved it stays in the
  service's memory and is written with the next save, and the log says `busy: the audit
  entry of a refusal was not saved ...`. So "Project NAME was made on the server;
  registering it had to wait for a lock" reaches the creator also when the lock stays
  held longer than the service's own wait.
- **A creator who asks again for a project they already have** (kittrial-5bb.156). The
  web record of a host-created project keeps a digest of the request that registered it
  (`host_created.operation`). The same request sent again, with the same idempotency
  key, is sent to the endpoint, whose journal answers what happened, and is answered 201
  with the project: this is what follows a "cannot say". Any other creation request for
  that name from its creator (a new key, or none) is answered 409 "You already have
  project NAME: you created it on this server on DATE. Nothing was made again." with
  `detail` `{"project": NAME, "state": "yours"}`; the page shows it as a notice with a
  link, not as an error on the name. The date is the web record's own. Only the account
  the project is registered to by its own creation, and that is still a member of it,
  gets that sentence. Every other account gets "Project name NAME is not available:
  choose another name", with nothing about who has the name or since when. A record
  written by an earlier kit has no digest, so its creator's repeat with the old key is
  answered with the 409 sentence, not 201. Three more cases:
  - The same request from **another session** of the same account (logged in again; the
    page keeps its key). The endpoint's operation identity belongs to the session that
    made the project, so it will not answer from its journal; the service answers the
    409 "You already have project NAME ..." (not "could not be created").
  - A project an operator has since **retired on the host** (`admin.py retire-project`)
    keeps its web record. Its creator is told 409 "You created project NAME on this
    server on DATE, and it has since been retired there, so it is no longer served. The
    name is not available: choose another name.", `detail` `{"project": NAME, "state":
    "retired"}`, shown on the name like any other refusal, with no link. The service
    reads that from the root itself (no endpoint process, no lock).
  - The digest is the service's own. It is not part of the project view any reader gets.
- **The last-use stamp of a request does not wait a minute** (kittrial-5bb.156). Every
  authenticated request stamps the session's (or credential's, and agent's) last use
  and saves the state, because the endpoint checks a session's idle deadline against
  the file. That save waits 5 seconds for the lock the first time; when the lock cannot
  be had the stamp stays in memory, the request goes on, and until a save succeeds every
  further request tries once without waiting (log: `busy: a last-use stamp was not
  saved ...`, once, then `The state is saved again ...`). What follows from a stamp that
  is only in memory, both ways:
  - If the service stops before the next save, the last use is lost and the session
    reads as idle sooner after the restart. That is the safe side. Nothing lets a
    session live past its deadline: the service decides from its own memory first, where
    a deadline that has passed refuses the request whatever is unsaved.
  - A live session is not refused as idle by the endpoint because of it. A read does not
    carry the session to the endpoint. A write that does is not sent until the state is
    saved: the service saves first (waiting up to the full minute) and, when the lock
    still cannot be had, answers 503 `busy`, "not completed", having sent nothing (log:
    `busy: the state could not be saved before ACTION was sent to the endpoint, so it was
    not sent`). So the longest a stamp stays unsaved is until the next request after the
    lock is free, and never past a write that reaches the endpoint.
  What this does NOT change, while the lock is held: only a read the service answers by
  itself is answered at once. A read that needs the endpoint still waits for the
  endpoint's own wait (up to 60 seconds) and is then answered 503 `busy`, as before.
  And a request that arrives while a write or a log-in is itself waiting for the lock
  queues behind it in the service (a read was seen to wait 54 seconds that way).
- **The server's configuration file cannot be read** (kittrial-5bb.156). Every failure
  to read `deployment.private.json` is one fault of the server: the file is not there,
  is not a regular file (a directory, a dangling link, a FIFO: refused at once, the
  endpoint does not wait on a FIFO), cannot be opened (closed to the service's user,
  for example), is not text, is not JSON (cut
  short), is not a JSON object, has no `password`, or has an `operators` or `verifiers`
  that is not a list. The endpoint's line names the file with the parser's or the
  system's words; the endpoint marks that answer (`"fault": "configuration"`), and the
  service answers 503 `server_configuration`, "The server's configuration cannot be
  read, so this request was not carried out. Ask an operator of the server to look.",
  on every route, for reads and writes alike. The line is in the service's log only
  (`configuration: the endpoint could not read the deployment configuration for
  ACTION: ...`). What is and is not stopped:
  - The file is read **only where it is used**. A request that needs nothing from it is
    answered as if it were whole: reading, setting and clearing a project's onboarding
    text, for example. Everything that starts bd needs it (the database password is in
    it), so task reads and every write are refused while it is damaged. Creating a
    project is refused with the same answer (not "could not be created; try again":
    trying again does not help).
  - A write that reserves an operation identity reads the file before it reserves, so
    it is refused with nothing done and its idempotency key stays usable: the same
    request works once the file is repaired. (Read for the first time inside the guarded
    write, it left the operation "outcome unknown" with nothing written, and the same
    key answered that until the reservation expired.)
  - Any `admin.py` command on the host names the file, as before. A caller who reaches
    the endpoint without the web service still reads the endpoint's own line.
- **Restart the web service in the same step as the files** (kittrial-5bb.156). The
  service is a long-running process and the endpoint is started anew for every request,
  so replacing the kit's files changes the endpoint at once and the service only when it
  is restarted. A service of the previous kit left running over the new endpoint applies
  the old rules to the new answers: it was seen to register, for a superuser, a
  half-made project whose creation record is damaged (201), which the new service
  refuses (409). Replace the files and restart the service together.
- **A registered project does not depend on its creation record** (kittrial-5bb.149).
  The endpoint serves a project that is initialized and whose creation record is
  absent, finished or damaged; it refuses one whose record reads running, incomplete or
  stalled, and one that was never initialized whatever became of its record. So the
  members of a working project keep it when that file is damaged. The damaged record
  still holds the name against a creation, still counts toward the server's limit and
  still blocks registration: the register route asks the host (`setup-status`,
  `creation_record`) and answers 409 "The creation record of project NAME is damaged;
  an operator must look at it first". Both lists flag such a project: "projects/NAME is
  initialized and is SERVED WITH A DAMAGED CREATION RECORD", with the command to set the
  record aside. The cost: an unfinished, unregistered creation whose record is then
  damaged is reachable by a caller who reaches the endpoint without the web service,
  as a project an operator made is.
- **A file in `project-creations/` whose name no project can have** (`UPPER.json`,
  `a.json`) is not a creation: it holds no name, is not counted, and both lists show it
  as `not-a-record` with one instruction, to move it out of that directory.
  `remove-creation` takes project names only.
- **A creation record that cannot be read** (not JSON, the wrong shape, another
  project's, a directory, a symlink, or a file the service account cannot open, as
  after a restore under another user) reads `damaged` in both lists. It holds its name, and it counts toward the
  server's limit of project databases, because it may stand for one. A creation of
  another name is not held up by it. The operator looks at the file and then runs
  `admin.py remove-creation NAME --actor OPERATOR --reason REASON`: for a damaged
  record that command **only sets the record aside**, as
  `project-creations/NAME.json.damaged-<UTC stamp>`, and touches nothing else, because
  what the record stood for is not known. If nothing is under `projects/NAME` the name
  is then free. If something is, it is then a project with no creation record: a
  superuser registers it if it is complete, or the operator retires it
  (`admin.py retire-project NAME`).
- **Something was made** (the database exists; a later step stopped): the request
  answers 409 with a sentence that names the project and says an operator must finish
  or remove it. Nothing is registered in the web interface, so no member, list or agent
  sees the project. The name is held and counts toward the creator's limit. **The kit
  never removes it**: that would mean dropping a database from a web request.
- **The process was killed**: the request that was running answers 503 "outcome unknown".
  The record still says what was started. Sent again, with the same idempotency key or a
  new one, the request is answered from that record: it resumes when nothing was made, and otherwise answers 409 with the sentence
  that an operator must finish or remove it. What the record reads as afterwards:
  - `incomplete`, when something was made;
  - `stalled`, when nothing was made (the kill came before the first write). It holds
    the name and a place. The same request resumes it; `remove-creation` clears it and
    frees the name, because no database exists for it.
  - A creation killed **during** `bd init` is `incomplete` and cannot be finished: the
    database may exist half made. `remove-creation` retires it and **the name is lost
    for good**, as for any retired project. The messages say so.
- **A creation that is running reads `running`**, not incomplete, and has no command:
  wait for it.
- **Nothing is served from a creation that has not finished**, and it cannot be
  registered. The endpoint answers every request for it "Unknown/uninitialized project:
  ... is a creation that has not finished", so the plain register route
  (`POST /v1/projects` without `create`) refuses it too, with 409 and that sentence. It
  has no backup target, merge slot or first backup yet.
- **A superuser sees every such creation** on the Projects page ("Projects on the
  server") and in `GET /v1/project-creations`: `items` (running, incomplete, stalled or
  damaged, each with who started it, the step it stopped at and the command for that
  reading), `unregistered` (made and not registered) and `server` (`used`, `limit`).
- **To finish it**, an operator runs `admin.py finish-project NAME`. It works when the
  project was initialized (`projects/NAME/.beads/metadata.json` exists): the settings,
  the backup target, the merge slot and a backup are each done or done again. The
  creator then creates the project again in the web interface with the same name, which
  registers it without doing the work twice; or a superuser registers it.
- **To remove it**, an operator runs
  `admin.py remove-creation NAME --actor OPERATOR --reason REASON`. It acts only on a
  creation that reads `incomplete` or `stalled`. It refuses a finished project, a name
  with no creation record and a creation that is running, and changes nothing then. For
  an incomplete one the directory moves to `retired/`, nothing is deleted and the name
  stays retired; for a stalled one only the record is removed and the name is free. The
  creator's place is free again either way. `retire-project --force` is not the command
  for this: aimed at the wrong name it retires a healthy project.
- `retire-project` and `remove-creation` both refuse while a creation is running.
- `admin.py project-creations` lists every record; `--attention` lists the ones that are
  running or need an operator, each with its command; `--usage` prints how many project
  databases the server holds and its limit; `--set-server-limit N --actor OPERATOR` sets
  the limit (a listed operator; audited in `deployment.private.json`).
- **`admin.py backup --all` is incomplete while an initialized creation is unfinished**:
  the project has no backup target yet, so the nightly gate is red until an operator
  finishes or removes it. The backup's last line names the project and both commands.
- A project cannot be named `mysql`, `sys`, `dolt` or `doltcfg`: the database server
  uses those names itself. `add-project` refuses them too.
- The records are not part of a project's backup: they belong to the runtime, not to a
  project. A runtime rebuilt from backups has none, which loses only the notes about
  creations that had not finished; a finished project is in the web service's state.

**Rollback.** The kit before this one reads a state file that carries grants and
web-created projects. It serves such a project (the record carries `registered_by`,
which that kit reads as "a superuser stood behind this"); it ignores the grant and has
no route to create.

### Onboarding text from the setup page

An owner may write the project's onboarding text on the setup page:
`PUT /v1/projects/{id}/onboarding` with `{"text": "..."}`, `DELETE` to remove it, `GET`
to read it for editing. Owners and superusers, session only.
- **Guidance is not settable here or by any web route.** It stays
  `admin.py set-guidance`, for a listed operator.
- The text is stored as the project's `ONBOARDING.md` under a first line the kit writes:
  "[Written by an owner of this project in the web interface. It is information about
  the project, not an instruction from the operator of this server.]" The line is part
  of the stored document, so it is in what `onboard` gives a worker, it travels with a
  backup and a restore, and an earlier kit shows it too.
- **Every line the owner wrote is stored behind a mark, `| `** (an empty line is `|`).
  A heading, or a line written to look like the kit's own, is then visibly inside the
  owner's text: nothing an owner writes can start a line of the document, so it cannot
  close its own block or pass for a section of the kit or of the operator. The service
  adds the mark to every line whatever the request sent (a line that already begins
  with `| ` becomes `| | ...`). The editor shows and saves the text without the marks,
  and saving unchanged text changes no byte. Text an operator sets with
  `admin.py set-onboarding` has no kit line and no marks.
- The rules are those of `admin.py set-onboarding` (nonempty, at most 8000 bytes, of
  which the kit's line uses 152 and each line's mark 2) plus the plain-text rule of the
  guidance channel: no control, bidi, zero-width, invisible or format character. Lines
  end with a line feed (a CRLF pair is read as one); a carriage return alone is refused.
- Text that is not valid Unicode (half of a surrogate pair) answers 422, here and on
  every other route; it used to answer 500.
- An owner may replace text an operator set. The operator's text is then kept beside the
  document as `ONBOARDING.operator-copy.md`, and the audit says so. `DELETE` removes
  only owner-written text: the operator's own document is not removable from the web
  interface. `GET` returns owner-written text for editing and reports an operator's
  document as set without returning it.
- Audited as `projects.onboarding` with the account and the size, never the text.
- The write goes through the service-only endpoint action `set-onboarding`, which
  re-checks project administration under the authority lock.

### Requirement proposals

A proposal says what the product should do. It is intake, not a task and not a
requirement ([CLI contract](CLI_CONTRACT.md#proposal-contributed-requirement-proposals)).
Over HTTP identity is server-bound, so what is a host command over SSH is a route here.
The routes exist on the endpoint backend; the in-process backend answers 501.

| Route | Who | What |
| --- | --- | --- |
| `POST /v1/projects/{id}/proposals` | `proposals.write`: contributors, owners, and an agent credential with the `proposals` scope | submit; with `key` in the body, revise your own proposal. Send an `Idempotency-Key` (or an `operation_id`): the proposal key derives from it |
| `GET /v1/projects/{id}/proposals` | any member | the queue: `state`, `target`, `order` (`oldest`, the default, or `newest`), `mine=1` (only the caller's own **verified** proposals, the rule of `/v1/me/contributions`: one that merely names the account is not counted; the answer then carries `unverified_omitted`, the number of proposals that name the caller's account as submitter but are not verified, such as one submitted over SSH under that name, so the caller can see that some exist without being shown them), `limit`, `cursor`. `total` is the count for the filter, not for the page. A cursor this route issued stays valid when the list shrinks under it: a cursor that now points past the end, including one issued before the total shrank, returns 200 with `items: []` and `next_cursor: null`, not a refusal (a cursor the route did not issue, or one used with a different query or account, is still a 409). Also `can_propose` and `can_triage` for the caller |
| `GET /v1/projects/{id}/proposals/{key}` | any member | the newest revision, the disposition timeline (`history` 1..50), the derived links, and `deciders` (the configured owner deciders) |
| `POST /v1/projects/{id}/proposals/{key}/dispositions` | `reviews.approve`: owners, in a signed-in session | triage (`operation: "review"`, the default) or the owner decision (`"decide"`), with `previous` and `proposal_sha256` from the detail read |
| `GET /v1/me/contributions` | a signed-in session | your own **verified** proposals across your projects, newest first, `limit` per page with a `cursor` (`next_cursor`); it pages through the newest 100 and stops there: the last page has `truncated: true` and no `next_cursor`, and older proposals are read per project from the queue. A cursor the route did not issue is refused with 409. A proposal that only names your account, or whose later revision someone else wrote, is not listed |

The rules:
- **The submitter is the account.** `submitter` is `account:<your user id>`; for an agent
  it is the agent's owner, and `submitted_by_agent` names the agent. A body that carries
  `submitter`, `submitted_by_agent`, `actor` or `origin` is refused. A worker credential
  cannot propose.
- **Only the submitter revises, and `verified` covers every revision.** A proposal
  with an `account:` submitter is written, revised and read as verified only through
  this service (the account or its agent). No SSH actor writes as an account, even one
  the actor map maps to that account. A `person:` submitter is revised over the plain
  endpoint by an actor the map resolves to that person. Repeating the stored
  `submitter` string in the payload proves nothing. A reader reports `identity: verified` only when the native author of
  every revision stands for the submitter; otherwise the proposal reads `unverified`
  and an `identity-broken` warning names the first revision someone else wrote.
- **Query values are validated, never forwarded as flags.** `state` is one of the
  proposal states and `target` a requirement key or area; anything else is 422. What
  is guaranteed, exactly:
  - enumerated values (`state`, `status`, `due`, ...) are checked against their closed
    set, and patterned values (keys, ids, `target`, `owner`, tags, numbers) against
    their pattern, before the endpoint is called;
  - a task title cannot start with `-` or `@`, on create or on update (tasks that
    already have such a title stay readable);
  - free text (a task description on create and on update, proposal, checkpoint and
    review bodies) travels as an attachment, never as an argument, so a text such as
    `--help`, `- item` or `@attachment:0` is stored as written. The one exception is
    clearing a description on update: the empty value is sent inline, because the
    native tool refuses an empty body file and an empty value cannot be a flag.
- **Who reads coordinator text.** A rejection reason, a coordinator question and an
  escalation question are returned only to the submitter and to members with
  `reviews.approve`. Everyone else gets `null` and `withheld: true`.
- **Nobody triages their own proposal**, and the owner decision comes from a different
  member than the one who escalated. An owner decision names an existing native decision
  issue (`decision: {decision_id}`). Today's behaviour, stated exactly: the check is only
  that the id names a native issue of type `decision` (or labelled `decision`). Any
  contributor can create such an issue through the endpoint, and a web decide accepts
  it; the owner the escalation named (`owner_identity`) is **not** enforced against the
  member who decides. The web service has no route that creates a decision issue.
  Tightening this rule is kittrial-5bb.87.
- **An operator who also has a web account** must map their operator actor to their
  account identity, or the no-self rules treat the two as different people: the same
  person could submit on the web and triage with the host command.
  `admin.py proposal-settings PROJECT --actor OPERATOR --namespace OPERATOR --to account:usr_<id>`.
  A namespace that itself has the shape of an account or agent id is refused.
  The mapping feeds the no-self rules and nothing else: it does not let that actor (or
  any SSH caller who declares a name in that namespace) revise the account's proposals
  or submit proposals that read as the account's.
- **No credential triages.** `reviews.approve` is never granted to a worker or agent
  credential.
- **Refusals** from the canonical rules (a stale read, an illegal transition, a no-self
  rule) answer 422 with the rule's message. Proposal text is never in an error body or
  the audit log; the audit records the action and the proposal key.
- **Authority is checked when a disposition is written.** The endpoint re-validates the
  member's capability against the live authority store before the write. A reader later
  counts that disposition because its author is an HTTP account; it does not re-check
  the member's current role. So removing a member's owner role does not undo what they
  triaged. An operator repairs a bad disposition with a void once `void-record` accepts
  these records (kittrial-5bb.74); no repair command exists before that.
- **The actor-shape reservation and its boundary.** Readers, SSH included, treat a
  record authored under an HTTP account or agent id as written by this service. The
  endpoint therefore refuses those shapes as a declared actor on every action unless
  this service launched it, which it knows from its own `--authority-store`
  command-line flag. That is airtight only for SSH callers confined to the endpoint
  command (an `authorized_keys` `command=` entry that ignores the caller's command
  line). A caller with a shell on the service account is inside the trust boundary and
  can bypass it, like every other check in the kit.
  - Launched by this service, the endpoint still requires the verified live-authority
    descriptor for every action under such an actor, whether or not the process was
    started with `--require-authority`, and the descriptor must name that actor (the
    account, or the agent whose credential it carries).
  - A worker credential's actor namespace cannot have the shape of an account or agent
    id other than the issuer's own account id.
- **The reservation does not reach back in time.** A kit from before this slice has no
  reservation: on it, an SSH caller can declare `usr_<someone's id>` as its actor and
  write a proposal (or a disposition) that this kit then reads as written under HTTP
  authority. That covers every deployment up to the upgrade, and any later rollback.
  - After the upgrade, and again after any rollback is rolled forward, run
    `admin.py proposal-http-records PROJECT --before <UTC time this kit went live>` for
    each project. It lists every proposal revision and disposition whose native author
    has an account or agent id shape, with the tracker's own creation time. Before this
    slice no web route wrote proposals, so on first upgrade the list should be empty;
    anything listed was planted. After a rollback, compare each record's
    `native_created_at` with the window in which the older kit was the endpoint:
    `--after <rollback time> --before <roll-forward time>` lists exactly that window.
  - The limit: the scan reports; it does not change how the records read. A planted
    record keeps reading as verified (a revision) or counted (a disposition) until a
    repair exists for these kinds (kittrial-5bb.74 for dispositions). Until then, treat
    a listed proposal as unverified by hand and have its real submitter resubmit.
    `native_created_at` is the tracker's time; an attacker with native access to the
    database is outside this check, as everywhere in the kit.
- **Rollback.** A kit from before this slice reads a web-written disposition as inert:
  the proposal shows its earlier state there, with a warning. Nothing is lost, backups
  and restores are unaffected, and the newer kit reads it as counted again.
  - Do **not** follow the older kit's advice for such a disposition. Its refusal says
    "re-adding them (`admin.py operators add ACTOR`) makes those records count again".
    For a web disposition the author is an account id: adding `usr_...` to the operator
    allowlist would make an HTTP account id an operator. Roll forward instead. This
    kit's `operators add` refuses an account- or agent-shaped id. Only the exact shape
    is reserved (`usr_` or `agent_` and 16 lowercase hex digits); a near miss such as
    15 digits or `USR_...` is an ordinary name. An allowlist that already holds such an
    id is not changed for you: `operators list` and the service at start print a warning
    that names it; remove it with `operators remove NAME --confirm-revoke`.
  - While the older kit is the endpoint, the actor-shape reservation is off. Run the
    scan above when you roll forward.

**In the web interface.**
- **Reviews** shows the proposal queue above the contribution groups: Submitted, Under
  review, Needs information, Escalated to the owner and Approved, and for members who can
  triage, Incorporated but not yet the accepted requirement. A row opens the proposal on
  the same page (`#/p/{id}/reviews?proposal={key}`): the text, why, evidence, what has
  happened, and the form for whoever acts next.
  - A member with `reviews.approve` gets the triage form, or the owner-decision form on
    an escalated proposal. It is not offered on their own proposal, nor the decision to
    the member who escalated.
  - The submitter gets a revise form while the proposal is submitted or a coordinator
    has asked a question.
- **Propose a requirement** is a button on Reviews (`?propose=1`). The form never sends
  a submitter.
- **My work** has a "My contributions" panel, and a project's task page shows one line
  about your own proposals there.
- Proposal text is always rendered as plain text. An evidence entry is a link only when
  it is a plain `https` URL with no user name or password in it.
- A server without these routes shows "Not available on this server" in place of the
  queue, and no My contributions panel. A project you are not a member of shows "Not
  found", not that.
- The queue reads each open state on its own, newest first, 50 at a time, with "Show
  older"; the counts are the server's totals. So a new submission is on the first page
  however many proposals the project has had. My contributions pages the same way.
- A refusal shows the rule the server named (for example an unknown decision id). When
  someone else changed the proposal while it was open, the form says to reload.
- When the server cannot confirm a save (no answer, or a 5xx), the form says so and
  stays. Pressing the same button again with nothing changed sends the same request
  under the same `Idempotency-Key`, so it cannot create a duplicate; changing the
  content makes it a new request. This holds for every form in the web interface, not
  only these. The key is kept in the page, so the protection ends with a reload: the
  message says to stay on the page, and to check whether the change was saved before
  sending it again after a reload.
- "Who decides" on an escalation offers the configured owner deciders who are owners of
  the project (only an owner can record the decision on the web), except the member who
  escalates. The form names the configured deciders it does not offer and why: not an
  owner here, not a web account in the project (a `person:` identity), or the escalator.
  When none can be offered, an operator escalates with the host command. With no
  deciders configured, the project's other owners are offered.
- A `person:` submitter is shown as "a named person, not a web account".

Not built yet:
- creating the decision issue from the web, so an owner's yes or no still needs an
  operator to file the decision issue first;
- promoting a feedback entry from the web (the HTTP feedback routes are not canonical on
  the endpoint backend; `proposal submit --from-feedback` on the client works);
- the scoreboard, statistics and the self-service scoreboard hide (slice 2).

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
`checkpoints`, `reviews`, `feedback`, `proposals`). `proposals` is recognised so that
a credential issued for the requirements-gathering design can be read here. It
grants nothing until that design's proposal routes exist. It may always read the project it is scoped
to; a write route requires the matching scope. No credential can administer
accounts or projects, issue or revoke credentials, or approve a review, whatever
the role of the account that issued it. Issuing a credential never lends the
issuer's authority to it.

Mutating calls accept an `Idempotency-Key`. On an uncertain `503` the client
raises `UncertainOutcome` carrying the key: retry the identical request with that
key to reconcile. Never retry an uncertain mutation with a new key.

**The server's time of a write.** The JSON body of a write that was carried out has
`server_time` at its top level, for example `"server_time":
"2026-10-06T07:50:12+00:00"` (UTC with its offset, whole seconds). On the endpoint
backend it is the time the endpoint gave for the write; where the service carries the
write out itself (accounts, members, agents, credentials) it is the service's clock.
The same request sent again with its `Idempotency-Key` is answered with the stored
body, so with the time of the write and not of the retry. Reads, refusals and uncertain
answers carry none. A log-in and a log-out carry none either (they make or end a
session and record nothing in a project).

The same value is in the response header **`X-Server-Time`** of every write that was
carried out, whatever the shape of its body: a caller has one place to look. That
matters for the answers that have no top level for the field: on the endpoint backend
a task change (`PATCH .../tasks/ID`) and a claim (`POST .../tasks/ID/claim`) answer
with the list bd prints, and some writes answer 204 with no body. The header is absent
exactly where the field is absent for an object body: reads, refusals, busy and
uncertain answers, a log-in and a log-out. A retried request gets the header with the
time of the write, because the time is kept beside the stored answer; an answer that
was stored by a kit from before this header has it on a retry only when its body is an
object with `server_time`. The web pages do not show it.

The service keeps the time in both of its records of a keyed write: with the answer as
it was sent (the idempotency record) and with the endpoint's result (the result
record). A retry that finds only the result record (the other gone, or never committed
because the service stopped between the two) is answered from it without asking the
endpoint, with the time of the write.

Two edges. During an upgrade, a write that was made through the kit from before this
field and is sent again through this one, when the service's own stored answer is gone:
whether the endpoint replays what it stored or the service answers from a result record
that kept no time, the answer has no `server_time` and no header (no time is known; the
service never puts the time of the retry there). And the time
is taken when the write has been carried out and cut to the whole second, while bd
rounds: a task's `updated_at` can read one second later. It is the wall clock, not
monotonic across a clock step.

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

### What an agent needs to write a checkpoint

`GET /v1/projects/{id}/tasks/{task}/brief` carries everything a checkpoint needs, so an
agent can write its first one from the brief alone.
- `activity_cursor`: the cursor a checkpoint must carry. It is the same value the
  history route returns. (With `--backend inprocess` it is null and not needed.)
- `checkpoint_template`: the record to send, for this task at this moment.
  - `send_to` and `method`: where it goes.
  - `body`: every field, with `previous` and `activity_cursor` already filled, the four
    texts empty, and `open_items` already holding **every open item of the previous
    checkpoint exactly as recorded**, `source` included. Sent as it is, the record
    carries them all forward. To resolve one, take it out of `open_items` and name its
    id in `resolved` with a reason and evidence.
  - `carried_open_items`: how many open items `body` holds. It is null when they could
    not all be read (the task changed during the read); `body.open_items` is then
    empty, the note says so, and the brief is read again.
  - The brief's own `checkpoint.open_items` shows the first ten, each now with its
    `source`; a task with more costs one further canonical read, which asks for all
    of them (the canonical `brief --items-limit` now goes up to 100).
  - `required`: the four texts to fill (`intent`, `acceptance`, `summary`,
    `next_action`); `optional`: what may be left out and when; `limits`; the shape of an
    `open_item` (with the allowed `kind` values) and of a `resolved_item`.
- **Four fields may be left out of the request**: `source_commit`, `branch`,
  `open_items` and `resolved`. The service sends the empty value for each. The stored
  canonical record is unchanged and always has every field. Over SSH nothing changes:
  `checkpoint --file` still takes the whole record.
- **A refusal names every problem with the record at once.** `error.detail` is the
  canonical sentence, as before; with two or more problems it is a numbered list of the
  same sentences. `error.problems` is the list, one sentence each. A stale `previous`
  and a changed cursor stay separate refusals: read the brief again and send its
  template.
  - A required field left out is one of those problems (`Invalid checkpoint: missing
    fields: summary`), named beside the others. The service no longer refuses it by
    itself first.
  - A field name the caller wrote is shown as it is only when it is letters, digits,
    `_`, `.` and `-`. Any other name is shown as a quoted string with control, bidi and
    non-ASCII characters and square brackets escaped, so a name cannot break the line
    or pass for another numbered problem.

### What a task change takes

`PATCH /v1/projects/{id}/tasks/{task}` changes a task's `title`, `description` and
`status` (`open` or `closed`), and nothing else. Its body may also carry `version`
(the in-process backend of the tests and the local preview checks it; the endpoint
backend does not read it) and `actor` (below). Anything else is refused, not
dropped:

- a field the route does not take (`priority`, `assignee`, `labels`, any other
  name) answers **422 `invalid_payload`**, "A task change does not take: priority.
  A task change takes: title, description, status", with the names again in
  `error.detail` (`unsupported`, `takes`). So does a body that takes one field and
  not another: the whole change is refused, not half of it carried out;
- a body that would change nothing (no field, only nulls, only `version`) answers
  422 "Nothing to change";
- a `status` other than `open` and `closed`, a `title` that is empty or not text
  and a `description` that is not text answer 422 with one sentence each. A claim
  is what sets a task in progress (`POST .../tasks/{task}/claim`).

Such a refusal comes before the idempotency key is looked at: nothing is reserved,
nothing is sent to the endpoint, nothing is audited as uncertain, and **the same
key serves the corrected request**. To give a task a priority, an assignee other
than the caller or a label there is no web route today.

Before this (kittrial-5bb.181) a change that carried none of the three fields was
sent to bd as an update with nothing in it. bd answers that with the words "No
updates specified"; the service could not read them and answered **503 "The
operation may have committed; reconcile with the same idempotency key"** for a
change that had not been made, kept the key, and answered the same to every retry,
with an audit entry of outcome `unknown` each time. A field beside one the route
takes was dropped without a word. A key that was left reserved that way on an
installation is not held for ever: the same request under it is now answered 422
like any other, and the key itself lapses a day after it was first used, as every
key does. Until then a DIFFERENT body under that key answers 409 "Idempotency key
reused with a different request payload": send the corrected request with a new
key. An operator has nothing to clean up; the `unknown` audit entries of that time
record requests that changed nothing.

**A task's title**, on create and on a change, is text, not blank, and at most 500
characters (bd's own limit). Anything else answers 422 before the key is looked at:
"Task title must be text and not empty", "Task title must be 500 characters or less
(it has 600)". A description that is not text is refused the same way.

**When bd itself refuses** (kittrial-5bb.185). bd says no in several forms: a JSON
object `{"error": ...}` with exit 1, a line `Error: ...` on standard error, and for a
row it cannot find `Error resolving ID: no issue found matching ...`. The endpoint
handed bd's exit code on as it came, everything that was not 0 was kept as an
operation whose outcome is unknown, and the caller was told **503 "The operation may
have committed; reconcile with the same idempotency key"** for an empty title, a
title of 600 characters or a task that does not exist, with the key kept and an
audit entry of outcome `unknown` each time. Now:

- a refusal bd makes before it writes is a refusal: the endpoint answers return code
  2 with bd's sentence and releases the operation identity; the service answers **404
  "Task not found"** when bd found no row of that name and **422** with bd's sentence
  otherwise. Nothing is kept, the key serves the corrected request, and no `unknown`
  entry is written;
- a refusal is recognised by its form AND its sentence together (`bd_refusals.py`):
  "no issue found matching", "validation failed for issue", "... cannot be empty",
  "invalid priority" and "invalid status", a title that "looks like a flag", "must be
  N characters or less". A failure that merely has the form of a refusal (a JSON
  error with another sentence, the database's own sentence) is **still an outcome
  nobody knows**, as before: that is the side to err on. A bd that changes its
  sentences falls back to that, and `tests/test_bd_refusals.py` notices it when it
  runs against a real bd;
- **a read is never said to "may have committed"**. A task that does not exist is 404
  on every task route (read, change, claim, checkpoint, review); any other read of
  the tracker that fails answers **503 `unavailable`**, "The tracker could not be
  read just now. Nothing was changed; try again shortly.", and a key sent with it
  is free.

A key that was left reserved by such an answer before this: the same request is now
answered 422 or 404 before the key is looked at; the key lapses a day after it was
first used; until then a different body under it answers 409, and the corrected
request goes with a new key.

**The actor of a task write is the caller's own.** Task create and task change take
an optional `actor`, the attribution label, under the rule of a claim, a checkpoint
and a review: a signed-in member may name only their own account; a worker
credential a label inside its own namespace (`NAME` or `NAME/...`); an agent its
own id. Any other name answers 403 and nothing is written. Before kittrial-5bb.181
these two routes handed the name on as it came, so a member could have a task made
(`created_by`) or changed under any name that does not have the shape of a web
account, a host session's included; a name with that shape was already refused by
the endpoint. Such a row cannot be told from a host actor's own by the row alone.
Two records can tell: the project's audit log has a `tasks.create` or
`tasks.update` entry with the real account at that second, and, when the request
carried an idempotency key, the project's operation journal has its row with both
names (`principal` `user:usr_...` and an `actor` that is not that account).

### The merge slot is not a task

Each project has one merge slot, `PROJECT-merge-slot`, an internal record. On the host it
is stored as an ordinary row, so earlier kits listed it as open, unclaimed work and
offered it to agents.
- It is in no task list, queue, `GET /v1/me/work` or `GET /v1/agents/me/next`, and it
  is not counted as claimable.
- Reading it as a task (the task, its brief, its history) answers 404.
- Claiming it, editing it, or posting a checkpoint or a review on it answers 409 with
  "PROJECT-merge-slot is the project's merge slot, an internal record, not a task".
- **The slot is the row with the exact id `PROJECT-merge-slot`** (or the type
  `merge-slot`), on every path: listings, reads and writes. The label is not part of
  the rule: a slot whose label was removed on the host is still the slot. A project
  name holds no hyphen, so a task whose id only ends in `-merge-slot`
  (`PROJECT-x-merge-slot`), or that only carries the label, is still a task.
- **No contributor write reaches it over SSH either.** Every `bd` write names the rows
  it writes, each name is resolved through bd before the write (bd resolves an id
  from any part of it: `slot`, `merge`, even a lone `-`), and a write that resolves
  to the slot is refused. See CLI_CONTRACT.md, "Raw bd writes: which rows a command
  names".
- **The label `gt:slot` is reserved.** No contributor adds, removes or replaces it on
  any row, so a task cannot be hidden with it and the slot cannot be exposed by taking
  it off.
- **A damaged slot is repaired by `merge-create`** (`coordinate`). It restores the
  label, clears an assignee, and sets the status from the holder bd recorded: in
  progress while held, open when free. It names no new holder and releases nobody. The
  answer lists what was changed in `repaired`. `merge-check`, `merge-acquire` and
  `merge-release` on a slot that is unavailable with no holder now say to run it.
- The web service never offers the row with the id `PROJECT-merge-slot`, whatever the
  endpoint lists (an older endpoint listed it in `work`).

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
read through one current-work snapshot reused by every agent in this request.
The endpoint backend pays one `work` command per page of 100 rows, up to its page
bound; there is no additional `bd list` or owner-filtered read. Counts include every
row read, while claimable suggestions and the final action list have separate caps.
`truncated` reports any of those bounds, including an incomplete queue walk.
My work retains its existing short principal-specific queue cache; its agent cards
reuse that queue and add no read. Standalone agent attention reads stay fresh on
each request. Authority is checked live in both cases.

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
agent's disable action revokes every credential; enable restores none, so issue a
new credential after enabling. This is the settled personal-agent decision from
2026-09-27 (design decision `01a0e2f2-569c-7d03-94ee-36b8dd61940a`).
The `working_directory` hint is returned only to the owner or a superuser. The browser
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

Over SSH the actor is self-declared, so several guarantees hold only where the caller
cannot choose the remote command: the HTTP actor-shape reservation and the operator-only
host commands (`admin.py`, including `requirement-apply` and `void-record`) are enforced by
`endpoint.py`/`admin.py` themselves, and a contributor key with an ordinary service-account
shell can run a different program instead. The forced-command wrapper
(`ssh_forced_command.py`, [confine contributor keys](OPERATIONS.md#confine-contributor-keys-with-a-forced-command))
is what makes the SSH path to `endpoint.py` confined. It also closes the last server-side
launch flag: `--authority-store`, `--authority-lock` and `--require-authority` belong to the
service's own launch command, and the wrapper never passes them, so an SSH caller cannot
reach the HTTP authority path.

The HTTP service's `--backend endpoint` launches `endpoint.py` itself as the service
account; that is host configuration and is unaffected by a contributor's confined key.
Operator host commands still need the service account's shell (a deliberately unrestricted
operator key, or direct host access), which is why `admin.py authorized-keys` prints that
line separately and unconfined.

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
- **A row that is not read** (kittrial-5bb.169). The kit does not read a tracker row
  whose JSON nests deeper than 750 levels (`record_json.ROW_NESTING_MAX`; deep
  metadata is enough). The rule is counted, not tried: it is the same on every Python
  (3.10 cannot parse about a thousand levels, 3.13 parses several thousand), and it is
  the rule the endpoint's own readers get with kittrial-5bb.141, from the same
  constant. The service takes an answer of the tracker that holds such a row apart row
  by row, so one such row does not fail the rest:
  - `GET /v1/projects/{id}/tasks` answers 200 with every readable row. The other is in
    `items` as `{"id", "status": "unknown", "unreadable": true, "malformed": true,
    "error": "Malformed issue row"}`: its id and nothing else of it (not its title,
    which is stored text nobody has read). The answer names it in `unreadable: [ids]`,
    on every page and under every filter. The field is absent when every row is
    readable. The page marks the row "Cannot be read" and says how many there are.
  - An answer that holds a deep element and is not a single row or a clean list of
    rows (text between the rows that JSON does not allow, a deep element that is not
    an object or has no id of a tracker id's shape, such as a deep error object) is
    refused as an answer that cannot be read, as a malformed answer always was.
  - The page, brief and history of that row, and a change or claim of it, answer 409
    `unreadable_row`: "Task ID exists, but its row cannot be read (it is malformed or
    nested too deeply). Ask an operator of the server to repair it." Nothing of the
    row is shown or changed through the service.
  - The review states come from a second read of the endpoint (`work`). An endpoint
    that cannot read such a row itself fails that read; the list is then still
    answered, with `review_states_unavailable: true`, `review_states_complete: false`
    and no review states. With every row readable a failure of that read is an error,
    as before.
  - What still depends on the endpoint: the brief of a READABLE task, the queue and My
    work ask the endpoint's `brief` and `work`, which must themselves cope with the
    row (kittrial-5bb.141). Until the endpoint does, those answer 503 while such a row
    exists; they never answered 500.
  - Repair is on the host (for deep metadata: `bd update ID --unset-metadata KEY`).
- **Reference catalog reads.** The routes are `GET /v1/projects/{id}/references` and
  `GET /v1/projects/{id}/references/{key}`, available to any project member
  (`CAP_READ`). They are the .41 slice 1, kittrial-5bb.66.
  - **Canonical binding:** they map the endpoint's read-only `ref list` and `ref get`.
  - **List query:** `tag` (comma-separated, all must match), `owner`, `state`, `due`,
    `authority` (`repository`, `url`, `attested` or `decision`), `limit` and `cursor`. A bad filter
    is a `422` before any canonical read.
  - **Authority kind:** every listed row and every `record` and `proposed` object
    carries `authority_kind`. An attestation also carries `authority_note`, which
    starts `NOT ACCEPTED.` for a draft; show that sentence wherever the entry is shown.
  - **404:** an unknown key, or an entry left without a record by an interrupted
    propose.
  - **Request bodies, on every route:** a body nested more than 64 levels deep is
    refused with `422` `Request body is not valid JSON`, before any route runs and
    with no operation reserved; a cursor that decodes to such JSON is an invalid cursor.
  - **Telemetry:** the get route runs `ref get`, which the endpoint counts in the
    [reference lookup-miss log](OPERATIONS.md#the-reference-lookup-miss-log). A get of an
    unknown key stores that key's words, a count and two timestamps, and no account. So a
    member with read access writes to that log; it is bounded and never backed up.
  - **In-process backend:** it holds no native records, so its catalog is empty.
  - **Read-only:** there are no HTTP writes; proposals use the client and acceptance
    uses `admin.py reference-apply`.
  - **Untrusted text:** a statement is returned as data and never placed in an error
    body or an audit record.
  - **No web view yet:** when one is added, it must render statements as plain text,
    never through `markdown()`.
- **`GET /v1/me/work` cost.** On the canonical binding each project costs one bounded
  `work` walk (up to 10 subprocess reads of 100 rows), for at most 50 of the caller's
  projects. The same principal's read of a project is reused for 20 seconds
  (`EndpointBackend.READ_CACHE_SECONDS`, in memory, bounded to 2048 entries), so
  repeated page loads do not re-export every project. A principal's own successful
  write drops their cached reads of that project, so their action shows at once.
  Authority is never cached:
  each project is re-authorized on every request, so a removed member loses it at
  once; only the task data may be up to 20 seconds old on My work. My work then adds
  at most `ME_WORK_DETAIL_MAX` (10) canonical `brief` reads per request, one per task,
  for the rows the `work` projection leaves blank (see "Agent prompts on My work"),
  cached the same way.
  The cache key is the principal (user id and credential id, so an agent never reads
  its owner's entry or the reverse), the project and the read kind.
- **Task list cost.** On the canonical binding one task-list page needs the project's
  `bd list` snapshot plus the review states from the bounded `work` walk (up to 11
  subprocess reads). Both are reused by the same principal for the same 20 seconds
  (one cache shared with My work), so paging, filtering and reloading cost nothing
  more until the entry expires; the principal's own successful write (claim, deliver,
  review, edit, membership change) drops their entries for that project at once, and
  another person's write shows up within 20 seconds. The queue and brief always read
  fresh.
- **Agent prompts on My work.** `GET /v1/me/work` also returns `agent_prompts`: one
  copyable prompt per agent the caller owns, built by `agent_prompts.py` from the same
  queue data, limited to the projects that agent is granted, grouped by project and
  tailored by the caller's live role there
  (approvers: reviews and re-reviews with revision, commit and contribution id,
  approved-but-not-integrated, blocked, unclaimed P0/P1 and stale claims, i.e. claimed
  with no recorded activity for 72 hours or more; workers: changes requested with the
  pending request item ids where the backend knows them, their claimed and delivered
  tasks, claimable tasks; viewers: a read-only status summary that tells the agent not
  to change anything; an agent with no granted project, or none its owner can still
  open, gets only a short "no projects yet; grant one on My agents" note, `kind:
  "empty"`, with no action list). My agents has a "Grant a project" form per agent.
  At most 25 items, then "and N more". Every prompt carries its
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
  canonical binding the `work` projection gives counts but not pending request ids,
  review times or checkpoint state. My work fills them from one canonical `brief` read
  per task (`EndpointBackend.task_detail`) for at most `ME_WORK_DETAIL_MAX` (10) tasks
  per request, highest value first: the caller's own changes-requested tasks (pending
  request item ids; the brief lists up to five, and the prompt says "5 of N" when there
  are more), contributions awaiting the caller's review (waiting since the revision
  was delivered), the caller's own claimed tasks and then other claimed tasks where the
  caller approves (blocked when the latest checkpoint lists unresolved items). Rows past
  the bound still say "read the task brief" and "wait time unknown" and are not marked
  blocked. My work rows show "Blocked" and the open request ids where known.
- **Review respond step (slice 2a).** Canonically a requested change stays open until
  the contributor records a `respond` resolution for it, even after a newer revision
  arrives. The task page shows the task's assignee each open request with a "Resolved"
  tick and a short note, plus shared evidence (by default the current revision and
  commit), and records one `respond` through `POST /v1/projects/{id}/tasks/{task}/reviews`
  with `{operation: "respond", contribution, previous, resolutions: [{request, item,
  reason, evidence}]}` (the brief's `requests[].request` and `requests[].id`). After a
  revision is delivered while requests are open, the page leads with this step. Only
  the task's assignee may respond (`403` otherwise; the route checks it and the
  canonical validator enforces it again at the write), and approval waits until no
  request is open. The canonical brief lists at most five open requests; the page
  says how many more there are, and they appear once the first ones are answered.
  The disposable in-process backend now follows the same rule: a request it records
  carries `needs_respond` and stays open across revisions until a `respond` resolves
  it. Requests recorded in older disposable state, without that flag, keep the old
  rule (the next revision resolves them).
- **Account lookup residual risk.** `GET /v1/accounts/lookup` answers exact usernames
  for a project administrator. With the in-process backend any account may create a
  project and so become one; with the endpoint backend only a superuser registers projects
  and becomes their first owner. An account holder who is a project administrator can
  therefore still test whether a given username exists.
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
- **What an agent is told to do next (`GET /v1/agents/me/next`, and the attention on
  each agent card).** Closed limitation `endpoint-blocked-signal`, and more
  (kittrial-5bb.114): on `--backend endpoint` this read used to take everything from
  the `bd list` snapshot, whose rows carry no review state and no checkpoint, so an
  agent with changes requested, a contribution awaiting review or a blocker was told
  there was nothing to do. The agent's own tasks now come from the canonical `work`
  view. The same unfiltered snapshot supplies own states and claimable suggestions,
  however many agents the owner displays. `/v1/agents/me/next`, owner lists and
  owner detail cost one canonical `work` command per page (100 rows), with no
  additional `bd list` or owner-filtered work command. In-process projects cost one
  snapshot read. Bounds still report `truncated`; nothing is cached across requests
  by the attention calculation, and live authority is checked before every project.
  - **One action per own task, in this order:** `changes-requested` (priority 1, with
    `requests`, the request-changes record ids), `blocked` (2: the latest checkpoint
    lists open items; with `open_items`, `blocked_since` and `newer_activity`),
    `in-progress` (3: claimed, not closed, nothing delivered yet), then review work
    and `claimable-task` (4, see below), then `awaiting-review` and
    `awaiting-integration` (5).
    `review-error` names the malformed state and asks an operator to reconcile it;
    other own states get `review-state`, naming the state and who acts next. Neither
    silently disappears from the action list. Action **kind names** are the client
    contract. Numeric priorities are relative sorting hints, can change between kit
    revisions, and must not be treated as a stable enum. With both undelivered work
    and a delivered contribution, the state is `working`, while the waiting action
    remains visible after work the agent can do.
    Explicitly unreadable checkpoint history keeps `open_items: null` and gets
    `checkpoint-error`, asking an operator to reconcile it. Unknown does not count
    as zero unresolved items or as undelivered work the agent can safely continue.
  - **Review work (kittrial-5bb.115).** Two kinds, for an agent that holds the reviews
    capability in the project. They come from the same `work` snapshot: no further
    read.
    - `to-review`: a contribution that awaits review and that this agent has not
      recommended yet. The agent reviews it and records a recommendation or requests
      changes. `who` is `agent`.
    - `review-recommended`: a contribution that awaits review and has a standing
      recommendation, shown to an agent **whose owner can approve**. An agent cannot
      approve: the action says to tell the owner it is ready. `who` is `owner`.
    - An agent is shown a delivery only if it could itself recommend it: not its own
      task or contribution, not its owner's, not another agent of its owner's, and
      only in a project it is granted.
    - Each action carries `recommended_by`, `contribution` and `commit`.
    - `counts` gains `review_recommended` and `to_review`. At most 20 review actions in
      all, over every project the agent is granted (recommended ones first), are
      listed, so claimable work stays on the list. The counts are exact and
      `truncated` says when more exist.
    - **The `state` values do not change for review work.** An agent with nothing of
      its own and something to review still reads `idle`. The `summary` names the
      review work whenever a count is not zero ("1 contribution(s) recommended for
      approval: tell the owner; 2 contribution(s) to review."), so an owner looking at
      the agent list is not told there is nothing to do.
    - The prompt copied from My work (`GET /v1/me/work`, `agent_prompts`) names the
      same work. A person who may review but not approve gets "Contributions you could
      review" with the contributions of other people that nobody of theirs has
      recommended yet; an approver's review lines add "recommended by N reviewer(s)".
    - **On a mixed installation** (this service over an endpoint that does not yet put
      `contribution_author` on a work row, in a staged upgrade or a rollback) no
      recommendation is counted on such a row: there is no `review-recommended`
      action, the queue row and My work show none, and every delivery that awaits
      review reads as not yet recommended, until the endpoint is updated. The service
      does not guess who delivered from the task's assignee. The task's brief still
      shows an independent recommendation. A delivery whose task was reassigned or
      unassigned afterwards is offered as `to-review` to the agent that delivered it;
      its recommendation is refused with 403 and nothing is written. A reviewer that
      has already recommended a delivery is not offered it again: the names that are
      not counted are on the row as `recommended_unchecked` and are used for that one
      question only. Update the endpoint and the service together and none of this is
      seen (docs/REVIEWS.md, "A mixed installation").
    - **"Already recommended?" is asked by person** (kittrial-5bb.154), of an agent's
      actions and of My work alike: an agent is not offered a delivery that it, its
      owner or another agent of its owner has recommended, and My work does not list
      it under "Contributions you could review". (An agent used to be asked whether
      THIS AGENT had recommended it, so a person's second agent was offered it.)
      Another person's agent is still offered it. A second recommendation by the same
      person is refused with 409 and the sentence in docs/REVIEWS.md, which names the
      ways on: request changes, or wait for a revision or a decision.
    - The prompt's "delivered by" names the contribution's author when the row gives
      it, not the task's assignee, so a reassignment does not rename who delivered.
      When the row does not give it (a mixed installation, above) the prompt reads
      "delivered by: not stated by this server (the task is assigned to NAME)": the
      assignee is named as the assignee, and nobody as the one who delivered.
    - After a reassignment the new assignee's agent lists the task as
      `awaiting-review` ("Waiting for a human review decision."). That is meant: the
      task is that agent's now, whoever delivered the contribution on it, and there is
      nothing for it to do until the review is decided.
  - **Order within a priority is part of the contract; the numbers are not.**
    `review-recommended`, `to-review` and `claimable-task` all carry priority 4 and
    are listed in that order, and within a kind by project and then by task. No kind was renumbered when these two were added. A
    client relies on the order of `next_actions` and on the kind names.
  - **Why the two waiting kinds are last.** An agent, and anything that wakes it,
    takes the first action. The agent can do nothing about a contribution that waits
    for a reviewer or for integration, so those never sit ahead of work it can do.
    They are still listed and counted.
    A redelivery with `supersedes` does not clear requested changes: submit a
    structured response answering each outstanding item against the current
    contribution. The canonical review state changes only when those items are
    answered (or the reviewer records their disposition).
  - **A blocked task can stay quiet.** `blocked_since` is when the latest checkpoint
    was written; its own subsequent authored records do not make `newer_activity`
    true. Another native actor's comment does; missing author attribution retains
    the previous conservative wake signal. On the in-process backend attributed
    owner edits also count. Native description edits lack reliable editor identity
    and are awaiting the recorded owner decision; no editor is inferred from the
    original creator. The brief's separate checkpoint freshness flag still compares
    the full snapshot for checkpoint reconciliation. An agent can leave a quiet task
    alone instead of
    re-reading it and writing another checkpoint on every wake.
  - **`counts`** are over the agent's own tasks: `claimed` (every task it holds that
    the `work` view lists: open ones, and closed ones whose review is still active),
    `changes_requested`, `blocked`, `in_progress` (not delivered and not blocked),
    `awaiting_review`, `awaiting_integration`, and `claimable` for the project. A task
    with changes requested and open checkpoint items counts in both.
  - **`state`**: `changes-requested`, `blocked`, `working`, `waiting-review`,
    `waiting-integration` or `idle`, the first that applies.
  - `GET /v1/me/work` (a person's own queue) still reads its blocked signal from the
    bounded per-task `brief` reads described above.
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
