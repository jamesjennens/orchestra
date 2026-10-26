# Reference lane nudge supervisor (kittrial-5bb.415)

A program to **copy and adapt**, as `tools/wake/` is: not a service the kit runs. Run it as the lane's
own user, on the host where the lane's terminal is. The contract is `docs/NUDGE_CARRIER.md`.

    python3 supervisor.py --config /home/you/.orchestra/nudge.json            # loops, 30 s apart
    python3 supervisor.py --config /home/you/.orchestra/nudge.json --once     # one poll

## What it does

Each poll it sends a heartbeat (`POST /v1/me/carrier`, the lanes it reaches), reads its own open
requests (`GET /v1/me/nudges`), and for each request for a lane in its config it claims it, then:

1. `resume`: types the kit's fixed text and Enter, **only if** the terminal's foreground process is the
   lane's configured `harness` and the last non-empty screen line matches its `prompt` **in full** (an empty pattern is refused at start), and
   the pane is not in tmux copy or scroll mode and not dead. The check and the typing are one tmux `if-shell -F`
   command that prints which branch ran; if the pane turned blocked after the earlier look, nothing is typed. Otherwise it types
   nothing and answers `failed not-at-prompt`; no terminal at all is `failed no-session`.
2. `restart`: restarts the lane's own `start_command` in the terminal, waits up to a minute for the harness
   at its prompt, then types the fixed `restart` text (`restarted`); no command or no prompt is
   `failed start-failed`.
3. A terminal that fails under it (tmux timeout, tmux gone) is answered `failed no-session`; the supervisor does
   not die holding the claim. A request id that is not `nudge-` and 24 hex digits is skipped and never used in a
   URL or a log line.
4. An unknown message key or message-set version is `unsupported` and nothing is typed. A lane that was
   typed to in the last five minutes is `coalesced`.

The text typed is always from `nudge_messages.py` of this kit, never from the server. It never reads a
task, a comment or the board: it calls those four routes and no others.

## The config holds the credential

`nudge.json` (see `nudge.json.example`) holds the `nudges` credential's token. The program **refuses to
start** if the file is a symbolic link, is not a regular file, belongs to another user, or is readable by
its group or others (`chmod 600`). It never prints the token: not in a log line, not in an error. The file is opened without following a link and judged by `fstat` of that same open file. The token goes
only in the `Authorization` header to the configured server; a redirect is refused, never followed.

Get the credential from the account that owns the lane: make an agent for that account with the scope
`nudges` (and only that scope), granted the lane's project. The scope is never combined with another.

## Limits of this reference

It is tested with a fake terminal; the tmux adapter is a thin one (`display-message`, `capture-pane`,
`if-shell` with `send-keys`, `respawn-pane`). Check your harness's prompt pattern by hand before you trust it, as the wake
README says of its own command. One instance per lane set: it keeps no lock file.
