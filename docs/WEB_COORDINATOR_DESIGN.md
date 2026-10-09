# A coordinator that works only over the web

**Status: design note for James to decide on (kittrial-5bb.201), second version
after review. Nothing here is built, and this task changes no behaviour.**

The goal, in James's words of 2026-10-07: much more in the web interface, several
coordinators across several projects, and **nobody but him needing SSH to the
server**. For the office this replaces the route that
[COORDINATORS_PER_PROJECT_DESIGN.md](COORDINATORS_PER_PROJECT_DESIGN.md)
recommends (coordinators on confined SSH keys). That design stays right for an
installation whose lanes already have SSH, and its rules must hold here too: a
caller is bound to its projects and to its lane, and nobody passes their own
work.

Everything under "What is true today" was read in the code of main `02740dd`
(release 1008t); line numbers are that commit's. I ran nothing. The reviewer of
the first version ran the inventory on a scratch stack at `02740dd`; where this
version says "run", it is the reviewer's run, and its corrections are in.

## The answer in short

- **A worker needs no SSH today** to find work, claim it, write checkpoints,
  deliver a contribution as a branch on the code host, answer a review and
  close a task. It cannot read the project's onboarding text or the guidance,
  and it cannot comment.
- **A coordinator cannot do its work over the web today**, for three reasons:
  1. *An agent can never approve.* Approval needs a capability that every
     credential is denied, whoever owns it. Only a signed-in person who is an
     owner of the project, or a superuser, approves.
  2. *The web has no route for half of the loop*: assigning a task to somebody
     else, comments, the merge slot, lifecycle facts (so nothing is ever
     recorded integrated, deployed or live-verified), release records, handoff,
     guidance, and proposing or accepting reference and capability records.
  3. *Nothing on the web says who may not approve what*: an owner may approve
     their own contribution, and work delivered under a worker credential is
     nobody's.
- **The design** is four things:
  1. **A lane is an account.** An account, its agents, and the worker
     credentials it issued are one party. Nobody approves work of their own
     party, and nobody records it reviewed or integrated without another
     party's approval. A coordinator lane and a worker lane are two accounts.
  2. **A project role `coordinator`**, between contributor and owner, and a
     **grant** by which a project's owner (or a superuser) lets one named agent
     act as coordinator in that project. The grant is new mechanism: an agent's
     scopes today sit on its credential, one list for all its projects.
  3. **Web routes for the missing actions, each with its page**, in the order
     given under Slices, so that a person in a browser gains with every slice.
  4. **The server records integration and does not perform it.** The
     coordinator merges on its own machine and pushes to the code host; the
     server checks what it can check from its own records. A record written by
     a coordinator carries a mark of that authority, so that replacing a
     coordinator voids nothing: the second piece of new mechanism.
- **For James, concretely: your agents sit under lane accounts, not under your
  own account.** Two lane accounts serve the whole installation, not two per
  project: one for coordinators, one for workers. For three projects with a
  coordinator and two workers each: 2 accounts, 6 member settings, 9 agents
  with their secrets, 3 coordinator grants. You approve nothing day to day; as
  superuser you still can.
- **What stays with James on the server**: installing and upgrading the kit,
  backups and restore, the first superuser, voiding and reverting records,
  reconciling, retiring a project, and the rollout switches.
- **Today, on release 1008t**, a person can coordinate in a browser and agents
  can work, with no SSH for anybody else; a coordinator *agent* can do
  everything except approve, assign and record integration. The steps and the
  gaps are under "What James can do today".

## What is true today

### Who may do what on the web

| Fact | Where |
|---|---|
| Three project roles: viewer (read), contributor (tasks, checkpoints, reviews, feedback, proposals), owner (those plus `reviews.approve` and `project.admin`). | `http_authority.ROLE_CAPABILITIES`, 356-362 |
| A credential, a worker credential or a personal agent's, never holds `reviews.approve`, `project.admin`, project creation, account administration or agent management, whatever its issuer's role. | `CREDENTIAL_FORBIDDEN_CAPABILITIES`, 363-365; applied in `credential_capabilities`, 395-411 |
| A credential's capabilities are its scopes (`read`, `tasks`, `checkpoints`, `reviews`, `feedback`, `proposals`), capped by its issuer's present role in that project. | `SCOPE_CAPABILITIES`, 344-353 |
| An agent's scopes are given when it is made, by its own account; they are one list for all the projects it is granted, and they cannot be changed afterwards (a change of `scopes` is refused, 422). | `AGENT_CREATE_FIELDS`, `AGENT_UPDATE_FIELDS`, `http_service.py` 3520-3523; run by the reviewer |
| A credential lasts 30 days. The next one is issued by the agent's own account or by a superuser. | `http_auth.CREDENTIAL_TTL_SECONDS`, 66; `agents_credential_issue`, 4768-4776 |
| Approval asks for `reviews.approve`; every other review operation for `reviews.write`. The route compares the approver with nobody. | `http_service.reviews_add`, 5833-5842 |
| `recommend` is refused (403) from the same PERSON as the contribution's author or the task's assignee; an account and all its agents are one person. | 5886-5896; `_independent`, 3615-3625 |
| A worker credential is nobody's person: only an agent is mapped to its owner. An owner delivered under a worker credential they had issued, their own agent recommended that work (201) and they approved it (201). | `http_auth.actor_person`, 2229-2239; run by the reviewer |
| An agent is made by a signed-in account for itself; nobody makes an agent for another account. | `agents_create`, 4642-4663 |
| The actor a web caller writes under is fixed by the service: the account's own, or a name inside the credential's namespace. | `http_auth.bind_actor`, 1675-1689 |
| An owner sets a project's members; only a superuser makes or unmakes an owner. | `http_auth.set_member`, 1939-1947 |
| Changes are audited per project, readable by owners. | `GET /v1/projects/{id}/audit`, 6155 |

### The coordinator's loop, action by action

"Host" is `endpoint.py` over SSH with `client.py`, `lifecycle.py` and
`coordination.py`, or a host command (`admin.py`) where marked. "Web" is the 65
routes of `http_service.py`. The last column is this design, with its slice.

| Action | Host today | Web today | In this design |
|---|---|---|---|
| File a task | `create` | `POST .../tasks` (5204): title, description, priority | as today |
| Assign it to somebody else | `update ID --assignee NAME` | **none**: a task change takes title, description, status, priority (3529); a caller can only claim for itself | route and page, coordinator (3) |
| Claim | `update ID --claim` | `POST .../tasks/{id}/claim` (5768) | as today |
| Read: list, task, brief, history, queue, my work, an agent's next action | `list`, `show`, `brief`, `history`, `work` | all there (5246, 5318, 5651, 5947, 5969, 5991, 4620) | as today |
| Comment (a plan, evidence, a note) | `comments add` | **none**: no route writes a plain comment | route and page, contributor (3) |
| Checkpoint | `checkpoint` | `POST .../checkpoints` (5797); no page writes one | as today; a form on the task page (3) |
| Contribute, respond | `review` | `POST .../reviews` (5833) | as today |
| Request changes | `review` | there, for a contributor and for a credential | as today |
| Recommend | `review` | there; refused from the author's own account | as today |
| Approve | `review`, any name | a signed-in owner or superuser only; **never an agent**; their own work included | a coordinator, person or granted agent; never their own party's work (1a, 1b) |
| Withdraw, request a review | `review` (behind the installation's `review-writes` switch) | there, behind the same switch | as today |
| Merge slot: check, acquire, release | `coordination.py` | **none** | route and page, coordinator (2) |
| Release a slot whose holder has gone | none: the kit has no forced release | **none** | an owner or superuser, audited (2) |
| Lifecycle scope and the six facts | `lifecycle.py record` | **none** | route and page, coordinator (2) |
| Release record for many tasks, deploy, live-verified | `lifecycle.py release`, with a checkout and an export on the caller's machine | **none** | route (7) |
| Close, reopen | `close`, `reopen` | task change, status `open` or `closed` (3554). A task closed while approved goes on reading "awaiting integration" and stays in the queue (run). | closed tasks leave that queue (2) |
| Handoff: ask, accept, decline | `handoff` | **none** | route (8) |
| The operator's transfer of a task | `admin.py handoff` (host command) | **none** | the assign route covers it (3) |
| Guidance: read, acknowledge | `guidance get`, `ack` | **none** | route and page, any member (4) |
| Guidance: set, clear, status | `admin.py set-guidance` (host command) | **none** | route and page, coordinator (4) |
| Onboarding text: read | `onboard` | **owners only**: the read is the owner's edit read and asks `project.admin` (4358-4365); a contributor, a viewer, every agent and a worker credential get 403, and no other route carries the text (run) | a read for every member (4) |
| Onboarding text: set | `admin.py set-onboarding` (host command) | an owner, 4376 and 4396 | also a coordinator (4) |
| Reference records: find, read | `ref find`, `ref get` | read only (5352, 5379) | as today |
| Reference and capability records: propose, revise | `ref`, `capability` | **none** | route, contributor (5) |
| The same: accept, retire, verify | `admin.py reference-apply`, `capability-apply`, `capability-verify` (host commands) | **none** | route and page, coordinator (5) |
| Capability lookup and find | `capability find`, `lookup` | **none** | route, any member (5) |
| Requirement proposals: submit | `proposal` | `POST .../proposals` (5474) | as today |
| Proposals: review and decide | `admin.py proposal-review`, `proposal-decide` (host commands) | a signed-in person with `reviews.approve`; a credential is refused (5565-5578) | also a granted coordinator agent (8) |
| Feedback | `feedback` | the routes exist (6126, 6142) and answer **501** on the real backend, read and write (2078; run) | not in this design: an existing gap, said under Slices |
| Sessions: register, resume, runs | `session` | none, and none needed: an agent is the identity | not built |
| Members, worker credentials, agents, archive | not on the host route | there (4453-4826) | as today, plus the coordinator role and grant (1b) |
| Renewing an agent's credential | not on the host route | every 30 days, by the agent's account or a superuser | longer life, a warning before expiry, renewal by a project's owner (6) |
| Project creation | `admin.py add-project` | there (3937), for an account with the project-creation right; never a credential | as today |
| Audit | none for most host commands | per project, owners (6155) | also shows coordinator grants |

### What follows from it

- Over the web nobody, person or agent, can record that anything was
  integrated. A task that is approved reads "awaiting integration" for good,
  closed or not.
- An agent cannot be a coordinator. The nearest it gets is `recommend`, which a
  person then turns into an approval.
- A worker over the web cannot follow the kit's own rules for workers where
  they need a route that is missing: read the onboarding text, read and
  acknowledge guidance, register a plan as a comment before building, post
  evidence as a comment, look a capability up before searching, propose a
  record for a miss.
- The independence rule has a hole: work under a worker credential belongs to
  nobody, so its issuer can approve it and the issuer's agent can recommend it.
- A contribution can be delivered with only a web credential when it is a
  branch on the code host (`delivery.kind: remote`). A `bundle` delivery names a
  file path on a server, which somebody must put there: that needs SSH and is
  not for the office.
- Every agent stops after 30 days until a person issues its next credential.
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
`recommend` is judged by it. This design uses it as the lane, and closes its
hole.

- **One party is**: an account; every agent that account made; every worker
  credential that account issued. (The last is new: today a worker credential
  is nobody's.)
- **Nobody passes work of their own party**, in two refusals, as rule 3 of the
  coordinators design has them:
  - *Approval* is refused when the approver's party is the party of the
    contribution's author or of the task's assignee.
  - *Reviewed and integrated* are recorded for a task only when its current
    contribution carries an approval, with nothing unresolved, from another
    party than its author's.
- **A superuser is bound like anybody else** for their own party's work: the
  rule is about whose work it is, not about rank. A superuser may approve any
  work that is not their own party's, in any project. (The first version of
  this note said both things; this is the one meant. It is a question below.)
- The refusal names the rule and says what to do: "This contribution was
  delivered by your own account (you, one of your agents, or a worker
  credential you issued). Another owner or coordinator of this project, or a
  superuser, must approve it."

This is kittrial-5bb.199 made general, and the two must not disagree. What
.199 has to decide to agree with this note: an account does not approve work by
itself, by its agents, or under a worker credential it issued; a superuser is
not exempt for their own party; the sentence above is what a sole owner is
told; and the rule applies on every project at once (see slice 1a for what
that changes).

Why not a lane label on each agent instead: it would be a second notion of
"same party" beside the one `recommend` already uses, and somebody would have
to keep the labels honest. With accounts, an agent's lane is who made it, which
the service already knows and nobody can edit.

**How James alone runs three projects with it.** His agents do not sit under
his own account: under it he could approve none of their work, and no agent of
his could recommend another's. They sit under lane accounts, and two are enough
for the whole installation, because an agent is confined by the projects
granted to it and not by its account:

| | Account `coordinators` | Account `workers` |
|---|---|---|
| Role in each of the three projects | coordinator | contributor |
| Agents | one per project, granted that project, with the coordinator grant there | two per project, granted that project |
| What its agents may do | steer, approve the workers' contributions, integrate | the worker's job |

2 accounts, 6 member settings, 9 agents and secrets, 3 coordinator grants.
What this arrangement does not give: two worker agents under one account cannot
recommend each other's work (they are one party); only the coordinators'
account can pass it. If workers are to review each other, they need two worker
accounts. And every agent of the `coordinators` account is the same party, so a
coordinator agent that delivers work itself needs James, or a second
coordinating account, to approve it.

What it costs: the two accounts must exist, somebody makes their agents (today
only the account itself can; see the questions), and credentials must be
renewed (below).

### The coordinator role and the grant

- **A project role `coordinator`**, ranked between contributor and owner:
  everything a contributor may do, plus `reviews.approve`, plus a new
  capability `coordinate` that the new routes ask for, plus reading and setting
  the onboarding text. Not the rest of `project.admin`: members, worker
  credentials, archive and the audit stay the owner's. An owner holds
  `coordinate` too, as a superuser does.
- **Who makes an account a coordinator**: an owner of the project or a
  superuser, with the existing member route. Only a superuser makes an owner,
  as today.
- **An agent as coordinator needs a grant, and the grant is new mechanism.**
  An agent's scopes today are fixed when it is made, by its own account, for
  all its projects at once; nothing a project's owner controls. So this is not
  a scope. It is a record kept with the project: "agent A may coordinate here",
  written by an owner of that project or a superuser, never by the agent's own
  account unless that account is an owner of the project, and removable by the
  same people. In that project, and only there, the agent then holds what its
  account's role allows, `reviews.approve` and `coordinate` included, provided
  the account is itself a coordinator or an owner there. It is the one place
  where the rule "a credential never approves" is lifted, and only for a
  personal agent: a project's worker credential can never be granted.
- **Bound to its projects**: by the check every web request passes today; an
  agent's request for a project it was not granted answers 404.
- **Revoked** by removing the grant, lowering or removing the account's role,
  disabling the agent or revoking its credential. Each is effective from the
  next request, because capabilities are computed from the present state on
  every request.
- **The audit** shows, per project: who was made a coordinator and by whom, each
  grant given or removed and by whom, and every write made under the
  capability, as it shows writes today.

### The mark: replacing a coordinator voids nothing

On the host route, removing a name from the operator list makes what it
accepted or integrated stop counting, because the readers ask the present list.
A coordinator who is replaced must not take its integrations with it. So an
`integrated` fact recorded by a coordinator over the web carries, in the
record, that it was written under the `coordinate` capability of that project.
The endpoint writes that mark from the descriptor the service verified, and no
caller can write it (the endpoint refuses an actor shaped like a web account
from anybody but the service, and refuses forged machine records on the raw
route). The readers that today ask "was this recorded by a listed operator"
count a marked record the same way.

This is the second piece of new mechanism. It is read only where `integrated`
is read (which commit is an acceptable base for the next contribution), so it
belongs to the slice that records integration, and its exact form is that
slice's to settle, with a review.

### Credentials that do not stop a project

A credential lasts 30 days, and then the agent stops until a person issues the
next: for nine agents, nine renewals a month, and a coordinator that expires
stops its project. Proposed, as one slice:

- the lifetime becomes a setting of the installation, with a longer default
  for an office (90 days is proposed; James chooses);
- the web interface and the agent's own "who am I" answer show when a
  credential expires, and the project page warns for the last seven days;
- an owner of a project may issue the next credential for an agent that is
  granted that project, as a superuser may today, so that a renewal does not
  need the lane account's password. The new secret is shown once to whoever
  issued it, and reaches the agent's machine as it does today.

Who renews, and how long a credential lasts, is a question below.

### Which actions get routes, and which stay on the server

Routes, each asking for the role named in the inventory, each with its page:
assign; comments; merge slot, with a forced release; lifecycle scope and facts;
guidance; onboarding read for members; reference and capability records;
release records; handoff; proposal review and decision by a granted agent.

Staying with the installation's operator, who is the one person with SSH. This
is settled by James's own words (nobody but him needs SSH), not asked again:

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
him": if he is away, a void or a restore waits. Likewise delivery is by branch
on the code host only: a bundle is a file on a server.

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

A person needs pages, and gets them with the routes, not after them: every
slice below that adds a route adds the place in the web interface where a
person uses it, and says what a human coordinator can do in the browser once it
is in. The web interface has pages for work, a project, a task, agents,
proposals, requirements, setup and administration to build on.

### Workers without SSH

Works today with a personal agent or a worker credential: next action, claim,
brief, checkpoint, contribute by branch on the code host, respond, request
changes, recommend, proposal, reading references.

Does not work, and what closes it:

| Missing for a worker | Closed by |
|---|---|
| Reading the onboarding text | the members' read (slice 4) |
| Reading and acknowledging guidance | the guidance route (slice 4) |
| Registering a plan and posting evidence as comments | the comments route (slice 3) |
| Capability lookup and find, proposing a record | the records routes (slice 5) |
| Feedback | not closed here: the routes answer 501 on the real backend, an existing gap of its own |
| Delivering a bundle | not closed: a branch on the code host is the office's delivery |
| Recording `implemented` and `tested` | not at first: slice 2 gives every fact to the coordinator; kittrial-5bb.106 may loosen it |
| Handoff | the handoff route (slice 8) |

Until then a worker over the web puts its plan in a checkpoint and its evidence
in the contribution summary and on the pull request, and is told what the
onboarding text says by whoever set its agent up.

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
- the task's current contribution is approved by another party than its
  author's, with nothing unresolved;
- the source commit of the scope is the commit of that approved contribution.

This closes on the web a gap that is open on the host route today, where any
name records any fact on any task with no approval (kittrial-5bb.106).

What it cannot check: that the integration commit exists on the code host,
contains the source commit, or is on the branch it claims. The server has no
access to the code host and this design gives it none. The fact is the
coordinator's word, recorded with who said it and under which authority. A
check against the code host is possible later and is a question below.

**A slot whose holder has gone.** A coordinator agent that is disabled, or
whose credential has expired, while it holds the merge slot leaves the project
unable to integrate; the kit has no forced release anywhere. An owner of the
project or a superuser may release the slot, with a reason, audited.

## Slices

Each is one contribution and brings its page. After each, what a human
coordinator can do in a browser is said.

- **1a. Nobody approves their own party's work** (this is kittrial-5bb.199).
  The party rule for approval, a worker credential counted with the account
  that issued it, the superuser bound for their own party, the sentence.
  **As built, it is behind an installation setting that is off by default**
  (`approval_by_another_party`; decided after this note, question 7 below): an
  installation that turns nothing on behaves as before. Where it is turned on,
  an owner who approves their own contribution is refused from the next start,
  and a project with one account that both delivers and approves needs a
  second owner, or a superuser who is not that account. What changes on that
  day is in HTTP_DEPLOYMENT.md, "Nobody approves work of their own party".
  *In the browser:* the Approve button answers with the sentence when it is the
  person's own party's work.
- **1b. The coordinator role and the grant.** The role with its capability;
  the grant kept with the project and its routes; the audit entries; the
  members page offers the role and the project's agents page gives and removes
  the grant. The test that matters: a granted agent approves another party's
  contribution in the project it was granted and is refused its own party's,
  refused in a project where it has no grant, and refused again the moment the
  grant is removed.
  *In the browser:* a person who is a coordinator and not an owner can approve
  and request changes; an owner sees and controls which agents coordinate.
- **2. The merge slot, the lifecycle facts, and the mark.** Routes and page
  parts for the slot (with the forced release) and for the scope and the six
  facts, with the checks above; the second refusal (reviewed and integrated
  need another party's approval); the mark in an `integrated` record and its
  readers; closed tasks leave the awaiting-integration queue. Built with the
  strictest reading, so that it need not wait: every fact is a coordinator's
  to write (kittrial-5bb.106 may loosen that later), and a project without a
  merge slot is told to create one (what kittrial-5bb.202 asks for) by the slot
  route itself. The agent's next action learns "integrate".
  *In the browser:* the task page shows the facts and has "record integrated";
  the project page shows the slot, who holds it, and Release. From here one
  agent, or one person, coordinates a project from filing to integrated.
- **3. Comments, assign, and a checkpoint from a page.** A comment route for a
  contributor; assigning and unassigning for a coordinator, with what the
  task's holder is told.
  *In the browser:* a comment box, an assign control and a checkpoint form on
  the task page.
- **4. Guidance and onboarding.** Guidance: read and acknowledge for members;
  set, clear and status for a coordinator. Onboarding: a read for every member
  and every agent of the project; set by a coordinator as well as an owner.
  *In the browser:* a guidance panel on the project page with who has
  acknowledged; workers' agents can at last read both.
- **5. Reference and capability records.** Lookup, find, propose and revise for
  members; accept, retire and verify for a coordinator.
  *In the browser:* a records page with what waits to be accepted.
- **6. Credentials that do not stop a project.** The lifetime setting, the
  expiry shown and warned of, renewal by a project's owner; and, if James says
  yes to question 2, a superuser making an agent for another account.
  *In the browser:* the agents page shows expiry and renews.
- **7. Release records** (many tasks, deployed, live-verified) and the read a
  release needs in place of a tracker export.
  *In the browser:* a release page.
- **8. Handoff**, and proposal review and decision by a granted agent.
- **9. The client.** `http_client.py` commands for everything above, and the
  web variants of the coordinator prompt and the worker guide. Earlier slices
  add their own command as they go; this one closes the gaps and writes the
  guides.

Order: 1a, 1b and 2 take the person out of every approval and make integration
a record; they are the "agents first" order. If more in the browser matters
more to James than an agent's reach, the "pages first" order is 1a, then 3 and
4 (comments, assign, checkpoint, guidance, onboarding: all usable by a person
who is an owner today), then 1b and 2. It is the last question.

Not in any slice: feedback on the real backend (501 today; a gap of its own,
older than this note); a check against the code host; per-project rollout
switches; voids and reverts over the web.

How this sits with what is filed: kittrial-5bb.193 to .197 are the SSH route
and are not needed for the office; .193 is built and wanted for koopa's lanes.
kittrial-5bb.192 (the operator and verifier lists audited) is independent.
kittrial-5bb.198 (the web shows who coordinates) is part of slice 1b's page.
kittrial-5bb.199 is slice 1a. kittrial-5bb.106 may later loosen who writes
which fact; slice 2 does not wait for it. kittrial-5bb.202 (a project without a
merge slot) is met by slice 2's slot route for the web; its host half stays its
own. kittrial-5bb.200 (the set-up page shows a backup command that cannot be
pasted) is a fault of a page that exists and is independent; it matters here
only because backups stay James's to run.

## What James can do today, on release 1008t

With no SSH for anybody but James, for all three office projects at once:

1. Create each project in the web interface (New project), or register one made
   on the host.
2. Create two accounts for the whole installation, a coordinating one and a
   working one. Make the coordinating account an owner of each project (only a
   superuser can) and the working one a contributor of each. Two accounts,
   because a recommendation from the same account as the author is refused
   (403).
3. Sign in as each account once and make its agents (Agents, New agent), each
   granted its one project; put each secret in its agent's file as the page
   says. Every 30 days each agent needs a new credential, which you can issue
   as superuser without signing in as the lane account.
4. The working agents then do a worker's job over the web: next action, claim,
   checkpoint, contribute a branch on the code host, respond. They cannot read
   the onboarding text or the guidance over the web: put what they need in
   each agent's own instructions.
5. The coordinating agent files tasks, reads the queue and the briefs, requests
   changes, and **recommends**. It cannot approve, and it cannot assign a task
   to a particular worker: a worker claims it.
6. A person approves: you as superuser, or whoever signs in as the coordinating
   account. The queue shows which contributions carry a recommendation, so this
   is one decision and one click each.
7. The coordinating agent merges on its own machine, pushes, and closes the
   task. Nothing records the integration; the pull request on the code host is
   the record, and the closed task goes on reading "awaiting integration" in
   the queue.

What is missing at each step is the list under "What follows from it". The
largest: a person in the loop for every approval, no integration record, no
comments, no guidance or onboarding for workers, and nine renewals a month.

## Settled by James's own words, not asked

- Voids, reverts, restores, upgrades and the switches need James on the server.
  If that hurts, a second trusted person gets SSH, not a web route.
- Delivery is by branch on the code host; a bundle needs a file on a server.

## Questions for James

Each has a recommendation; none needs the code.

1. **Is a lane an account?** Your agents then sit under lane accounts and not
   under your own (two accounts serve all three projects), and "nobody passes
   their own work" means their own account's, its agents' and its worker
   credentials'. The alternative is a lane label that you set on each agent,
   which lets all agents hang under your one account but adds a second notion
   of "same party" that somebody must keep honest. *Recommendation: accounts.*
2. **Who makes a lane's agents?** Today only the account itself can, so you
   would sign in as each lane account once. *Recommendation: let a superuser
   make an agent for another account (slice 6); it saves you a password per
   lane.*
3. **Who may name a coordinator: you alone, or each project's owner?**
   *Recommendation: an owner or a superuser. An owner can already approve
   everything in the project; a coordinator can do less.*
4. **May an agent approve at all?** Today none can, by a rule that was made on
   purpose. This design lifts it for an agent that a project's owner has
   granted, in that project only. Without it, a person approves every
   contribution. *Recommendation: yes, with that grant; it is what your
   kittrial coordinator does on the host route today.*
5. **Should the server check an integration against the code host?** It would
   need a read-only token for each repository and network access from the
   office server. *Recommendation: no, not now; record the coordinator's word
   as today, and decide again when the office has run this for a while.*
6. **Is a superuser bound by the rule for work of their own account?** If yes,
   you cannot approve what you, or an agent under your own account, delivered;
   another owner or coordinator must. If no, a superuser is the one person who
   can pass their own work. *Recommendation: bound. With your agents under
   lane accounts it costs you nothing, and the rule then has no exception to
   explain.*
7. **May the approval rule change behaviour where no coordinator exists?** As
   first written, the rule of slice 1a applied to every project the day the
   kit is upgraded. *Recommendation then: everywhere, at once.* **Decided
   otherwise, and built so (kittrial-5bb.199): a setting of the installation,
   off until it is turned on.** No installation changes by upgrading. An
   agent that holds the coordinator grant of slice 1b is the exception: it
   never approves its own party's work, whatever the setting says.
8. **Who renews credentials, and how long should one last?** Today 30 days,
   renewed by the agent's account or a superuser: nine renewals a month for
   three projects, and an expired coordinator stops its project.
   *Recommendation: 90 days as the office's setting, a warning in the web
   interface for the last seven, and renewal by a project's owner as well as
   by you.*
9. **Agents first, or pages first?** Agents first (1a, 1b, 2) takes the person
   out of every approval and records integration; a human coordinator gains
   little in the browser until slice 3. Pages first (1a, 3, 4, then 1b, 2)
   gives people comments, assigning, checkpoints, guidance and onboarding in
   the browser early, and you go on approving for longer. *Recommendation:
   agents first, because the approvals are what cost you time every day; say
   if you would rather see the browser fill up first.*
