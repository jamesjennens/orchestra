# Nudging a stalled lane: an audited request, a fixed message, a carrier (design note, copilot-goomba lane)

Design note for `kittrial-5bb.409`. Base: main `5c10d43` (`Integrate kittrial-5bb.388 (6e0a362) for 1024h`).
**Design only: no product code in this delivery.**

Plan checkpoint `01a1725b`, verified by the coordinator on 2026-10-25, with these answers:
- **Q1** the request lives in the authority state. **Q2** an agent may request, with a tighter limit and
  the same issuer rule as `kittrial-5bb.410`. **Q3** an audit-only note of at most 200 characters, never
  delivered. **Q4** the carrier is a dedicated credential scope, never `coordinate`. **Q5** the VS Code
  Bridge gets a contract only. **Q6** an automatic nudge only on `your_move_idle`, one per idle episode.
- **Added:** §2 gives the exact fixed text of each message and what the lane sees; §5 says how a nudge is
  shown, and withheld from automatic sending, when the lane's own declared state says it is legitimately
  waiting on someone else.

**Citations.** Every claim about the kit is `file:line` on main `5c10d43`. Three tasks are **not on that
main** and are cited by task id, never by line: `kittrial-5bb.405` (Lanes scope: owners, coordinators and
coordinator-grant agents read every lane), `kittrial-5bb.410` (a grant counts only while its issuer is still
an owner or a superuser) and `kittrial-5bb.407` (server-written delegation). Where this note leans on one,
it says "after 1024h" and says what it needs from it.

## 0. The problem in one paragraph

An interactive lane (Claude Code, Copilot CLI, Codex, an agent window driven by the VS Code Bridge) can stop
at its prompt, for example after a model API error. It is not in a kit wait, so nothing the kit writes can
reach it. Today only the host operator can restart it, by typing into the lane's terminal as the lane's
user. A project coordinator works on the web and cannot. This note adds the missing route in four parts: a
**request** a coordinator writes, a **closed set of messages** the lane may receive, a **carrier** that
delivers one, and an **audit and a board** that show what happened. It adds no way to tell a lane what to do.

## 1. What exists today (the facts this design stands on)

- **The board already finds a stalled lane.** `lanes.py:33` names three flags. `your_move_idle` is set when
  the one move decision says the move is the holder's and the holder has written nothing on the task for
  longer than the threshold (`lanes.py:243-244`; the age is `holder_idle_seconds`, `lanes.py:165-176`).
  `waiting_too_long` is set only when the lane's own declared state is `waiting-on-coordinator`,
  `waiting-on-review` or `waiting-on-user` and its checkpoint is older than the threshold
  (`lanes.py:239-240`; `docs/LANE_BOARD_DESIGN.md:139-144`). The default threshold is 15 minutes, 1 to 10080,
  and an argument of the read (`lanes.py:25-26`). **A lane waiting on someone else is not a stalled lane:**
  only `your_move_idle` means "the lane owes a move and is silent".
- **The kit's wake reaches only a lane in a wait.** `wait` returns when something is the lane's move
  (`waiting.py:20-23`); `tools/wake/README.md:3-5` says the kit "cannot know how a harness is woken" and
  leaves that to the lane. A session stopped at its prompt is outside both. `tools/wake/README.md:104`:
  "A lane must not end its turn to wait without a waker."
- **The coordinator already hears about a quiet lane.** The coordinator form of `wait` has a `lane-quiet`
  trigger for any of the three flags (`waiting.py:138`, `waiting.py:666-677`). Nothing acts on it but the
  coordinator, and a coordinator on the web has no way to act.
- **A lane is a registered session or a personal agent.** The session registry holds `actor`, `name` and
  an owners map actor to principal (`sessions.py:73-107`); it also carries run records with a
  `last_heartbeat` (`sessions.py:108-129`). `http_authority.actor_parties` turns an agent actor into its
  owner account (`http_authority.py:539-560`); that is the fact §3 uses to say whose lane a carrier may serve.
- **Who may coordinate is one rule.** A role's capabilities are in `ROLE_CAPABILITIES`
  (`http_authority.py:378-392`): a coordinator and an owner hold `coordinate`, an owner also `project.admin`.
  A credential never holds `coordinate`; an agent gets it only from the project's coordinator grant, only in
  that project and only while its account holds it (`http_authority.py:612-627`, `GRANT_CAPABILITIES`,
  `http_authority.py:392`). After `kittrial-5bb.410` the same function also requires the grant's issuer to
  be still an owner or a superuser. `decide()` (`http_authority.py:781`) reads a superuser as an owner of
  every project.
- **Who reads the board on main.** `project_lanes` (`http_service.py:12893`) pages the one `lanes` read and
  `_lanes_scope` (`http_service.py:12955`) gives `all` only to a listed installation operator, `own` to
  everybody else. `kittrial-5bb.405` (after 1024h) gives `all` to owners, coordinators and coordinator-grant
  agents. The nudge route of §1.3 does not depend on that page scope: it decides its own authority.
- **Audit and rate limit patterns exist.** One redacted audit event per action, reason cut at 200
  characters (`http_auth.py:1266-1287`); the list is bounded (`AUDIT_LIMIT`, `http_auth.py:113`). A
  rate limit answers 429 `rate_limited` (`http_auth.py:243-244`) with a `Retry-After` header
  (`http_service.py:5551-5553`). A mutating route is wrapped in `_mutate`, which checks one capability and
  makes the request idempotent by `Idempotency-Key` or `operation_id` (`http_service.py:5610-5625`,
  `http_service.py:139`, `http_service.py:3226`). A grant route is the model for a project-scoped,
  server-written record kept beside the project (`grant_coordinator`, `http_auth.py:3439-3465`, route
  `http_service.py:9550-9561`).

## 1. The nudge request

### 1.1 The record

`lane-nudge-v1`, written **only by the server**, kept in the authority state beside `coordinator_grants`
as `state['nudges'][project] = [record, ...]`, newest last (Q1). It is bounded: at most 200 records per
project and 30 days, the oldest finished one first, so the state file does not grow without end (the audit
list is bounded the same way, `http_auth.py:953-954`).

| Field | Meaning |
| --- | --- |
| `id` | random, server-made; never derived from the lane or the time |
| `project`, `lane` | the project and the lane's canonical actor label, as the board shows it |
| `message` | one of the closed set of §2: `resume`, `restart` |
| `reason` | one of a closed list: `stalled-at-prompt`, `api-error`, `lost-wake`, `no-answer`, `other` |
| `note` | optional, at most 200 characters, **audit only**, never sent to the lane or a carrier (Q3) |
| `requested_by` | `{kind: person|agent|auto, user_id, agent_id}` taken from the principal, never from the body |
| `requested_at` | server time |
| `declared_wait` | snapshot of the lane's declared state at the request (§5): `{state, waiting_on, since, whose_move}` or null |
| `state` | `pending` → `claimed` → `delivered` \| `restarted` \| `failed` \| `unsupported` \| `coalesced`; or `expired` \| `not-deliverable` \| `cancelled` |
| `claimed_by`, `claim_expires_at`, `outcome_at`, `outcome_detail` | set by the carrier (§3), `outcome_detail` a closed code, not text |

### 1.2 Who may request

One rule, the existing one, read **live at the request** and again when a carrier claims it:

- **A person**: an owner or a coordinator of **that** project (`coordinate`, `http_authority.py:378-392`), or
  a superuser (`decide()` reads one as an owner, `http_authority.py:781`). A contributor, a viewer, a
  member of another project and an anonymous caller are refused with the one sentence the project routes
  already use for "not here".
- **An agent**: only through its coordinator grant in that project, which `credential_capabilities` turns
  into `coordinate` (`http_authority.py:572-594`, `http_authority.py:612-627`). After `kittrial-5bb.410`
  that grant counts only while its issuer is still an owner or a superuser (Q2): a demoted, removed or
  disabled issuer ends the agent's right to nudge at the next request, with no new code in the nudge route.
  An agent may not nudge **its own lane or a lane of its own party** (`actor_parties`,
  `http_authority.py:539-560`): a stalled coordinator agent cannot be woken by itself, and the rule stops
  one owner's agents from nudging each other in a loop.
- **No delegation is needed.** `kittrial-5bb.407` (after 1024h) makes a decision issue the root of a
  coordinator agent's right to **accept a record**. A nudge accepts no record and changes no authority, so
  it uses the live grant alone. If the owner wants a project where agents may not nudge, the owner removes
  the grant, which is the control the owner already has.
- **A release grant, a requirement-editor role and a delegation are not a nudge right.** Only `coordinate`
  is checked, so no other capability widens it (`CAP_RELEASE` is held apart, `http_authority.py:341`).

### 1.3 The route

`POST /v1/projects/{pid}/nudges`, body `{operation_id, lane, message, reason, note?,
acknowledge_declared_wait?}`; an unknown field is refused, as the neighbouring grant route refuses one
(`refuse_unknown_fields`, `http_service.py:9553`). It is a `_mutate` with `capability=CAP_COORDINATE`, so
the capability check, the idempotency reservation and the audit are the existing ones.
`GET /v1/projects/{pid}/nudges?lane=` lists the newest records for the board.

Rules, each a test in slice 1:

1. **The lane must be in this project.** The route resolves `lane` against the project's own lanes read
   (every lane, whatever the caller's page scope); a lane that is absent, or that exists only in another
   project, is the **same** 404 sentence, so the route is no oracle for another project's lanes.
2. **Coalescing.** A second request for the same lane and message while one is `pending` or `claimed`
   answers the existing record (200, the same `id`), writes nothing and audits nothing: a double click is
   one nudge.
3. **Rate limits**, per project, answered 429 with `Retry-After`:
   a person: 1 per lane per 5 minutes and 6 per lane per hour, 30 per project per hour;
   an agent: 1 per lane per 15 minutes, 3 per lane per hour, 10 per agent per hour. An automatic nudge has
   its own bound (§4). A refused request writes no record.
4. **Declared wait.** If the lane's own declared state is `waiting-on-*` the request needs
   `acknowledge_declared_wait: true`, else 409 with the sentence of §5; the snapshot is stored either way.
5. **Time.** A `pending` record expires after 15 minutes (`expired`); a claim holds for 2 minutes.
6. **Audit**: `lanes.nudge.request`, outcome `committed`, with `project_id` and `reason =
   '<message> <lane>: <reason>'` cut at 200 characters (`http_auth.py:1266-1287`); the note is appended
   inside that cut. The carrier's outcome is audited too, `lanes.nudge.outcome`. The audit carries who
   (`user_id`, `credential_id`, so the agent), the lane, the time and the reason, which is what the task
   asks for.
7. **A request is never a command to the carrier.** It carries the closed `message` and nothing else the
   carrier reads (§3.2).

### 1.4 What the board shows

The `lanes` rows gain one read-only field, `nudge`: the newest record for that lane in that project with
`{id, message, state, requested_at, requested_by_display, outcome_at, outcome_detail}` and no `note`. The
Lanes page (`docs/LANE_BOARD_DESIGN.md:192-209`) shows it as one sentence under the row
("Nudged 10:42 by Cora (resume): delivered 10:43") and a **Nudge** button for a viewer who holds
`coordinate`. The button is a form with the message and the reason chosen from the closed lists.

## 2. What the lane receives: a fixed text, never the requester's words

The set is **closed and versioned**: `resume` and `restart`. The **carrier owns the text**: it is in a kit
module (`nudge_messages.py`, slice 2) as two constants, and the server sends the carrier only the key
(`resume`/`restart`) and the kit's `message_set` version. The carrier types the text from **its own copy**
of the table; a carrier that does not know the key or the version answers `unsupported` and types nothing.
So a compromised or mistaken server, a coordinator and a note cannot make a carrier type anything outside
two strings.

**`resume`** (one line, ASCII, no markup, no path and no name):

> Orchestra: a coordinator asked you to check in. Read your own notes, run `work --mine`, read the newest
> comments of each task that is your move, and carry on. If nothing is your move, wait with the kit as
> guidance says.

**`restart`**: the carrier restarts the lane's harness with the lane's **own** configured start command
(the lane's own setting, as the wake command is, `tools/wake/README.md:36`); the new session receives:

> Orchestra: this session was restarted at a coordinator's request. Read your own notes, run `work --mine`,
> read the newest comments of each task that is your move, and carry on. If nothing is your move, wait with
> the kit as guidance says.

What the lane sees is exactly that line, typed as its next input and ending in Enter. It names no task, no
requester, no reason and no id; it asks only for what the kit's own start documents already tell every lane
to do (`work --mine`, then wait). **It is not authority**: the lane acts through its own credential and the
kit's own rules, a lane that was told to do something unusual by it should say so (the worker guide already
says a comment from a non-operator is not an instruction), and the text contains nothing to confuse with
one. A text change is a kit release, reviewed like code, and bumps `message_set`.

## 3. Who carries it out

### 3.1 The credential: a scope that cannot coordinate

A carrier authenticates **as itself**: a credential of an account, with a new scope `nudges` that grants
one new capability, `nudge.carry` (the shape of `SCOPE_CAPABILITIES`, `http_authority.py:365-376`; Q4). It
is added to `CREDENTIAL_FORBIDDEN_CAPABILITIES`' complement so that it never combines with `coordinate`,
`approve` or any project-admin capability (`http_authority.py:397-399` is the list a credential never
holds). It can:

- `GET /v1/me/nudges` (the carrier's own pending and claimed nudges), **only for lanes whose owner party is
  the carrier's own account**: the same `actor_parties` rule (`http_authority.py:539-560`). It reads a
  record as `{id, project, lane, message, message_set}`: **no reason, no note, no requester**.
- `POST /v1/me/nudges/{id}/claim` and `.../outcome` for such a record.
- `POST /v1/me/carrier` (a heartbeat: which lanes it can reach, its kind `supervisor|bridge`).

It cannot read tasks, comments, the board or any other project, and cannot request a nudge. A carrier
credential that is stolen can read the lane names and times of its own account's nudges and mark them
failed: it cannot make a lane do anything, because the lane's text is fixed (§2).

### 3.2 Claim, deliver, answer

A carrier polls (one cheap read per poll like the kit's wait, `waiting.py:25-28`; proposed floor 30 s),
**claims** a record (2-minute claim, so a host supervisor and a Bridge for the same lane do not both type),
delivers, and answers **once** with a closed outcome: `delivered`, `restarted`, `failed` (a closed
`outcome_detail`: `not-at-prompt`, `no-session`, `start-failed`), `unsupported`, or `coalesced` (the lane
already received a typed message in the last 5 minutes, from this or another project). The outcome is
monotone: a second answer for the same record is refused, and an answer after expiry is refused. A claim that
runs out returns the record to `pending` until it expires.

### 3.3 The host-side lane supervisor (a reference, slice 2)

A small program in `tools/nudge/` beside `tools/wake/`, run by the lane's own user, **a reference to copy,
as `tools/wake/` is** (`tools/wake/README.md:7-9`), not a service the kit runs. It reads its credential from
its own config, the only credential input (as `wake.sh` does, `tools/wake/README.md:33-36`), polls, and for
each record:

1. **Checks the target is the harness at its prompt** before typing: the pane's foreground process matches
   the lane's configured harness command and the screen shows the harness's own prompt. Typing into a shell,
   an editor or a running tool is the one real danger of this design (a typed line is executed by
   whatever has the terminal), so a supervisor that cannot tell answers `failed not-at-prompt`.
2. Types the fixed text and Enter (`resume`), or stops the session, starts it with the lane's own start
   command and types the text (`restart`); answers.
3. Never runs the lane's work and never reads its tasks.

Terminal control (a tmux pane, a screen session, a pty) is the lane's own setting, written in its env file;
the kit supplies the loop, the lock and the log lines, as `wake.sh` does.

### 3.4 The VS Code Bridge: a contract, not code

The Bridge is **not in this repository** (`tools/wake/README.md:93-96` says only that it wakes its own agent
and needs no second waker). So this note specifies the contract and nothing else: the Bridge is a carrier of
kind `bridge`: it holds a `nudges` credential of the account that runs the window, claims a record for a lane
it drives, types the §2 text into the agent window through its own interface (never a command, never a
file), answers with the §3.2 outcomes and sends the heartbeat. Any change to the Bridge is its own task in
its own repository.

## 4. Automatic nudges (Q6)

Off by default, and **a project setting an owner turns on** (`project.admin`, the coordination settings
the web already has): an installation that never asked for a program to type into its lanes does not get one
by upgrading. When on, a nudge is requested by the server (`requested_by.kind = auto`, audit action
`lanes.nudge.auto`, no user) and only when **all** of these hold, read from one `lanes` read:

1. The lane has the **`your_move_idle`** flag for a task: the move is the lane's own and it has written
   nothing on it for longer than `max(2 × threshold, 30 minutes)` (`lanes.py:243-244`). Not `waiting_too_long`
   (§5) and not `expect_by_passed`.
2. The lane's declared state for that task is **not** `waiting-on-*` (§5).
3. A carrier for the lane's owner has been seen within 3 poll intervals (§6).
4. No automatic nudge has been sent in this **idle episode**: the episode starts at the lane's newest write
   on the task and ends at its next one, so a nudge that wakes the lane resets it, and a lane that stays
   stalled is nudged **once**.
5. At most 2 automatic nudges per lane per 24 hours and 1 per 6 hours, across projects.
6. The message is `resume`; an automatic nudge never restarts a session.

**When the coordinator is told.** The kit already tells the coordinator at the threshold (`lane-quiet`,
`waiting.py:666-677`). With the setting on and a carrier present, the coordinator is told **one threshold
after** the automatic nudge instead of at once, if the lane still has not written; the row carries the
nudge and its outcome, so the coordinator reads "nudged 10:42, delivered, still silent 10:58". A lane with no
carrier, or with the setting off, is told at the threshold exactly as today. The evaluation runs when a
carrier polls (the one who can deliver) and when the board is read, so the service needs no timer of its own.

## 5. A lane that is legitimately waiting

The lane's own declared state (`lane-state-v1`, `docs/LANE_BOARD_DESIGN.md:30-55`; `lanes.declared_state`,
`lanes.py:126-134`) is the lane saying "my move is somebody else's". The board separates three cases:

| Declared state | whose_move | Board | Manual nudge | Automatic nudge |
| --- | --- | --- | --- | --- |
| `working`, or none declared | the lane's | `your_move_idle` flag when silent | allowed | allowed (§4) |
| `waiting-on-*` | **not** the lane's | chip "Waiting on `<who>` since `<age>` (the lane's own record)", `waiting_too_long` after the threshold | allowed **with an explicit confirmation** | **never** |
| `waiting-on-*` | the lane's (the declaration is stale: the 2026-10-11 case, `docs/LANE_BOARD_DESIGN.md:143-145`) | chip "Declared waiting, but the move is the lane's" | allowed, confirmation | **never**: a stale declaration is a question for the coordinator, not a reason to type into a session |

A manual nudge to a waiting lane is allowed because the coordinator may know the lane's wait has died (a
kit wait that never woke). The confirmation is part of the contract, not the page: without
`acknowledge_declared_wait: true` the route answers 409 "This lane says it is waiting on `<who>` since
`<age>`. Nudge it anyway?" (the sentence is fixed; `<who>` is the closed `waiting_on` value), the Lanes page
shows the same sentence and a second click, and the record stores the snapshot so the audit shows the
coordinator knew. An automatic nudge never reads a declared wait as a stall.

## 6. A lane with no carrier

A carrier sends its heartbeat with the lanes it can reach (§3.1). A lane whose owner has **no carrier seen
within 3 poll intervals** has no deliverable nudge. Then:

- The Nudge button is still shown but its form says "No supervisor is running for this lane's owner, so a
  nudge cannot be delivered" (the same sentence as the record's outcome); a request is **still accepted
  and audited** (a coordinator's attempt is a fact worth keeping) and is marked `not-deliverable` after
  its 15-minute expiry, never `delivered`, and never silently dropped.
- The row's `nudge.state` shows `not-deliverable` and the coordinator reads the one place left to act: the
  lane's owner, outside the kit.
- Automatic nudges are not sent (§4 rule 3), and the coordinator is told at the threshold as today.

A session that is gone entirely (no process) is the `restart` case: a supervisor that can start the lane's
harness answers `restarted`; one that cannot answers `failed start-failed`.

## 7. Security

| Case | Rule |
| --- | --- |
| Who may request | `coordinate` in **that** project, live; an agent needs an effective grant whose issuer still qualifies (`kittrial-5bb.410`); never `release`, a delegation or a role of another project |
| Cross-project | the lane is resolved in the request's project only; another project's lane is the same 404 as a missing one; a carrier reads only its own account's lanes; the audit and the record are per project |
| Free text | none reaches a lane or a carrier: `message` and `reason` are closed lists, `note` is audit-only and cut at 200 characters, a carrier types only its own constants |
| Injection by the server | the carrier's text table is its own and versioned; an unknown key or version is `unsupported` |
| Terminal injection | the carrier types only into a harness at its prompt (§3.3); the text is one ASCII line with no control characters |
| Rate limits and loops | §1.3 rule 3, coalescing, a self- and own-party rule for agents, §4 caps, one outcome per record |
| Replay and duplicates | `Idempotency-Key`/`operation_id` on the request (`http_service.py:3226`); a monotone state; a claim lease; an outcome answered once |
| Authority | a nudge **never grants or changes authority**: no capability, role, grant or scope is read or written by it; the lane acts through its own credential; the carrier scope holds one capability and never `coordinate` |
| Stolen carrier credential | reads lane names and times of one account, can fail a nudge; cannot request, read tasks, or make a lane act |
| Revocation | demoting a requester or an issuer ends the next request; a request already `pending` is re-checked at claim, and is `cancelled` if its requester no longer holds `coordinate` |
| Audit | every request and outcome is one redacted event with who, lane, time and reason (`http_auth.py:1266-1287`); a refused request is audited as refused |
| Older kits | the new field is read-only and additive on the lanes row; an older client ignores it; an older carrier answers `unsupported` |

## 8. Slices (four)

1. **The request** (authorization; full adversarial review with mutants). `lane-nudge-v1`, the route, the
   closed lists, the live authority check, the same-404, coalescing, rate limits, the declared-wait
   confirmation, the audit, expiry, the board's read-only `nudge` field. No carrier yet: every request ends
   `not-deliverable`. Tests: one per row of §7 and §1.3; mutants: a requester of another project, a
   contributor, an agent whose issuer was demoted, a body that names a note as a message.
2. **The carrier** (authorization). The `nudges` scope, `nudge.carry`, the three carrier routes, claim and
   outcome, the heartbeat, `nudge_messages.py`, the reference supervisor in `tools/nudge/`, and the Bridge
   contract in the docs. Tests: a carrier reads only its own account's lanes and never a reason or a note; a
   second outcome is refused; an unknown version is `unsupported`; the scope never combines with
   `coordinate`.
3. **The web and the guidance.** The Lanes page's nudge sentence, button, closed-list form,
   declared-wait confirmation and no-carrier sentence (`web/js/views/project.js`), the coordinator guide
   text (`agent_prompts.py`) and `docs/LANE_BOARD_DESIGN.md`, `docs/HTTP_DEPLOYMENT.md` and
   `tools/wake/README.md` pointing at it.
4. **Automatic nudges.** The project setting (default off), the eligibility of §4, the idle episode, the
   caps, the later coordinator wake. Tests: never on `waiting_too_long`; never on a declared wait, either
   kind of §5; once per episode; no carrier means none; setting off means none.

Slices 1 and 2 change who may do what, so each is an authorization change. Slice 3 depends on 1 and on
`kittrial-5bb.405` (a coordinator sees the lanes), slice 4 on 2 and 3.

## 9. What this note does not do

- It writes no code, and changes no existing behaviour.
- It does not design the Bridge's own change: only the contract it must meet.
- It does not decide the numbers in §1.3 and §4 for ever: they are starting bounds a project may tighten
  by setting, and slice 1 or 4 may argue for others with evidence.
- It adds no free-text nudge and no "run this" message. If a coordinator needs to tell a lane something,
  the lane's comment on its task is still the route, and the kit's wait wakes it when it is waiting.
