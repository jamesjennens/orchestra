# Orchestra

A small, working pilot for coordinating independent contributors and coding agents across projects. Beads stores jobs, tasks and comments on one Linux server; contributors use their own machines and SSH keys. Code stays in each project's normal repository and PR workflow. Orchestra is the public name of the project previously called `beads-team-kit`.

The kit pins Beads 1.2.2 and Dolt 2.2.0, verifies release checksums, installs an isolated user service, and provides a Python SSH client. Server support tested: Linux x86-64 with Python 3.10+ and user systemd. Client tested: Windows Python 3.11 with OpenSSH. No Python packages required.

## Start here

We are now using the harness to develop the harness. Start with the [draft BRD](docs/BRD.md), its [exact requirement manifest](docs/requirements-baseline.json), the [proposed system design](docs/SYSTEM_DESIGN.md) and [first implementation sequence](docs/IMPLEMENTATION_PLAN.md). The workflow direction is accepted; the detailed baseline remains a review candidate. The offline validator and BRD publisher are implemented: see [usage](docs/REQUIREMENTS.md). [Native export adaptation, revision impact views and an acknowledged-plan launch gate](docs/REQUIREMENTS_INTEGRATION.md) are also available through ordinary CLI commands.

- Operators: [installation and recovery](docs/OPERATIONS.md).
- Project direction and remaining work: [implementation plan](docs/PLAN.md).
- Operational feedback and next priorities: [pilot follow-up](docs/PILOT_FEEDBACK.md).
- Lifecycle evidence, activity cursors, safe child creation and merge slots: [operational commands](docs/OPERATIONAL_WORKFLOW.md).
- Compact current task state, persistent unresolved items and paginated evidence: [briefings and checkpoints](docs/BRIEFINGS.md).
- Copilot and remote development: [office setup](docs/COPILOT_REMOTE_DEV.md).
- Optional headless workers: [Cline launch and recovery](docs/CLINE_WORKERS.md), [Hermes planning and resumption](docs/HERMES_WORKERS.md).
- Contributors and agents: [working agreement](docs/WORKFLOW.md).
- Starting a new worker: [copy-paste prompt](templates/WORKER_PROMPT.md), with project placeholders and PowerShell/POSIX commands.
- Starting from an empty directory: [server onboarding](docs/ONBOARDING.md). One SSH command returns shared rules and the private project entry point; no existing project checkout is required.
- Project owners: copy [project entry point](templates/READ_ME_FIRST.md) into your repository and fill in its placeholders. Reference it from both the repository README and AGENTS.md (and other agent instruction files you use).
- Job authors: [job](templates/JOB.md), [task](templates/TASK.md), [report](templates/REPORT.md), [annotation](templates/ANNOTATION.md).
- Decision authors: [decision template](templates/DECISION.md).
- Evidence: [validation](reports/VALIDATION.md) and [machine-readable integration results](reports/integration.json).

## Model

| Concept | Representation |
| --- | --- |
| Project | Separate Beads database, repository entry point, named owner(s) |
| Job | Beads epic: intended outcome, acceptance, impact, current summary |
| Task | Child issue: one bounded piece of work, intent, plan, assignee, checkpoints |
| Journal entry | Issue comment: dated finding, handoff, report or correction |
| Current state | Issue status/notes plus generated CURRENT.md |
| Accepted code | Repository commit/PR accepted under the project's existing merge permissions |

Subsequent work on an existing job normally gets another task. A replacement worker can resume an interrupted task after checking the previous claim and checkpoint. A completed implementation task does not automatically mean its job has been accepted.

The `refresh` command builds current summaries, complete issue pages and daily journals with backlinks for later annotations. Beads is authoritative; Markdown is a dated projection. Refresh is an explicit step in the working agreement, not a transaction with the preceding update. A failed refresh can be repeated safely.

## Scope of the pilot

This is an independent community kit built around Beads and Dolt, not an official distribution of either project. It is published under the [MIT license](LICENSE) at [jamesjennens/orchestra](https://github.com/jamesjennens/orchestra). The tested scope and pending workplace validation are explicit in the plan. Contributions use the [contribution guide](CONTRIBUTING.md). Upstream dependencies retain their own licenses.

This is a trusted-team setup, with SSH access and repository merge permissions providing the access boundaries. Actor names provide attribution, not verified identity. Project databases separate queries and records, not access rights between contributors. There is no scheduler, automatic conflict detection, web UI, role system or automatic off-machine backup. Impact statements and human/agent judgment decide which work can proceed together.

The shared SSH service account can be reached using separately revocable contributor keys. People with shell access to that account can access all its coordination data. Use separate service accounts/deployments if projects require separate access. Do not share a private SSH key.

This package contains only generic code, instructions and synthetic test results; no existing project history or credentials. Review workplace hosting and data rules using your normal process before installing there.

## Upstream

See [Beads](https://github.com/gastownhall/beads), its [Dolt architecture notes](https://github.com/gastownhall/beads/blob/main/docs/architecture/dolt.md), and [Dolt server configuration](https://www.dolthub.com/docs/sql-reference/server/configuration/). Version pins and archive hashes are in versions.json. Revalidate upgrades in a separate deployment before changing a team's server.
