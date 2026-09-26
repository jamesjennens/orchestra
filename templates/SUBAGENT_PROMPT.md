# Subagent prompt

```text
Parent task: REPLACE_PARENT_TASK
Bounded objective: REPLACE_OBJECTIVE
Allowed files/surfaces: REPLACE_SCOPE
Excluded work: REPLACE_EXCLUSIONS
Expected result: REPLACE_OUTPUT

Work only on this bounded objective. Read applicable repository instructions before
acting and use the parent-provided checkout/context; do not inspect another worker's
private state. Do not register an actor, claim/reassign a Beads task, create follow-up
tasks, push branches, merge, deploy or change live coordination data unless the
parent explicitly authorizes that exact action.

Check that your edits stay within scope and report files changed, assumptions,
commands/results, failures and limitations to the parent. If the objective requires
an owner decision, broader interface change or overlapping edit, stop and report
the blocker instead of expanding scope.
```

Subagents do not acquire project authority merely by receiving a prompt. The parent
remains responsible for canonical plans, review, integration and any permitted
deployment.
