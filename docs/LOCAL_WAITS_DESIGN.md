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
| Local waits | `client.py wait --recheck-in DURATION`, `--until-file PATH` and `--until-exit PID`. The wait returns on the first of the server event or the local condition. A local return prints `local_wake: {kind, what, at}` and keeps the caller's fingerprint. No route, host or port is added, and the server is asked no more often than without the options. `--until-exit` only checks: `kill(pid, 0)` on POSIX; `OpenProcess` and `WaitForSingleObject(h, 0)` on Windows, where access denied reads as alive. It never signals or ends anything. Its limits: on POSIX a process this account may not signal (PID 1, another user's), an unreaped zombie and a reused PID all read as alive, so wait only on your own job's PID and pair it with `--recheck-in` or a timeout. When the server answers with a timeout, the local conditions are checked once more, so `--timeout 0` with an existing file wakes. | `client.py` (`_local_options`, `_process_alive`, `_wait_command`); tests/test_client_local_waits_588.py |
| Local facts | An optional `local_fact` = `{kind: job-finished\|job-failed\|job-partial\|time-reached\|file-seen, what (≤160), at, detail (≤300)}`, carried in the checkpoint file's `lane_state` (the `Kind: lane-state-v1` record), with any state. `what` and `detail` are each one printable line: NUL, newlines, tabs, ANSI escapes, C1 controls, bidi controls and line separators are refused (r2). A brief shows a `local_fact` only beside the checkpoint its record was written with. It is NOT a key of the checkpoint payload itself, which stays the closed set it was (section 5). It is the worker's own statement: shown as "Observed locally: …", and it changes no review or lifecycle state. A checkpoint that adds only a `local_fact` is accepted with no new server activity. | `lane_state.py`, `lanes.py`, `briefing.py`; tests/test_own_job_588.py |
| Own-job wait | Lane state `waiting-on-own-job` with a required `job` (≤120). It refuses `waiting_on` and allows `expect_by` and `not_before`. It is never a `checkpoint-to-coordinator` question. With no `expect_by`, it is overdue 4 hours after its **episode** began: the first of an unbroken run of own-job records by the holder on that task, so re-posting the wait does not restart the clock (r2; the rule .598 states for every wait). Before its (effective) `expect_by` it raises no flag, `your_move_idle` included (r2). After it, the existing `lane-quiet` and `timer-due` triggers tell the coordinator. It is not nudged before then. | `lane_state.py`, `lanes.py`, `nudges.py`, `waiting.py`, `client.py`, the web lists |
| What is new | Measured from the lane's **own** newest checkpoint; a later checkpoint by someone else is new activity itself (T1). Two comments in one second are ordered by the tracker's sequence (T2). One sentence, `wake_summary`, also says when the task itself changed (a title, description, acceptance or other field edit; r2). It says only that the task changed, not which field (`briefing.py`, `WAKE_TASK_CHANGED`), and only when no entry at all, by others or by the lane, came after the checkpoint (`task_changed_unexplained`). So an edit is not said when the lane's own comment or anyone else's also came in that time: the entry explains the stale cursor, and the edit cannot be told apart from it (r3, note 2). The sentence is built in one place and carried by work rows, brief, every web route and the wait items, and never fingerprinted (T4). | `briefing.py` (`own_baseline`, `entry_order_key`, `wake_summary`, `wake_summary_text`), `http_service.py`, `waiting.py`, `client.py` |
| `wrote_since` | `wrote_since(row, checkpoint, actor_or_role, in_role)`: did the actor or role waited on write on this task after this checkpoint? The lane's own comments never count, and neither does a comment on another task. For .585 and .598. | `briefing.py` |

## 2. The Bridge local wait (spec, not built here)

The Bridge (the editor extension that wakes an agent) gets the same three kinds of local wait. Nothing here changes the server. Where a choice could go either way, this spec makes it the same as the kit's `client.py wait` options, so the two never disagree.

**The tools:**
- `orchestra_local_wait {kind, value, label}` registers a wait and answers `{wait: N}`, its slot number (1-3).
- `orchestra_local_wait_cancel {wait: N}` removes one pending wait and answers `{cancelled: true}`, or `{cancelled: false}` when there was none.

**Inputs, checked before anything is registered.** A bad input is answered `{error: "<field>: <sentence>"}` and nothing is registered.

| Input | JSON type | Accepted |
|---|---|---|
| `kind` | string | exactly `"time"`, `"file"` or `"process"` |
| `value` with `time` | string | a duration `^[1-9][0-9]{0,9}(s\|m\|h\|d)$`, more than zero and at most 14 days (the client's `--recheck-in` rule) |
| `value` with `file` | string | an absolute path, 1-1024 characters, every character printable (no NUL, newline or control). Existence is asked as Node's `fs.existsSync` does: it follows symlinks, so a broken symlink is "not there" and a directory is "there", the same as the client's `os.path.exists`. The file is never opened or read. |
| `value` with `process` | number | an integer from 1 to 2147483647. A string is refused, not converted. |
| `label` | string | 1-120 characters, one printable line (the lane-state `job` rule). Untrusted: shown escaped, and never put into a prompt. |

**How a wait is watched:**
- One poll every 5 seconds for all pending waits. No `fs.watch`: polling behaves the same on every platform, on network drives and through symlinks.
- `time` ends when the monotonic clock passes its deadline. A sleep of the machine does not end it early.
- `file` ends at the first poll that sees the path. A file that appears and goes again between two polls can be missed (the client's 1-second slice has the same limit).
- `process` ends at the first poll where Node's `process.kill(pid, 0)` throws `ESRCH`. `EPERM` reads as alive. That call tests and never signals. No other signal is ever sent.
- The limits are the client's: a zombie or a reused PID reads as alive. So the agent waits only on its own job's PID, and pairs a `process` wait with a `time` wait as a bound.

**Pending waits:**
- At most 3 per lane. A fourth is refused (`{error: "wait: 3 waits are pending; cancel one first"}`), never silently replacing one.
- A wait ends when its condition is met, or when it is cancelled.
- Waits are not persisted: a Bridge restart drops them all. The status bar then shows "Local waits were dropped by a restart" until it is dismissed, and no prompt is sent for them.
- The Bridge reads no checkpoints, so it never clears a wait because of one. An agent that no longer needs a wait cancels it.

**When a wait ends:**
1. The Bridge submits **one fixed prompt**, which names each wait by kind and slot only: *"Your local wait ended (file, wait 2). Read the result yourself, then checkpoint what you saw with a `local_fact` in your `lane_state`."*
2. **No text is ever put into the prompt.** That covers the label, the path, the job's output and the process. The agent knows which wait it set, by its slot, and reads the result itself with its own tools. So the label cannot carry an injection into a prompt.
3. **Coalescing.** The first prompt goes at once. Any wait that ends in the 60 seconds after a prompt is held, and when those 60 seconds are over, one prompt names every held wait: *"Your local waits ended (time, wait 1; file, wait 3). …"*.
4. **The rate bound, as the .582 watchdog has.** At most 6 prompts in any sliding 60 minutes per lane. A wait that ends past the bound is not prompted, now or later: it is never retried. It stays on the status bar as ended until it is dismissed.

**The status bar:**

| When | It shows |
|---|---|
| One wait pending | **"Waiting on own job: LABEL, since TIME"** |
| More than one pending | "Waiting on own job: LABEL, since TIME (+N more)", the oldest first. Clicking lists them all, each with a Cancel. |
| A wait ended | "Own job ended: LABEL" (escaped), until it is dismissed |

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
