"""Child process for the real-signal stress test of admin.signal_termination_guard.

Run as ``python sigterm_stress_child.py REPO PREVIOUS SECONDS``. A kernel timer
(``timer_create``, so no Python thread and no patched signal function is involved)
sends one real SIGTERM per trial, armed inside a guarded block whose length adapts so
that the stop lands around the block's end and the guard's exit, wherever this
machine's timer actually fires.
After every trial the child checks the guard's postconditions and, on the first
violation, prints it and exits 1. PREVIOUS is the handler installed before the guard:
``custom`` (counts deliveries), ``ignore`` (SIG_IGN) or ``default`` (SIG_DFL, so a stop
the previous handler owns ends this process with SIGTERM, which the parent counts).
On completion it prints one JSON line of counts and exits 0. Linux only.
"""
import ctypes
import json
import random
import signal
import sys
import time
import traceback

sys.path.insert(0, sys.argv[1])
import admin  # noqa: E402

PREVIOUS, SECONDS = sys.argv[2], float(sys.argv[3])
MIN_SIZE, STEP = 20, 1.06

libc = ctypes.CDLL(None, use_errno=True)


class Timespec(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]


class Itimerspec(ctypes.Structure):
    _fields_ = [('it_interval', Timespec), ('it_value', Timespec)]


class Sigevent(ctypes.Structure):
    # Linux: union sigval, int signo, int notify, then padding to 64 bytes.
    _fields_ = [('sigev_value', ctypes.c_void_p), ('sigev_signo', ctypes.c_int),
                ('sigev_notify', ctypes.c_int), ('_pad', ctypes.c_byte * 48)]


CLOCK_MONOTONIC, SIGEV_SIGNAL = 1, 0
timer = ctypes.c_void_p()
event = Sigevent(sigev_signo=signal.SIGTERM, sigev_notify=SIGEV_SIGNAL)
if libc.timer_create(CLOCK_MONOTONIC, ctypes.byref(event), ctypes.byref(timer)) != 0:
    raise OSError(ctypes.get_errno(), 'timer_create')


def arm(seconds):
    value = Itimerspec(it_value=Timespec(int(seconds), int((seconds % 1) * 1e9) or 1))
    if libc.timer_settime(timer, 0, ctypes.byref(value), None) != 0:
        raise OSError(ctypes.get_errno(), 'timer_settime')


def remaining():
    value = Itimerspec()
    libc.timer_gettime(timer, ctypes.byref(value))
    return value.it_value.tv_sec * 1e9 + value.it_value.tv_nsec


received = []
if PREVIOUS == 'custom':
    previous = lambda signum, frame: received.append(signum)  # noqa: E731
elif PREVIOUS == 'ignore':
    previous = signal.SIG_IGN
else:
    previous = signal.SIG_DFL
signal.signal(signal.SIGTERM, previous)
mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())


def work(n):
    total = 0
    for i in range(n):
        total += i
    return total


def trial(delay, size):
    """One guarded block of ``size`` steps, a stop due ``delay`` seconds into it."""
    outcome = {'raised': 0}
    try:
        with admin.signal_termination_guard():
            arm(delay)           # from inside the block: the stop is due after setup
            work(size)
    except admin.TerminatedBySignal:
        outcome['raised'] = 1
    while remaining():
        pass
    work(20)                     # bytecode boundaries for a stop now pending at `previous`
    return outcome


def calibrate(size):
    """Seconds one guarded block of ``size`` steps takes here."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    started = time.perf_counter()
    for _ in range(200):
        with admin.signal_termination_guard():
            arm(10)
            work(size)
        libc.timer_settime(timer, 0, ctypes.byref(Itimerspec()), None)
    signal.signal(signal.SIGTERM, previous)
    return (time.perf_counter() - started) / 200


def main():
    # The stop must land around the block's end and the guard's exit, wherever this
    # machine's timer actually fires: a virtual machine can fire one a millisecond or
    # more late (timer slack, coarse clock events), so a fixed block was always over
    # before the stop arrived, and on a fast machine the guard's exit alone outlasts a
    # short block. So after every trial the stop moves later if it was raised inside
    # the guard and earlier if it reached the previous handler: first by the delay, and
    # once the delay is down to its floor, by lengthening the block.
    size, floor = MIN_SIZE, calibrate(MIN_SIZE) / 4
    centre = floor
    counts = {'trials': 0, 'raised': 0, 'previous': 0, 'ignored': 0}
    deadline = time.monotonic() + SECONDS
    while time.monotonic() < deadline:
        delay = random.uniform(0.7 * centre, 1.3 * centre)
        before = len(received)
        try:
            outcome = trial(delay, size)
        except BaseException:
            print('escaped: ' + traceback.format_exc().replace('\n', ' | '))
            sys.exit(1)
        problems = []
        if signal.getsignal(signal.SIGTERM) is not previous and signal.getsignal(signal.SIGTERM) != previous:
            problems.append('handler %r' % (signal.getsignal(signal.SIGTERM),))
        if signal.pthread_sigmask(signal.SIG_BLOCK, ()) != mask:
            problems.append('mask %r' % (signal.pthread_sigmask(signal.SIG_BLOCK, ()),))
        if getattr(admin, '_termination_guards', None):
            problems.append('guards %r' % (admin._termination_guards,))
        got = len(received) - before
        if PREVIOUS == 'custom' and outcome['raised'] + got != 1:
            problems.append('stop delivered %d times' % (outcome['raised'] + got))
        if problems:
            print('trial %d (delay %.1fus, block %d): %s' % (counts['trials'], delay * 1e6, size, '; '.join(problems)))
            sys.exit(1)
        if outcome['raised']:
            if size > MIN_SIZE:
                size = max(MIN_SIZE, int(size / STEP))   # stop later: shorter block first
            else:
                centre = min(centre * STEP, 0.05)        # then a later stop
        elif centre > floor:
            centre = max(floor, centre / STEP)           # stop earlier: shorter delay first
        else:
            size = min(int(size * STEP) + 1, 10000000)   # then a longer block
        counts['trials'] += 1
        counts['raised'] += outcome['raised']
        counts['previous'] += got
        counts['ignored'] += int(PREVIOUS == 'ignore' and not outcome['raised'])
        # Report progress, so a parent can count trials of a process SIG_DFL ended.
        if PREVIOUS == 'default':
            sys.stderr.write('t\n')
            sys.stderr.flush()
    print(json.dumps(counts))


main()
