#!/bin/sh
# Thin POSIX entry point for the kit client.
# Resolves the kit from this script's own location, so the caller's cwd does not
# matter; forwards every argument and exits with the client's own code.
# Set BEADS_PYTHON to one Python executable path (no flags) when "python3" is not
# on PATH.
KIT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
PY=${BEADS_PYTHON:-python3}
exec "$PY" "$KIT/client.py" "$@"
