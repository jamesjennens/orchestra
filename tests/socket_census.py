"""Socket census for the HTTP test suites (kittrial-5bb.430): what each test leaves open.

Usage (from tests/): python socket_census.py MODULE [MODULE...] | --discover [--netstat-every N] [--cheap] [--json OUT]

It runs the modules under unittest in this one process and, after every test, counts what the test left:
live socket objects (listening and connected, found through the garbage collector), live threads and
``serve_forever`` threads. Every N tests (and at the start and the end) it also reads ``netstat -ano -p tcp``
on Windows and counts loopback TIME_WAIT for everyone and LISTENING/ESTABLISHED for this process. Growth is
attributed to the test class that left it. ``--cheap`` skips the per-test garbage-collector walk (the whole
suite then runs at nearly its usual speed) and counts threads per test, sockets only at each sample. No retry, no sleep: it only reads.

``open_sockets`` and ``server_threads`` are also what the teardown guard in test_harness_teardown uses.
"""
import gc
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from collections import Counter, defaultdict


def open_sockets():
    """``(listening, connected)``: live socket objects of this process that are not closed."""
    listening = connected = 0
    for obj in gc.get_objects():
        try:
            if not isinstance(obj, socket.socket) or obj.fileno() == -1:
                continue
        except Exception:  # noqa: BLE001 - a half-built object is not a live socket
            continue
        try:
            obj.getpeername()
            connected += 1
        except OSError:
            try:
                listening += 1 if obj.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) else 0
            except OSError:
                pass
    return listening, connected


def server_threads():
    """Live threads running a ``serve_forever`` loop."""
    return sum(1 for t in threading.enumerate()
               if 'serve_forever' in getattr(getattr(t, '_target', None), '__name__', ''))


def netstat():
    """Loopback TCP counts from ``netstat -ano -p tcp`` (Windows), or None elsewhere."""
    if os.name != 'nt':
        return None
    out = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True, text=True, errors='replace').stdout
    pid = str(os.getpid())
    counts = Counter()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 5 or parts[0] != 'TCP' or not parts[1].startswith('127.0.0.1:'):
            continue
        state, owner = parts[3], parts[4]
        if state == 'TIME_WAIT':
            counts['time_wait'] += 1
        elif owner == pid:
            counts[state.lower()] += 1
    return dict(counts)


class Census(unittest.TextTestResult):
    every = 25
    cheap = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []
        self.by_class = defaultdict(lambda: Counter())
        self.before = None
        self.count = 0

    def sockets(self):
        return (0, 0) if self.cheap else open_sockets()

    def startTest(self, test):
        self.before = (self.sockets(), server_threads(), threading.active_count())
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        (listen0, conn0), servers0, threads0 = self.before
        listen1, conn1 = self.sockets()
        servers1, threads1 = server_threads(), threading.active_count()
        name = type(test).__module__ + '.' + type(test).__qualname__
        left = self.by_class[name]
        left['tests'] += 1
        left['listening'] += listen1 - listen0
        left['connected'] += conn1 - conn0
        left['server_threads'] += servers1 - servers0
        left['threads'] += threads1 - threads0
        self.count += 1
        if self.count % self.every == 0:
            self.sample()

    def sample(self):
        listening, connected = open_sockets()
        self.samples.append(dict(tests=self.count, listening=listening, connected=connected,
                                 server_threads=server_threads(), threads=threading.active_count(),
                                 netstat=netstat()))


def main(argv):
    out = None
    if '--json' in argv:
        i = argv.index('--json'); out = argv[i + 1]; del argv[i:i + 2]
    if '--cheap' in argv:
        argv.remove('--cheap'); Census.cheap = True
    if '--netstat-every' in argv:
        i = argv.index('--netstat-every'); Census.every = int(argv[i + 1]); del argv[i:i + 2]
    if argv == ['--discover']:
        # The whole suite in one process, as tests/run_ci.py runs it on CI.
        here = os.path.dirname(os.path.abspath(__file__))
        suite = unittest.defaultTestLoader.discover(here)
    else:
        suite = unittest.defaultTestLoader.loadTestsFromNames(argv)
    runner = unittest.TextTestRunner(resultclass=Census, verbosity=0, stream=sys.stderr)
    start = dict(tests=0, netstat=netstat(), listening=open_sockets()[0], connected=open_sockets()[1],
                 server_threads=server_threads(), threads=threading.active_count())
    result = runner.run(suite)
    result.sample()
    report = dict(modules=argv, ran=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  start=start, samples=result.samples,
                  peak={k: max(s[k] for s in result.samples) for k in ('listening', 'connected', 'server_threads', 'threads')}
                  if result.samples else {},
                  peak_time_wait=max(((s['netstat'] or {}).get('time_wait', 0) for s in result.samples), default=None),
                  leaking_classes={name: dict(c) for name, c in sorted(result.by_class.items())
                                   if c['listening'] or c['connected'] or c['server_threads'] > 0})
    text = json.dumps(report, indent=1)
    if out:
        with open(out, 'w', encoding='utf-8') as handle:
            handle.write(text)
    print(json.dumps({k: report[k] for k in ('modules', 'ran', 'failures', 'errors', 'peak', 'peak_time_wait')}))
    for name, c in report['leaking_classes'].items():
        print('%-70s %s' % (name, c))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
