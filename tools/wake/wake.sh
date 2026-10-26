#!/bin/sh
# Poll for work and wake this lane's harness, once per new fingerprint.
#
# An EXAMPLE waker from the Orchestra kit (tools/wake). It runs
# `client.py wait --since FINGERPRINT`, keeps the last fingerprint in a state file
# beside it, and when the wait says something is this lane's move it runs ONE wake
# command - the setting that starts a new turn of the harness this lane runs in. The
# kit cannot know how a harness is woken, so the lane fills that one command in.
#
# The rules it keeps, in order of what goes wrong without them:
#   * one wake per new fingerprint: the fingerprint is written to the state file
#     BEFORE the wake command runs, so a crash, a restart or a second scheduler tick
#     cannot run the command twice for the same fingerprint;
#   * one instance at a time: a lock directory beside the state file, with the
#     process id in a file created exclusively (O_EXCL, not mkdir, which is not
#     atomic on every coreutils), so a cron line and a systemd timer can overlap
#     safely; only that directory, its pid file and a takeover marker are ever
#     removed, never a path recursively;
#   * fail closed on a bad setting: an empty, root, home or foreign WAKE_STATE,
#     WAKE_LOCK or WAKE_LOG is refused before anything is created or removed;
#   * bounded: client.py wait is given --timeout, so one run ends by itself;
#   * one log line per wake, and nothing else in the wake log;
#   * it reads no secret: the client config is the only credential input, and the
#     wake command is the lane's own setting;
#   * it never runs the work: it only invokes the wake command and exits.
#
# Linux and macOS use this file (POSIX sh). The scheduler wiring is beside it:
# linux/orchestra-wake.service and linux/orchestra-wake.timer for a systemd --user
# timer, linux/crontab.example for cron, and macos/com.orchestra.wake.plist for
# launchd. See README.md in this directory for the whole setup.
#
# Exit codes: 0 the run is over normally (a wake, a timeout, or another instance is
# already running); 1 a setting is unusable, the state file is not a fingerprint, or
# the wake command failed; 2 the client refused the wait; 3 the client could not
# reach the service or did not answer a wait read.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

usage() {
    cat <<'EOF'
Usage: wake.sh [--env-file FILE] [SETTINGS...]

Poll `client.py wait` and run ONE wake command when something is this lane's move.

Settings (a flag wins over the env file, which wins over the environment):
  --client FILE        client.py from this kit (WAKE_CLIENT)
  --config FILE        this lane's private client configuration (WAKE_CONFIG)
  --project NAME       project id (WAKE_PROJECT)
  --actor ACTOR        this lane's saved actor (WAKE_ACTOR)
  --wake COMMAND       the one command that starts a new turn of this lane's
                       harness; run once per new fingerprint with sh -c
                       (WAKE_COMMAND)
  --python COMMAND     interpreter that runs client.py; default python3
                       (WAKE_PYTHON)
  --timeout SECONDS    bound on one wait, given to client.py wait; default 3600
                       (WAKE_TIMEOUT)
  --interval SECONDS   poll interval, given to client.py wait; default is the
                       client's own 300 (WAKE_INTERVAL)
  --state FILE         last fingerprint; default wake.state beside this script
                       (WAKE_STATE)
  --lock DIRECTORY     one-instance lock; default wake.lock beside this script
                       (WAKE_LOCK)
  --log FILE           one line per wake; default wake.log beside this script
                       (WAKE_LOG)
  --env-file FILE      sh file of WAKE_* settings, sourced before the flags; a
                       file with spaces in the wake command needs no scheduler
                       quoting this way
  -h, --help           this text

Exit codes: 0 normal end, 1 setting/state/wake failure, 2 the client refused,
3 the client could not be reached. See README.md for the scheduler files.
EOF
}

usage_error() {
    printf 'wake.sh: %s\n' "$1" >&2
    printf 'Run "wake.sh --help" for the settings.\n' >&2
    exit 2
}

require_value() {
    if [ "$2" -lt 2 ]; then
        usage_error "$1 needs a value"
    fi
}

# The env file is sourced, not parsed by the scheduler, so a wake command with
# spaces, pipes or quotes needs no scheduler-specific quoting. It holds this lane's
# settings only; it is not a place for secrets (the client config is).
env_file=
previous=
for argument in "$@"; do
    if [ "$previous" = '--env-file' ]; then
        env_file=$argument
    fi
    previous=$argument
done
if [ -n "$env_file" ]; then
    if [ ! -f "$env_file" ]; then
        printf 'wake.sh: no environment file: %s\n' "$env_file" >&2
        exit 2
    fi
    . "$env_file"
fi

PYTHON=${WAKE_PYTHON:-python3}
CLIENT=${WAKE_CLIENT:-}
CONFIG=${WAKE_CONFIG:-}
PROJECT=${WAKE_PROJECT:-}
ACTOR=${WAKE_ACTOR:-}
WAKE=${WAKE_COMMAND:-}
TIMEOUT=${WAKE_TIMEOUT:-3600}
INTERVAL=${WAKE_INTERVAL:-}
# The three paths use "-" rather than ":-": an unset setting takes the default, while
# a set but empty one is refused below instead of silently becoming the default.
STATE=${WAKE_STATE-$SCRIPT_DIR/wake.state}
LOCK=${WAKE_LOCK-$SCRIPT_DIR/wake.lock}
LOG=${WAKE_LOG-$SCRIPT_DIR/wake.log}

while [ $# -gt 0 ]; do
    case $1 in
        --env-file) require_value "$1" "$#"; shift 2 ;;
        --client) require_value "$1" "$#"; CLIENT=$2; shift 2 ;;
        --config) require_value "$1" "$#"; CONFIG=$2; shift 2 ;;
        --project) require_value "$1" "$#"; PROJECT=$2; shift 2 ;;
        --actor) require_value "$1" "$#"; ACTOR=$2; shift 2 ;;
        --wake) require_value "$1" "$#"; WAKE=$2; shift 2 ;;
        --python) require_value "$1" "$#"; PYTHON=$2; shift 2 ;;
        --timeout) require_value "$1" "$#"; TIMEOUT=$2; shift 2 ;;
        --interval) require_value "$1" "$#"; INTERVAL=$2; shift 2 ;;
        --state) require_value "$1" "$#"; STATE=$2; shift 2 ;;
        --lock) require_value "$1" "$#"; LOCK=$2; shift 2 ;;
        --log) require_value "$1" "$#"; LOG=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage_error "unknown argument: $1" ;;
    esac
done

[ -n "$CLIENT" ] || usage_error '--client FILE (client.py from this kit) is required'
[ -n "$CONFIG" ] || usage_error '--config FILE (this lane'"'"'s client config) is required'
[ -n "$PROJECT" ] || usage_error '--project NAME is required'
[ -n "$ACTOR" ] || usage_error '--actor ACTOR is required'
[ -n "$WAKE" ] || usage_error '--wake COMMAND is required: it is the one thing the kit cannot know'
case $WAKE in
    REPLACE_*) usage_error '--wake is still the template placeholder: fill in the command that starts a new turn of this lane' ;;
esac

case $TIMEOUT in
    ''|*[!0-9]*) usage_error '--timeout must be a whole number of seconds' ;;
esac
if [ "$TIMEOUT" -lt 1 ]; then
    usage_error '--timeout must be at least 1 second'
fi
case $INTERVAL in
    '') ;;
    *[!0-9]*) usage_error '--interval must be a whole number of seconds' ;;
esac
if [ ! -f "$CLIENT" ]; then
    usage_error "no client.py at $CLIENT"
fi
if [ ! -f "$CONFIG" ]; then
    usage_error "no client config at $CONFIG"
fi

# A PATH FROM CONFIGURATION IS CHECKED BEFORE ANYTHING IS CREATED OR REMOVED. A typo
# in WAKE_STATE, WAKE_LOCK or WAKE_LOG must fail closed: refusing an unsafe path is
# always better than writing to, or removing, something that was not meant. The lock
# is this waker's own directory; nothing else may ever be deleted, and nothing here is
# removed recursively.
trim_slashes() {
    printf '%s' "$1" | sed 's:/*$::'
}

physical_path() {
    # The physical path of an existing directory; empty when it is not one.
    physical=$(CDPATH= cd -- "$1" 2>/dev/null && pwd -P) || physical=
    printf '%s' "$physical"
}

home_setting=
if [ -n "${HOME:-}" ]; then
    home_setting=$(trim_slashes "$HOME")
fi
home_physical=
if [ -n "${HOME:-}" ] && [ -d "$HOME" ]; then
    home_physical=$(physical_path "$HOME")
fi

path_is_home() {
    candidate=$1
    if [ -n "$home_setting" ] && [ "$(trim_slashes "$candidate")" = "$home_setting" ]; then
        return 0
    fi
    if [ -n "$home_physical" ] && [ -d "$candidate" ]; then
        physical=$(physical_path "$candidate")
        if [ -n "$physical" ] && [ "$physical" = "$home_physical" ]; then
            return 0
        fi
    fi
    return 1
}

refuse_setting() {
    printf 'wake.sh: %s\n' "$1" >&2
    exit 1
}

check_setting_path() {
    setting=$1
    value=$2
    expected=$3
    if [ -z "$value" ]; then
        refuse_setting "$setting is empty: it must name a path this lane owns"
    fi
    if [ -z "$(trim_slashes "$value")" ]; then
        refuse_setting "$setting is $value, the filesystem root: refusing to create, write or remove it"
    fi
    if path_is_home "$value"; then
        refuse_setting "$setting is $value, the home directory: refusing to create, write or remove it"
    fi
    if [ "$expected" = lock ] && [ -L "$value" ]; then
        refuse_setting "$setting is $value, a symbolic link: refusing to follow or remove it"
    fi
    if [ -d "$value" ]; then
        if [ "$expected" = file ]; then
            refuse_setting "$setting is $value, an existing directory: it must name a file"
        fi
        # An existing lock directory is this waker's own only when it is empty or
        # holds its one pid file. Anything else belongs to someone else.
        for entry in "$value"/* "$value"/.[!.]* "$value"/..?*; do
            if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
                continue
            fi
            case ${entry##*/} in
                pid|stale.[0-9]*) ;;
                *) refuse_setting "$setting is $value, an existing directory that is not this waker's lock (it holds $entry): move it away" ;;
            esac
        done
    elif [ "$expected" = lock ] && [ -e "$value" ]; then
        refuse_setting "$setting is $value, an existing file: the lock must be a directory this waker owns"
    fi
}

check_setting_path WAKE_STATE "$STATE" file
check_setting_path WAKE_LOCK "$LOCK" lock
check_setting_path WAKE_LOG "$LOG" file

# A lane normally keeps the script, its state, its lock and its log in one private
# directory of its own; create the parents so a first run does not fail silently.
mkdir -p "$(dirname "$STATE")" "$(dirname "$LOCK")" "$(dirname "$LOG")"

err_file=$(mktemp "${TMPDIR:-/tmp}/orchestra-wake.XXXXXX") || err_file=${TMPDIR:-/tmp}/orchestra-wake.$$.err
lock_owned=0
takeover_marker=
release() {
    rm -f "$err_file"
    if [ -n "$takeover_marker" ]; then
        rm -f "$takeover_marker"
    fi
    if [ "$lock_owned" -eq 1 ]; then
        # The lock is a directory this waker made and holds one pid file in. Remove
        # exactly that file, then the directory with rmdir, which refuses a directory
        # that still holds anything: no recursive removal of a configured path, ever.
        rm -f "$LOCK/pid"
        rmdir "$LOCK" 2>/dev/null || true
    fi
}
trap 'release' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ONE INSTANCE AT A TIME. The atomic step is creating LOCK/pid with the shell's own
# noclobber (set -C opens it with O_EXCL), which the kernel decides: exactly one of
# any number of runs creates it. mkdir only makes the directory the file lives in and
# is NOT the lock: some coreutils (uutils 0.10, Ubuntu 26.04) let two concurrent
# mkdir of one path both report success, so nothing here relies on its answer. A
# directory with no pid yet is left alone: another run may be between its mkdir and
# its pid write. A stale lock (its pid is gone) is taken over by one run only: it
# must first create LOCK/stale.PID exclusively, then remove the one pid file and
# retake the lock exclusively; every other run that finds the marker exits. Nothing
# is ever checked and then created non-exclusively, and nothing is removed
# recursively. The EXIT trap releases the lock only when this run owns it.
exclusive_write() {
    ( set -C; printf '%s\n' "$$" > "$1" ) 2>/dev/null
}

pid_in() {
    pid_text=
    if [ -f "$1" ]; then
        pid_text=$(cat "$1" 2>/dev/null || true)
    fi
    case $pid_text in
        ''|*[!0-9]*) pid_text= ;;
    esac
    printf '%s' "$pid_text"
}

attempt=0
while [ "$lock_owned" -eq 0 ]; do
    attempt=$((attempt + 1))
    if [ -d "$LOCK" ]; then
        held_pid=$(pid_in "$LOCK/pid")
        if [ -z "$held_pid" ] || kill -0 "$held_pid" 2>/dev/null; then
            exit 0
        fi
        marker=$LOCK/stale.$held_pid
        if ! exclusive_write "$marker"; then
            taker=$(pid_in "$marker")
            if [ -n "$taker" ] && ! kill -0 "$taker" 2>/dev/null; then
                printf "wake.sh: the lock %s is stale and the run that was taking it over (pid %s) died; remove the marker and the next run takes it: rm -f '%s'\n" "$LOCK" "$taker" "$marker" >&2
                exit 1
            fi
            exit 0
        fi
        takeover_marker=$marker
        if [ "$(pid_in "$LOCK/pid")" != "$held_pid" ]; then
            exit 0
        fi
        rm -f "$LOCK/pid"
    else
        mkdir "$LOCK" 2>/dev/null || [ -d "$LOCK" ] || true
    fi
    if exclusive_write "$LOCK/pid"; then
        lock_owned=1
    elif [ -f "$LOCK/pid" ] || [ "$attempt" -ge 3 ]; then
        # Another run holds it; or, after three tries, the directory cannot be used.
        if [ -f "$LOCK/pid" ] || [ -d "$LOCK" ]; then
            exit 0
        fi
        printf 'wake.sh: cannot create the lock %s\n' "$LOCK" >&2
        exit 1
    fi
done
if [ -n "$takeover_marker" ]; then
    rm -f "$takeover_marker"
    takeover_marker=
fi

last=
if [ -f "$STATE" ]; then
    IFS= read -r last < "$STATE" || true
    last=$(printf '%s' "$last" | tr -d '\r')
    case $last in
        ''|wait1:*) ;;
        *)
            printf 'wake.sh: %s does not hold a wait fingerprint; move it away and let the waker run again\n' "$STATE" >&2
            exit 1
            ;;
    esac
fi

set -- wait
if [ -n "$last" ]; then
    set -- "$@" --since "$last"
fi
set -- "$@" --timeout "$TIMEOUT"
if [ -n "$INTERVAL" ]; then
    set -- "$@" --interval "$INTERVAL"
fi

set +e
output=$("$PYTHON" "$CLIENT" --config "$CONFIG" --project "$PROJECT" --actor "$ACTOR" "$@" 2>"$err_file")
code=$?
set -e

if [ "$code" -eq 1 ]; then
    # client.py wait's timeout: the bound was reached and nothing is the lane's move.
    exit 0
fi
if [ "$code" -ne 0 ]; then
    detail=$(tail -n 1 "$err_file" 2>/dev/null || true)
    printf 'wake.sh: client.py wait exited %s%s\n' "$code" "${detail:+: $detail}" >&2
    exit "$code"
fi

parsed=$(printf '%s\n' "$output" | "$PYTHON" -c '
import json
import sys

try:
    view = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(view, dict):
    raise SystemExit(1)
fingerprint = view.get("fingerprint")
items = view.get("items")
if not isinstance(fingerprint, str) or not fingerprint:
    raise SystemExit(1)
print(fingerprint)
print(len(items) if isinstance(items, list) else 0)
') || {
    printf 'wake.sh: client.py wait exited 0 but printed no wait answer\n' >&2
    exit 3
}
fingerprint=$(printf '%s\n' "$parsed" | sed -n '1p')
item_count=$(printf '%s\n' "$parsed" | sed -n '2p')

case $fingerprint in
    wait1:*) ;;
    *) printf 'wake.sh: client.py wait printed a fingerprint this kit does not write\n' >&2; exit 3 ;;
esac
case $item_count in
    ''|*[!0-9]*) printf 'wake.sh: client.py wait printed an unusable item count\n' >&2; exit 3 ;;
esac

if [ -n "$last" ] && [ "$fingerprint" = "$last" ]; then
    # The same state the lane already acted on: never a second wake for it.
    exit 0
fi

# Record the new fingerprint BEFORE waking. A crash between this write and the wake
# costs one wake (the lane is woken by the next new state, or by the operator),
# while a crash after the wake would run the command twice for the same state. The
# task's rule is the first: never more than once per new fingerprint.
temporary=$STATE.$$
printf '%s\n' "$fingerprint" > "$temporary"
mv "$temporary" "$STATE"

if [ "$item_count" -eq 0 ]; then
    # The state changed but nothing is the lane's move now (work moved away). Record
    # where the lane is, but do not wake it to find nothing.
    exit 0
fi

wake_failed=0
WAKE_FINGERPRINT=$fingerprint WAKE_ITEMS=$item_count sh -c "$WAKE" || wake_failed=1
result=ok
if [ "$wake_failed" -ne 0 ]; then
    result=failed
fi
printf '%s wake fingerprint=%s items=%s result=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$fingerprint" "$item_count" "$result" >> "$LOG"

if [ "$wake_failed" -ne 0 ]; then
    printf 'wake.sh: the wake command failed. The new fingerprint is recorded, so it will not run again for this state; fix and test the command once\n' >&2
    exit 1
fi
exit 0
