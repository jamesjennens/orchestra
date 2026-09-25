# Intent

Parent job and bounded outcome:

## Plan

Steps and expected verification:

## Impact

Likely files, interfaces/contracts, compatibility, shared resources and overlaps:

## Acceptance

What makes this task complete (distinguish implementation from project acceptance):

## Execution context

Actor, repository, base SHA, branch/worktree location:

## Handoff

First next step and known blockers; append checkpoint comments as work proceeds.

# Create-child descriptions

`create-child` accepts the section headers above for type `task` (and `bug`,
`feature`, `chore`) without native validation, so a description may adapt them.
A `decision` is different: the identical `bd create --dry-run --validate` is run
first, and bd expects the section names `Decision`, `Rationale` and
`Alternatives Considered` (matched case-insensitively as text, so a heading, bold
text or prose mention qualifies). Nothing is reserved when that preflight
refuses. The create-child error names the required headings only when bd reports
missing sections, so keep the names in the description even when a section is
short.
