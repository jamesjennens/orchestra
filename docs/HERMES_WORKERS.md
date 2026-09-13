# Headless Hermes workers

Hermes is another optional command-line worker. Beads holds the task and report; the worker harness supplies execution and resumable context. No MCP integration is required.

The locally inspected CLI supports `chat`, `--provider`, `--model`, `--resume`, `--max-turns`, `-Q` and `-q`. This trial uses the configured DeepSeek provider with the `deepseek-flash` alias and default reasoning. A local alias-normalization message is not independent evidence of the backend model version. Credentials stay in the operator's existing harness configuration.

## Separate planning and execution

Prepare an isolated checkout, a bounded task with an explicit actor and client command, and the exact test command. First launch only a planning phase:

```sh
hermes chat --provider deepseek --model deepseek-flash --max-turns 12 -Q -q "Read the task and contract, claim with the supplied actor, post your plan to Beads, then stop after acknowledgement. Do not edit source files."
```

The actual prompt must include the task ID, client configuration path, file scope and exclusions. Commands must match the worker shell: PowerShell's `&` call operator does not work as the same prefix in bash.

Capture the session ID and verify the plan comment independently. Only then resume that session with a separate execution instruction:

```sh
hermes chat --resume SESSION_ID --provider deepseek --model deepseek-flash --max-turns 35 -Q -q "The coordinator verified the plan comment. Execute only the approved file scope, run the specified tests, post a report with evidence, and leave the task for coordinator acceptance."
```

The validator/publisher trial demonstrated this operator-enforced launch boundary. The kit now also supplies [worker_gate.py register/check/run](REQUIREMENTS_INTEGRATION.md), which verifies an exact canonical plan and claim before invoking the worker argv. Use it after the planning phase. A successful plan comment alone does not grant deployment or source-publication authority; the gate is not OS confinement.

## Reports and resumption

Preserve the checkout, session ID and logs outside the public repository. If a turn limit ends before the report is posted, resume the same session with a narrow handoff instruction. Check Beads before retrying a write: a missing final message does not mean the comment failed. Ask the worker to leave explicit implemented/tested/reviewed/integrated/deployed/live-verified facts, with unknown or not-performed where appropriate.

Do not treat a worker's suite result as coordinator acceptance. Independently review the source and run relevant tests before integrating. The validator trial exposed a malformed-Unicode error not covered by the initial worker suite; integration added a regression test.

Supply a concrete canonical test command in the brief. One validator run repeatedly generated temporary verification scripts after a local completion check requested fresh evidence. The source had not changed; repeated verification added no useful confidence. The worker should report the actual suite result, preserve a checkpoint if it cannot finish, and avoid repeated create/run/delete verification loops.

The headless worker runs under its existing local permissions and customizations. Do not disable approvals or ignore rules to get past a denial. External provider access must cover the actual source/task material being sent. Keep private project histories, credentials and runtime state out of generic test prompts and public artifacts.
