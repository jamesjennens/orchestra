A `decision` created through `create-child` is preflighted with the identical
`bd create --dry-run --validate`, so bd decides validity and nothing is reserved
when it refuses. bd expects the section names `Decision`, `Rationale` and
`Alternatives Considered`; it matches them case-insensitively as text, so a
heading, bold text or a prose mention all qualify. Include all three even when a
section is short. The create-child error names the required headings only when bd
itself reports missing sections.

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
