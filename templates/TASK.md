# Intent

Parent job and bounded outcome:

## Plan

Steps and expected verification:

## Impact

Likely files, interfaces/contracts, compatibility, shared resources and overlaps:

## Owns / must not change

Files or areas this task owns (may change) and files or areas it must not change,
even when the change looks small or obviously correct:

- Owns:
- Must not change:

Keep both lists specific enough that another worker can check them before editing
(for example a file, module, command or document path, or a named area such as a
shared contract or generated view). If a change is genuinely needed in an area
another open task owns, raise a note to that task's owner or the coordinator and
coordinate it instead of editing quietly.

## Acceptance

What makes this task complete (distinguish implementation from project acceptance):

## Execution context

Actor, repository, base SHA, branch/worktree location:

## Handoff

First next step and known blockers; append checkpoint comments as work proceeds.

# Create-child descriptions

`create-child` accepts the section headers above for type `task` (and `bug`,
`feature`, `chore`) without native validation, so a description may adapt them. That
includes `## Owns / must not change`: it is an ordinary task section, not a heading
bd requires, so a task whose ownership is already clear is not refused for omitting
it. A `decision` is different: the identical `bd create --dry-run --validate` is run
first, and bd expects the section names `Decision`, `Rationale` and
`Alternatives Considered` (matched case-insensitively as text, so a heading, bold
text or prose mention qualifies). Nothing is reserved when that preflight
refuses. The create-child error names the required headings only when bd reports
missing sections, so keep the names in the description even when a section is
short.
