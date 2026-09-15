# Copilot and VS Code Remote SSH

The kit's existing client runs from a terminal using Python and SSH. It has no MCP dependency. Whether the office's Copilot harness can execute it must be tested in that environment; documentation support alone does not establish an organisation's enabled permissions.

## Workstation mode (implemented)

Install/configure the client as in OPERATIONS.md. Copilot can use the same commands as a person when terminal access is permitted. If the agent cannot execute a command, the person can run it and return the result; do not treat a suggested command as an executed claim.

## Remote workspace (planned office path)

A person opens a Linux folder using VS Code Remote SSH. The integrated terminal operates on the remote host, so ordinary agent terminal commands can use tools installed there without the agent establishing the original SSH connection.

Each contributor should have a separate checkout and branch. A shared development server does not mean a shared mutable working tree. The source repository remote supplies commits/PRs; the canonical coordination service supplies task state.

The explicit same-host local transport is available through client.local.example.json. It invokes the Linux endpoint directly with the same JSON protocol; it never silently falls back to SSH. It respects service-account permissions: simply logging into the same machine as another Linux user does not grant access to the runtime. Separate users can retain their own workspaces and individually revocable SSH access to the service account. If the development and coordination servers differ, network access is still needed between them. See [operational commands](OPERATIONAL_WORKFLOW.md).

Windows contributors can use beads.cmd with ordinary Python and no PowerShell execution-policy change. POSIX contributors use `sh /path/to/orchestra/beads.sh`. Both wrappers locate client.py relative to themselves and work from another directory. Set BEADS_PYTHON to one executable path if Python is not on PATH.

## Repository onboarding

Copy templates/copilot-instructions.md to `.github/copilot-instructions.md` in the participating repository, fill in paths and identity setup, and point it to the completed READ_ME_FIRST.md and WORKFLOW.md. Also link those documents from the README and AGENTS.md. Keep commands short enough for ordinary terminal use; no assistant-specific connector is required.

## Office smoke test

Use a disposable project/task. Verify the agent can discover instructions; list and claim the task; attach a local UTF-8 plan; report a checkpoint; refresh the views; and read the result from a second contributor's workspace. Then exercise a branch/PR/merge and record the merge commit in Beads. Record which IDE mode, command approvals and connection arrangement were actually used.

References: [VS Code Remote SSH](https://code.visualstudio.com/docs/remote/ssh), [Copilot IDE agent mode](https://docs.github.com/en/copilot/how-tos/chat-with-copilot/chat-in-ide), and [Copilot repository instructions](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions-in-your-ide/add-repository-instructions-in-your-ide).
