# Requirements bootstrap status

First candidate: **draft-0.1**, created 2026-09-13. Current canonical project: `kittrial`. Connection details belong in operator-local configuration, not this repository.

| Record | Canonical ID |
| --- | --- |
| Requirements/design job | kittrial-pth |
| Drafting and reconciliation | kittrial-pth.1 |
| Requirements R01–R12 | kittrial-pth.2 through kittrial-pth.13; exact mapping in requirements-baseline.json |
| Independent review | kittrial-pth.14 |
| BRD narrative | kittrial-pth.15 |
| Draft publication candidate | kittrial-pth.16 |
| User decision accepting workflow direction | kittrial-pth.17 |
| Revision validator | kittrial-pth.18 |
| Offline publisher prototype | kittrial-pth.19 |
| Change impact/reassessment | kittrial-pth.20 |
| Acknowledged planning barrier | kittrial-pth.21 |

The BRD, exact source manifest, proposed design and implementation sequence are available for review. Local verification checked all twelve requirement hashes, narrative hash, overall manifest hash, unique IDs and inclusion of exact source text in the BRD. Baseline hash: `ef326e4d1376a2bd2d85887f69143ff785a7695693c9eae3d39e8d82f175e95f`.

Independent Hermes/DeepSeek review completed on 2026-09-13 and is recorded on kittrial-pth.14, comment `01a0995f-79bc-7a0e-b709-f6467636c5c0`. The initial approval block was resolved and the review task has been closed after coordinator disposition. See [the offline contract](REQUIREMENTS_CONTRACT.md) for the disposition of each finding and the bounded validator/publisher interface. Query Beads for current implementation state.

No exact baseline acceptance is recorded. The user accepted the direction of using the harness to build itself; all generated requirement wording remains draft. Do not equate task creation or this publication with acceptance or implemented functionality.

Bootstrap publication was manually coordinated from canonical source records. Until a tested publisher is implemented, preserve this candidate and create a new named candidate for substantive corrections instead of replacing its content under the same baseline name. Record change rationale in Beads, and carry the revised source content/hashes into the next manifest. The exact old manifest must remain available through Git history or a retained baseline artifact.

Next: implement and independently verify the validator/publisher slice against the draft contract. Owner acceptance of a baseline is a separate, content-specific record. Source code remains replaceable; its requirement and evidence references persist.
