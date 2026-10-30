# Requirements-driven coordination — proposed design

Status: draft architecture for the first requirements-driven slice. The offline requirements and operational slices implement parts of this design; current interfaces are documented in REQUIREMENTS_INTEGRATION.md and OPERATIONAL_WORKFLOW.md. The associated BRD remains a draft baseline, not an owner-approved specification.

The first offline implementation is specified by [REQUIREMENTS_CONTRACT.md](REQUIREMENTS_CONTRACT.md), including review dispositions. It separates the content manifest, hash-bound acceptance evidence and artifact receipt; it consumes structured snapshots rather than inferring revisions from a native export. That narrower contract takes precedence for the prototype below.

## Authority and record types

Beads is authoritative for requirements, narrative sections, decisions, change proposals and task state. Published BRD files are immutable/reproducible views of a selected baseline. Git records publication artifacts and implementation changes; it does not become a second editable requirement database.

Initially use native issue types plus explicit labels (`requirement`, `brd-section`, `change-proposal`, `baseline`) to avoid assuming custom server schemas or unavailable endpoint commands. The bootstrap uses task issues with requirement labels; normal issue open/closed state means record workflow, not whether a requirement has been delivered. The richer acceptance/revision fields below require an implemented adapter and tests.

Proposed record payload:

- Requirement: stable canonical issue ID, title, statement, rationale/source, acceptance criteria, requirement-acceptance state, revision identifier, content hash and supersession links.
- BRD section: ordered narrative section, revision identifier and content hash. Requirement references resolve against the selected baseline.
- Decision/change proposal: affected requirement revisions, rationale, alternatives, deciding authority, acceptance and impact findings.
- Baseline: manifest of exact section/requirement revisions and hashes, publication state, author/approver evidence and artifact hash.
- Design/task/evidence: exact baseline or requirement revision references, plus needs-reassessment when those inputs change.

Use canonical Beads IDs for links. R01-style display keys in the initial draft are stable aliases, not predicted child IDs. Creation always uses the returned native ID.

## Revision and publication contract

1. Read the selected canonical records once into a candidate snapshot. The snapshot includes narrative as well as requirements.
2. Validate unique identities, known references, acceptance state and content hashes. Draft publication permits proposed content but displays it as draft. Accepted publication rejects unaccepted selected records or unresolved blocking references.
3. Compute content identities using documented canonical UTF-8 JSON serialization (sorted keys, no incidental whitespace); exclude the hash field itself. Persist the exact source content in the manifest, so a future mutable issue edit cannot alter an old publication.
4. Write a candidate manifest and generated Markdown together in a new revision directory, then validate their consistency. Only after success move the current-publication pointer atomically. Do not truncate a previous baseline in place.
5. Record publication/acceptance evidence in Beads. Because filesystem and database updates are not one transaction, use a candidate ID and explicit recovery state. Repeated finalization must be idempotent; a partially written candidate must not appear current.

The first bootstrap publication is a manually coordinated draft using this hash convention. It is evidence for specifying the workflow, not a production publisher or an immutable baseline registry. An owner accepting a baseline must identify its exact manifest hash, not merely say that the latest document is approved.

## Change and impact handling

When implementation discovers a mismatch, classify it as defect, ambiguity or change proposal. A draft amendment never changes an accepted baseline. On acceptance, retain the prior revision and produce a new one, link the deciding record, and traverse revision references to mark downstream designs/tasks/evidence for reassessment.

Reassessment is distinct from failure and does not delete historical tests. Reaffirmation must cite the new requirement revision and explain why existing evidence still applies. Detect unrelated infrastructure/exploratory tasks using an explicit rationale instead of forcing artificial requirement links.

## Integration with worker execution

Workers receive a task ID and client command, fetch the brief and relevant baseline, claim with explicit actor, then write their plan. Implementation begins only after successful acknowledgement of that write. The launch process should separate planning and execution phases where practical; prose alone failed this barrier in the first Cline trial.

Keep role enforcement in existing account/repository controls for the trusted-team pilot. Requirement acceptance still needs named authority/evidence; an agent's self-declared actor is not authentication. No public endpoint or untrusted multi-tenant write access is implied.

## Read path and compatibility

One export supplies requirements, comments and current labels. Build BRD, lifecycle table and activity views locally rather than issuing one query per record. A lifecycle event schema must be verified before extending the comment-only activity feed.

SSH and explicit same-host execution are implemented client transports. Actual office Copilot validation remains an independent task and must not block an offline publisher prototype. Existing canonical task IDs, comments, backup paths and claims are preserved; no live project migration is part of this slice.


## Kit lock order

Every canonical route and host adapter uses the same acquisition order:

1. The deployment semantic mutex (`.interface-mutations.lock`), only for operations that can change the program/interface graph or its operator evidence.
2. A project coordination lock. Cross-project snapshots take one project's lock at a time and release it before the next; the final write takes only its own project's lock.
3. The live authority/store lock, shared for an authorized snapshot and exclusive for a mutation.
4. The operation journal/receipt locks and native effect.

A route may omit a class of lock, but cannot acquire an earlier class while holding a later one. In particular, no read under authority may acquire an area/project lock. A store-only service mutation takes authority alone; it releases authority before invoking a canonical backend. Canonical calls already use `serialize=False` in the service and recheck authority in the endpoint. Host recovery/restore follows the same project-before-authority rule.

An interface write prepares caller-authorized cross-project snapshots under the semantic mutex before its final project/authority boundary. Each snapshot checks current permission with project then authority already held. The final effect refreshes current accounts, roles and grants under authority and gates even cached rows again. Its frozen graph adapter cannot perform project I/O: a missing or newly readable area is incomplete and requires a retry. Membership and registration therefore need no semantic mutex and cannot form the old area/authority cycle. Ordinary proposals and non-retirement acceptance snapshot only the defining pair; accepting retirement inventories every possible linked consumer within the area budget. Unrelated references and v1 capabilities do not wait for this mutex; program-link references and interface declarations, including withdrawals, do.

For retirement, one coverage classifier applies to link sides and capabilities. An acceptance that exists but no longer passes the live check is incomplete, including removed operators, lost roles and disabled accounts; its draft-only reader projection is not proof that nobody accepted it. Only a record never accepted or an actual withdrawal with currently live approval is outside scope; an inert withdrawal is incomplete until reaffirmed by current authority. No incomplete snapshot proves absence of consumers or providers.
