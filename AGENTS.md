# Working on this kit

Read README.md and docs/OPERATIONS.md. Keep this repository generic: no project histories, runtime databases, credentials or machine-specific client configuration.

Run unit tests for changes to transport or rendering. Integration tests mutate data and restart a server; use a separate disposable deployment. Never point them at a live team's configuration. Preserve version pins unless an upgrade is explicitly being validated.

This repository supplies templates for participating projects. Their agents follow docs/WORKFLOW.md plus that project's completed READ_ME_FIRST.md.
