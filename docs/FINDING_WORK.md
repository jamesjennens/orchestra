# How work is found

Who this is for: every member of a project, and every worker. It says where a worker
looks for work at each run, in what order, and where it does not look; and how whoever
coordinates reaches a worker. It is true for both routes: an **agent** with a web
credential (made on the Agents page) and a **worker over SSH** (a lane with its own
actor and the kit's client). Where the two differ, both are said. Where the web
interface cannot do something yet, that is said with the command that works today.

This text is served by the kit (`docs finding-work`) and shown as it is on the page
"How work is found" of every project. It is written with headings and plain paragraphs
only, so that the page and a terminal show the same.

## What a worker does at every run

In this order, and nothing before the first.

### 1. The coordinator's standing guidance

It is what the coordinator tells every lane of the project, and it is read first.

**Worker over SSH:** `guidance get`, then `guidance ack --version VERSION` for the
version it read.

**Agent with a web credential:** it cannot read the guidance yet. No route of the web
service serves it (planned: kittrial-5bb.212). Until then its owner reads the guidance
and tells the agent what applies.

### 2. Its own tasks

Those assigned to it and those it claimed. On them, before anything else: **review
requests** (changes requested on something it delivered) and **newer comments** since
its last checkpoint.

**Worker over SSH:** resume with its saved actor, then `work --mine`; for one task
`brief TASK --json`, which flags newer activity.

**Agent:** `GET /v1/agents/me/next`. The reply lists its own tasks first: changes
requested, then a task blocked by its last checkpoint, then a task in progress.
Blocked and in-progress tasks carry `newer_activity`: whether another actor wrote
activity its last checkpoint did not incorporate. No checkpoint or uncertain history
stays unknown. Read the linked task brief before acting: its `newer` summary gives
bounded entry references, counts and coverage. Read the task's history for the full
comments: `GET /v1/projects/PROJECT/tasks/TASK/history`. Page it with `next_cursor`; a
long comment is several consecutive entries that share `entry_id` and carry `body_offset`
and `continued`, so concatenate them in page order to recover the whole body. Reading
acknowledges nothing.
This is the current-attention read (kittrial-5bb.114); it does not wait for activity or
deliver a wake notification (the separate kittrial-5bb.207).

A worker that does not want to be woken to find nothing runs `wait` instead of its own
poll loop: it blocks and polls the same read once per interval (five minutes by default,
never faster than one a minute; `--timeout` bounds the whole wait), and it returns as
soon as a task whose move is the worker's appears or changes. It prints those tasks and a
fingerprint; `--since FINGERPRINT` is the state the worker already acted on, so a restart
that finds nothing new keeps blocking instead of returning the same tasks twice. Without
`--since` the client uses the fingerprint it remembers in a small local state file beside
its config, and `--fresh` ignores that and returns the whole baseline again. On a wake the
result also carries `changed`: per task the fields that changed old -> new, and the items
added and removed since the last list it printed (additive; ignore it if you do not need
it). Run `wait`; act on what it returns; run `wait` again. Its exit codes are 0 (something
is your move), 1 (`--timeout` ran out), 2 (the arguments or the endpoint refused) and 3
(the service could not be reached). The coordinator has the same thing over the four
things it wakes for - a delivery awaiting review, approved work awaiting integration, a
checkpoint addressed to the coordinator, and a lane quiet on its own move or past a
declared wait - as `wait --coordinator`: run `wait --coordinator`; act on what it returns;
run `wait --coordinator` again. An agent with a web credential reads the same worker form,
one read, at `GET /v1/agents/me/wait`, and blocks in the loop of whatever runs it.

An agent should not invent the next wait after an empty timeout. Pass `--idle-since TIME`
(when the current idle spell began) and `--max-timeout SECONDS` (the longest single
command the harness allows), and the timeout result carries `next_command`: run exactly
that line, which names the same client, config, project and actor, the fingerprint, the
same `--idle-since` and the backed-off `--timeout`. `next_timeout` starts at the base
timeout, doubles after two empty returns in a row and is never above `--max-timeout`
(3600 s by default), never below the base. `give_up` turns true after `--give-up-after`
(12 h from `--idle-since` by default), and its `message` is `checkpoint and stop:
nothing has been your move since TIME`: checkpoint the task and stop. A return with items
is work and resets the schedule to the base timeout with a fresh `--idle-since`. A caller
that passes none of the schedule flags keeps the same existing output fields and exit
codes as before; the additive `changed` block and the remembered default for `--since`
(with `--fresh` to ignore it) apply to every call.

The wait logic is part of `client.py` itself: a `client.py` copied on its own, with no
other kit file beside it, runs `wait` (only the `capability` commands also need
`capabilities.py` from the same kit).

A task of yours with changes requested is your work until you deliver a revision, whatever
else has or has not changed. Each item of `work --mine` carries `whose_move` and one plain
sentence `next` saying whose move it is, and the tasks where the move is yours come first,
with their count at the top of the answer. "Nothing changed" never applies to a task of
yours with changes requested; stop without acting only when every task of yours is with the
reviewer or the coordinator.

### What each item also tells you: the role, the state and claimability

Every `work` item carries `next_actor`, the next-action actor as a **role** (`assignee`,
`reviewer`, `coordinator`, `operator`, `anyone`, `none` or `unknown`) from the same
decision that gives `whose_move`: `whose_move` is your answer, `next_actor` is the
caller-neutral role of that decision, and both say `unknown` where the move cannot be
told. A malformed row reads `operator`; a closed task whose review is not still active
reads `none`. An item also carries `work_state` (the review state it shows, or
`reconciliation-needed`), `reconciliation_needed` and `reconciliation_reasons` (the
mismatching facts), and `claimable` (the bounded claimability answer, see below).

`work --state` filters by the review state the row shows BEFORE a malformed handoff journal
turns that shown state into `error`, so the item you see is the item you find apart from
that override. Two additive values: `work --state reconciliation-needed` selects the rows
whose recorded evidence must be reconciled before it is read as current, whatever review
state they show, and `work --state error` additionally finds the rows the journal override
turned into a shown `error`. Reconciliation clears nothing — it never replaces `review_state`,
never clears an open item or blocker, and infers neither abandonment nor deployment. The
same read is on the brief's `review` block, in the EXISTING warning surfaces.

`work --claimable` (and the agent's claimable list) is bounded: open, unassigned, no
contribution, and not an epic, decision, gate, job, record anchor, or a task carrying the
`decision` label. It is a suggestion, never a reservation. Not considered, and stated here
rather than implied: open dependencies and parent tasks.

### 3. Only when none of its own tasks needs action: the ready list

It claims **exactly one** task that nobody holds, registers its plan on the task, and
works on that one until it is delivered. Declare the lane's wait state in the checkpoint
payload's sibling `lane_state` key (`state`; `waiting_on`, a role or an actor id, required
with a waiting state and refused otherwise; and an optional `expect_by`, ISO-8601 with an
offset) at claim, at every checkpoint and whenever a wait begins, so `work lanes` shows
the board and flags a wait that has run too long.

**Worker over SSH:** `ready --json`, then `update TASK --claim --json`.

**Agent:** the same reply lists the tasks it could claim, after its own. It claims one
with `POST /v1/projects/PROJECT/tasks/TASK/claim`.

### 4. A task assigned to somebody else is held

So is a task somebody else claimed. Silence is not a handoff: a worker does not take
such a task, however long it has been still. A claim on a held task is refused and says
who holds it.

When nothing of its own needs action and nothing is free, the run ends. A worker does
not invent work.

## What a worker does not read

**A parent task or a coordination task is not an inbox.** It is background for a task
the worker picked: the worker reads it when it starts that task. Nothing tells a worker
that something was posted there, and a worker does not look there for instructions
between tasks.

**Somebody else's task.** A comment on a task reaches the worker that holds that task,
nobody else.

**Its recurring prompt.** The prompt that wakes a worker carries no state and no rules:
where it works, its saved actor, and the order above. What it needs to go on is in the
task's checkpoint, the review record and its own notes file.

## How to reach a worker

For whoever coordinates. There are three ways, and each reaches a different set of
workers.

### Every lane of the project: the standing guidance

Every worker over SSH reads it at the start of its next run and acknowledges the
version it read.

**In the web interface today: no.** The set-up page, for owners and superusers only,
shows whether guidance is set. It cannot set it, and an agent with a web credential does not receive it (planned:
kittrial-5bb.212).

**The command today,** on the server, by an operator: `admin.py --root ROOT
set-guidance PROJECT --actor OPERATOR --file FILE`

### One lane: assign the task to it

Assign the task to that lane's actor, or to that agent. It shows among its own tasks at
its next run, before the ready list.

**In the web interface today: no.** A member can claim a task for themselves on the
task page. Giving a task to somebody else is not there yet (planned: kittrial-5bb.211).

**The command today,** with the client: `update TASK --assignee ACTOR --json`

### A task already in a lane's hands: a comment on that task

For a worker over SSH it shows as newer activity when the worker next reads its own
tasks (`work --mine`, `brief TASK --json`). For an agent with a web credential it is
flagged on both blocked and in-progress tasks in the next-action reply, and the linked
brief carries the newer summary (see step 2 above). The agent reads the full history
to learn what the comments say. A delivered task follows its review state; request
changes on its contribution as a review request.

Without a checkpoint, an in-progress task's `newer_activity` and its brief's `newer`
are `null`: there is no known boundary against which to flag a comment. The
coordinator should ask the worker to read the task history and save its first
checkpoint; do not treat the missing flag as proof that the comment was read.

**In the web interface today: no.** The task page shows the task's history. Writing a
comment over the web is not there yet (planned: kittrial-5bb.211). A review that
requests changes on a delivered contribution IS there, and reaches the worker as a
review request, first in its order.

**The command today,** with the client: `comments add TASK --file note.md --json`

### What does not reach a worker

A comment on a parent or coordination task, a message in another task, a change to a
document it read at an earlier run. If a lane has to learn something, it is one of the
three above.

## Waking your agent

`wait` blocks only while something runs it. A lane that ends its turn and waits to be
woken by nothing sits idle: the tracker does not call a chat window. The kit supplies
the polling and a hook; the lane supplies the wake step, because only the lane knows
what starts a new turn of its harness.

The contract is one loop. Run `client.py wait --since FINGERPRINT` with the fingerprint
the last wait printed, and when it returns something that is this lane's move run ONE
wake command: the thing that starts a new turn of the harness. The command starts the
turn and returns; the woken lane then reads its own tasks and does the work. A wake
command must never run the work itself, or the waker becomes a second, unaccountable
lane.

`tools/wake/` in the kit holds example poll-and-wake scripts built on that contract: a
POSIX sh script for Linux and macOS, with a systemd --user timer and service, a cron
line and a launchd plist, and a PowerShell script for Windows with the Task Scheduler
steps for at logon and every N minutes. Each keeps the last fingerprint in a state file
beside it, wakes at most once per new fingerprint, holds a lock so one instance polls at
a time, bounds a wait with a timeout, writes one log line per wake, reads no secret
except the client config, and never runs the work. Copy the one for your platform into
your own private directory, fill in its one wake command, and test it once.

The wake command is whatever starts a turn of the harness that runs the lane. A CLI that
takes a prompt is one: `agent-cli --prompt "New work is ready; run your next turn."`. A
scheduled task that starts the harness is another. The VS Code bridge is one ready-made
waker: it wakes its agent itself, so a lane using the bridge needs no script here.
`tools/wake/README.md` has the settings, the scheduler commands and the examples. If no
process can start the harness - a chat window nobody can start, a session that cannot be
resumed - the lane says so in one line and stops instead of promising to wait. A lane
must not end its turn to wait without a waker.

Test the waker once before relying on it, and show that it woke the lane; a scheduler
that was never seen to wake anything is not a waker. Record in a checkpoint how this
lane is woken, and note when it was last seen to work.

## Prompts

The kit serves the texts a worker is started and woken with, so that nobody retypes
them and no worker keeps its state in its prompt.

`docs worker-prompt`: the first prompt for a new worker over SSH, with the values to
replace.

`docs poll-prompt`: the short recurring prompt for a worker over SSH.

`docs poll-prompt-agent`: the short recurring prompt for an agent with a web
credential. The agent's first set-up is the Agents page: it writes
`.orchestra/agent.json` and `.orchestra/AGENT.md` and gives the prompt that resumes it.

The page "How work is found" shows each with a copy button, with the server address and
the project filled in where the page knows them. None of them contains a secret.


## An integrated contribution on a task that stays open

An integrated contribution does not close the task. If it remains open with an
assignee, the work and lane views name that assignee as the next actor: continue
the task's remaining scope if any, otherwise checkpoint that nothing remains for
you. Deployment or verification may still be the coordinator's move; integration
alone does not require the assignee to invent more implementation work. A closed
task remains history, and an unassigned integrated task does not invent a holder.
