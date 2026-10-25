# The lane nudge carrier (kittrial-5bb.415)

Slice 2 of `docs/design/LANE_NUDGE_DESIGN.md` (kittrial-5bb.409). A coordinator's request
(`POST /v1/projects/{pid}/nudges`, `docs/LANE_BOARD_DESIGN.md` section 12) asks that a fixed message reach a
stalled lane. A **carrier** carries it out: a program that can type into the lane's harness. This document is
the contract of a carrier, including the VS Code Bridge, which is not in this repository.

## The credential

An agent of the lane owner's account, with the scope `nudges` and no other. The scope grants one capability,
`nudge.carry`, which no role holds. It is refused combined with any other scope (agent creation, renewal and
worker credentials answer 422), and a stored credential that is mixed anyway holds nothing. It holds no
baseline read: with it every other route answers 403, and the dispatcher refuses it everywhere except the four
routes below. A stolen carrier credential can read the lane names and times of its own account's requests and
answer them: it cannot make a lane do anything, because the text is fixed.

## The four routes (credential only, none takes a project)

- `GET /v1/me/nudges`: the carrier's open requests (pending, or claimed by it) in the projects its agent is
  granted, for lanes whose only owner party is the carrier's own account. Each item is exactly
  `{id, project, lane, message, message_set}`: never the reason, the note or the requester.
- `POST /v1/me/nudges/{id}/claim`: a two-minute lease. A claim that lapses makes the request pending again;
  another carrier's live claim is a 409. The requester is checked **now**: one who no longer coordinates the
  project has the request `cancelled` and the answer is `{claimed: false, state: "cancelled"}`; type nothing.
- `POST /v1/me/nudges/{id}/outcome` with `{outcome, detail?}`, once: `delivered`, `restarted`, `failed` (with
  `detail` one of `not-at-prompt`, `no-session`, `start-failed`), `unsupported` or `coalesced`. A second
  outcome, an outcome after the request's fifteen minutes, an outcome for a request not claimed, and one from
  a carrier that does not hold the claim are refused (409 or 403).
- `POST /v1/me/carrier` with `{kind: "supervisor" | "bridge", lanes: [...]}` (at most 50 lane labels): the
  heartbeat. Poll no faster than every 30 seconds (the answer says `poll_seconds`).

Both claim and outcome are audited (`lanes.nudge.claim`, `lanes.nudge.outcome`); the heartbeat is not.

## The text

The server sends the key (`resume` or `restart`) and `message_set` (`nudge-messages-v1`). The carrier types
the line from its **own** copy of `nudge_messages.py`. A key or set it does not know is answered
`unsupported` and nothing is typed. Never type the requester's words, a reason or a note: the carrier never
receives them.

## When no carrier is there

A request nobody claimed ends after 15 minutes: `not-deliverable` (`no-carrier`) when no carrier that reaches
the lane sent a heartbeat in the 90 seconds (three polls) before that moment, `expired` (`unclaimed`) when one
did. One claimed and never answered is `expired` (`unanswered`). The board's `nudge` field shows the state.

## The reference supervisor

`tools/nudge/supervisor.py` (README beside it): types only when the pane's foreground process is the lane's
harness at its prompt, and refuses to start when its config (which holds the token) is a symlink or readable
by group or others.

## The VS Code Bridge (a contract, not code)

A Bridge is a carrier of kind `bridge`. It must: hold a `nudges` credential of the account that runs the
window; send the heartbeat with the lanes it drives; poll `GET /v1/me/nudges`; claim a request for a lane it
drives; type the fixed text for the key into the agent window through its own interface (never a command,
never a file, never anything else) only when the agent is waiting for input; answer with one outcome; answer
`unsupported` for a key or `message_set` it does not know; and never read tasks. Any change to the Bridge is
its own task in its own repository.
