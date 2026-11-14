#!/usr/bin/env python3
"""Disposable endpoint launcher: count process kinds, never credentials or bodies."""
import itertools
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

log = os.environ.get('CONTEXT_FIND_TRACE_LOG')
sequence = itertools.count()


def event(kind, **data):
    if not log:
        return
    row = dict(kind=kind, pid=os.getpid(), sample=os.environ.get('CONTEXT_FIND_SAMPLE'),
               at=time.monotonic(), **data)
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(row, separators=(',', ':')) + '\n').encode())
    finally:
        os.close(descriptor)


class TracedPopen(subprocess.Popen):
    def __init__(self, args, *positional, **kwargs):
        self.identity, self.began, self.reported = next(sequence), time.monotonic(), False
        argv = [str(value) for value in args] if isinstance(args, (list, tuple)) else []
        self.executable = Path(argv[0]).name if argv else 'shell'
        command = None
        if self.executable in ('bd', 'bd.exe'):
            tail, index = argv[1:], 0
            while index < len(tail):
                if tail[index] in ('--directory', '--actor'):
                    index += 2
                elif tail[index].startswith('-'):
                    index += 1
                else:
                    command = tail[index]
                    break
        event('process-start', identity=self.identity, executable=self.executable, command=command)
        super().__init__(args, *positional, **kwargs)

    def communicate(self, input=None, *args, **kwargs):
        query = input.decode(errors='replace') if isinstance(input, bytes) else input
        if self.executable in ('dolt', 'dolt.exe') and isinstance(query, str) and 'HASHOF(' in query:
            event('head-query', identity=self.identity)
        return super().communicate(input, *args, **kwargs)

    def wait(self, *args, **kwargs):
        status = super().wait(*args, **kwargs)
        if not self.reported:
            self.reported = True
            event('process-end', identity=self.identity, returncode=status,
                  seconds=time.monotonic() - self.began)
        return status


if __name__ == '__main__':
    subprocess.Popen = TracedPopen
    entry = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(entry).resolve().parent))
    event('entry-start')
    try:
        runpy.run_path(entry, run_name='__main__')
    finally:
        event('entry-end')
