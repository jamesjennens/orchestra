#!/usr/bin/env python3
"""Time one canonical ``work`` read on a project (kittrial-5bb.339).

Usage: python3 tools/read_timing.py RUNTIME_ROOT PROJECT [--fresh]

Builds a fresh interpreter per read, exactly as the web service pays it, and prints
the wall time of ``endpoint.execute(action='work', args=['--limit','20'])`` plus the
split into imports and execute. ``--fresh`` removes the project's read cache first,
so the first read pays the full export; without it the first read populates the
cache and the rest are served from it. Nothing here writes to the tracker: the only
files touched are the project's ``.read-cache/`` pair.

The before/after numbers quoted in kittrial-5bb.339's delivery were taken with this
script on the /dev/shm fixture described in the evidence (a scratch Dolt server,
never a live installation).
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('read_timing.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import json
import subprocess
import shutil
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]

CODE = (
    "import sys,time,json\n"
    "from pathlib import Path\n"
    "t0=time.perf_counter()\n"
    "sys.path.insert(0,%r)\n"
    "import endpoint\n"
    "t1=time.perf_counter()\n"
    "answer=endpoint.execute(Path(%r),{'project':%r,'actor':'op','action':'work',"
    "'args':['--limit','20','--json']})\n"
    "t2=time.perf_counter()\n"
    "page=json.loads(answer['stdout'])\n"
    "print(json.dumps({'imports':round(t1-t0,3),'execute':round(t2-t1,3),"
    "'total':round(t2-t0,3),'rc':answer['returncode'],'items':len(page['items'])}))\n"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runtime_root', help='the runtime root holding projects/')
    parser.add_argument('project', help='the project to read')
    parser.add_argument('--fresh', action='store_true',
                        help='remove the read cache first, so the first read pays the full export')
    args = parser.parse_args()
    root = Path(args.runtime_root).resolve()
    name = args.project
    project = root / 'projects' / name
    if not (project / '.beads' / 'metadata.json').is_file():
        raise SystemExit('not an initialized project under that runtime root')
    if args.fresh:
        shutil.rmtree(str(project / '.read-cache'), ignore_errors=True)
    code = CODE % (str(KIT), str(root), name)
    times = []
    for attempt in range(1, 5):
        started = time.perf_counter()
        done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                              cwd=str(KIT))
        wall = time.perf_counter() - started
        line = [line for line in done.stdout.splitlines() if line.startswith('{')]
        if not line:
            raise SystemExit('read failed: %s' % (done.stderr[-300:] or done.stdout[-300:]))
        print('read %d: wall %.2f s %s' % (attempt, wall, line[-1]))
        times.append(json.loads(line[-1])['total'])
    print('best of 4: %.2f s' % min(times))


if __name__ == '__main__':
    main()
