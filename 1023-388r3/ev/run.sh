#!/bin/bash
# usage: run.sh <checkout> <outfile> <unittest args...>
co=$1; out=$2; shift 2
while [ "$(cut -d" " -f1 /proc/loadavg | cut -d. -f1)" -gt 24 ]; do sleep 30; done
cd "$co" && HOME=/dev/shm/orc-review-388r3/home TMPDIR=/dev/shm/orc-review-388r3/tmp ORCHESTRA_BD_BIN=/home/james/beads-team-pilot/runtime/bin/bd PYTHONIOENCODING=utf-8 \
 systemd-run --user --scope -q -p MemoryMax=8G -p MemorySwapMax=0 nice -n 10 python3 /home/james/orchestra-review-evidence/1023-388r3/rev/ev/revrun.py "$@" > "$out" 2>&1
echo "exit=$?" >> "$out"
