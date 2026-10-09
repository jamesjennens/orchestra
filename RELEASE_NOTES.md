# Orchestra 0.1.0 — pilot

Orchestra coordinates small teams of people and independent coding agents using a canonical Beads service, ordinary terminal commands and repository PRs. This first public pilot includes:

- Durable job/task briefs, acknowledged worker plans, correction history and generated current/journal views.
- Collaborative requirement revisions, reproducible BRD snapshots and impact/reassessment views.
- Six evidence-scoped lifecycle facts, native state events and an offline activity feed with durable cursors.
- Recoverable native child creation and project-wide merge-slot coordination.
- SSH or explicit Linux local transport, Windows CMD/POSIX wrappers and Copilot onboarding instructions.
- A confined per-project coordinator can run its acceptance commands over a bound SSH key without a shell; through that route a record is accepted only after a reviewed proposal. The host `clear-guidance` JSON line gained a `changed` key and exits 0 with "nothing was set" instead of a traceback when there was nothing to clear.
- Pinned Beads/Dolt installation, initial backups, journal-aware recovery and optional backup timer templates.

MIT licensed. Upstream binaries are downloaded and verified during installation; they are not bundled in these source archives. Extract the source and start with README.md and docs/OPERATIONS.md. No third-party Python packages are required.

Validation: 257 unit tests run on both Windows and Linux, with platform-specific skips; native lifecycle/concurrency/decision checks, restart/restore, local transport, and backup transfer between isolated deployments also passed. See reports/OPERATIONS_VALIDATION.md for scope and evidence.

This remains a trusted-team pilot. Actor names are attribution, not authenticated identity. Exact BRD acceptance is still separate from accepting the workflow direction. Actual workplace Copilot permissions, separate-user rollout, off-machine backup scheduling and a physical host-loss exercise remain deployment-specific checks. Existing live projects need a reviewed migration; installing this release does not migrate them automatically.
