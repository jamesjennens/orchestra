# Local waits, local facts and "waiting on my own job" (kittrial-5bb.588)

**Status:**
- The kit half is **built** with this task, in the parts listed in section 1.
- The Bridge half (section 2) is a **written spec**. The Bridge is built outside this repository, and its build is a follow-up task.

## Why

A worker often waits on its **own** job: a long test run, a replay, a build.

**What went wrong before:**
- The kit could only express server waits. So the worker either:
  - declared `waiting-on-coordinator`, and appeared in the coordinator's queue as a question; or
  - stopped, because the guidance said "if nothing new is actionable, leave it unchanged and stop".
- A result the worker saw on its own machine (the job finished or failed) did not count as "new". So nobody recorded it until a server event happened to wake the worker.
- When something did wake it, each reader made up its own "no new activity" sentence from the fields.
- Those fields were measured from the newest checkpoint **by anyone**, so a coordinator's comment could vanish behind the coordinator's own later checkpoint.

## 1. The kit half (built)

| Piece | What | Where |
|---|---|---|
| Local waits | `client.py wait --recheck-in DURATION`, `--until-file PATH` and `--until-exit PID`. The wait returns on the first of the server event or the local condition. A local return prints `local_wake: {kind, what, at}` and keeps the caller's fingerprint. No route, host or port is added, and the server is asked no more often than without the options. `--until-exit` only checks: `kill(pid, 0)` on POSIX; `OpenProcess` and `WaitForSingleObject(h, 0)` on Windows, where access denied reads as alive. It never signals or ends anything. | `client.py` (`_local_options`, `_process_alive`, `_wait_command`); tests/test_client_local_waits_588.py |
| Local facts | An optional `local_fact` = `{kind: job-finished\|job-failed\|job-partial\|time-reached\|file-seen, what (≤160), at, detail (≤300)}`, carried in the checkpoint file's `lane_state` (the `Kind: lane-state-v1` record), with any state. It is NOT a key of the checkpoint payload itself, which stays the closed set it was (section 5). It is the worker's own statement: shown as "Observed locally: …", and it changes no review or lifecycle state. A checkpoint that adds only a `local_fact` is accepted with no new server activity. | `lane_state.py`, `lanes.py`, `briefing.py`; tests/test_own_job_588.py |
| Own-job wait | Lane state `waiting-on-own-job` with a required `job` (≤120). It refuses `waiting_on` and allows `expect_by` and `not_before`. It is never a `checkpoint-to-coordinator` question. With no `expect_by`, it is overdue 4 hours after it began, and the existing `lane-quiet` and `timer-due` triggers tell the coordinator. It is not nudged before then. | `lane_state.py`, `lanes.py`, `nudges.py`, `waiting.py`, `client.py`, the web lists |
| What is new | Measured from the lane's **own** newest checkpoint; a later checkpoint by someone else is new activity itself (T1). Two comments in one second are ordered by the tracker's sequence (T2). One sentence, `wake_summary`, is built in one place and carried by work rows, brief, every web route and the wait items, and never fingerprinted (T4). | `briefing.py` (`own_baseline`, `entry_order_key`, `wake_summary`, `wake_summary_text`), `http_service.py`, `waiting.py`, `client.py` |
| `wrote_since` | `wrote_since(row, checkpoint, actor_or_role, in_role)`: did the actor or role waited on write on this task after this checkpoint? The lane's own comments never count, and neither does a comment on another task. For .585 and .598. | `briefing.py` |

## 2. The Bridge local wait (spec, not built here)

The Bridge (the editor extension that wakes an agent) gets the same three kinds of local wait. Nothing here changes the server.

**The tool:** `orchestra_local_wait {kind: "time" | "file" | "process", value, label}`.

| Input | Meaning |
|---|---|
| `kind: "time"` | `value` is a duration (`30m`, at most 14 days). |
| `kind: "file"` | `value` is a path. The Bridge only asks whether it exists, and never reads or opens it. |
| `kind: "process"` | `value` is a process id. The Bridge only checks that it exists (Node's `process.kill(pid, 0)`, which tests and never signals). It never sends any other signal. |
| `label` | The agent's own words for the job, at most 120 characters, one line. Untrusted text: escaped when shown. |

**Pending waits:** at most 3 per lane, and a fourth replaces the oldest.

**When a wait ends:**

1. The Bridge submits **one fixed prompt**: *"Your local wait ended (KIND: LABEL). Read the result yourself, then checkpoint what you saw with a `local_fact` in your `lane_state`."*
2. **No text from the job, the file or the process is ever typed into the prompt.** The agent reads the result itself, with its own tools.
3. **The rate bound, as the .582 watchdog has:**
   - at most 6 local wakes an hour per lane;
   - waits that end within the same minute are coalesced into one prompt that names each.
   - Past the bound, the Bridge shows the ended wait on the status bar and does not prompt.

**The status bar:**

| When | It shows |
|---|---|
| While a wait is pending | **"Waiting on own job: LABEL, since TIME"** |
| When the wait ends | "Own job ended: LABEL" |

**What the agent does around it:**

- **Before it waits:**
  - it records a checkpoint with lane state `waiting-on-own-job`, `job` = the label, and `expect_by` when it knows one;
  - it calls `orchestra_local_wait`.
- **When woken:** it checkpoints a `local_fact` (in its `lane_state`) with the result.
- The Bridge writes no checkpoint itself.

**Two waits at once.** A Bridge that also runs the server wait wakes on whichever comes first. A server wake prints the item's `wake_summary`; a local wake prints the fixed prompt above.

**Office:** no network access, route, host or port is added. All of it runs on the worker's own machine.

## 3. What a woken worker prints

- Every reader (the agent, the Bridge, a person reading `wait` output) prints each item's **`wake_summary`**.
- It never composes its own "no new activity" from the fields. In brief's text form the sentence is the line "What is new: …".
- `tools/wake` only starts the agent with a fixed prompt. It reads no activity, so it has nothing to print. The agent it wakes prints the sentence.

## 4. If a coordinator's comment seems not to wake a worker

Check first whether the coordinator and the worker are **the same actor**. A parent actor and its sub-labels, `alice/session` and `alice/session/sub0`, count as one actor (`briefing.same_activity_actor`; test_changes_since_checkpoint.py `SubLabelActivityCase`). So a coordinator acting under the worker's actor prefix writes "own" activity, which by design never wakes that worker.

The fix is separate actors, not a code change. This check is in the coordinator guidance (docs/OPERATIONAL_WORKFLOW.md).

## 5. Rollback

| Piece | An older kit |
|---|---|
| `local_fact` | It rides in the lane-state record, never in the checkpoint payload, on purpose. An older kit refuses a checkpoint with an unknown key, which would break that task's checkpoint chain and its brief. An unknown lane-state field makes an older kit refuse only that one record, so the board shows no declared state for the lane. |
| `waiting-on-own-job` | An older lane-state reader refuses the record as an unknown state, and the board shows no declared state for that lane. Nothing else breaks. |
| `wake_summary` | An additive row field that older readers ignore. |
| The client wait options | Client-only. |
