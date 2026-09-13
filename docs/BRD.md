# Business requirements: team coordination harness

**Baseline: draft-0.1 — proposed detail, not an accepted baseline.**

Published from canonical Beads records. This readable publication is not a second editable source of requirements. See [exact source manifest](requirements-baseline.json), [proposed design](SYSTEM_DESIGN.md) and [implementation sequence](IMPLEMENTATION_PLAN.md).

Baseline content SHA-256: `ef326e4d1376a2bd2d85887f69143ff785a7695693c9eae3d39e8d82f175e95f`.

## Purpose
Enable small teams of people and independent coding agents to maintain a durable agreement about what to build, coordinate contributions across interruptions, and judge implementation against that agreement. Code may be replaced while requirements, decisions and evidence remain accessible.

## Users
Project owners accept intent and outcomes; contributors gather requirements and implement bounded work; coordinators reconcile dependencies, questions and changes; reviewers evaluate evidence; operators maintain the canonical service and recovery. One person may perform several functions and projects may have joint owners. These functions do not require a new permission hierarchy.

## Scope
The first product is an optional requirements-driven workflow over the existing small-team coordination kit. It supports collaborative discovery, a coherent current BRD, design and implementation traceability, and feedback from implementation to accepted intent. Existing task claims, comments, decisions, terminal access and repository reviews remain in use.

## Collaboration flow
Gather proposals and examples; reconcile ambiguities and alternatives; accept a bounded baseline; derive design and work; build and verify; feed defects, ambiguities and change proposals back into the records. Prototypes may resolve uncertainty before every requirement is accepted. Never amend an accepted requirement merely to excuse nonconforming code.

## Success
A new worker can explain the intended outcome and current uncertainties from the BRD, trace its assignment to exact requirement revisions, and leave evidence another worker can verify. A requirement change makes affected work visible for reassessment, while the earlier baseline remains reproducible.

## Initial exclusions
No automatic code generation guarantee, AI subscription sharing, replacement of repository merge permissions, strong untrusted multi-tenancy, or requirement that MCP/one assistant vendor be available. Public distribution excludes private project histories and runtime data.

## Open decisions
Exact first-baseline scope and detailed acceptance criteria remain draft. Joint-owner approval policy is configurable and not yet selected. Public GitHub destination/license are unresolved. Implementation storage conventions and publisher design are proposals until validated.


## Requirements

### R01: Collaborative project intent
Canonical record: `kittrial-pth.2` · revision 1 · draft

#### Requirement
People and agents can contribute needs, examples, questions and constraints to one project, with one or more named owners. Proposed material remains distinct from accepted commitments.

#### Rationale
Source: User direction, 2026-09-13.

#### Acceptance criteria
Two contributors can propose and discuss a requirement; an owner can accept or reject it with rationale. The current BRD distinguishes accepted content, proposals and unresolved questions.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R02: Durable requirement identity and history
Canonical record: `kittrial-pth.3` · revision 1 · draft

#### Requirement
Each requirement retains a stable identifier, rationale, scope and observable acceptance criteria across implementation rewrites. Revisions preserve prior meaning and evidence rather than overwriting history.

#### Rationale
Source: User direction, 2026-09-13.

#### Acceptance criteria
A changed requirement keeps its identity and has distinguishable old/new revisions; prior baseline references remain resolvable. Replacing code does not delete its requirements.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R03: Current coherent BRD and reproducible baselines
Canonical record: `kittrial-pth.4` · revision 1 · draft

#### Requirement
The BRD is the readable document of record for current accepted intent, with coherent purpose, scope, workflows and constraints. Its narrative and requirement revisions have a single canonical authority; published baselines identify exactly what was included.

#### Rationale
Source: Accepted workflow direction, 2026-09-13; precise publication contract proposed.

#### Acceptance criteria
Reproducing a named baseline yields the same substantive content. No unpublished proposal silently changes an accepted baseline. Each section and requirement resolves to its source revision. Missing or conflicting sources fail publication visibly.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R04: Design and work traceability
Canonical record: `kittrial-pth.5` · revision 1 · draft

#### Requirement
Design choices, jobs, tasks and verification evidence identify the requirement revisions they address. The team can distinguish accepted scope, planned work and delivered behavior without reading every historical comment.

#### Rationale
Source: User direction, 2026-09-13.

#### Acceptance criteria
For one requirement, list related designs, tasks and evidence; identify accepted requirements with no implementation plan and tasks with no scope rationale. Exploratory work can identify an explicit question rather than pretending to implement an accepted requirement.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R05: Implementation discoveries feed back explicitly
Canonical record: `kittrial-pth.6` · revision 1 · draft

#### Requirement
Defects, ambiguities and proposed changes discovered while coding become linked records. Agents cannot revise accepted requirements merely to match their implementation. Accepted changes trigger reassessment of affected designs, jobs and evidence.

#### Rationale
Source: User direction, 2026-09-13.

#### Acceptance criteria
Demonstrate a defect fixed without changing intent, an ambiguity resolved by a decision, and an approved scope change. Preserve the original assertion. Downstream items show needs-reassessment until reviewed against the new revision.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R06: Cheap current-state and activity reads
Canonical record: `kittrial-pth.7` · revision 1 · draft

#### Requirement
A coordinator can retrieve current work, lifecycle facts and recent comments across a project without querying each task separately. Activity includes comments on old tasks even when issue.updated_at is unchanged.

#### Rationale
Source: Operational pilot feedback supplied by user.

#### Acceptance criteria
One export supports an activity scan across hundreds of tasks, preserving timestamp ties. Current views show lifecycle facts and freshness; unknown facts are not silently rendered as false or complete.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R07: Evidence-scoped lifecycle
Canonical record: `kittrial-pth.8` · revision 1 · draft

#### Requirement
Implemented, tested, reviewed, integrated, deployed and live-verified remain independent facts tied to the applicable source revision, artifact or release and supporting evidence. Requirement acceptance is a separate dimension.

#### Rationale
Source: Operational pilot feedback supplied by user.

#### Acceptance criteria
Closing a task does not imply all six facts. A new revision cannot inherit tests/review of unrelated code. A retracted live-verification assertion remains in history while current state reflects the correction.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R08: Safe independent workers and recoverable handoffs
Canonical record: `kittrial-pth.9` · revision 1 · draft

#### Requirement
Workers read a self-contained brief, claim work under explicit identity, and obtain acknowledgement of their plan before implementation. Separate workspaces, declared file/interface impacts and controlled integration prevent conflicting writes. Interruptions preserve intent and progress.

#### Rationale
Source: Initial user coordination requirements and pilot feedback.

#### Acceptance criteria
Concurrent claims have one winner; missing actor is refused; tasks use returned IDs rather than guessed child numbers. Recover an interrupted worker without duplicating completed actions. A shared integration slot serializes conflict resolution, with explicit handoff for a paused holder.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R09: Ordinary tools in restricted environments
Canonical record: `kittrial-pth.10` · revision 1 · draft

#### Requirement
Contributors can participate using permitted terminal commands and repository files. MCP and a particular assistant vendor are optional. Human-established remote development is supported without requiring the agent to initiate that connection.

#### Rationale
Source: User office constraints, 2026-09-11.

#### Acceptance criteria
A worker reads/claims/reports through a terminal in the permitted office harness; repeat on a remote workspace. Commands work from unrelated working directories and paths with spaces or fail clearly. No global execution-policy weakening is required.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R10: Owner-controlled contribution acceptance
Canonical record: `kittrial-pth.11` · revision 1 · draft

#### Requirement
Code contributions use isolated branches and reviewable changes linked to jobs and requirements. Project owners control acceptance through the repository workflow. Coordination state records the resulting integration identity; it does not replace repository permissions.

#### Rationale
Source: User ownership and PR discussion.

#### Acceptance criteria
Trace an accepted contribution from requirement revision to task, review and merge commit. A PR closed without merge is not accepted code. Browser-based PR creation remains possible when CLI integrations are unavailable.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R11: Durable operation and recoverable publication
Canonical record: `kittrial-pth.12` · revision 1 · draft

#### Requirement
Canonical project records and published baselines survive interruption and can be backed up and restored. Generated summaries are not manually appended on every feature branch; stable rules and original evidence remain discoverable.

#### Rationale
Source: Operational pilot feedback and requirements direction.

#### Acceptance criteria
Verify backup initialization, restore on a separate target, and recovery from a partially completed update/publication. Feature branches need not merge routine generated report pointers. Restored baselines retain their record/revision links.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


### R12: Portable public kit with private project state
Canonical record: `kittrial-pth.13` · revision 1 · draft

#### Requirement
The reusable kit can be distributed independently of any project history, credentials or machine-local setup. Outside contributors can use ordinary issues/PRs without access to private coordination services. Strong multi-tenant isolation is outside the initial small trusted-team scope.

#### Rationale
Source: User public-repository and small-team direction.

#### Acceptance criteria
Source archive contains no runtime database, credentials or private project history; external contribution instructions require no private Beads access. Tested capabilities and planned ones are clearly distinguished. Public owner/name/license remain explicit publication decisions.

#### Acceptance state
Draft: user approved the direction, not this exact wording or every proposed criterion.

#### Revision
1 (draft); preserve prior revision evidence on change.


## Publication status

This is the first manual bootstrap of the workflow using the existing kit. Revision-aware editing, accepted-baseline publication and impact propagation are not yet implemented. Proposed acceptance criteria are reviewable requirements, not claims of delivered functionality. An accepted baseline must cite the exact manifest hash and owner decision.
