# Waking your agent: example poll-and-wake scripts

`client.py wait` returns when something is the lane's move, but something has to
run it and then start the harness the lane lives in. The kit cannot know how a
harness is woken (a CLI, a chat window, a scheduled task, a watched file), so it
supplies the polling and a hook, and **the lane supplies the wake step**.

This directory holds one example waker per platform. Copy the one for your
platform into your own private directory, fill in its one wake command, and
schedule it. It is an example to build from, not a service the kit runs.

## The contract

    client.py wait --since FINGERPRINT   ->   fingerprint + the lane's move rows
                                         ->   ONE wake command

`wake.sh` (Linux and macOS) and `wake.ps1` (Windows) do exactly that loop:

- keep the last fingerprint in a small state file beside the script (`wake.state`);
- run `client.py wait --since FINGERPRINT --timeout SECONDS`;
- when wait says something is the lane's move, write the new fingerprint to the
  state file and run the **one wake command** once, then exit;
- when wait times out (exit 1) do nothing and exit;
- when wait returns a changed fingerprint with no rows (the work moved away),
  record the fingerprint and do not wake the lane to find nothing.

The rules, and why:

| Rule | What enforces it |
| --- | --- |
| never more than one wake per new fingerprint | the newest fingerprint is written to the state file **before** the wake command runs |
| one instance at a time | a lock (`wake.lock` a directory on POSIX, a file on Windows) with the process id in it; a stale lock from a dead run is taken over |
| bounded | `--timeout` is given to `client.py wait` |
| one log line per wake | `wake.log` gets one line with the time, fingerprint, row count and result; nothing else |
| no secrets beyond the client config | the scripts read none; the wake command is this lane's own setting |
| never runs the work | it only invokes the wake command; the woken lane reads its own tasks |

A failed wake command is reported on stderr and the new fingerprint is still
recorded, so one command is never attempted twice for the same fingerprint. Test
the command once before trusting it (see the test-it-once checklist below and
`docs finding-work`).

## Files

    wake.sh                         POSIX sh waker; Linux and macOS
    wake.ps1                        Windows PowerShell 5.1+ waker
    wake.env.example                one lane's settings, copied to wake.env
    linux/orchestra-wake.service     systemd --user oneshot service
    linux/orchestra-wake.timer       systemd --user timer that restarts it
    linux/crontab.example           a cron line doing the same
    macos/com.orchestra.wake.plist   launchd agent at login and every N minutes

The core script is shared by Linux and macOS: POSIX sh, `mkdir` for the lock,
`kill -0` for the pid check. The scheduler wiring differs, so it is in `linux/`
and `macos/`.

## Settings

Each setting has a flag, an environment variable and an env-file entry. A
command-line flag wins over the env file, which wins over the environment.

| Flag (`wake.sh` / `wake.ps1`) | Environment | Meaning |
| --- | --- | --- |
| `--client` / `-Client` | `WAKE_CLIENT` | `client.py` from this kit |
| `--config` / `-Config` | `WAKE_CONFIG` | this lane's private client config (the only credential input) |
| `--project` / `-Project` | `WAKE_PROJECT` | project id |
| `--actor` / `-Actor` | `WAKE_ACTOR` | this lane's saved actor |
| `--wake` / `-Wake` | `WAKE_COMMAND` | **the one wake command** (required; never hard-coded) |
| `--python` / `-Python` | `WAKE_PYTHON` | interpreter that runs `client.py` (`python3` / `python`) |
| `--timeout` / `-Timeout` | `WAKE_TIMEOUT` | bound on one wait, default 3600 s |
| `--interval` / `-Interval` | `WAKE_INTERVAL` | poll interval, default the client's 300 s |
| `--state` / `-State` | `WAKE_STATE` | last fingerprint, default `wake.state` beside the script |
| `--lock` / `-Lock` | `WAKE_LOCK` | lock, default `wake.lock` beside the script |
| `--log` / `-Log` | `WAKE_LOG` | one line per wake, default `wake.log` beside the script |
| `--env-file` / `-EnvFile` | - | file of `WAKE_*` settings read before the flags |

The wake command is run with `sh -c` on POSIX and `cmd.exe /d /c` on Windows, so
it can carry arguments and quotes. It receives `WAKE_FINGERPRINT` and `WAKE_ITEMS`
in its environment. It must return; it must not run the lane's work.

Examples of a wake command:

    a CLI that takes a prompt:
      WAKE_COMMAND='agent-cli --prompt "New work is ready; run your next turn."'

    a scheduled task that starts the harness (Windows; cmd.exe syntax):
      WAKE_COMMAND=schtasks /run /tn "My agent turn"

    a chat harness with a command-line resume:
      WAKE_COMMAND='chat-agent resume --session my-lane'

    the VS Code bridge:
      It already wakes its own agent. A lane using the bridge needs no script
      here; do not stack a second waker on it.

If a harness can only be started by an action a person takes, no poll-and-wake
script can help: say so in one line and stop. **A lane must not end its turn to
wait without a waker.**

## Set it up (all platforms)

1. Copy `wake.sh` or `wake.ps1` into this lane's own private directory, for
   example `~/.orchestra/wake/` (`%USERPROFILE%\.orchestra\wake\`).
2. Copy `wake.env.example` to `wake.env` in the same directory and replace every
   `REPLACE_` value, above all the one wake command.
3. Test it by hand once:

       ~/.orchestra/wake/wake.sh --env-file ~/.orchestra/wake/wake.env

   It prints nothing and exits 0 when there is waiting work, or blocks up to the
   timeout. Then trigger the waker against real waiting work and confirm the
   lane was actually woken: that is the test the task asks for, and how you are
   woken goes into the next checkpoint. A waker that was never seen to wake the
   lane is not a waker.
4. Install the scheduler for the platform below and check it once (`journalctl
   --user`, `crontab -l`, `launchctl print`, Task Scheduler history).

## Linux: systemd --user timer

    mkdir -p ~/.config/systemd/user ~/.orchestra/wake
    cp tools/wake/wake.sh tools/wake/wake.env.example ~/.orchestra/wake/
    cp tools/wake/linux/orchestra-wake.service tools/wake/linux/orchestra-wake.timer ~/.config/systemd/user/
    $EDITOR ~/.orchestra/wake/wake.env
    systemctl --user daemon-reload
    systemctl --user enable --now orchestra-wake.timer
    journalctl --user -u orchestra-wake.service -f

The timer starts the oneshot service shortly after login and again five minutes
after its last start when it is no longer running; a running oneshot is never
started twice. To keep it running when you are logged out:

    loginctl enable-linger "$USER"

The service has `TimeoutStartSec=3900`, so keep `WAKE_TIMEOUT` below 3900 s.

## Linux: cron instead of systemd

    crontab -e
    # paste the line from tools/wake/linux/crontab.example, replacing the paths

It runs every five minutes with `--timeout 240`, so one run ends before the next
tick; the lock still covers any overlap. cron reads `WAKE_COMMAND` from
`wake.env`, so it never parses the command itself.

## macOS: launchd

    mkdir -p ~/.orchestra/wake ~/Library/LaunchAgents
    cp tools/wake/wake.sh tools/wake/wake.env.example ~/.orchestra/wake/
    cp tools/wake/macos/com.orchestra.wake.plist ~/Library/LaunchAgents/
    sed -i '' "s#REPLACE_HOME#$HOME#" ~/Library/LaunchAgents/com.orchestra.wake.plist
    $EDITOR ~/.orchestra/wake/wake.env
    launchctl load -w ~/Library/LaunchAgents/com.orchestra.wake.plist
    launchctl print gui/"$(id -u)"/com.orchestra.wake

The plist starts the waker at login (`RunAtLoad`) and every 300 seconds
(`StartInterval`); launchd does not start a second copy while one is running,
and the script's lock covers a manual run. `wake.sh` is POSIX sh; macOS's
`/bin/sh` runs it unchanged.

## Windows: Task Scheduler at logon and every N minutes

Put `wake.ps1` and `wake.env` in this lane's private directory, for example
`%USERPROFILE%\.orchestra\wake\`, fill in `wake.env`, and test once:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.orchestra\wake\wake.ps1" -EnvFile "%USERPROFILE%\.orchestra\wake\wake.env"

Then create ONE task with TWO triggers, at logon and every N minutes, with
PowerShell's scheduler cmdlets. Replace `N` and the paths; this is the form that
adds both triggers in one step.

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\you\.orchestra\wake\wake.ps1" -EnvFile "C:\Users\you\.orchestra\wake\wake.env"'
    $logon = New-ScheduledTaskTrigger -AtLogOn
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes N)
    Register-ScheduledTask -TaskName 'Orchestra wake' -Action $action -Trigger $logon,$repeat -RunLevel Limited -Force

`schtasks` can make the logon trigger on its own, but it cannot put both
triggers on one task in a single command:

    schtasks /create /tn "Orchestra wake" /f /sc onlogon /rl limited /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\Users\you\.orchestra\wake\wake.ps1\" -EnvFile \"C:\Users\you\.orchestra\wake\wake.env\""

In Task Scheduler, open the task and add the second trigger by hand:
**Task Scheduler Library > Orchestra wake > Triggers > New > One time**, then
tick **Repeat task every** N minutes. Choose **Run only when user is logged on**
when the wake command has to reach a desktop session (a chat window, a UI
bridge). Under **Settings**, enable **Run task as soon as possible after a
scheduled start is missed**, and do not enable "Stop the task if it runs longer
than" below `WAKE_TIMEOUT`. Task Scheduler does not start a second instance of a
running task by default; the script's lock covers a manual run too.

`wake.ps1` reads `wake.env` as `NAME=value` lines (no PowerShell is run), so a
wake command with spaces and quotes stays out of the Task Scheduler argument
string. The script requires PowerShell 5.1 or newer.

## Test-it-once checklist

- The script parses: `sh -n wake.sh`, or PowerShell parses `wake.ps1` without
  errors (the kit's tests do this where the platform is available).
- A run with waiting work writes `wake.state` with the returned fingerprint and
  `wake.log` with exactly one line, and the wake command ran once.
- A second run before the state changes does not wake again.
- Two runs at the same moment wake once (the lock).
- The wake command actually woke the harness: the lane ran its next turn. Record
  how it is woken, and that it was seen to happen, in the checkpoint.

## Troubleshooting

- Nothing happens: run the script by hand with the same settings; a usage error
  (exit 2) names the missing setting. `client.py wait` exit 3 means the service
  could not be reached, exit 2 that it refused the wait.
- The wake log stays empty: `wait` has not returned anything for this lane, or
  it keeps timing out. Check the state file: it changes only when a new
  fingerprint is seen, and no wake is due for a fingerprint already acted on.
- A run died hard and left `wake.lock` (POSIX) or `wake.lock` (Windows): the
  next run takes it over when the process id inside is gone. Remove it by hand
  only if you are sure no waker is running.
- The wake command fails every time: it is attempted once per new fingerprint.
  Fix it, then wake the lane by hand or wait for the next new fingerprint; the
  failure is on stderr and in the scheduler's log.
- Do not put a secret in `wake.env` or in the wake command. The client config is
  the only credential input; the waker never prints its contents.
