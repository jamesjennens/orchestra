# Project coordination

Before work, read REPLACE_WITH_READ_ME_FIRST_PATH and REPLACE_WITH_WORKFLOW_PATH. They define project purpose, named owners, task access and the contribution workflow.

Use REPLACE_WITH_CLIENT_COMMAND to query the canonical Beads project REPLACE_WITH_PROJECT. Use the configured contributor/session actor. Do not initialize another database or assume a local checkout contains current coordination state.

Inspect claims and expected file/interface impacts before editing. Claim a bounded task and save its intent, plan and acceptance before implementation. If terminal execution fails or is unavailable, report that fact; a proposed command is not a successful claim.

Use a separate contribution branch containing the task ID. Record the branch/base commit and checkpoints. Preserve another worker's claim during interruptions unless a handoff is agreed.

Submit changes through a PR with task/job IDs, tests and remaining issues. Record its URL in Beads. Keep a normal coding task open until merge; distinguish PR-ready from owner acceptance and deployment. After acceptance, record the merge commit, update current state and refresh.

Never commit service runtime data, credentials or machine-local client configuration. Follow the user's authorization and repository rules for pushes, PRs and other external actions.
