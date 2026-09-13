# Working on this kit

Read README.md and docs/OPERATIONS.md. Keep this repository generic: no project histories, runtime databases, credentials or machine-specific client configuration.

For kit development, also read docs/BRD.md, docs/requirements-baseline.json, docs/SYSTEM_DESIGN.md and docs/IMPLEMENTATION_PLAN.md. The BRD publication is currently draft. Its source records live in canonical Beads; do not hand-edit the generated requirement text to fit code. Record a linked question/change proposal and publish a new candidate after reconciliation. See docs/REQUIREMENTS_STATUS.md for source IDs and current bootstrap status. Existing Git and deployment authorization still applies.

The offline validator/publisher are implemented; read docs/REQUIREMENTS.md and docs/REQUIREMENTS_CONTRACT.md before changing their interface. Keep immutable publication names and exact snapshot hashes. Headless worker planning and resumption are documented in docs/HERMES_WORKERS.md; the coordinator must verify plan acknowledgement before authorizing implementation.

Read docs/REQUIREMENTS_INTEGRATION.md for explicit native revision comments, impact/history input and worker_gate.py register/check/run. The gate verifies canonical acknowledgement before launching through its CLI; it does not provide OS confinement or exactly-once execution. Impact reports are views, not automatic task-state mutations.

Run unit tests for changes to transport or rendering. Integration tests mutate data and restart a server; use a separate disposable deployment. Never point them at a live team's configuration. Preserve version pins unless an upgrade is explicitly being validated.

This repository supplies templates for participating projects. Their agents follow docs/WORKFLOW.md plus that project's completed READ_ME_FIRST.md.
