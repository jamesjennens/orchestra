# Requirements-driven first slice

Status: proposed sequence derived from the draft BRD. The owner accepted using this workflow to develop the kit; exact requirements and architecture have not been represented as individually approved.

## First iteration: establish intent

Register requirements and narrative in the canonical kit project, publish BRD draft 0.1 and its content manifest, obtain independent review, reconcile findings and record remaining decisions. This uses existing client commands; do not claim a new product mode exists yet.

## Bounded implementation work

This table is the roadmap, not a statement that all rows are in the first slice. The validator and offline publisher are the immediate implementation scope, governed by [REQUIREMENTS_CONTRACT.md](REQUIREMENTS_CONTRACT.md). The review dependency has been satisfied; exact baseline acceptance remains separate.

Validator, publisher, native export adaptation (`kittrial-pth.23`), offline impact/reassessment (`.20`) and the acknowledged-plan CLI gate (`.21`) are now integrated locally and independently tested on Windows/Linux. Usage: [REQUIREMENTS.md](REQUIREMENTS.md) and [REQUIREMENTS_INTEGRATION.md](REQUIREMENTS_INTEGRATION.md). Lifecycle/activity integration and office/recovery work remain pending.

| Work | Requirement coverage | Scope and checks |
| --- | --- | --- |
| Revision contract and record validator | R02, R03, R05 | Define payloads and canonical hashes; reject duplicate IDs, unresolved references and acceptance/revision inconsistencies; round-trip native exports |
| Offline draft/accepted BRD publisher | R01, R03, R11 | Read one export; render ordered narrative/requirements; atomic current pointer; interrupted-write and repeat-finalization tests; no runtime deployment |
| Change proposal and impact traversal | R04, R05, R07 | Immutable old references, explicit new acceptance, needs-reassessment propagation and reaffirmation; cycle/missing-link tests |
| Lifecycle and activity integration | R06, R07 | Extend existing comment feed/current renderer using verified native state events, commit/release evidence and explicit unknown states |
| Worker planning barrier | R08 | Separate confirmed plan registration from executable work; demonstrate a worker cannot start implementation while plan write is failed/pending |
| Office transport and operator recovery | R09, R11 | Same-host client, .cmd/POSIX entry points, canonical paths, separate-account access trial, off-machine recovery drill |

R10 applies to review/integration of every code contribution. R12 applies to every public artifact. Current public owner/license decisions remain unresolved; none of these tasks authorizes publication of private state.

## Sequencing and acceptance

Canonical task mapping: validator `kittrial-pth.18`, publisher `kittrial-pth.19`, impact `kittrial-pth.20`, planning barrier `kittrial-pth.21`. All currently depend on the independent baseline review `kittrial-pth.14`; publisher and impact also depend on the validator. Lifecycle/activity and office/recovery work remain later roadmap scope, not additional claimed tasks in this bootstrap.

Validate the record contract before implementing the publisher; the impact feature depends on that contract and stable baseline references. Prototype against draft requirements with assumptions explicitly recorded. Do not call a prototype owner-accepted production behavior.

For each contribution, record its requirement revisions, intended files/interfaces, acceptance tests, branch/commit and six independent lifecycle facts. Use isolated checkouts and native returned IDs. An independent reviewer checks behavior and tests before integration. Keep service deployment separate from local code acceptance.

Use the kit's own baseline as the first publisher fixture, then demonstrate a realistic change: an owner revises a requirement, the prior BRD remains reproducible, an affected task becomes needs-reassessment, and later evidence reaffirms or replaces its old verification. That round trip is the first end-to-end milestone.

## Owner decisions remaining

- Who besides the initiating owner may accept requirements/baselines, and is one joint owner sufficient or is joint approval required? Keep configurable; do not invent a role hierarchy.
- Which requirements belong in the first accepted baseline versus later scope?
- Approve or revise the exact requirement acceptance criteria after review; acceptance of the direction is not blanket acceptance of generated detail.
- Confirm public repository destination and license separately.

Exploratory design and draft tooling can proceed under the existing instruction to build the harness. Publishing an accepted baseline needs a concrete owner acceptance record tied to its content hash.
