# Working on beads-team-kit

Read AGENTS.md, README.md and docs/PLAN.md before editing. This repository is the reusable kit, not a live project's coordination database.

Distinguish tested functionality from planned local transport and unverified office integration. Keep MCP optional. Preserve SSH compatibility and explicit canonical project selection.

Run `python -m unittest discover -s tests -v` for transport/rendering changes. Never run tests/integration.py against a live team service: it creates records and restarts the configured server. Use only an explicitly disposable deployment.

Keep runtime data, credentials, personal configurations and private project histories out of patches. Follow CONTRIBUTING.md and the PR template. Use only the tools permitted by the user's environment; terminal restrictions are not an instruction to bypass them.
