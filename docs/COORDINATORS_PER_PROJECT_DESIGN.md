# Several projects on one installation, each with its own coordinator

**Status: design note for James to decide on (kittrial-5bb.178). Nothing here is
built, and this task changes no behaviour.**

Everything under "What is true today" was read in the code on main `2d321be`;
nothing was run for this note. The measured facts are the coordinator's, from an
office installation of release `ff83602` in a container (note of 2026-10-07 on
the task); where the code and that note meet they agree, and the one place
where the code says more than the note is marked. Line numbers are those of
`2d321be`.

## The answer in short

- **Today nothing on the host route separates projects or people.** A caller
  who reaches `endpoint.py` writes its own actor name and its own project name
  into the request, and neither is checked against anything. The operator list
  is one list for the installation, and on the endpoint it gates almost nothing.
  Three coordinators on one installation are kept apart by instruction only.
- **The web route does separate projects and people**, but a coordinator cannot
  do its work there: there is no web route for assigning to another, comments,
  the merge slot, lifecycle facts, handoff, or accepting reference and
  capability records.
- **Whoever has the service user's shell is outside every rule of the kit**, on
  every project, whatever is built. Every coordinator has that shell today. So
  the first condition of any confinement is not code: a coordinator must
  connect with its own SSH key, confined to the endpoint.
- **The smallest design** is then four small rules, all on the host route:
  1. a confined key names the projects it may use, and the endpoint refuses any
     other project;
  2. a confined key names who it belongs to (a *principal*), and may only act
     as actors that principal registered;
  3. each project has its own coordinator list, which takes the place of the
     installation's operator list for that project;
  4. an approval is refused when the approver and the author of the
     contribution (or the assignee) are the same principal.
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
| Lifecycle facts (implemented ... live-verified, scope, release) | any name on any task; the payload's actor must equal the request's actor and that is all (`lifecycle.py` 1478). This is kittrial-5bb.106. |
| Handoff | the current owner hands off; the receiver accepts. The operator's transfer is a host command. |
| Checkpoint | any name; directions only from the assignee |
| Propose or revise a reference, a capability, a proposal, a requirement draft | any name. Accepting, deciding, voiding and verifying are host commands. |
| `guidance status` | a name on the operator list. It is the only action the endpoint itself refuses for want of that list. |

So on the endpoint the operator list does three things only: it lets a listed
name withdraw or request a review for somebody else, it opens `guidance status`,
and it decides on **reads** whose records count (an `integrated` fact makes a
later base acceptable only when a listed name recorded it, `review_workflow.py`
1811; a void or an acceptance counts only from a listed name). Since the name is
the caller's own word, each of these holds only against a caller who does not
write a listed name. The code says this where it matters (`endpoint.py` 675-681:
"the actor is self-declared there, so everything that rests on the operator
allowlist is a host command").

**More than the coordinator's note says:** the note found that only
`set-guidance` and `proposal-review` needed `operators add`. That is right for
the loop that was run; the full list of host commands that check `--actor`
against the list is below.

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
| `set-onboarding`, `capability-misses-clear`, `reference-misses-clear` | project | none |
| `operators`, `verifiers` add and remove | **installation** | none, and **not audited**: no `--actor`, no log; the only trace is the copy in a backup's sidecar |
| `install`, `add-project`, `finish-project`, `authorized-keys`, `backup*`, `restore-new`, `service`, `record-store`, `journal` | installation or project | none |

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
  the approver with nobody, so **an owner may approve their own contribution**
  (`http_service.py` 5318-5321). Only `recommend` has a person rule: nobody
  recommends their own work or their own agent's (`_independent`, by person).
- There is no route for the operator list, the merge slot, lifecycle facts,
  handoff, assigning to another, comments, or writing reference and capability
  records. Over the web nobody can integrate.
- Changes are audited per project (`GET /v1/projects/{id}/audit`, owner only).

### Where projects are separated today and where they are not

| | Separated |
|---|---|
| Web: which projects a person or an agent credential can read and write | yes |
| Web: who may approve | yes (owners), but not from themselves |
| Host route: which project a caller may name | **no** |
| Host route: which actor a caller may be | **no** |
| Host route: who may approve, take the merge slot, record integration | **no** (anybody) |
| Operator and verifier lists | **no** (one per installation) |
| Rollout switches (`review-writes`, `checkpoint-provenance-writes`) | **no** (one per installation) |
| The shell, the Dolt server, the kit, backups, `deployment.private.json` | **no**, and cannot be by any list |

## The design

### Three kinds of caller

The design rests on telling these apart, so they have names here:

- **The installation's operator**: whoever has the service user's shell (an
  unrestricted key, a local login). Authority over everything. This should be
  James and as few others as possible. No rule below binds them and none
  pretends to.
- **A bound key**: an SSH key whose `authorized_keys` line runs the forced
  command and names its principal and its projects. Everything below is about
  these.
- **The web service**: unchanged.

A coordinator is confined only if it is a bound key. A coordinator that keeps a
shell is the installation's operator, whatever a list says. That is the price
of the design and it is paid in convenience: a confined coordinator cannot run
`admin.py`. Which of its host commands it gets back through the endpoint is
slice 7 and a question for James.

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
  entry, so they keep their names and their history.

The registry is written only by the server and is not reachable through `bd`.
Why the registry and not a stamp in every record: one place, and none of the
record formats changes.

### Rule 3: a coordinator list per project

`deployment.private.json` gains `coordinators`: project to a list of
principals. A host command adds and removes (`coordinators add PROJECT
PRINCIPAL --actor OPERATOR --reason ...`), under the lock the operator list
uses, and every change is written to an audit file
(`coordinators.audit.json`, as `review-writes` does), and the list is carried
in the backup sidecar as the operator list is.

For a request in project P the endpoint hands the modules, in place of the
installation's operator list, this set: the installation's operators, and every
actor of P whose principal is a coordinator of P. Every existing check
(`actor in operators`, "recorded by a listed name") then reads per project with
no change to the modules: a coordinator of A is nobody in B.

A project that has an entry in `coordinators`, even an empty one, is a
**confined project**; rule 4 applies to those.

### Rule 4: no approval from oneself

In a confined project `review approve` is refused when the approver's principal
is the principal of the contribution's author or of the task's assignee. An
actor without a principal is compared by the name key the follow-on gate
already uses (`author_key`). The refusal names the rule and says who may
approve.

It is limited to confined projects because today's behaviour is deliberate and
documented, and a project worked by one agent alone must not come to a stop on
upgrade.

What "the same" means is James's choice when he names principals. One principal
per person is the web's rule (an agent is its owner) and would mean that no
agent of James may approve the work of another agent of James. One principal
per lane (`person:kittrial-coordinator`, `person:orc-coord`) stops an agent
approving its own work and nothing more. See the questions.

### What these rules do not do

- They do not bind anybody with a shell.
- They do not make approval, the merge slot or integration a coordinator's
  privilege: a bound worker may still approve another's work and take the slot
  in its own project. That is slice 6, and it waits for the decision on
  kittrial-5bb.106 (who may write which lifecycle fact).
- They do not stop two principals acting together.
- They do not change the web route. An owner still approves their own
  contribution there.
- A key that is not bound keeps working exactly as today until the
  installation says otherwise (slice 3).

### What stays with the installation whatever is chosen

The service user's shell and everything it reaches: the Dolt server and every
project's database, `deployment.private.json` (operators, verifiers,
coordinators, the two rollout switches), the kit and its upgrade, backups and
restore, creating and retiring projects, `authorized_keys`, the web service's
process, its state document and its superusers.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| **A per-project operator list alone**, with no change to keys | The names on it are the caller's own word, and on the endpoint the list gates almost nothing (approval, the slot and lifecycle facts are open to everybody). It would change which records count on reads and confine nobody. It is rule 3, which is worth something only on top of rules 1 and 2. |
| **Map the web roles onto the host actions** (an owner, or an owner's agent, is the coordinator) | A coordinator would have to do its work as a web principal, and the web has none of the routes: assigning, comments, the merge slot, lifecycle facts, handoff and record acceptance are six families of routes, each a contribution or more, and the rule that an agent credential never approves would have to be reopened. It is the largest option and delivers nothing until most of it exists. Kept as a direction: the web interface can show the coordinators of a project early and edit them later (slice 8), and a key's principal can later be tied to a web account. |
| **One installation per project** | The only option that also confines somebody with a shell, provided each installation has its own OS user. Costs three services, ports, Dolt servers, backup sets and upgrades, three web log-ins per person and no view across projects. Right today for a project that must be closed off now (see below); not the general answer. |
| **One OS user per project inside one installation** | The projects share one Dolt server, one `deployment.private.json`, one kit and one web state document. Not separable by file permissions without redesigning the runtime. |
| **Bind a key to one exact actor** | A new session registers a new actor, so every registration would need James to edit `authorized_keys`. A principal is set once per key. |
| **Stamp the principal into every record** instead of the registry | Every record family's format changes, with its validators and its readers, where one map in one server-written file serves all of them. |
| **Secrets or signed requests per actor on the host route** | A second credential system beside the web's, when SSH already says which key is calling. |
| **Refuse self-approval everywhere at once** | Would stop a project that one agent works alone, on upgrade and without warning. |

## Migration

Nothing changes for an installation that configures nothing: keys without
`--project` and `--principal` behave as today, a project without a
`coordinators` entry behaves as today.

For an installation that wants confinement (the office one; jjbp with im2 on
koopa), in this order, each step safe to stop after:

1. Upgrade the kit.
2. For each agent that is to be confined, make a key pair of its own and
   install the bound line (`authorized-keys --principal ... --project ...`
   prints it); set `"forced_command": true` in its client configuration. From
   here that agent reaches only its projects.
3. Give its existing actors to its principal (the host command of rule 2).
   From here it can act only as itself.
4. `coordinators add` for each project. From here the lists are per project and
   rule 4 holds in those projects.
5. Remove the coordinators' names from the installation's operator list
   (`operators remove --confirm-revoke`; the command already says which of
   their records stop counting, and they count again through the coordinator
   list for their own project).
6. Take the shell away from everybody who should not be the installation's
   operator: remove their unrestricted `authorized_keys` lines.
7. When every key is bound, set the installation to bound keys only (slice 3):
   the wrapper then refuses a line that names no principal or no project, so a
   forgotten old line fails closed.

What breaks if step 2 is done and nothing else: nothing. What breaks at step 6:
the coordinator can no longer run host commands; until slice 7 James runs them.

An older kit cannot read a registry that has the `owners` map (its validator
refuses unknown keys), so a downgrade after step 3 needs the map removed first.
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
This is worth doing now: it is step 2 of the migration, and it removes the
largest part of what a coordinator can do by mistake.
It works only where the coordinators connect over SSH; an agent that runs on
the host as the service user cannot be confined at all.

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
2. `coordinators add P person:NAME --actor OPERATOR --reason "..."`.
3. The coordinator registers its session; the actor is recorded as its
   principal's.

Removing one: `coordinators remove P person:NAME --actor OPERATOR --reason
"..."` (effective from the next request, since the list is read on every
request), then remove the key's line.

Audit: every `coordinators` change and every adoption of an actor is an entry
(time, operator, project, principal, change, reason) in an audit file that a
host command prints. The registry shows which principal each actor belongs to.
`operators` and `verifiers` changes get the same entry, which they lack today.
What the kit cannot audit is `authorized_keys` itself, which is sshd's file:
the printed line carries the principal and the projects in its comment so that
the file can be read by eye, and a read-only `admin.py` listing of the bound
lines is part of slice 1.

In the web interface, in two steps (slice 8): first the project page shows its
coordinators and the principals of its registered actors, to its owners; later
a superuser adds and removes a coordinator there, audited in the web audit log
as well. Issuing a key stays a host action: the web service must not write
`authorized_keys`.

## Slices

Each is one contribution, and no order of them leaves an installation less
safe than before.

1. **A key names its projects** (rule 1). Wrapper, endpoint,
   `authorized-keys --project`, a read-only listing of bound lines, documents.
   The test that matters: every endpoint action, with a bound key, naming
   another project, raw `bd` included. Touches the `authorized-keys` printing,
   so it follows kittrial-5bb.182.
2. **A key names its principal; actors belong to principals** (rule 2).
   Wrapper, endpoint, the registry's `owners` map, the adoption command with
   its audit, `session show`.
3. **Bound keys only**, an installation setting: the wrapper refuses a line
   without a principal or a project; `setup-status` reports it.
4. **Coordinators per project** (rule 3): the list, its command, its audit, the
   backup sidecar and restore, the per-project set on the endpoint. With it,
   the missing audit of `operators` and `verifiers` changes.
5. **No approval from oneself** in confined projects (rule 4), with
   `docs/REVIEWS.md` and what `work` and `brief` show for a contribution that
   waits for somebody else.
6. **Coordinator-only writes** in confined projects: approval, taking the merge
   slot, and the lifecycle facts that kittrial-5bb.106 decides are not a
   contributor's. After that decision.
7. **A confined coordinator's host commands through the endpoint**, for its own
   project, one contribution per family: guidance (set, clear, status);
   accepting reference and capability records; proposal review and decision;
   the operator's handoff. Each is a check that exists and is unreachable today
   only because the actor is self-declared (`endpoint.py` 675-681); with a
   bound key it is not.
8. **The web interface**: show a project's coordinators and principals; then
   let a superuser change them.

Slices 1 to 5 are the design asked for. 6 to 8 are what it makes possible.

## Questions for James

1. **Who keeps a shell?** Confinement needs each coordinator to connect over
   SSH with its own bound key and to give up the shell. Is that acceptable for
   the office coordinators, and for the second coordinator of im2 on koopa?
2. **What is a principal: a person or a lane?** Per person, no agent of yours
   may approve work of another agent of yours, which would end the way kittrial
   itself is run today. Per lane, an agent cannot approve its own work and that
   is all. Recommendation: per lane, and say so in the documents.
3. **Should approval and the merge slot be a coordinator's alone** in a
   confined project (slice 6), or stay open to every bound worker of the
   project?
4. **Should the web route also refuse an owner approving their own
   contribution?** A project with one owner would then need a superuser or a
   second owner for every approval.
5. **Which host commands should a confined coordinator get back** (slice 7)?
   Recommendation: guidance, accepting records, proposal reviews and the
   operator's handoff; voiding, reverting, reconciling, backups, retiring and
   the rollout switches stay with the installation's operator.
6. **The web interface:** is showing the coordinators enough to begin with, or
   is changing them there wanted from the start?
7. **The two rollout switches are per installation.** Three projects share
   them. Leave that, or make them per project (a slice of its own, not sized
   here)?
