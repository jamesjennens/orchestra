# Working on Orchestra

Read AGENTS.md, README.md and docs/PLAN.md before editing. This repository is the reusable kit, not a live project's coordination database.

Read docs/OPERATIONAL_WORKFLOW.md for local transport, lifecycle evidence, child request IDs and merge slots. Distinguish tested functionality from unverified office integration. Keep MCP optional. Preserve SSH compatibility and explicit canonical project selection.

Run `python -m unittest discover -s tests -v` for transport/rendering changes. Never run tests/integration.py against a live team service: it creates records and restarts the configured server. Use only an explicitly disposable deployment.

Keep runtime data, credentials, personal configurations and private project histories out of patches. Follow CONTRIBUTING.md and the PR template. Use only the tools permitted by the user's environment; terminal restrictions are not an instruction to bypass them.
