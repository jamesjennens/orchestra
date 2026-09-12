# Plan: portable team coordination

Updated 2026-09-11. This document owns the kit's implementation roadmap. Project-specific work belongs in each project's Beads database.

## Intended outcome

Small teams can queue meaningful work, contribute using whichever permitted coding assistant they have, and resume across interruptions or usage limits. Each project can have one or several owners. Owners accept code through the repository's normal PR and merge controls. Coordination holds intent, plans, impact, checkpoints, decisions and evidence.

Start with a few contributors across one or two projects. Keep MCP, vendor-specific agent orchestration and a new role system optional. No shared AI account or subscription is required; each contributor uses their own permitted assistant access.

## Agreed architecture

- One canonical Beads database per project on an operator-managed Linux deployment.
- Durable jobs are epics; bounded contributions are child tasks. Later work normally gets another child, while resuming interrupted work continues its existing task.
- Plans declare probable files, interfaces, migrations, shared resources and known overlaps. These are advisory conflict checks, not automatic locks on source files.
- Claims coordinate task ownership. A paused worker retains its claim until an explicit handoff; running out of model usage does not release it automatically.
- Comments preserve dated findings and reports. Later annotations link to originals; current summaries point to the current conclusion.
- README and agent instruction files lead to READ_ME_FIRST and the working agreement. Generated views are explicitly dated, with Beads authoritative.
- Source code lives in separate contributor checkouts/branches. Beads service state lives outside those source checkouts and is not merged through contribution PRs.

## Access modes

| Mode | State | Path |
| --- | --- | --- |
| Local workstation, remote coordinator | Implemented and tested | Python client -> SSH -> endpoint -> canonical database |
| VS Code Remote SSH workspace | Environment option; office validation pending | Human connects VS Code; workspace terminal commands run on development server |
| Same-host local endpoint client | Planned | Python client -> local endpoint process, no nested SSH |
| Separate Linux accounts on shared server | Design/validation pending | Explicit service access boundary; each account has its own checkout |
| MCP adapter | Not required | Consider only if a future harness benefits from it |

A human-established Remote SSH workspace can avoid asking the agent to initiate an SSH connection. It does not bypass terminal restrictions, server filesystem permissions or service-account access. Copilot must still be allowed to run commands in that workspace. A remote development host different from the coordination host still needs a permitted connection between them.

## Completed pilot

- Pinned Beads/Dolt installation with checksum verification and an isolated loopback service.
- Portable SSH client, project creation, canonical task claims and comment updates.
- Generated current view, issue pages and daily journals with annotation backlinks.
- Native backup and restore into a separate database.
- Five unit checks on Windows/Linux and a disposable integration exercise covering independent clients, claim races, concurrent comments, project separation, restart and restore.
- Generic job/task/report/annotation templates and operating instructions.

## Next: address observed coordination costs

The first operational pilot reported roughly thirty tasks across three agent types. Preserve the model, but prioritize queryable lifecycle facts, a single-export activity feed, enforced actor identity, concurrency-safe child IDs, merge coordination and generated summaries before broadening distribution. See [pilot feedback and acceptance criteria](PILOT_FEEDBACK.md) for the implementation order, verified native capabilities and migration boundaries. These improvements are planned, not yet shipped.

## Then: make the office workflow easy

1. **Local transport:** add an explicit transport selector while preserving existing SSH configuration. Local mode uses a subprocess argument array and the same JSON protocol; it must never silently switch hosts or databases. Test local/SSH parity, attachment handling, failure/timeout reporting and actor attribution.
2. **Account access:** validate the simplest acceptable access arrangement for contributors' separate Linux accounts. Do not make runtime data world-readable or silently grant broad sudo access. The existing shared service-account pilot is a trusted-team boundary, not verified per-person authorization.
3. **Convenient commands:** add small PowerShell and POSIX wrappers around the common client. Keep project/actor configuration explicit and credentials out of the repository. Do not require GitHub CLI or MCP.
4. **Copilot onboarding:** ship repository instructions and run a smoke test in the actual permitted IDE: read instructions, list a task, claim a disposable task, save a plan/report, and verify another contributor can see it. Repeat in Remote SSH if local execution/connectivity is restricted.
5. **Two-person office trial:** exercise parallel non-overlapping tasks, a deliberate overlap, an interrupted handoff and a complete PR/merge/acceptance cycle. Confirm private workspaces share the same coordination state.
6. **Operations:** schedule off-machine backups through the team's chosen mechanism; test recovery on a replacement host. Record actual ownership and retention arrangements in deployment-local documentation.

Acceptance: contributors can start from repository instructions without a pasted chat message; the agent uses available shell commands; one canonical claim wins; a paused contribution is discoverable; reviewers can trace merged commits to tasks; restored records are verified.

## Git and PR workflow

Default: claim -> plan -> branch -> implement/test/checkpoint -> PR -> owner review/merge -> reconcile Beads.

- One branch per bounded contribution, named with its task ID; separate checkout/worktree for each independent worker.
- Record repository, base commit, branch and current commit in the task. Push useful checkpoints to the permitted remote so another contributor can access them.
- Include job/task IDs, behavior, impact, validation and remaining issues in the PR; record its URL back in Beads.
- Normally keep a coding task open until its PR merges. Record awaiting-review in notes without inventing a custom status. A separately defined implementation task may close at PR-ready only when an explicit review/acceptance task remains.
- Owners use the forge's normal protection/review rules. Beads actor names and owner prose do not enforce merge permissions.
- On merge, record the resulting merge/squash commit and acceptance evidence, update dependencies/current summaries and refresh. PR closure without merge is not acceptance.
- GitHub is the first public-host target. Keep the conceptual workflow compatible with Bitbucket and other forges; opening a PR through a browser is a supported manual path.

No automatic PR creation, status synchronization, webhook or CI-driven task closure is implemented. Begin with templates and explicit updates; add automation only after trial evidence identifies a repeated failure, with idempotence and recovery for partially completed operations.

## Public repository preparation

Proposed name: beads-team-kit. Proposed license: MIT, subject to owner's confirmation. No public remote exists yet.

- [x] Keep a standalone repository containing generic implementation, docs and synthetic validation only.
- [x] Publish a candid README, contributor workflow, Copilot instructions/template and PR template locally for review.
- [x] Distinguish implemented features from local transport, account integration and office tests that remain planned.
- [ ] Confirm GitHub owner/organisation, name and license; add chosen LICENSE with correct attribution.
- [ ] Review the exact tracked tree and history for credentials, host-specific/private project data and third-party notices. Archive only tracked files.
- [ ] Add lightweight CI for portable unit tests; keep deployment integration explicitly opt-in on a disposable server.
- [ ] Create the public repository, push reviewed commits, verify displayed files/links and configure normal review expectations.
- [ ] Publish an initial pilot release with checksums and tested-platform notes. Download dependencies from upstream; do not bundle upstream binaries in this kit's source archive.

Public-source availability is separate from permission to publish any team's actual coordination records. Runtime state, private client configuration and workplace histories stay out of this repository.

## Later, only if justified

Scheduled backup helpers; PR synchronization; a read-only dashboard; more operating systems/architectures; richer conflict hints beyond the shared-path merge convention. View generation/reconciliation and lifecycle/activity visibility have moved into the next tranche based on operational feedback. Strong multi-tenant isolation, job scheduling and replacing the repository's permissions are outside the small-team pilot.
