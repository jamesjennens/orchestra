# Contributing

Start with README.md and docs/PLAN.md. This is a small-team pilot; describe the concrete problem and keep changes bounded. External contributors can use GitHub issues and PRs without access to anyone's private Beads service. If a contribution has a Beads task, include its project/task reference without exposing private content.

Use your own branch/checkout. Explain intended behavior, affected files/interfaces and tests in the PR. Update documentation when changing installation or workflow behavior. Never include runtime databases, SSH keys, private configuration, copied workplace data or live integration output.

Run `python -m unittest discover -s tests -v`. Changes to server behavior should additionally be exercised on a disposable Linux deployment, following docs/OPERATIONS.md. The integration suite restarts its configured service; never run it against a team's live database. Report checks you actually ran and distinguish them from proposed validation.

Maintainers review and merge contributions. Creating or closing a coordination task does not merge code or establish project acceptance. Keep platform claims tied to tested environments and label unfinished work clearly.

Licensing is pending the repository owner's selection before initial public publication. Do not interpret this preparation document as a license grant.
