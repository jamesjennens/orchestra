# Orchestra 0.1.0 — pilot

Orchestra coordinates small teams of people and independent coding agents using a canonical Beads service, ordinary terminal commands and repository PRs. This first public pilot includes:

- Durable job/task briefs, acknowledged worker plans, correction history and generated current/journal views.
- Collaborative requirement revisions, reproducible BRD snapshots and impact/reassessment views.
- Six evidence-scoped lifecycle facts, native state events and an offline activity feed with durable cursors.
- Recoverable native child creation and project-wide merge-slot coordination.
- SSH or explicit Linux local transport, Windows CMD/POSIX wrappers and Copilot onboarding instructions.
- A confined per-project coordinator can run its acceptance commands over a bound SSH key without a shell; through that route a record is accepted only after it was proposed as a draft (two recorded steps; the same actor may do both). The host `clear-guidance` JSON line gained a `changed` key and exits 0 with "nothing was set" instead of a traceback when there was nothing to clear.
- Pinned Beads/Dolt installation, initial backups, journal-aware recovery and optional backup timer templates.
- The program view (kittrial-5bb.453): `GET /v1/projects/{program}/program`, the client's `trace --program` and a Program page in a program project's menu. Each area shows its link state and, only where you may read it, its open tasks, lanes waiting long and coordinators; program requirements no readable area cites. Interfaces read "not available" until the interface register lands.
- Decisions across levels (kittrial-5bb.454): a program decision (`Binds:`, `Owner-only:`) binds an area only when the area adopts it (`Adopts:`, and `Per:` for an owner-only decision adopted by a coordinator), checked live at every read; the program view lists each decision with its areas' states and the open `suggestion` tasks. A host route now refuses any actor that reads as an account or agent id once normalised (upper case, white space, `/label`, non-ASCII look-alikes, `usr_x`).
- Pointers into other projects: `req:`, `design:` and the new `decision:` take a `<project>/` qualifier and resolve only for a reader of that project (`not-visible` otherwise, the same for a project that does not exist). A design document that already contains a qualified `design:` token, or `req:/x`, `req:A/` or `req:A/@3` (an empty project or key, which is now prose), reads `differs` until it is accepted again.

MIT licensed. Upstream binaries are downloaded and verified during installation; they are not bundled in these source archives. Extract the source and start with README.md and docs/OPERATIONS.md. No third-party Python packages are required.

Validation: 257 unit tests run on both Windows and Linux, with platform-specific skips; native lifecycle/concurrency/decision checks, restart/restore, local transport, and backup transfer between isolated deployments also passed. See reports/OPERATIONS_VALIDATION.md for scope and evidence.

This remains a trusted-team pilot. Actor names are attribution, not authenticated identity. Exact BRD acceptance is still separate from accepting the workflow direction. Actual workplace Copilot permissions, separate-user rollout, off-machine backup scheduling and a physical host-loss exercise remain deployment-specific checks. Existing live projects need a reviewed migration; installing this release does not migrate them automatically.
