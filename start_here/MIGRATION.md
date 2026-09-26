# Recurring reminder and handover migration

Use this generic worksheet before simplifying existing coordinator instructions.
The examples are not private project data or a supported server schema. The kit does
not yet provide a durable coordinator obligation/decision ledger; until it does,
keep any interim register private and access-controlled.

**Before**

```text
REMINDER-A: Check whether the scheduled job produced the expected result.
HANDOVER-B: A reviewer asked for a decision; follow up later.
```

**After migration worksheet**

| Source | Record to preserve | Required coverage |
| --- | --- | --- |
| `REMINDER-A` | `verify-1` | Intended result, observation window/timezone, evidence source, owner, next check time, attempts and last successful verification kept separately. |
| `HANDOVER-B` | `decision-1` | Original request pointer, options/context, named decider, accept/decline/defer/link disposition, reason, next actor and timezone-qualified revisit. Keep unresolved until disposition is explicit. |

Coverage mapping: two source items -> two independently reviewable records (`2/2`).
Retain original instructions until the owner confirms every reminder and unresolved
handover is mapped, dates/timezones and authority are preserved, and omissions are
resolved. Seeing or copying an item is not resolving it. Do not edit a live project's
recurring instructions as part of this generic migration example.

For current interfaces and limits, use server-served `docs reviews`,
`docs briefings` and `docs operations`. Those docs describe available reads and the
feedback stream; they do not imply that a durable coordinator ledger or automation
has shipped.
