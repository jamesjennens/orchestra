# Beads team kit

A small, working pilot for coordinating independent contributors and coding agents across projects. Beads stores jobs, tasks and comments on one Linux server; contributors use their own machines and SSH keys. Code stays in each project's normal repository and PR workflow.

The kit pins Beads 1.2.2 and Dolt 2.2.0, verifies release checksums, installs an isolated user service, and provides a Python SSH client. Server support tested: Linux x86-64 with Python 3.10+ and user systemd. Client tested: Windows Python 3.11 with OpenSSH. No Python packages required.

## Start here

- Operators: [installation and recovery](docs/OPERATIONS.md).
- Contributors and agents: [working agreement](docs/WORKFLOW.md).
- Project owners: copy [project entry point](templates/READ_ME_FIRST.md) into your repository and fill in its placeholders. Reference it from both the repository README and AGENTS.md (and other agent instruction files you use).
- Job authors: [job](templates/JOB.md), [task](templates/TASK.md), [report](templates/REPORT.md), [annotation](templates/ANNOTATION.md).
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

This is a trusted-team setup, with SSH access and repository merge permissions providing the access boundaries. Actor names provide attribution, not verified identity. Project databases separate queries and records, not access rights between contributors. There is no scheduler, automatic conflict detection, web UI, role system or automatic off-machine backup. Impact statements and human/agent judgment decide which work can proceed together.

The shared SSH service account can be reached using separately revocable contributor keys. People with shell access to that account can access all its coordination data. Use separate service accounts/deployments if projects require separate access. Do not share a private SSH key.

This package contains only generic code, instructions and synthetic test results; no existing project history or credentials. Review workplace hosting and data rules using your normal process before installing there.

## Upstream

See [Beads](https://github.com/gastownhall/beads), its [Dolt architecture notes](https://github.com/gastownhall/beads/blob/main/docs/architecture/dolt.md), and [Dolt server configuration](https://www.dolthub.com/docs/sql-reference/server/configuration/). Version pins and archive hashes are in versions.json. Revalidate upgrades in a separate deployment before changing a team's server.
