# Working on this kit

Read README.md and docs/OPERATIONS.md. Keep this repository generic: no project histories, runtime databases, credentials or machine-specific client configuration.

For kit development, also read docs/BRD.md, docs/requirements-baseline.json, docs/SYSTEM_DESIGN.md and docs/IMPLEMENTATION_PLAN.md. The BRD publication is currently draft. Its source records live in canonical Beads; do not hand-edit the generated requirement text to fit code. Record a linked question/change proposal and publish a new candidate after reconciliation. See docs/REQUIREMENTS_STATUS.md for source IDs and current bootstrap status. Existing Git and deployment authorization still applies.

Run unit tests for changes to transport or rendering. Integration tests mutate data and restart a server; use a separate disposable deployment. Never point them at a live team's configuration. Preserve version pins unless an upgrade is explicitly being validated.

This repository supplies templates for participating projects. Their agents follow docs/WORKFLOW.md plus that project's completed READ_ME_FIRST.md.
