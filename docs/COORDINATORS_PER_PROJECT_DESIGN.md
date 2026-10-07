# Several projects on one installation, each with its own coordinator

**Status: design note (kittrial-5bb.178), second version after review; James
decided the seven questions on 2026-10-07 ("recommended for all"). Built so far:
slice 1, a key names its projects (kittrial-5bb.193; see "Bind a key to its
projects" in [OPERATIONS.md](OPERATIONS.md)). The rest of this note describes
the design and the state of main when it was written, not what is built.**

Everything under "What is true today" was read in the code on main `2d321be`;
I ran nothing for this note. What was run is the coordinator's and the
reviewer's: an office installation of release `ff83602` in a container (note of
2026-10-07 on the task), and the review of the first version of this note, which
checked the description of today against `2d321be` on a scratch runtime and
found it accurate; its corrections are in. Line numbers are those of `2d321be`.

## The answer in short

- **Today nothing on the host route separates projects or people.** A caller
  who reaches `endpoint.py` writes its own actor name and its own project name
  into the request, and neither is checked against anything. The operator list
  is one list for the installation, and on the endpoint it gates little.
  Three coordinators on one installation are kept apart by instruction only.
- **Today nothing on the host route asks for a review either.** A session can
  contribute on its own task and, with no approval at all, take the merge slot,
  record the task reviewed, integrated, deployed and live-verified, and close
  it; every call succeeds (run by the reviewer on `2d321be`).
- **The web route does separate projects and people**, but a coordinator cannot
  do its work there: there is no web route for assigning to another, comments,
  the merge slot, lifecycle facts, handoff, or accepting reference and
  capability records.
- **Whoever has the service user's shell is outside every rule of the kit**, on
  every project, whatever is built. Every coordinator has that shell today. So
  the first two conditions of any confinement are not code:
  1. a coordinator connects with its own SSH key, confined to the endpoint,
     and **no longer runs on the server or with a shell there**;
  2. **the keys are kept apart on the machine the agents run on**: an agent
     that can read another agent's private key or client folder is that agent
     too.
- **The smallest design** is then three rules on the host route:
  1. a confined key names the projects it may use, and the endpoint refuses any
     other project;
  2. a confined key names who it belongs to (a *principal*), and may only act
     as actors that principal registered;
  3. between principals, nobody passes their own work: an approval is refused
     from the principal of the author or the assignee, and a task is not
     recorded reviewed or integrated unless another principal approved its
     contribution.
- With rules 1 and 2 **the one operator list needs no copy per project**: a
  listed name can then be used only by the key of the principal that owns it,
  in the projects of that key. A list per project is described as an option
  and not recommended.
- It is built in slices that each stand alone; the first one (rule 1) already
  confines every agent with a confined key, coordinator or worker, to its own
  projects.

## What is true today

### The host route (`endpoint.py`, over SSH or run locally)

| Fact | Where |
|---|---|
| The project is whatever the request names. The only checks are that the directory exists, is initialized and is not an unfinished web creation. | `endpoint.execute`, lines 423-430 |
| The actor is whatever the request names. Only its shape is checked. A name shaped like a web account is refused unless the web service launched the endpoint. | lines 431-436, 458 |
| No registration is needed to act. Only `session resume` and `session run` look the actor up in the project's registry (`projects/NAME/.sessions.json`). `sessions.py` opens with "Attribution, not authentication." | `sessions.py` |
| The SSH forced command fixes the runtime root and the endpoint and nothing else: not the project, not the actor. `admin.py authorized-keys` says so itself: "Confinement binds the key to the endpoint, not to an actor". | `ssh_forced_command.py`; `admin.py` 4424-4426 |
| The operator list and the verifier list are flat lists of names in `deployment.private.json`, one per installation ("deployment-wide authority for every project"). | `admin.operators`, `admin.verifiers` |
| No check anywhere on this route depends on which project is named. | the whole of `execute` and the modules it calls |

What each action asks of the caller on this route:

| Action | Who may do it today |
|---|---|
| Create a task, assign it to anybody, close it, reopen it, comment (`bd`) | any name. The guards are about what is written (reserved labels, record anchors, the merge slot row, forged machine records), never about who writes. |
| `review contribute`, `respond` | the name that is the task's assignee (`review_workflow.py` 1923) |
| `review approve` | **any name, the contribution's own author included.** Nothing compares the approver with anybody (1966). `docs/REVIEWS.md` says it: "`approve` itself does not refuse the contribution's author." |
| `review request-changes` | any name |
| `review recommend` | any name except the author and the assignee (`review_recommendations.py`) |
| `review withdraw`, `request-review` | the author (or the assignee), **or a name on the operator list** (1939-1949) |
| Merge slot: create, check, acquire | any name. Release: the name that holds it (`coordination.py` 395). |
| Lifecycle facts (implemented ... live-verified, scope, release) | any name on any task, **approved or not**; the payload's actor must equal the request's actor and that is all (`lifecycle.py` 1478). Neither `lifecycle.py` nor `coordination.py` looks at the review. This is kittrial-5bb.106. |
| Handoff | the current owner hands off; the receiver accepts. The operator's transfer is a host command. |
| Checkpoint | any name; directions only from the assignee |
| Propose or revise a reference, a capability, a proposal, a requirement draft | any name. Accepting, deciding, voiding and verifying are host commands. |
| `guidance status` | a name on the operator list. It is the only action the endpoint itself refuses for want of that list. |

What the operator list does on the endpoint:

- it lets a listed name withdraw a contribution or request a review for
  somebody else;
- it opens `guidance status`;
- on **writes that are read later** it decides whose records count: an
  `integrated` fact makes a later base acceptable only when a listed name
  recorded it (`review_workflow.py` 1811); a void or an acceptance counts only
  from a listed name;
- on **reads** it decides what a caller is shown: the items of the capability
  and reference queues and of the proposal queue, and the attention items of
  `work`, are shown to a listed name and only counted for anybody else
  (`capability_records.py` 1047 and 1113, `reference_records.py` 1059,
  `proposal_records.py` 2083, `work.py` 87).

Since the name is the caller's own word, each of these holds only against a
caller who does not write a listed name. The code says this where it matters
(`endpoint.py` 675-681: "the actor is self-declared there, so everything that
rests on the operator allowlist is a host command").

### Host commands (`admin.py`, with the service user's shell)

| Command | Project or installation | Check |
|---|---|---|
| `set-guidance`, `clear-guidance`, `compact-guidance-acks`, `guidance-status` | project | `--actor` on the operator list |
| `reference-apply`, `capability-apply`, `-retire`, `-alias-reject`, `-alias-propose` | project | same |
| `capability-verify` | project | operator or verifier list |
| `proposal-review`, `proposal-decide`, `proposal-settings` | project | operator list; review also needs the actor mapped to a person (`proposal-settings --map-actor`) |
| `requirement-backfill`, `-apply`, `-reconcile`, the `*-reconcile` commands, `reconcile-request` | project | operator list |
| `void-record`, `revert-record`, `anchor-release` | project | operator list |
| `retire-project`, `remove-creation`, `project-creations --set-server-limit` | project / installation | operator list |
| `review-writes`, `checkpoint-provenance-writes` (switches) | **installation** | operator list; audited |
| `handoff` (the operator's transfer) | project | none: it never reads the list |
| `set-onboarding`, `capability-misses-clear`, `reference-misses-clear`, `proposal-http-records` (a read) | project | none |
| `operators`, `verifiers` add and remove | **installation** | none, and **not audited**: no `--actor`, no log; the only trace is the copy in a backup's sidecar |
| `install`, `add-project`, `finish-project`, `authorized-keys`, `backup*`, `restore-new`, `service`, `record-store`, `journal` | installation or project | none |

`admin.py` reads the operator list in 36 places, and `guidance.py` (479) reads
it by itself from the installation's file.

All of these checks compare a name typed on a command line by somebody who
already has the shell, can type any name, and can edit
`deployment.private.json` and the databases directly. They guard against a
mistake, not against a person. **A shell is authority over every project of the
installation, and no list changes that.**

### The web route

- Roles per project: viewer, contributor, owner (`http_authority.ROLE_CAPABILITIES`).
  Only a superuser makes or unmakes an owner (`http_auth.set_member`). A
  superuser counts as owner of every project.
- A request is refused with 404 outside the projects the person is a member of,
  and an agent credential outside the projects it was granted; the endpoint
  checks the same again before it writes (`http_authority.run_guarded`).
- Approval needs `reviews.approve`, which only an owner has; an agent credential
  can never have it (`CREDENTIAL_FORBIDDEN_CAPABILITIES`). The route compares
  the approver with nobody, so **an owner may approve their own contribution**,
  although the comment above the line says "the contributor who delivered the
  work can contribute or request changes, never approve" (`http_service.py`
  5318-5321). Only `recommend` has a person rule: nobody recommends their own
  work or their own agent's (`_independent`, by person).
- There is no route for the operator list, the merge slot, lifecycle facts,
  handoff, assigning to another, comments, or writing reference and capability
  records. Over the web nobody can integrate.
- Changes are audited per project (`GET /v1/projects/{id}/audit`, owner only).

### Where projects and people are separated today and where they are not

| | Separated |
|---|---|
| Web: which projects a person or an agent credential can read and write | yes |
| Web: who may approve | yes (owners), but not from themselves |
| Host route: which project a caller may name | **no** |
| Host route: which actor a caller may be | **no** |
| Host route: who may approve, take the merge slot, record integration | **no** (anybody, and with no approval) |
| Operator and verifier lists | **no** (one per installation) |
| Rollout switches (`review-writes`, `checkpoint-provenance-writes`) | **no** (one per installation) |
| The shell, the Dolt server, the kit, backups, `deployment.private.json` | **no**, and cannot be by any list |
| Private keys and client folders of agents under one OS user on the machine they run on | **no**, and no rule on the server can |

## The design

### Three kinds of caller

- **The installation's operator**: whoever has the service user's shell (an
  unrestricted key, a local login, an agent that runs on the server as that
  user). Authority over everything. This should be James and as few others as
  possible. No rule below binds them and none pretends to.
- **A bound key**: an SSH key whose `authorized_keys` line runs the forced
  command and names its principal and its projects. Everything below is about
  these.
- **The web service**: unchanged.

### Two conditions that are not code

**The coordinator gives up the shell.** A coordinator is confined only if it is
a bound key. In plain words: the agent no longer runs on the server, and it can
no longer run `admin.py`, `bd`, a backup or anything else there; it has the
endpoint and nothing more. What it needs of the host commands comes back
through the endpoint (slice 3), and the rest stays with James.

**The keys are kept apart where the agents run.** The server knows a key, not
who holds it. Three agents under one OS user on the code host can read each
other's private key and client folder, and each is then every principal: it is
the cheapest way round every rule here. Each agent that is to be told apart
needs its own OS user (or its own machine), with its private key and its client
configuration readable by that user only, and no shared SSH agent. Where that
is not done, the rules below separate what agents do by mistake, which is worth
having, and nothing else.

### Rule 1: a key names its projects

`ssh_forced_command.py` takes `--project NAME` (repeatable) in its own
arguments, which come from the `authorized_keys` line and never from the
connection, and passes them to the endpoint as `--key-project NAME`. The
endpoint, before anything else in `execute`, refuses a request whose project is
not among them, with the same words as for a project that does not exist (the
web route's 404), and refuses the three web-only actions that name no project.

Nothing else changes: the client already sends only the endpoint's path
(`"forced_command": true`), and the project stays in the request.

This rule alone confines every agent that has a bound key, coordinator or
worker, to its projects: no reading, no task, no merge slot, no lifecycle fact
anywhere else.

### Rule 2: a key names its principal, and actors belong to principals

A **principal** is who a key belongs to: a short name James chooses when he
issues the key, in the `person:NAME` form the proposal settings already use
(`proposal-settings --map-actor ... --to person:NAME`). Several keys may carry
the same principal.

- The wrapper takes `--principal NAME` and passes `--key-principal NAME`.
- The project's session registry gains one map, `owners`: actor to principal.
  `session register` over a bound key records the new actor under the key's
  principal.
- Over a bound key every other action is refused unless the request's actor is
  owned by the key's principal in that project. So a bound key cannot write
  under another's name, an operator's included.
- Actors that exist already (the present coordinators and workers) are given to
  a principal once by a host command, with `--actor`, `--reason` and an audit
  entry, so they keep their names and their history. It refuses a name on the
  operator list that another principal owns in another project.

The registry is written only by the server and is not reachable through `bd`.
Why the registry and not a stamp in every record: one place, and none of the
record formats changes.

**What rules 1 and 2 do to the operator list.** The list stays one list of
actor names for the installation. A session's name is made by the server for
one project's registry; under rule 2 only the key of the principal that owns it
can use it, and under rule 1 only in that key's projects. So "coordinator of
project A" is: an actor of A, owned by a principal, on the operator list. It is
nobody in B, because no bound key can be that actor in B. James makes one with
`operators add`, as today.

### Rule 3: between principals, nobody passes their own work

Two refusals, both in terms of principals, so an installation that has none
behaves as today:

- **Approval.** `review approve` is refused when the approver's actor and the
  actor of the contribution's author, or of the task's assignee, are owned by
  the same principal.
- **Reviewed and integrated.** An actor that a principal owns may record the
  lifecycle facts `reviewed` and `integrated` for a task only when the task's
  current contribution carries an approval, with nothing unresolved, from an
  actor of **another** principal than the contribution's author. A task with no
  contribution cannot be recorded reviewed or integrated by such an actor.

Without the second refusal the first would bar one record and nothing that
follows from it: the same agent could go on to the merge slot and record its own
unapproved work integrated, and as a listed name its `integrated` fact would
make that commit an acceptable base for the next contribution
(`review_workflow.py` 1811).

The refusals name the rule and say who may approve.

What "the same" means is James's choice when he names principals. One principal
per person is the web's rule (an agent is its owner) and would mean that no
agent of James may approve the work of another agent of James. One principal
per lane (`person:kittrial-coordinator`, `person:orc-coord`) stops an agent
passing its own work and nothing more. See the questions.

### What these rules do not do

| Way round | Closed by |
|---|---|
| The agent has a shell on the server | nothing in the kit; do not give it one |
| The agent can read another agent's private key or client folder | nothing on the server; separate OS users where the agents run |
| An `authorized_keys` line from before the upgrade (see Migration) | cleaning `authorized_keys` by hand; the listing of slice 1 flags such lines |
| Two principals acting together | nothing |
| A commit pushed to the repository without any record in the tracker | the repository's own permissions; the kit records, it does not hold the branch |
| An owner approving their own contribution on the web | not changed here (question 4) |
| A bound worker approving another principal's work, or taking the merge slot, in its own project | not changed here; kittrial-5bb.106 decides who may write which fact (question 3) |

A key that is not bound keeps working exactly as today until the installation
says otherwise (slice 4), with the limit described under Migration.

### What stays with the installation whatever is chosen

The service user's shell and everything it reaches: the Dolt server and every
project's database, `deployment.private.json` (operators, verifiers, the two
rollout switches), the kit and its upgrade, backups and restore, creating and
retiring projects, `authorized_keys`, the web service's process, its state
document and its superusers.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| **A per-project operator list** | Without rules 1 and 2 the names on it are the caller's own word and it confines nobody. With them it adds nothing the one list does not already give (above), and it costs a decision at each of the 36 places `admin.py` reads the list and at `guidance.py` 479, which reads the installation's file by itself. Kept as an option for an installation that wants James's own listed name to count in one project and not in another; not sized here. |
| **Map the web roles onto the host actions** (an owner, or an owner's agent, is the coordinator) | A coordinator would have to do its work as a web principal, and the web has none of the routes: assigning, comments, the merge slot, lifecycle facts, handoff and record acceptance are six families of routes, each a contribution or more, and the rule that an agent credential never approves would have to be reopened. It is the largest option and delivers nothing until most of it exists. Kept as a direction: the web interface can show who coordinates a project early (slice 8), and a key's principal can later be tied to a web account. |
| **One installation per project** | The only option that also confines somebody with a shell, provided each installation has its own OS user. Costs three services, ports, Dolt servers, backup sets and upgrades, three web log-ins per person and no view across projects. Right today for a project that must be closed off now (see below); not the general answer. |
| **One OS user per project inside one installation** | The projects share one Dolt server, one `deployment.private.json`, one kit and one web state document. Not separable by file permissions without redesigning the runtime. |
| **Bind a key to one exact actor** | A new session registers a new actor, so every registration would need James to edit `authorized_keys`. A principal is set once per key. |
| **Stamp the principal into every record** instead of the registry | Every record family's format changes, with its validators and its readers, where one map in one server-written file serves all of them. |
| **Secrets or signed requests per actor on the host route** | A second credential system beside the web's, when SSH already says which key is calling. |
| **Refuse self-approval by name everywhere at once** | Would stop a project that one agent works alone, on upgrade and without warning. Rule 3 speaks of principals, so nothing changes where none are set. |
| **Refuse only the approval** (the first version of this note) | Bars one record; the same agent integrates its unapproved work all the same. |

## Migration

Nothing changes for an installation that configures nothing: keys without
`--project` and `--principal` behave as today, and with no principals rule 3
refuses nothing.

For an installation that wants confinement (the office one; jjbp with im2 on
koopa), in this order, each step safe to stop after:

1. Upgrade the kit.
2. Give each agent that is to be told apart its own OS user where it runs, and
   make a key pair there that only that user can read.
3. For each, install the bound line (`authorized-keys --principal ... --project
   ...` prints it) and set `"forced_command": true` in its client
   configuration. From here that agent reaches only its projects.
4. Give its existing actors to its principal (the host command of rule 2).
   From here it can act only as itself, and rule 3 holds between it and the
   others.
5. Take the shell away from everybody who should not be the installation's
   operator: remove their unrestricted `authorized_keys` lines, and stop
   running their agents on the server.
6. **Clean `authorized_keys` by hand.** A line names the wrapper and the
   endpoint by absolute path. On an office installation that path carries the
   release (`install/releases/<release>/kit/...`, kittrial-5bb.182), so a line
   printed before the upgrade goes on running the OLD wrapper and the OLD
   endpoint, which know none of these rules, against the live runtime, for as
   long as that release's folder is there. Nothing in the new kit can refuse
   it. Remove or reprint every such line; the listing of slice 1 flags each
   line that points at a kit other than the installed one. Removing the old
   release's folder also closes them, at the price of the rollback.
7. When every line is bound and points at the installed kit, set the
   installation to bound keys only (slice 4): the wrapper then refuses a line
   that names no principal or no project. This closes a forgotten line of the
   CURRENT kit; it does nothing about step 6's.

After every later upgrade of an office installation the printed lines name the
old release again and must be printed and installed again, until
kittrial-5bb.182 gives the kit a path that does not change.

What breaks at step 5: the coordinator can no longer run host commands; until
slice 3 James runs them.

An older kit cannot read a registry that has the `owners` map (its validator
refuses unknown keys), so a downgrade after step 4 needs the map removed first.
The slice says so in its documentation.

## What James can do today, for three office projects with three coordinators

Nothing below needs new code. Three ways, by how much they separate:

**A. As it is (instruction only).** Create the three projects; each coordinator
registers a session in its own. If the coordinators are to run
`set-guidance`, `proposal-review` or the record acceptance commands themselves,
they need the shell and their names on the operator list.
What each can then do outside its project: everything. Read and write every
task, approve, take the merge slot, record integration, accept and void
records, change the operator list, read and restore backups, in all three
projects.

**B. Confined keys, which exist today.** Give each coordinator a key of its own
installed with the contributor line that `admin.py authorized-keys` prints, and
`"forced_command": true` in its client configuration. Keep the shell for
James, who then runs the host commands for all three (guidance, accepting
records, proposal reviews, voids, backups).
What each can then do outside its project: no shell, no host command, no file,
no database. But on the endpoint still everything, because it may name any
project and any actor: create, assign, approve, take the merge slot, record
integration and close, in the other two projects, and read them.
To know before choosing it:
- It works only where the coordinators connect over SSH from another machine or
  another OS user; an agent that runs on the server as the service user cannot
  be confined at all.
- The three keys must be kept apart where the agents run (above), or each
  coordinator is all three.
- After every upgrade of the office installation the printed line names the old
  release and must be printed and installed again (kittrial-5bb.182).
- A real SSH connection from another machine to an office installation has been
  made once: the coordinator, 2026-10-07 03:37 UTC, from a Windows machine to a
  scratch office installation on koopa, with the `python` key in the client
  configuration. It worked. Nobody has done it yet against the office server
  itself.
This is worth doing now: it is steps 2 and 3 of the migration, and it removes
the largest part of what a coordinator can do by mistake.

**C. Three installations under three OS users.** Full separation today. Costs
as in the table above.

On the web side, in every case: make each project's person its owner (a
superuser does this), and create agents with credentials for the workers. That
separates the workers by project today. It does not help the coordinators,
because they cannot integrate over the web.

## Making and removing a coordinator under this design

Making one, by the installation's operator:

1. Issue the key: `authorized-keys --key-file K --principal person:NAME
   --project P` and install the printed line.
2. The coordinator registers its session; the actor is recorded as its
   principal's.
3. `operators add ACTOR`, with who did it and why (slice 5).

Removing one: `operators remove ACTOR --confirm-revoke` (effective from the next
request, since the list is read on every request; the command already says
which of its records stop counting), then remove the key's line.

Audit: every change of the operator and verifier lists and every adoption of an
actor is an entry (time, operator, name, change, reason) in an audit file that a
host command prints; today these changes leave no trace. The registry shows
which principal each actor belongs to. What the kit cannot audit is
`authorized_keys` itself, which is sshd's file: the printed line carries the
principal and the projects in its comment so that the file can be read by eye,
and the read-only listing of slice 1 shows every bound line, its principal, its
projects and whether it points at the installed kit.

In the web interface (slice 8): the project page shows, to its owners, the
principals of its registered actors and which of them are listed operators.
Changing them there is not proposed: issuing a key is a host action (the web
service must not write `authorized_keys`), and the operator list is the
installation's.

## Slices

Each is one contribution, and no order of them leaves an installation less
safe than before. The order is the one the review recommended.

1. **A key names its projects** (rule 1). Wrapper, endpoint,
   `authorized-keys --project`, a read-only listing of the lines in
   `authorized_keys` (principal, projects, and a flag on any line that points
   at a kit other than the installed one), documents. The test that matters:
   every endpoint action, with a bound key, naming another project, raw `bd`
   included. Touches the `authorized-keys` printing, so it follows
   kittrial-5bb.182.
2. **A key names its principal; actors belong to principals** (rule 2).
   Wrapper, endpoint, the registry's `owners` map, the adoption command with
   its audit, `session show`.
3. **A coordinator's acceptance commands through the endpoint**, for a bound
   actor on the operator list, in its own project, one contribution per family:
   (a) guidance: set, clear, status; (b) accepting reference and capability
   records; (c) proposal review and decision; (d) the operator's handoff and
   `set-onboarding`. Each is a check that exists and is unreachable today only
   because the actor is self-declared (`endpoint.py` 675-681); with a bound key
   it is not. Without this slice James runs every acceptance for three
   projects, so it comes straight after rule 2.
4. **Bound keys only**, an installation setting: the wrapper refuses a line
   without a principal or a project; `setup-status` reports it.
5. **The operator and verifier lists are audited**: `--actor` and `--reason`
   on add and remove, an audit file, a command that prints it. Small, useful
   today, and independent of everything else here; it can be done first.
6. **Nobody passes their own work** (rule 3): the two refusals, with
   `docs/REVIEWS.md` and what `work` and `brief` show for a contribution that
   waits for somebody else. It takes its list of facts from the decision on
   kittrial-5bb.106 and is built with it, not before it.
7. **Coordinator-only writes**, if James wants them (question 3): approval and
   taking the merge slot only for a listed actor of the project. Also with
   kittrial-5bb.106.
8. **The web interface** shows who coordinates a project.

Slices 1, 2 and 6 are the design asked for. Not sized: a per-project operator
list, and per-project rollout switches.

## Questions for James

Each has a recommendation; 6 and 7 are defaults that stand unless he says
otherwise.

1. **Do the coordinators give up the server?** Confinement means the agent no
   longer runs on the server and has no shell there: it works from another
   machine or another OS user, over SSH, with its own key, and cannot run
   `admin.py`, `bd` or a backup. Until slice 3 James runs the acceptance
   commands for it. *Recommendation: yes for the three office coordinators and
   for the second coordinator of im2; one coordinator James trusts with
   everything may keep the shell and is then the installation's operator, not a
   project's coordinator.*
2. **What is a principal: a person or a lane?** Per person, no agent of yours
   may approve work of another agent of yours, which would end the way kittrial
   itself is run today. Per lane, an agent cannot pass its own work and that is
   all. *Recommendation: per lane, said so in the documents.*
3. **Should approval and the merge slot be a coordinator's alone** in a
   project, or stay open to every bound worker of it? This is the decision
   already waiting on kittrial-5bb.106 (which facts a contributor may write and
   which only a listed operator). *Recommendation: decide it there; this note
   needs only that rule 3 uses the same list of facts.*
4. **Should the web route also refuse an owner approving their own
   contribution?** The code's own comment says the contributor never approves;
   the route lets an owner who contributed approve. A project with one owner
   would then need a second owner or a superuser for every approval of the
   owner's own work. *Recommendation: yes, as a task of its own, with the
   sentence that tells a sole owner what to do.*
5. **Which host commands does a confined coordinator get back** (slice 3)?
   *Recommendation: guidance, accepting records, proposal reviews, the
   operator's handoff and the onboarding text; voiding, reverting, reconciling,
   backups, retiring and the rollout switches stay with the installation's
   operator.*
6. **The web interface.** *Default: it shows who coordinates a project and
   changes nothing; keys and the operator list stay host actions.*
7. **The two rollout switches are per installation**, so three projects share
   them. *Default: leave them; both are rollout switches for a kit
   feature, not settings of a project.*
