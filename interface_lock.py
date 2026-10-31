"""Deployment-wide interface mutation order: this lock, then project locks.

No POSIX import at module scope. The kit's common order is this semantic mutex
when needed, one project coordination lock at a time, authority/store, and journals.
No effect under authority acquires another project's lock. Interface snapshots are
prepared before that effect and frozen; live permissions are refreshed at use.
Only program-link references and capability interface declarations/withdrawals need
this mutex. Store-only membership/account/agent/registration mutations use authority
alone; unrelated reference/capability writes use their ordinary project/authority
boundary. A bounded acquisition refuses busy rather than waiting indefinitely.
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
