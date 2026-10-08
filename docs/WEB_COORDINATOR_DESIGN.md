# A coordinator that works only over the web

**Status: design note for James to decide on (kittrial-5bb.201). Nothing here is
built, and this task changes no behaviour.**

The goal, in James's words of 2026-10-07: much more in the web interface, several
coordinators across several projects, and **nobody but him needing SSH to the
server**. For the office this replaces the route that
[COORDINATORS_PER_PROJECT_DESIGN.md](COORDINATORS_PER_PROJECT_DESIGN.md)
recommends (coordinators on confined SSH keys). That design stays right for an
installation whose lanes already have SSH, and its rules must hold here too: a
caller is bound to its projects and to its lane, and nobody passes their own
work.

Everything under "What is true today" was read in the code of main `02740dd`
(release 1008t); line numbers are that commit's. I ran nothing for this note
and could not read the coordinator's measurement of release `ff83602`; where
the two disagree, the measurement is what happened and this note is what the
code says.

## The answer in short

- **A worker needs no SSH today.** A person or an agent with a web credential
  can find work, claim it, write checkpoints, deliver a contribution as a branch
  on the code host, answer a review, and close a task.
- **A coordinator cannot do its work over the web today**, for three reasons:
  1. *An agent can never approve.* Approval needs a capability that every
     credential is denied, whoever owns it. Only a signed-in person who is an
     owner of the project, or a superuser, approves.
  2. *The web has no route for half of the loop*: assigning a task to somebody
     else, comments, the merge slot, lifecycle facts (so nothing is ever
     recorded integrated, deployed or live-verified), release records, handoff,
     guidance, and proposing or accepting reference and capability records.
  3. *Nothing on the web says who may not approve what*: an owner may approve
     their own contribution.
- **The design** is four things:
  1. **A lane is an account.** The web service already treats an account and all
     of its agents as one party when it judges a recommendation. The same rule
     is applied to approval and to recording a task reviewed or integrated:
     nobody passes work of their own account. A coordinator lane and a worker
     lane are two accounts; no new kind of identity is needed.
  2. **A project role `coordinator`**, between contributor and owner, and an
     agent scope `coordinate` that hands that role to one named agent in one
     project. Granted by an owner of the project or a superuser, revoked the
     same way, effective at once, audited per project.
  3. **Web routes for the missing actions**, each asking for the coordinator
     role in that project, in the order given under Slices.
  4. **The server records integration and does not perform it.** The
     coordinator merges on its own machine and pushes to the code host, as the
     kittrial coordinator does today; the server checks what it can check from
     its own records and says so.
- **What stays with James on the server**: installing and upgrading the kit,
  backups and restore, the first superuser, voiding and reverting records,
  reconciling, retiring a project, and the rollout switches.
- **Today, on release 1008t**, James can run an office project with a person as
  coordinator and agents as workers with no SSH for anybody else; a coordinator
  *agent* can do everything except approve and record integration, so a person
  clicks Approve on its recommendation. The steps are under "What James can do
  today".

## What is true today

### Who may do what on the web

| Fact | Where |
|---|---|
| Three project roles: viewer (read), contributor (tasks, checkpoints, reviews, feedback, proposals), owner (those plus `reviews.approve` and `project.admin`). | `http_authority.ROLE_CAPABILITIES`, 356-362 |
| A credential, a worker credential or a personal agent's, never holds `reviews.approve`, `project.admin`, project creation, account administration or agent management, whatever its issuer's role. | `CREDENTIAL_FORBIDDEN_CAPABILITIES`, 363-365; applied in `credential_capabilities`, 395-411 |
| A credential's capabilities are its scopes (`read`, `tasks`, `checkpoints`, `reviews`, `feedback`, `proposals`), capped by its issuer's present role in that project. | `SCOPE_CAPABILITIES`, 344-353 |
| Approval asks for `reviews.approve`; every other review operation for `reviews.write`. The route compares the approver with nobody. | `http_service.reviews_add`, 5833-5842 |
| `recommend` is refused from the same PERSON as the contribution's author or the task's assignee; an account and all its agents are one person. | `_independent`, 3615-3625; `http_auth.actor_person`, 2229-2239 |
| An agent is made by a signed-in account for itself; nobody makes an agent for another account. | `agents_create`, 4642-4663 |
| The actor a web caller writes under is fixed by the service: the account's own, or a name inside the credential's namespace. | `http_auth.bind_actor`, 1675-1689 |
| An owner sets a project's members; only a superuser makes or unmakes an owner. | `http_auth.set_member`, 1939-1947 |
| Changes are audited per project, readable by owners. | `GET /v1/projects/{id}/audit`, 6155 |

### The coordinator's loop, action by action

"Host" is `endpoint.py` over SSH with `client.py`, `lifecycle.py` and
`coordination.py`, or a host command (`admin.py`) where marked. "Web" is the 65
routes of `http_service.py`. The last column is this design.

| Action | Host today | Web today | In this design |
|---|---|---|---|
| File a task | `create` | `POST .../tasks` (5204): title, description, priority | as today |
| Assign it to somebody else | `update ID --assignee NAME` | **none**: a task change takes title, description, status, priority (3529); a caller can only claim for itself | route, coordinator |
| Claim | `update ID --claim` | `POST .../tasks/{id}/claim` (5768) | as today |
| Read: list, task, brief, history, queue, my work, an agent's next action | `list`, `show`, `brief`, `history`, `work` | all there (5246, 5318, 5651, 5947, 5969, 5991, 4620) | as today |
| Comment (a plan, evidence, a note) | `comments add` | **none**: no route writes a plain comment | route, contributor |
| Checkpoint | `checkpoint` | `POST .../checkpoints` (5797) | as today |
| Contribute, respond | `review` | `POST .../reviews` (5833) | as today |
| Request changes | `review` | there, for a contributor and for a credential | as today |
| Recommend | `review` | there; not from the author's own account | as today |
| Approve | `review`, any name | a signed-in owner or superuser only; **never an agent**; their own work included | coordinator, person or agent; never their own account's work |
| Withdraw, request a review | `review` (behind the installation's `review-writes` switch) | there, behind the same switch | as today |
| Merge slot: check, acquire, release | `coordination.py` | **none** | route, coordinator |
| Lifecycle scope and the six facts | `lifecycle.py record` | **none** | route; who may write which fact is kittrial-5bb.106's decision |
| Release record for many tasks, deploy, live-verified | `lifecycle.py release`, with a checkout and an export on the caller's machine | **none** | route, later slice |
| Close, reopen | `close`, `reopen` | task change, status `open` or `closed` (3554) | as today |
| Handoff: ask, accept, decline | `handoff` | **none** | route, later slice |
| The operator's transfer of a task | `admin.py handoff` (host command) | **none** | the assign route covers it |
| Guidance: read, acknowledge | `guidance get`, `ack` | **none** | route, any member |
| Guidance: set, clear, status | `admin.py set-guidance` (host command) | **none** | route, coordinator |
| Onboarding text | `admin.py set-onboarding` (host command) | read 4358; set and clear by an owner, 4376 and 4396 | as today; also a coordinator |
| Reference records: find, read | `ref find`, `ref get` | read only (5352, 5379) | as today |
| Reference and capability records: propose, revise | `ref`, `capability` | **none** | route, contributor |
| The same: accept, retire, verify | `admin.py reference-apply`, `capability-apply`, `capability-verify` (host commands) | **none** | route, coordinator |
| Capability lookup and find | `capability find`, `lookup` | **none** | route, any member |
| Requirement proposals: submit | `proposal` | `POST .../proposals` (5474) | as today |
| Proposals: review and decide | `admin.py proposal-review`, `proposal-decide` (host commands) | a signed-in person with `reviews.approve`; a credential is refused (5565-5578) | also a coordinator agent |
| Feedback | `feedback` | write and read (6126, 6142) | as today |
| Sessions: register, resume, runs | `session` | none, and none needed: an agent is the identity | not built |
| Members, worker credentials, agents, archive | not on the host route | there (4453-4826) | as today |
| Project creation | `admin.py add-project` | there (3937), for an account with the project-creation right; never a credential | as today |
| Audit | none for most host commands | per project, owners (6155) | also shows coordinator grants |

### What follows from it

- Over the web nobody, person or agent, can record that anything was
  integrated. A task that is approved shows "awaiting integration" until it is
  closed.
- An agent cannot be a coordinator. The nearest it gets is `recommend`, which a
  person then turns into an approval.
- A worker over the web cannot follow the kit's own rules for workers where
  they need a route that is missing: register a plan as a comment before
  building, post evidence as a comment, read and acknowledge guidance, look a
  capability up before searching, propose a record for a miss.
- A contribution can be delivered with only a web credential when it is a
  branch on the code host (`delivery.kind: remote`). A `bundle` delivery names a
  file path on a server, which somebody must put there: that needs SSH and is
  not for the office.
- `http_client.py` is a client library with four commands (`login`, `logout`,
  `whoami`, `call`). Its methods cover accounts, projects, members, worker
  credentials, tasks, claim, checkpoint, review, history, feedback and audit
  (164-331). It has no method for brief, queue, my work, an agent's next action,
  agents, proposals or references; `call` reaches them as raw requests, and the
  agent instructions use `curl`.

## The design

### A lane is an account

James decided for the SSH route that a principal is a lane, not a person: an
agent cannot pass its own work, and that is all. On the web the party that
exists already is the account: `actor_person` maps an agent to its owner, and
`recommend` is judged by it. This design uses it as the lane.

- A coordinator lane is one account; a worker lane is another. Each has its
  agents. `lane-a-coordinator` and `lane-a-workers` are accounts as `james` is;
  no human need ever sign in as them after their agents are made.
- **Nobody passes work of their own account**, in two refusals, as rule 3 of
  the coordinators design has them:
  - *Approval* is refused when the approver's account is the account of the
    contribution's author or of the task's assignee.
  - *Reviewed and integrated* are recorded for a task only when its current
    contribution carries an approval, with nothing unresolved, from another
    account than its author's.
- This is the web item already filed as kittrial-5bb.199 (an owner approving
  their own contribution), made general. A project with one account that both
  delivers and approves is told what to do: the sentence names the rule and
  says a second account, or a superuser, must approve.

Why not a lane label on each agent instead: it would be a second notion of
"same party" beside the one `recommend` already uses, and somebody would have to
keep the labels honest. With accounts, an agent's lane is who made it, which the
service already knows and nobody can edit.

What it costs: one account per lane to create and to keep a password for (or to
leave with a reset pending). An agent is made only by its own account, so
somebody signs in as the lane's account once to make its agents; see the
questions.

### The coordinator role

- **A project role `coordinator`**, ranked between contributor and owner:
  everything a contributor may do, plus `reviews.approve`, plus a new
  capability `coordinate` that the new routes ask for. Not `project.admin`:
  members, worker credentials, archive and the audit stay the owner's.
  An owner holds `coordinate` too, as a superuser does.
- **Who grants it**: an owner of the project or a superuser, with the existing
  member route. (An owner already decides who is a contributor; a coordinator
  can do less than the owner who names it.) Only a superuser makes an owner, as
  today.
- **An agent as coordinator**: a new agent scope `coordinate`. It is the one
  scope that lifts the rule that a credential never approves, and only for a
  personal agent, never for a project's worker credential. It is given per
  agent and per project, by an owner of that project or a superuser, not by the
  agent's own account unless that account is an owner; and it is capped, like
  every scope, by the present role of the agent's account, so the account must
  itself be a coordinator or an owner of the project.
- **Bound to its projects**: by the same check every web request passes today;
  an agent's request for a project it was not granted answers 404.
- **Revoked** by removing the scope from the agent, lowering or removing the
  account's role, disabling the agent or revoking its credential. Each is
  effective from the next request, because capabilities are computed from the
  present state on every request.
- **What it must NOT undo.** On the host route, removing a name from the
  operator list makes what it accepted or integrated stop counting, because the
  readers ask the present list. A coordinator who is replaced must not take its
  integrations with it. So a record written by a coordinator over the web
  carries, in the record, that it was written under the `coordinate` capability
  of that project; the endpoint writes that mark from the descriptor the
  service verified, and no caller can write it (the endpoint refuses an actor
  shaped like a web account from anybody but the service, and refuses forged
  machine records on the raw route). Readers count such a record as they count
  a listed operator's. This mark is the one new piece of mechanism in the
  design; its exact form is the first slice's to settle, with a review.
- **The audit** shows, per project: who was made a coordinator and by whom, each
  agent given or losing the scope and by whom, and every write made under the
  capability, as it shows writes today.

### Which actions get routes, and which stay on the server

Routes, each asking for the role named in the inventory: assign; comments;
merge slot; lifecycle scope and facts; guidance (read and acknowledge for
members, set and clear for a coordinator); reference and capability records
(lookup and propose for members, accept, retire and verify for a coordinator);
release records; handoff; proposal review and decision by a coordinator agent.

Staying with the installation's operator, who is the one person with SSH:

| Stays on the server | Why |
|---|---|
| Installing, upgrading and rolling back the kit; the service's start and stop | they replace the program that would answer |
| Backups, restore, the backup schedule | they read and write every project's data and the secrets file |
| The first superuser; `deployment.private.json` | the root of every other grant |
| Voiding and reverting records, the reconcile commands, record anchors | repairs after a fault; rare, and they decide which records count |
| Retiring a project, removing an unfinished creation | not reversible from the web |
| The rollout switches (`review-writes`, `checkpoint-provenance-writes`) | one per installation |
| The operator and verifier lists | they stay the host route's; the web does not read them for a coordinator |
| `authorized_keys` | sshd's file; with nobody else on SSH there is one line in it |

A fault that needs one of these needs James. That is the price of "nobody but
him", and the note does not hide it: if James is away, a void or a restore
waits.

### What an agent needs, and what a person needs

An agent that coordinates needs, beyond the routes:

- **One client.** `http_client.py` grows a command for each thing a coordinator
  does, named as `client.py` names them where one exists (`brief`, `work`,
  `review`, `comments`, `guidance`, `capability`, `ref`), so that the
  coordinator prompt and the worker guide change little. It stays one file of
  standard-library Python that is copied alone, and reads its secret from the
  agent's own file as the `curl` instructions do.
- **The reads it steers by**: brief, queue, my work, next action exist; the
  merge slot's state and a task's lifecycle facts come with their routes.
- **Idempotency it can rely on**: a write takes an `Idempotency-Key`, and an
  exact retry under the same key replays the first answer. The client makes
  the key, keeps it until it has an answer, and sends it again after a timeout;
  "outcome unknown" is answered by reading, as today.
- **No session to register**: the agent is the identity, and its name in the
  tracker is fixed by the service.

A person needs pages. The web interface has pages for work, a project, a task,
agents, proposals, requirements, setup and administration. It would gain: on
the task page, assign, comment, approve and request changes with the rule's
sentence when refused, and the lifecycle facts; on the project page, the queue
by state, the merge slot, guidance, and who coordinates (kittrial-5bb.198); a
records page for reference and capability records waiting to be accepted.

### Workers without SSH

Works today with a personal agent or a worker credential: next action, claim,
brief, checkpoint, contribute by branch on the code host, respond, request
changes, recommend, feedback, proposal, reading references and onboarding.

Does not work, and what closes it:

| Missing for a worker | Closed by |
|---|---|
| Registering a plan and posting evidence as comments | the comments route |
| Reading and acknowledging guidance | the guidance route |
| Capability lookup and find, proposing a record | the records routes |
| Delivering a bundle | not closed: a branch on the code host is the office's delivery |
| Recording `implemented` and `tested` | the lifecycle route, if kittrial-5bb.106 lets a contributor write them |
| Handoff | the handoff route |

Until then a worker over the web puts its plan in a checkpoint and its evidence
in the contribution summary and on the pull request.

### Integration without a checkout on the server

Nothing changes in who merges. The coordinator fetches the approved branch on
its own machine, merges, runs what it runs, and pushes to the code host. The
server never held the repository on the host route either: `lifecycle.py`
reads the caller's checkout on the caller's machine and sends facts.

What the server records, in order: the merge slot acquired for the task and
target; the lifecycle scope (source commit, integration commit, release,
environment); `reviewed` and `integrated`; the slot released; later `deployed`
and `live-verified`, usually for many tasks at once as a release record.

What the server can check from its own records, and will:

- the caller holds `coordinate` in the project, and holds the merge slot when
  it records `integrated`;
- the task's current contribution is approved by another account than its
  author's, with nothing unresolved;
- the source commit of the scope is the commit of that approved contribution.

What it cannot check: that the integration commit exists on the code host,
contains the source commit, or is on the branch it claims. The server has no
access to the code host and this design gives it none. The fact is the
coordinator's word, recorded with who said it and under which authority, which
is what it is today. A check against the code host is possible later (a
read-only token and one request) and is a question below.

## Slices

Each is one contribution. Slices 1 and 2 together let one agent coordinate one
project over the web from filing to integrated; after slice 1 alone it can file,
steer, approve and close, and an approved task shows "awaiting integration"
until it is closed.

1. **The coordinator role, the agent scope, and nobody passes their own
   account's work on approval.** The role and its capability; the scope for a
   personal agent; the approval rule with its sentence (this is
   kittrial-5bb.199, made general); the mark in records written under the
   capability; the audit entries; documents. The test that matters: an agent
   with the scope approves another account's contribution, and is refused its
   own account's, in a project it was granted and in no other.
2. **The merge slot and the lifecycle facts.** Routes for the slot and for the
   scope and facts, with the checks above and kittrial-5bb.106's list of who
   writes which fact; the second refusal (reviewed and integrated need another
   account's approval). The agent's next action learns "integrate".
3. **Comments and assign.** A comment route for a contributor; assigning and
   unassigning for a coordinator, with what the task's holder is told.
4. **Guidance.** Read and acknowledge for members; set, clear and status for a
   coordinator.
5. **Reference and capability records.** Lookup, find, propose and revise for
   members; accept, retire and verify for a coordinator.
6. **The client.** `http_client.py` commands for everything above, and the web
   variants of the coordinator prompt and the worker guide. Earlier slices add
   their own command as they go; this one closes the gaps and writes the guides.
7. **Release records** (many tasks, deployed, live-verified) and the read a
   release needs in place of a tracker export.
8. **Handoff**, and proposal review and decision by a coordinator agent.
9. **Pages** for a person: task page actions, the project's queue, slot,
   guidance and coordinators (with kittrial-5bb.198), the records page.

Not in any slice: a check against the code host; per-project rollout switches;
voids and reverts over the web.

How this sits with what is filed: kittrial-5bb.193 to .197 are the SSH route
and are not needed for the office; .193 is built and wanted for koopa's lanes.
kittrial-5bb.192 (the operator and verifier lists audited) is independent.
kittrial-5bb.198 is slice 9's first page. kittrial-5bb.199 is in slice 1.
kittrial-5bb.106 decides the list slice 2 uses. kittrial-5bb.202 (a project
without a merge slot) should be done before slice 2 reaches an old project.
kittrial-5bb.200 (the set-up page shows a backup command that cannot be pasted)
is a fault of a page that exists and is independent of all of this; it matters
here only because backups stay James's to run.

## What James can do today, on release 1008t

For each office project, with no SSH for anybody but James:

1. Create the project in the web interface (New project), or register one made
   on the host.
2. Create two accounts for it, a coordinating one and a working one, and make
   the coordinating account the project's owner and the working one a
   contributor. (Two accounts, because a recommendation from the same account
   as the author is not counted.)
3. Sign in as each account once and make its agents (Agents, New agent), with
   the project granted; put each secret in its agent's file as the page says.
4. The working agents then do a worker's whole job over the web: next action,
   claim, checkpoint, contribute a branch on the code host, respond.
5. The coordinating agent files tasks, reads the queue and the briefs, requests
   changes, and **recommends**. It cannot approve.
6. A person approves: James as superuser, or whoever signs in as the
   coordinating account. The queue shows which contributions carry a
   recommendation, so this is one decision and one click each.
7. The coordinating agent merges on its own machine, pushes, and closes the
   task. Nothing records the integration; the pull request on the code host is
   the record.

What is missing at each step is the list under "What follows from it". The
largest: a person in the loop for every approval, no integration record, no
comments, no guidance.

## Questions for James

Each has a recommendation; none needs the code.

1. **Is a lane an account?** Each coordinator and each group of workers gets an
   account of its own, and "nobody passes their own work" means their own
   account's. The alternative is a lane label that you set on each agent, which
   lets all agents hang under your one account but adds a second notion of
   "same party" that somebody must keep honest. One consequence to accept with
   it: a person cannot approve the work of an agent made under their OWN
   account, so agents belong under lane accounts and not under yours; as
   superuser you can still approve anything of any lane. kittrial-5bb.199 has
   to decide the same point (an owner approving their own agent's work) and
   should decide it the same way. *Recommendation: accounts.*
2. **Who makes a lane's agents?** Today only the account itself can. Either you
   sign in as each lane account once, or a superuser is allowed to make an
   agent for another account. *Recommendation: let a superuser do it, as part
   of slice 1; it is a small change and saves you a password per lane.*
3. **Who may name a coordinator: you alone, or each project's owner?**
   *Recommendation: an owner or a superuser. An owner can already approve
   everything in the project; a coordinator can do less.*
4. **May an agent approve at all?** Today none can, by a rule that was made on
   purpose. This design lifts it for one scope that an owner grants to one
   agent in one project. Without it, a person approves every contribution.
   *Recommendation: yes, with that scope; it is what your kittrial coordinator
   does on the host route today.*
5. **Should the server check an integration against the code host?** It would
   need a read-only token for each repository and network access from the
   office server. *Recommendation: no, not now; record the coordinator's word
   as today, and decide again when the office has run this for a while.*
6. **Is it acceptable that voids, reverts, restores and the switches need you?**
   They are rare, but they wait for you when you are away. *Recommendation: yes
   for now; if it hurts, a second trusted person gets SSH, not a web route.*
7. **Bundles.** Delivery by bundle needs a file on a server. *Recommendation:
   the office delivers by branch on the code host only.*
8. **Order.** Slices 1 and 2 first, then comments and guidance (3 and 4), then
   records, client, releases, handoff, pages. *Recommendation: as listed; say
   if pages for people matter more to you than an agent's reach, and 9 moves
   up.*
