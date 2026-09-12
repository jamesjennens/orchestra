# Headless Cline workers

This is an optional worker harness. Coordination remains accessible through ordinary client commands; neither Cline nor MCP is required by the kit.

## Launch contract

The locally inspected Cline CLI 3.0.61 supports `--provider`, `--model`, `--cwd`, `--timeout`, `--id`, `--data-dir` and JSON output. The tested provider/model identifiers are `deepseek` and `deepseek-flash`. These are CLI identifiers; confirm availability in the actual account rather than inferring a marketing/model version from the alias.

Use explicit provider/model selection and omit `--thinking` to retain provider-default behavior. A previous user-reported run stalled with high thinking; omission worked in this trial. That observation does not establish that every stall is a reasoning-setting problem.

For new workers, prefer a dedicated persistent runtime directory outside the source repository:

```text
cline --provider deepseek --model deepseek-flash --data-dir /path/to/private/worker-state --cwd /path/to/worker-checkout --timeout 1200 --json "Read and claim the specified Beads task, execute its bounded brief, test, commit locally, and report evidence back to Beads."
```

Adapt paths to the operating system. Supply the exact client command, configuration path, project and actor in the launch brief. Use the existing configured credentials; do not put API keys on the command line or copy credential files into the repository. Validate authentication and task reading with the selected data directory before assigning substantial work.

The user reports that isolated data directories avoided coupling headless runs to an interactive hub's lifetime. The first kit delegation succeeded using the existing runtime; shutdown isolation is not yet independently tested here. A dedicated directory is a proposed reliability default, not proof that backend failure is impossible.

## Durable work and recovery

- Create the self-contained task in canonical Beads first. Include owned files, exclusions, acceptance, runtime/test commands and the integration authority.
- Use a separate checkout and unique actor. Have the worker claim, record its plan before editing, and leave checkpoints and a completion report.
- Record the Cline session ID, runtime directory, checkout/base commit and Beads task ID outside public source content. A tool-event ID or conversation ID is not necessarily the resumable session ID.
- Preserve the checkout and runtime after a timeout/disconnection. Inspect Git state, Beads comments, session history and the backend before assigning the task to another worker. Missing output does not prove that a write failed.
- Resume with `--id SESSION_ID` using the original runtime/configuration and checkout, plus explicit provider/model flags. Do not point an existing session at a fresh empty data directory and expect its context to follow. Migrating runtime state needs a separately validated procedure; otherwise start a new session with an explicit Beads/Git handoff.
- Concurrent provider failures can indicate a shared local dependency, but do not establish the cause. Check local backend health separately from provider/network reachability. Do not report a diagnosis as confirmed without evidence.
- Cline log schemas can differ: this trial exposed tool-call inputs and output chunks, while another reported version/run exposed IDs only. Treat logs as supplementary evidence. Repository changes, independently rerun tests and durable Beads reports establish completion.

The CLI help says prompt mode enables tool auto-approval by default. Scope and filesystem/account boundaries therefore matter: give the worker only the intended project, and explicitly exclude live deployments, other project histories and credential inspection. Never use a separate harness to bypass a denial applying to the same operation.

## First delegation evidence

A Windows headless Cline worker used the Linux-hosted Beads service over the existing SSH client, claimed a task, implemented the offline comment activity feed in an isolated checkout, and produced a local commit. The coordinator independently ran all 17 unit tests successfully. This validates cross-harness task consumption and reporting; it does not mean Cline ran on Linux or that a GitHub PR was opened.
