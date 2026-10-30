"""Deployment-wide interface mutation order: this lock, then project locks.

No POSIX import at module scope. Interface and program-link mutations participate. Capability mutations select
this lock when their payload or stored revisions touch interfaces, including an
implicit withdrawal by a v1 revision. Unrelated v1 capability writes use only
their project lock. A bounded acquisition refuses busy rather than waiting indefinitely.
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
