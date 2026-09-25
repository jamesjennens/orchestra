Required section headers for a `decision` created through `create-child`:
`## Decision`, `## Rationale` and `## Alternatives Considered`. Native
validation (`bd create --type decision --validate`) refuses a description that
omits any of them; include all three even when a section is short.

## Decision

State the decision, scope, deciding authority and current applicability.

## Rationale

Evidence, constraints and reasoning. Link source issues/comments and distinguish user direction from worker inference.

## Alternatives Considered

Options considered, including retaining the current behavior, and why they were not selected.

## Consequences

Affected jobs/interfaces, risks, follow-ups and circumstances that warrant revisiting this decision.

## Prior decisions

References to decisions this supports, qualifies or supersedes. Preserve the original record and update current pointers explicitly.
