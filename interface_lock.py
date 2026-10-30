"""Deployment-wide interface mutation order: this lock, then project locks.

No POSIX import at module scope. All canonical record mutations participate, including
capability versions without interfaces (which can withdraw an old consumer) and program
link mutations. A bounded acquisition refuses busy rather than waiting indefinitely.
"""
from contextlib import contextmanager, ExitStack
from pathlib import Path

TIMEOUT = 5.0
BUSY = 'Interface mutations are busy; retry the identical operation later.'


class Busy(ValueError):
    status = 503


@contextmanager
def held(root, timeout=TIMEOUT):
    from http_authority import file_lock
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(Path(root) / '.interface-mutations.lock', timeout=timeout))
        except TimeoutError:
            raise Busy(BUSY) from None
        yield
