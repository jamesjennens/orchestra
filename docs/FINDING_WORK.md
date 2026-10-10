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
feedback** (changes requested on something it delivered) and **newer comments** since
its last checkpoint.

**Worker over SSH:** resume with its saved actor, then `work --mine`; for one task
`brief TASK --json`, which flags newer activity.

**Agent:** `GET /v1/agents/me/next`. The reply lists its own tasks first: changes
requested, then a task blocked by its last checkpoint, then a task in progress.
Blocked and in-progress tasks carry `newer_activity`: whether another actor wrote
activity its last checkpoint did not incorporate. No checkpoint or uncertain history
stays unknown. Read the linked task brief before acting: its `newer` summary gives
bounded entry references, counts and coverage. Read the task's history for the full
comments: `GET /v1/projects/PROJECT/tasks/TASK/history`. Reading acknowledges nothing.
This is the current-attention read (kittrial-5bb.114); it does not wait for activity or
deliver a wake notification (the separate kittrial-5bb.207).

### 3. Only when none of its own tasks needs action: the ready list

It claims **exactly one** task that nobody holds, registers its plan on the task, and
works on that one until it is delivered.

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
changes on its contribution for review feedback.

Without a checkpoint, an in-progress task's `newer_activity` and its brief's `newer`
are `null`: there is no known boundary against which to flag a comment. The
coordinator should ask the worker to read the task history and save its first
checkpoint; do not treat the missing flag as proof that the comment was read.

**In the web interface today: no.** The task page shows the task's history. Writing a
comment over the web is not there yet (planned: kittrial-5bb.211). A review that
requests changes on a delivered contribution IS there, and reaches the worker as
feedback, first in its order.

**The command today,** with the client: `comments add TASK --file note.md --json`

### What does not reach a worker

A comment on a parent or coordination task, a message in another task, a change to a
document it read at an earlier run. If a lane has to learn something, it is one of the
three above.

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
