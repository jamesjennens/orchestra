#!/usr/bin/env python3
"""Operator commands for an isolated, user-systemd Beads/Dolt deployment."""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('admin.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer; on an office installation that is the bundled interpreter, INSTALL_ROOT/current/python-runtime/....\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from contextlib import contextmanager
from bootstrap import install as install_binaries
import record_json
from requirements import content_hash
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

#: The live operation-journal store inside one project directory. It is included in a
#: native project backup through ``snapshot_journal``/``restore_journal`` below (`bd
#: backup` itself covers Dolt only).
JOURNAL_STORE_NAME='.http-operations.sqlite3'

#: The per-run scheduled-backup status file inside the runtime's backups directory.
#: One run writes one record covering the projects it attempted, so an operator's
#: off-machine copy can read which projects have a complete backup pair.
BACKUP_STATUS_NAME='backup-status.json'

#: Explicit ceiling for one project's native Dolt sync through the SQL client. Unlike
#: ``bd backup sync``, the SQL client has no fixed ~10 s read timeout, so this only
#: bounds a genuinely hung server; a large database legitimately needs minutes.
BACKUP_SYNC_TIMEOUT=1800

#: Explicit ceiling for one native restore through the SQL client (``restore-new``).
#: ``bd backup restore`` has the same fixed ~10 s read timeout as ``bd backup sync``, so the
#: restore uses the SQL client too, with the same generous bound for a genuinely hung server.
RESTORE_TIMEOUT=BACKUP_SYNC_TIMEOUT

#: Suffix of the durable copy of a project's last COMPLETE coordination sidecar. A
#: failed or interrupted run must never destroy the previous restorable pair, so the
#: last complete sidecar is kept here and ``coordination_backup`` can fall back to it.
LAST_COMPLETE_SUFFIX='.coordination.last-complete.json'

#: Additive key in a COMPLETE coordination sidecar that records a manifest of the native
#: backup directory at the moment that generation completed. ``restore-new`` recomputes it
#: and warns when the directory no longer matches it, because a run that is killed outright
#: (or an interrupted sync) can leave the native directory partly rewritten while the
#: restore serves the previous complete sidecar and its operation-journal snapshot. The
#: sidecar schema stays 1: the manifest is additive and readers ignore keys they do not know.
NATIVE_MANIFEST_KEY='native_backup'

def checked(cmd, **kwargs):
    return subprocess.run(list(map(str,cmd)),text=True,encoding='utf-8',capture_output=True,check=True,**kwargs)

class TerminatedBySignal(BaseException):
    """The guarded section was stopped by ``SIGTERM`` (see ``signal_termination_guard``).

    ``BaseException`` on purpose: ``backup_project``'s ``last_complete_guard`` catches
    ``BaseException`` and its ``finally`` must run, exactly as they do for a
    ``KeyboardInterrupt``. An ordinary ``except Exception`` on the way out must not be able
    to swallow an operator's stop request.
    """

    def __init__(self,signum):
        super().__init__('terminated by signal %d'%signum)
        self.signum=signum

# The active termination guards, innermost last (kittrial-5bb.122). raise_termination
# consults them, so a stop that the interpreter runs late - at whatever bytecode
# boundary follows the signal - still lands on the guard's rules. Only the main thread
# installs a handler, so only main-thread guards are listed. A record holds the guard
# weakly (kittrial-5bb.124): a guard whose exit never runs - left behind by
# contextlib.ExitStack when a stop lands between the block and the exit - is not kept
# alive by its record, so its finaliser can release it. The record also keeps what a
# release needs (kittrial-5bb.125): a guard collected where its finaliser cannot
# release it - on another thread, where signal.signal is refused - leaves a dead
# record that the main thread releases at its next chance (_reap_abandoned).
_termination_guards=[]

class _GuardRecord:
    """One listed guard: a weak reference to it, the handler it replaced, and whether a
    stop it recorded is still owed to that handler once the guard is gone."""
    __slots__=('ref','previous','held')

    def __init__(self,guard,previous):
        self.ref=weakref.ref(guard)
        self.previous=previous
        self.held=False

def _listed_guards():
    """The listed guards still alive, innermost last."""
    return [guard for guard in (record.ref() for record in _termination_guards) if guard is not None]

def _reap_abandoned():
    """Release the records of guards that were collected without being released: dead
    records after the last live one (kittrial-5bb.125).

    Runs only on the main thread, where the handler can be restored: in the handler
    itself and at the start of every guard's ``__enter__``. The outermost of those
    records names the handler that was installed before them; it is put back if
    ``raise_termination`` is still installed. Returns whether one of them had recorded
    a stop, which the caller then sends to the handler now installed."""
    if threading.current_thread() is not threading.main_thread():
        return False
    live=[index for index,record in enumerate(_termination_guards) if record.ref() is not None]
    first=live[-1]+1 if live else 0
    dead=_termination_guards[first:]
    if not dead:
        return False
    del _termination_guards[first:]
    try:
        if signal.getsignal(signal.SIGTERM) is raise_termination:
            signal.signal(signal.SIGTERM,dead[0].previous)
    except _TERMINATION_ERRORS:
        pass
    return any(record.held for record in dead)

def _resend_sigterm():
    """Send a stop to whatever handler is installed now (it runs at once)."""
    try: signal.raise_signal(signal.SIGTERM)
    except _TERMINATION_ERRORS:
        try: os.kill(os.getpid(),signal.SIGTERM)
        except _TERMINATION_ERRORS: pass

_TERMINATION_ERRORS=(ValueError,OSError,RuntimeError,AttributeError,TypeError)

def _running_guard(frame):
    """``(guard, code)``: the innermost listed guard whose own setup or exit is on the
    stack at ``frame`` and the guard method running there, or ``(None, None)``.

    Python passes the handler the frame that was running when it checked for signals.
    That can be a helper the guard calls - ``signal.signal`` and ``signal.pthread_sigmask``
    are Python functions in ``Lib/signal.py`` - so the whole stack is walked, not just
    the frame passed (kittrial-5bb.122 review: the guard's own frame is not the one
    Python passes while it is inside those wrappers). A guard's block runs in the
    caller's frame, never under ``__enter__`` or ``__exit__``, so a stop in the block is
    not mistaken for one in the guard's code. A guard that is no longer listed (its
    exit has unlisted it) does not count: a stop handled in its last lines belongs to
    the guards still listed, or to the previous handler. A guard the collector is
    finalising has lost its weak reference and does not count either: the handler first
    releases its dead record (``_reap_abandoned``) and passes the stop on."""
    listed=_listed_guards()
    while frame is not None:
        if frame.f_code in _GUARD_CODES:
            owner=frame.f_locals.get('self')
            for guard in listed:
                if guard is owner:return guard,frame.f_code
        frame=frame.f_back
    return None,None

def _stop_guards():
    """Mark every listed guard stopped: the stop is raised once, for all of them."""
    for guard in _listed_guards():guard.stopped=True

def raise_termination(signum,frame):
    """Signal handler that turns a stop into an exception so the cleanup path runs.

    The FIRST stop becomes ``TerminatedBySignal``, and every active guard is marked
    stopped. A second ``SIGTERM`` would otherwise kill the interpreter inside the cleanup
    the first one started - including inside ``terminate_process_group``, before the
    ``killpg`` that stops the client group, or in an outer guard's block after an inner
    guard raised the stop - so while any guard is stopped this handler ignores later
    stops; it stays installed until each guard restores its previous handler.
    ``SIGKILL`` remains the operator's way to force a stop that runs no cleanup.

    Python runs this handler at a bytecode boundary after the signal arrived, so it can
    run inside a guard's own setup or exit, or in a function they call. There it never
    raises (the guard's restore would be skipped): the stop is recorded on THAT guard,
    which raises it once its previous handler and mask are back (kittrial-5bb.122).
    The one exception is the end of ``__enter__``, once the handler is installed and the
    guard is armed: a stop recorded there was only raised at the block's exit, after the
    whole block ran. There the handler releases the guard itself (previous handler,
    mask, record) and raises from ``__enter__``, exactly as a stop taken during setup is
    raised, so the block does not run (kittrial-5bb.124). ``raise_signal`` or
    ``interrupt_main`` cannot defer it to the block: the interpreter runs the handler
    again at its next check, which is still inside this handler.

    Guards that were collected without being released are released first
    (``_reap_abandoned``). If that puts the previous handler back, this stop is that
    handler's: it is sent on to it, after any stop those guards had recorded.
    """
    if _reap_abandoned():
        _resend_sigterm()
    if signal.getsignal(signum) is not raise_termination:
        _resend_sigterm()
        return
    listed=_listed_guards()
    if listed:
        guard,code=_running_guard(frame)
        if guard is not None:
            guard.held=True
            if code is _GUARD_ENTER_CODE and guard.entered:
                # The end of __enter__: release here and raise from __enter__, as a stop
                # taken during setup is, so the block does not run (kittrial-5bb.124).
                guard._release()
                guard._raise_held(False)
            return
        if any(guard.stopped for guard in listed):
            return
        _stop_guards()
        raise TerminatedBySignal(signum)
    # Installed without a listed guard (a guard could not restore its previous handler):
    # raise once and ignore later stops.
    try: signal.signal(signum,signal.SIG_IGN)
    except _TERMINATION_ERRORS: pass
    raise TerminatedBySignal(signum)

def _current_sigmask():
    """This thread's signal mask, unchanged, or None where masks are not available."""
    if not (hasattr(signal,'pthread_sigmask') and hasattr(signal,'SIG_BLOCK')):
        return None
    try: return signal.pthread_sigmask(signal.SIG_BLOCK,())
    except _TERMINATION_ERRORS: return None

def _take_pending_sigterm():
    """Take every ``SIGTERM`` the kernel holds for this (masked) thread; True if any.

    ``sigtimedwait`` where it exists (Linux and most POSIX systems). macOS has no
    ``sigtimedwait``; there ``sigpending`` shows the held stop and ``sigwait`` takes it
    without blocking, since it is already pending. Where neither is available the held
    stop is not taken here: it reaches the previous handler when the mask is restored,
    which is the behaviour before kittrial-5bb.122."""
    taken=False
    try:
        if hasattr(signal,'sigtimedwait'):
            while signal.sigtimedwait({signal.SIGTERM},0) is not None:taken=True
        elif hasattr(signal,'sigpending') and hasattr(signal,'sigwait'):
            while signal.SIGTERM in signal.sigpending():
                signal.sigwait({signal.SIGTERM})
                taken=True
    except _TERMINATION_ERRORS: pass
    return taken

class signal_termination_guard:
    """Turn ``SIGTERM`` into a ``TerminatedBySignal`` exception for the duration of the block.

    ``backup_project``'s ``last_complete_guard`` (which catches ``BaseException``) and its
    ``finally`` are what restore the previous complete sidecar, drop the staged journal
    snapshot and stop the native sync client. A ``SIGINT`` (Ctrl-C) already raises
    ``KeyboardInterrupt``, but a ``SIGTERM`` terminates the interpreter outright, so that
    cleanup never ran and the ``dolt`` client kept writing ``backups/<name>`` after the
    backup lock was released. A handler that raises puts a normal stop back on the cleanup
    path; the previous handler is restored on exit.

    A handler can only be installed in the main thread (``signal.signal`` raises
    ``ValueError`` elsewhere), an embedded host may have its own handlers and a platform
    may refuse the signal, so an install that is not possible is skipped instead of
    failing the backup for a reason unrelated to it: the caller keeps the previous
    behaviour, which is the honest limitation this cannot remove. Once a stop has been
    turned into the exception, later ``SIGTERM`` delivery is ignored until the outermost
    guard exits (see ``raise_termination``), so the cleanup it started cannot itself be
    interrupted; a very short window whose state must change as one unit additionally
    holds the signal with ``sigterm_blocked``.

    A class rather than a ``@contextmanager`` generator (kittrial-5bb.122 review): the
    handler recognises a stop that lands in the guard's own code by the ``__enter__`` and
    ``__exit__`` frames on the stack, and with a generator the stack between the block
    and the restore also held ``contextlib``'s frames, where a raised stop skipped the
    restore. The exit holds ``SIGTERM`` in this thread, restores the previous handler,
    takes any stop the kernel holds (``_take_pending_sigterm``), restores the mask and
    only then unlists the guard; each of those steps runs whatever the one before it
    raised. A stop recorded during setup or exit is then raised as ``TerminatedBySignal``
    - after the previous handler and mask are back - unless a guard already raised one.
    A stop that arrives after the guard is unlisted belongs to the previous handler.
    If the block is abandoned (``GeneratorExit``: the generator that holds the ``with``
    was closed, for example by the collector) a recorded stop cannot be raised into the
    closer, so it is sent again to the previous handler (``signal.raise_signal``) instead
    of being dropped. A stop recorded during setup is raised from ``__enter__`` after the
    same restore, so the block does not run.
    """

    def __init__(self):
        self.stopped=False
        self.held=False
        self.listed=False
        self.entered=False
        self.previous=None

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            return self
        if _reap_abandoned():
            _resend_sigterm()   # owed to the handler the abandoned guards replaced
        try:
            previous=signal.getsignal(signal.SIGTERM)
        except _TERMINATION_ERRORS:
            return self
        if previous is None:
            return self   # installed outside Python: it could not be restored
        self.previous=previous
        self._record=_GuardRecord(self,previous)
        _termination_guards.append(self._record)
        self.listed=True
        try:
            signal.signal(signal.SIGTERM,raise_termination)
        except _TERMINATION_ERRORS:
            self._release()
            return self
        except BaseException:
            self._release()
            raise
        self.entered=True   # from here a stop releases the guard and raises (raise_termination)
        if self.held:
            self._release()
            self._raise_held(False)
        return self

    def __exit__(self,kind,error,traceback):
        if self.listed:
            self._release()
            self._raise_held(kind is not None and issubclass(kind,GeneratorExit))
        return False

    def __del__(self):
        """A guard collected while still listed never ran its exit (kittrial-5bb.124):
        ``contextlib.ExitStack`` can drop it when a stop lands in its own code between the
        block and the guard's exit. Release it here, so the previous handler is back and
        no stopped record outlives it; a stop it had recorded goes to the previous
        handler, as for an abandoned block, or, while an outer guard is still active, to
        the innermost of those, which raises it at its exit. The kit itself uses plain
        ``with``.

        Only on the main thread (kittrial-5bb.125): elsewhere ``signal.signal`` is refused,
        and a stop sent from there would be raised in the main thread wherever it happens
        to be. There the guard only notes a stop it owes; its record stays, dead, until
        the main thread's next stop or next guard releases it (``_reap_abandoned``). At
        interpreter exit the guard is released but a stop it recorded is not sent on: the
        process is already ending, and the exit status it was asked to end with is kept
        rather than replaced by death by ``SIGTERM``. Nothing raised here can escape a
        finaliser usefully, so every exception, ``BaseException`` included, stops here."""
        try:
            if not self.listed:
                return
            if threading.current_thread() is not threading.main_thread():
                self._record.held=self.held and not self.stopped
                return
            self._release()
            live=_listed_guards()
            if live:
                # Sent on now, the stop would be raised by an outer guard's handler inside
                # this finaliser, where nothing can catch it: the innermost live guard
                # takes it instead and raises it at its exit.
                if self.held and not self.stopped and not any(guard.stopped for guard in live):
                    live[-1].held=True
            elif not sys.is_finalizing():
                self._raise_held(True)
        except BaseException:
            pass

    def _release(self):
        """Restore the previous handler and the mask and unlist the guard, on every path."""
        mask=_current_sigmask()
        try:
            try:
                if mask is not None:signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM})
            finally:
                try:
                    try: signal.signal(signal.SIGTERM,self.previous)
                    except _TERMINATION_ERRORS: pass
                finally:
                    # A stop the kernel holds for this thread arrived while the guard was
                    # exiting; it would otherwise reach the previous handler (usually
                    # SIG_DFL, ending the process with no cleanup) the moment the mask is
                    # restored. The guard takes it instead.
                    if mask is not None and _take_pending_sigterm():self.held=True
        finally:
            try:
                if mask is not None:
                    try: signal.pthread_sigmask(signal.SIG_SETMASK,mask)
                    except _TERMINATION_ERRORS: pass
            finally:
                self.listed=False
                record=getattr(self,'_record',None)
                _termination_guards[:]=[listed for listed in _termination_guards if listed is not record]

    def _raise_held(self,abandoned):
        """Raise a stop recorded in the guard's own code, once for all guards."""
        if not self.held or self.stopped or any(guard.stopped for guard in _listed_guards()):
            return
        self.stopped=True
        if abandoned:
            _resend_sigterm()
            return
        _stop_guards()
        raise TerminatedBySignal(signal.SIGTERM)

_GUARD_CODES=frozenset(method.__code__ for method in (
    signal_termination_guard.__enter__,signal_termination_guard.__exit__,
    signal_termination_guard._release,signal_termination_guard._raise_held,signal_termination_guard.__del__))
_GUARD_ENTER_CODE=signal_termination_guard.__enter__.__code__

@contextmanager
def sigterm_blocked():
    """Hold ``SIGTERM`` delivery for the duration of one very short critical window.

    ``signal_termination_guard`` turns a stop into an exception, which is what runs the
    cleanup. Two windows in ``backup_project``'s critical section must not be split by
    that exception, because the state the cleanup would then see is not yet consistent:

    * ``promote_journal_snapshot`` followed by the guard's ``promoted`` flag: a stop
      between them makes the guard restore the previous complete sidecar beside the
      journal that was already promoted, pairing two different generations.
    * ``Popen`` followed by the handle's pid/process recording in ``spawn_sync_client``:
      a stop between them orphans the sync client, whose group the cleanup can no longer
      find by pid.

    A signal delivered while blocked stays pending and is handled as soon as the mask is
    restored, so the stop is deferred, never dropped. ``signal.pthread_sigmask`` is
    POSIX-only; where it (or the mask call) is unavailable the window is simply not
    widened, which is the pre-existing behaviour rather than a new failure.
    """
    previous=None
    if hasattr(signal,'pthread_sigmask') and hasattr(signal,'SIG_BLOCK'):
        try: previous=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM})
        except (ValueError,OSError,RuntimeError,AttributeError): previous=None
    try:
        yield
    finally:
        if previous is not None:
            try: signal.pthread_sigmask(signal.SIG_SETMASK,previous)
            except (ValueError,OSError,RuntimeError,AttributeError): pass

class SyncClientHandle:
    """Bookkeeping for a native sync client started in its own session.

    ``checked()`` runs children through ``subprocess.run`` and hides the pid, so a caller
    that has to be able to stop a child (and anything that child spawned) cannot use it.
    ``spawn_sync_client`` fills this in: ``pid``/``process`` name the client before the wait
    starts and ``finished`` becomes True only once the child has actually been waited for.
    ``backup_project`` terminates the group from its ``finally`` while the handle is
    unfinished, so a signal or an exception cannot leave the client writing
    ``backups/<name>`` after the backup lock is released, and a run in which the client
    exited normally never signals a pid the OS may have recycled by then.
    """

    def __init__(self):
        self.pid=None
        self.process=None
        self.finished=False

def terminate_process_group(handle):
    """Kill and reap a sync client's whole process group when it is still running.

    Called from ``backup_project``'s ``finally`` on every exit path. It is a no-op when
    nothing was started or the client already exited and was waited for. A client still
    running when the run unwinds (SIGTERM/SIGINT, an exception, the explicit ceiling) is
    killed as a GROUP - ``os.killpg(os.getpgid(pid), SIGKILL)`` - so a ``dolt`` helper it
    spawned cannot keep writing ``backups/<name>`` once the backup lock is released. A group
    that is already gone (``ProcessLookupError``) or one this process may not signal
    (``PermissionError``) is tolerated, so the run's real failure is reported instead of
    being replaced by a secondary one. The group is never this process's own group: that
    would only be possible if the child had not become a session leader, and killing it
    would kill the kit itself.
    """
    if handle is None or handle.pid is None or handle.finished:
        return
    process=handle.process
    pid=handle.pid
    handle.pid=None
    if hasattr(os,'killpg') and hasattr(os,'getpgid') and hasattr(os,'getpgrp'):
        group=None
        try: group=os.getpgid(pid)
        except OSError: group=None
        if group is not None and group!=os.getpgrp():
            try: os.killpg(group,signal.SIGKILL)
            except OSError: pass
    elif process is not None:
        try: process.kill()
        except OSError: pass
    if process is not None:
        try: process.wait()
        except OSError: pass
        for stream in (process.stdout,process.stderr):
            if stream is None:continue
            try: stream.close()
            except OSError: pass

def spawn_sync_client(command,handle,**kwargs):
    """Run the native sync client in its OWN session; return its captured stdout.

    The contract mirrors ``checked`` (UTF-8 text, captured stdout/stderr, ``check=True``,
    an optional ``timeout``), with the two differences the long native sync needs:

    * ``start_new_session=True`` - the client leads its own session and process group, so
      ``terminate_process_group`` reaches the ``dolt`` client AND whatever it spawned, not
      only the one process.
    * ``handle`` - a ``SyncClientHandle`` given the pid before the wait starts and marked
      finished only after the child has been waited for, so the caller can stop exactly the
      client that is still running and never a pid that has already been reaped.

    ``checked()`` keeps its exact behaviour for every other caller in this file: this is the
    one subprocess that runs in its own session.
    """
    args=list(map(str,command))
    timeout=kwargs.pop('timeout',None)
    # A stop landing between the spawn and the pid record would orphan the client: the
    # cleanup has no pid to kill and the group keeps writing the backup. SIGTERM is held
    # for exactly those statements; a stop arriving here is delivered when the window
    # closes and then runs the normal cleanup against a handle that already knows the pid.
    with sigterm_blocked():
        process=subprocess.Popen(args,text=True,encoding='utf-8',stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,start_new_session=True,**kwargs)
        handle.pid=process.pid
        handle.process=process
        handle.finished=False
    try:
        stdout,stderr=process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The explicit ceiling was reached. ``subprocess.run`` kills only the direct child
        # on its own timeout path; this stops the whole group and reaps it before reporting.
        terminate_process_group(handle)
        raise
    handle.finished=True
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode,args,output=stdout,stderr=stderr)
    return stdout

def validate_name(name):
    if not re.fullmatch(r'[a-z][a-z0-9]{1,23}',name): raise ValueError('Project: 2-24 lowercase letters/digits, beginning with a letter')
    return name

def root_path(value):
    p=Path(value).expanduser().resolve()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(p)) or p==Path('/'):
        raise ValueError('Use an explicit non-root absolute Linux path without spaces')
    return p

def read_json_file(path,what,encoding=None):
    """The parsed JSON of a file the kit was pointed at.

    A file that is not JSON (or not text) is a refusal that NAMES THE FILE: the bare
    ``JSONDecodeError`` says only a line and a column, which for a broken
    ``deployment.private.json`` or ``--file`` payload hides which file to fix. A file that
    cannot be opened keeps its ``OSError``, which already names the path.
    """
    try:
        return json.loads(Path(path).read_text(encoding=encoding))
    except UnicodeError as error:
        raise ValueError('%s %s is not readable text: %s'%(what,path,error)) from None
    except ValueError as error:
        raise ValueError('%s %s is not valid JSON: %s'%(what,path,error)) from None

class ConfigurationUnreadable(ValueError):
    """``deployment.private.json`` cannot be used as it is. The message names the file, for the operator.

    Not JSON, not text, not a JSON object, or a setting in it of the wrong kind. A
    ``ValueError`` with the words it always had where it had any, so every host command
    says what it said. Its own class so that the endpoint can mark the answer as a fault of
    the server and the web service can keep the path from the people it serves
    (kittrial-5bb.156). A file that cannot be OPENED keeps its ``OSError`` here (it names the
    path already); the endpoint marks that one too (``endpoint.configuration_fault``).
    """

def deployment_document(marker):
    """The parsed ``deployment.private.json`` at ``marker``, a JSON object; anything else is :class:`ConfigurationUnreadable`.

    Only a regular file is read. A FIFO in its place would block the reader until somebody
    wrote to it, and a directory has nothing to parse: both are refused at once, by opening
    without waiting and looking at what was opened (review of kittrial-5bb.156). A file that
    is not there, or cannot be opened, keeps its ``OSError``.
    """
    descriptor=os.open(str(marker),os.O_RDONLY|getattr(os,'O_NONBLOCK',0))
    try:
        import stat
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConfigurationUnreadable('Deployment configuration %s is not a regular file'%marker)
    finally:
        os.close(descriptor)
    try:
        document=read_json_file(marker,'Deployment configuration')
    except ValueError as error:
        raise ConfigurationUnreadable(str(error)) from None
    if not isinstance(document,dict):
        raise ConfigurationUnreadable('Deployment configuration %s is not a JSON object'%marker)
    return document

def deployment_password(root):
    """The database password in the deployment configuration; a file without one is :class:`ConfigurationUnreadable`."""
    password=config(root).get('password')
    if not isinstance(password,str):
        raise ConfigurationUnreadable('Deployment configuration %s has no password'%(root/'deployment.private.json'))
    return password

def config(root):
    return deployment_document(root/'deployment.private.json')

def operators(root, strict=False):
    """Server-side operator allowlist for void records.

    The deployment configuration (`deployment.private.json`'s `operators`) is
    the single authority source. Only these actors may author an operator void:
    the endpoint supplies this set to every read and the host `void-record`
    command refuses any other actor. A deployment that configures no operators
    authorizes nobody, so a forged or self-authored void comment is inert. This
    is never read from the void payload.

    `ORCHESTRA_OPERATORS` is not an authority source: a value that differs from
    the deployment configuration used to authorize an actor in an admin shell
    while the endpoint (config only) ignored the void. With `strict=True`
    (host-side write commands) that mismatch is refused loudly instead of being
    accepted in one place and ignored in another. Direct library use may still
    pass `operators` explicitly to `recovery.configured_operators`.
    """
    found=[]
    marker=root/'deployment.private.json'
    if marker.is_file():
        value=deployment_document(marker).get('operators')
        if isinstance(value,list):found.extend(value)
        elif isinstance(value,str):found.append(value)
        elif value is not None:raise ConfigurationUnreadable('deployment operators must be a list of actor identities')
    from recovery import configured_operators
    allowed=configured_operators(found)
    if strict:
        shell=configured_operators((os.environ.get('ORCHESTRA_OPERATORS') or '').replace(',',' ').split())
        if shell and shell!=allowed:
            raise ValueError('ORCHESTRA_OPERATORS is set in this shell but is not an authority source; '
                             'deployment.private.json operators is. Add the actor with `admin.py operators add` '
                             'or unset ORCHESTRA_OPERATORS before this command.')
    return allowed

def review_workflow_writes(root, strict=False, warnings=None):
    """The per-installation switch for WRITING the new review-workflow shapes.

    kittrial-5bb.94 item 3 asked for a two-step ship: this kit's READERS understand
    the new operations and fields (withdraw, request-review, resolve-item,
    decline-review, an item severity and a request-changes summary), but writing
    them is refused unless this installation opts in, because a kit built before
    the change fails closed on any chain carrying one. `deployment.private.json`'s
    `review_workflow_writes` is the single source; absent or false means OFF, so a
    fresh install and a rolled-back one behave identically. Unlike `operators`
    there is deliberately no `ORCHESTRA_*` fallback: it is not an authority list,
    it is a deployment capability, and the endpoint supplies it to the review write
    path. The coordinator turns it on (`admin.py review-writes on --actor OPERATOR`)
    once the rollback target is a kit that reads the new shapes.

    A value that is neither true/false nor absent is read as OFF with a warning
    (kittrial-5bb.110 item 2): raising made `work` and `review TASK` fail for every
    actor on the installation while `brief` still answered. When `warnings` is a
    list the warning is appended to it (the endpoint surfaces it on stderr);
    otherwise it is printed to stderr here.
    """
    enabled = False
    marker = root/'deployment.private.json'
    if marker.is_file():
        value = deployment_document(marker).get('review_workflow_writes')
        if isinstance(value,bool):
            enabled = value
        elif value is not None:
            message = ('deployment review_workflow_writes is %r, not true or false; reading it as off '
                       '(no new-shaped review write is allowed)' % (value,))
            if warnings is not None:
                warnings.append('WARNING: ' + message)
            else:
                print('WARNING: ' + message,file=sys.stderr)
    return enabled

#: The key of the checkpoint switch's audit list inside deployment.private.json, and the
#: prefix a damaged value is kept aside under (kittrial-5bb.131).
CHECKPOINT_AUDIT_KEY='checkpoint_provenance_audit'

#: The fields of one audit entry, exactly as every kit since kittrial-5bb.1 writes them.
CHECKPOINT_AUDIT_FIELDS=frozenset({'actor','at','action','previous','enabled'})

def checkpoint_audit_entry(item):
    """Whether one audit entry has the shape ``checkpoint-provenance-writes`` writes."""
    return (isinstance(item,dict) and set(item)==CHECKPOINT_AUDIT_FIELDS
            and isinstance(item['actor'],str) and isinstance(item['at'],str)
            and item['action'] in ('on','off') and isinstance(item['previous'],bool)
            and isinstance(item['enabled'],bool))

def checkpoint_provenance_audit(cfg):
    """``(entries, damage)`` for the checkpoint switch audit in a deployment config.

    A list of entries of the written shape reads as the history; anything else is
    ``([], reason)``: not a list (kittrial-5bb.131), or an entry that is not a record of
    exactly the fields the switch writes (kittrial-5bb.136; a list of arbitrary objects
    used to count as a readable history). The reason names what is there, never its
    content."""
    value=cfg.get(CHECKPOINT_AUDIT_KEY,[])
    if not isinstance(value,list):
        return [],'%s is a %s, not a list of records'%(CHECKPOINT_AUDIT_KEY,type(value).__name__)
    for index,item in enumerate(value):
        if not checkpoint_audit_entry(item):
            return [],'entry %d of %s is not a record of the shape the switch writes'%(index,CHECKPOINT_AUDIT_KEY)
    return value,None

def checkpoint_provenance_switch(root,action,actor):
    """Read or flip ``checkpoint_provenance_writes`` with its operator audit.

    A flip holds the deployment switch lock (``review_writes_lock``) across the whole
    read-modify-write of deployment.private.json, as ``review-writes`` does: without it
    two simultaneous flips each rewrote the file from the same read and one audit entry
    was lost (kittrial-5bb.131: 18 of 24 recorded). The one lock serialises both switches,
    which rewrite the same file.

    A damaged audit (``checkpoint_provenance_audit`` not a list of records, a hand edit)
    no longer blocks the switch. ``status`` reads it as an empty history and warns; the
    next flip keeps the damaged value aside in the same file under
    ``checkpoint_provenance_audit_damaged_<UTC stamp>`` and starts a fresh list, as
    ``review-writes`` keeps a damaged audit file aside under a dated name. Warnings go to
    stderr; the result says whether the audit read."""
    from briefing import checkpoint_writes_enabled
    from recovery import identity
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    actor=identity(actor,'Invalid actor identity')
    if actor not in operators(root,strict=True):
        raise ValueError('checkpoint-provenance-writes requires an actor on the deployment operator allowlist')
    if action not in ('status','on','off'):raise ValueError('Invalid checkpoint provenance switch action')
    if action=='status':
        current=checkpoint_writes_enabled(root)
        audit,damage=checkpoint_provenance_audit(config(root))
        if damage is not None:
            print('WARNING: the checkpoint provenance switch audit is damaged (%s); it reads as an empty history. '
                  'The next on/off keeps it aside in deployment.private.json under %s_damaged_<UTC stamp> and '
                  'starts a fresh list.'%(damage,CHECKPOINT_AUDIT_KEY),file=sys.stderr)
        return dict(checkpoint_provenance_writes=current,audit_records=len(audit),audit_readable=damage is None)
    enabled=action=='on'
    with review_writes_lock(root):
        current=checkpoint_writes_enabled(root)
        cfg=config(root)
        audit,damage=checkpoint_provenance_audit(cfg)
        if enabled==current:
            # A flip that changes nothing writes nothing, as `review-writes` does
            # (kittrial-5bb.136): the history records changes, and a repeated `on` cannot
            # rewrite the file or set a damaged audit aside.
            return dict(checkpoint_provenance_writes=current,audit_records=len(audit),
                        audit_readable=damage is None,changed=False)
        if damage is not None:
            kept='%s_damaged_%s'%(CHECKPOINT_AUDIT_KEY,utc_stamp().replace(':','').replace('-',''))
            suffix=1
            while kept+('' if suffix==1 else '_%d'%suffix) in cfg:suffix+=1
            kept+=('' if suffix==1 else '_%d'%suffix)
            cfg[kept]=cfg.pop(CHECKPOINT_AUDIT_KEY)
            print('WARNING: the checkpoint provenance switch audit was damaged (%s); it is kept aside in '
                  'deployment.private.json as %s and this flip starts a fresh list.'%(damage,kept),file=sys.stderr)
        if enabled:cfg['checkpoint_provenance_writes']=True
        else:cfg.pop('checkpoint_provenance_writes',None)
        cfg[CHECKPOINT_AUDIT_KEY]=audit+[dict(actor=actor,at=utc_stamp(),action=action,
                                              previous=current,enabled=enabled)]
        atomic_private_write(marker,json.dumps(cfg))
    if not enabled:
        print('Warning: existing provenance tasks refuse new legacy checkpoints; disabling does not make their history readable by older kits.',file=sys.stderr)
    return dict(checkpoint_provenance_writes=enabled,audit_records=len(audit)+1,audit_readable=True,changed=True)

#: Audit record of the switch flips, beside deployment.private.json. It is
#: deployment-level (there is one switch per installation, not per project), so it
#: is not part of any project's coordination backup. `review-writes` writes it under
#: ``REVIEW_WRITES_LOCK`` so a flip records the value it replaced.
REVIEW_WRITES_AUDIT = 'review-writes.audit.json'
REVIEW_WRITES_LOCK = '.review-writes.lock'
#: The append-only audit history's schema. Version 1 was the single-record form an
#: earlier kit wrote; it is still READ as its one entry so an upgrade keeps the record.
REVIEW_WRITES_AUDIT_SCHEMA = 2
#: How many flips the short history keeps. The audit exists to answer "who turned it
#: on, when, and who turned it off", not to be an unbounded log.
REVIEW_WRITES_AUDIT_MAX = 20


def _review_writes_entry(record):
    """Whether one audit entry is the shape `review-writes` writes."""
    return (isinstance(record,dict) and record.get('schema_version')==1
            and isinstance(record.get('review_workflow_writes'),bool)
            and isinstance(record.get('set_by'),str) and isinstance(record.get('set_at'),str)
            and isinstance(record.get('previous'),bool))


def _review_writes_history(record):
    """``(entries, damage)`` for a decoded audit file; ``damage`` is None when it reads.

    One place decides what a readable audit history is, so the reader and the flip that
    keeps a DAMAGED file aside cannot disagree (kittrial-5bb.110 item 3 P3). ``not JSON``
    never reaches here: the caller reports the parse failure itself.
    """
    if not isinstance(record,dict):
        return [],'not a JSON object'
    if record.get('schema_version')==1:
        return ([record],None) if _review_writes_entry(record) else ([],'malformed entry')
    if record.get('schema_version')!=REVIEW_WRITES_AUDIT_SCHEMA:
        return [],'schema %r is not 1 or %d'%(record.get('schema_version'),REVIEW_WRITES_AUDIT_SCHEMA)
    entries=record.get('entries')
    if not isinstance(entries,list):
        return [],'entries is not a list'
    kept=[entry for entry in entries if _review_writes_entry(entry)]
    if len(kept)!=len(entries):
        return kept,'malformed entr%s'%('y' if len(entries)-len(kept)==1 else 'ies')
    return kept,None


def _read_review_writes_audit(root):
    """``(entries, damage)`` for the audit file; ``(None, None)`` when it is absent.

    The ONE place the audit file is parsed, so the reader, the damage report and the flip
    that keeps a damaged file aside cannot disagree (kittrial-5bb.110 item 3 P3).
    ``damage`` is None when the file is absent or reads cleanly.
    """
    path=root/REVIEW_WRITES_AUDIT
    if not path.is_file():
        return None,None
    try:
        record=record_json.loads(path.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError):
        return [],'unreadable or not valid JSON'
    return _review_writes_history(record)


def _review_writes_audit_damage(root):
    """Why the audit file is not a readable history, or None when it is absent/readable."""
    return _read_review_writes_audit(root)[1]


def keep_damaged_review_writes_audit(root,stamp=None):
    """Rename a DAMAGED audit file aside under a dated name; None when it reads cleanly.

    The next flip REPLACES the history with a fresh readable one, so a file this kit
    cannot read used to be destroyed silently and `status` then reported
    ``audit_agrees: true`` beside an empty history (kittrial-5bb.110 item 3 P3). The
    damaged bytes are kept beside the deployment file instead, under
    ``review-writes.audit.json.damaged-<UTC date-time>`` (``.N`` on collision), and the
    caller says so. Returns the path kept aside.
    """
    if _review_writes_audit_damage(root) is None:
        return None
    from datetime import datetime,timezone
    path=root/REVIEW_WRITES_AUDIT
    stamp=stamp or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    aside=root/('%s.damaged-%s'%(REVIEW_WRITES_AUDIT,stamp))
    number=1
    while aside.exists():
        aside=root/('%s.damaged-%s.%d'%(REVIEW_WRITES_AUDIT,stamp,number));number+=1
    os.replace(path,aside)
    return aside


def review_writes_audit(root,warnings=None):
    """The switch-flip history this kit recorded, oldest first; ``[]`` when unreadable.

    A SHORT APPEND-ONLY HISTORY, not the last flip (kittrial-5bb.110 item 3): after one
    operator turns the switch on and another turns it off, both entries stay, each naming
    WHO, WHEN and the value replaced, so the audit says how the switch got where it is.
    Schema 2 is ``{schema_version, entries: [...]}``, bounded to the last
    ``REVIEW_WRITES_AUDIT_MAX`` flips. The pre-history schema-1 single-record form an
    older kit wrote is read as its one entry.

    Read through ``record_json.loads``: a deeply nested file raises ``NestingError`` (a
    ``ValueError``) instead of ``RecursionError``, so `review-writes status` can never
    die with a traceback. A file that exists but is not a readable history -- not JSON, a
    list, schema 3, malformed entries -- is reported in `warnings` and the entries that
    ARE readable are returned, rather than silently reading as "no history"
    (kittrial-5bb.110 item 3 P3).
    """
    entries,damage = _read_review_writes_audit(root)
    if entries is None:
        return []
    if damage is not None and warnings is not None:
        warnings.append('WARNING: the review-writes audit file %s is damaged (%s); the switch history '
                        'shown is incomplete, and the next flip keeps the file aside under a dated name '
                        'before writing a fresh history' % (root/REVIEW_WRITES_AUDIT,damage))
    return entries


def write_review_writes_audit(root, enabled, actor, previous, entries=None):
    """APPEND who flipped ``review_workflow_writes``, when, and the value replaced.

    The history passed in (`entries`, oldest first) plus the new entry is written as one
    atomic 0600 record, trimmed to the last ``REVIEW_WRITES_AUDIT_MAX`` flips. The caller
    holds ``REVIEW_WRITES_LOCK`` for the whole read-modify-write so two concurrent flips
    cannot lose one another (kittrial-5bb.110 item 3).
    """
    from datetime import datetime,timezone
    entry = {'schema_version':1,'review_workflow_writes':bool(enabled),'set_by':actor,
             'set_at':datetime.now(timezone.utc).isoformat(),'previous':bool(previous)}
    history = [item for item in (entries or []) if _review_writes_entry(item)]
    history.append(entry)
    history = history[-REVIEW_WRITES_AUDIT_MAX:]
    atomic_private_write(root/REVIEW_WRITES_AUDIT,
                         json.dumps({'schema_version':REVIEW_WRITES_AUDIT_SCHEMA,
                                     'entries':history}))
    return entry


#: How long a change to deployment.private.json waits for another one to finish before it
#: refuses (kittrial-5bb.136). Every such change takes milliseconds, so a holder that keeps
#: the lock this long is stuck; refusing says so instead of hanging the command for good.
DEPLOYMENT_LOCK_WAIT_SECONDS = 10
DEPLOYMENT_LOCK_POLL_SECONDS = 0.05


class DeploymentLockBusy(ValueError):
    """``deployment_config_lock`` gave up on a lock another change still holds; nothing was changed."""


@contextmanager
def deployment_config_lock(root):
    """Serialise one read-modify-write of deployment.private.json with an exclusive flock.

    EVERY writer of that file takes it: both deployment switches (``review-writes``,
    ``checkpoint-provenance-writes``), ``operators add|remove``, ``verifiers add|remove``,
    ``project-creations --set-server-limit`` (``project_creation.set_server_limit``) and
    the restore merges of operators and verifiers (kittrial-5bb.136). Each re-reads the
    file under the lock, so no change is lost to another made at the same instant. The
    file keeps the name ``.review-writes.lock`` (REVIEW_WRITES_LOCK), so a flip on an older
    kit still excludes a change here.

    A holder that does not finish within ``DEPLOYMENT_LOCK_WAIT_SECONDS`` makes the change
    refuse with nothing changed, rather than wait for good; reads never take the lock, and
    a killed holder releases it with its process. Once it is held, temporary copies an
    interrupted write left beside the file are removed (``remove_private_write_leftovers``).
    POSIX-only, like every other
    coordination lock in the kit: on a host without ``fcntl`` the atomic file writes still
    stand. The lock file holds no state and is never backed up.
    """
    handle=(root/REVIEW_WRITES_LOCK).open('a')
    try:
        try:
            import fcntl
        except ImportError:
            fcntl=None
        if fcntl is not None and not hasattr(fcntl,'LOCK_NB'):
            fcntl.flock(handle,fcntl.LOCK_EX)   # a platform without non-blocking flock waits
        elif fcntl is not None:
            deadline=time.monotonic()+DEPLOYMENT_LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:
                        raise DeploymentLockBusy('Nothing was changed: another change to deployment.private.json still holds '
                                         'its lock (%s) after %d s. Run the command again; if this repeats, find '
                                         'the process holding it (for example `fuser %s`).'
                                         %(REVIEW_WRITES_LOCK,DEPLOYMENT_LOCK_WAIT_SECONDS,REVIEW_WRITES_LOCK)) from None
                    time.sleep(DEPLOYMENT_LOCK_POLL_SECONDS)
        if fcntl is not None:
            # Only while the lock is held: no writer of this kit is mid-write now, so a
            # temporary copy beside the file is a leftover, never another writer's.
            remove_private_write_leftovers(root)
        yield handle
    finally:
        handle.close()

def remove_private_write_leftovers(root):
    """Remove temporary copies the private files' interrupted writes left behind.

    ``atomic_private_write`` writes a sibling ``.NAME.XXXXXXXX`` (the ``mkstemp`` name) and
    renames it over the file; a writer killed between the two leaves that copy behind. For
    ``deployment.private.json`` that copy holds the full configuration with the Dolt password
    (kittrial-5bb.142). The audits this kit appends to are written exactly the same way, under
    the same deployment lock, so their leftovers are removed too (kittrial-5bb.192 review item
    4): a kill inside the audit's own write used to leave
    ``.authority-changes.audit.json.XXXXXXXX`` for good. The next locked write removes them,
    which is the only moment no writer of this kit can be mid-write. Returns the names removed.

    The authority-changes audit has a SECOND leftover shape: the copy of a damaged audit goes
    through ``.authority-changes.audit.json.damaged-<STAMP>[.N].tmp-<16 hex>`` and a kill inside
    that copy leaves the temporary name behind, which the 8-character pattern never matched
    (kittrial-5bb.229 rev-2 item 4). ``AUTHORITY_CHANGE_COPY_LEFTOVER`` is that alternation,
    added for this audit alone.
    """
    removed=[]
    try:
        names=os.listdir(root)
    except OSError:
        return removed
    for target in ('deployment.private.json',AUTHORITY_CHANGES_AUDIT,ACTOR_ADOPTIONS_AUDIT,REVIEW_WRITES_AUDIT):
        alternatives=[r'\.[A-Za-z0-9_]{8}']
        if target==AUTHORITY_CHANGES_AUDIT:alternatives.append(AUTHORITY_CHANGE_COPY_LEFTOVER)
        pattern=re.compile(r'\.'+re.escape(target)+r'(?:'+'|'.join(alternatives)+r')')
        found=[]
        for name in sorted(names):
            if not pattern.fullmatch(name):continue
            try:
                os.unlink(os.path.join(str(root),name))
            except OSError:
                continue
            found.append(name)
        if not found:continue
        removed.extend(found)
        print('Removed %d temporary cop%s of %s left by an interrupted write: %s'
              %(len(found),'y' if len(found)==1 else 'ies',target,', '.join(found)),file=sys.stderr)
    return removed

#: The name the switch code and kittrial-5bb.110's tests use.
review_writes_lock=deployment_config_lock


def review_writes_command(root, actor, action):
    """Read or flip ``review_workflow_writes``; returns ``(result, warnings)``.

    The actor must be on the deployment operator allowlist, so a contributor that
    reaches the host command line cannot turn the switch on or off
    (kittrial-5bb.110 item 1 / review mutation M15). A flip then APPENDS who set it and
    when to the audit history under the deployment lock (item 3). An action that does not
    change the value writes nothing at all, so an on-that-changes-nothing cannot
    overwrite the history. `status` reports ``audit_agrees`` and warns when the switch
    value and the last recorded flip disagree (an older kit, or a hand edit, changed one
    without the other). Extracted from the CLI so the allowlist and audit behaviour are
    unit-testable without a subprocess.
    """
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    from recovery import identity
    actor=identity(actor,'Invalid actor identity')
    authority=operators(root, strict=True)
    if actor not in authority:
        raise ValueError('review-writes requires an actor on the deployment operator allowlist '
                         '(deployment.private.json operators); ' + actor + ' is not on it')
    if action=='status':
        warnings=[]
        enabled=review_workflow_writes(root,warnings=warnings)
        history=review_writes_audit(root,warnings=warnings)
        last=history[-1] if history else None
        # The switch and the audit are written together by a flip, so they disagree only
        # when something else changed one of them: an older kit (which does not know the
        # audit file at all) or a hand edit. A DAMAGED audit file is a disagreement too:
        # it must not read as "no history, everything agrees" (kittrial-5bb.110 item 3).
        damage=_review_writes_audit_damage(root)
        agrees=damage is None and (last is None or last['review_workflow_writes']==enabled)
        if last is not None and not agrees:
            warnings.append('WARNING: deployment review_workflow_writes is %s but the recorded audit '
                            'history last says %s (set by %s at %s); the switch was changed without '
                            'recording it here (an older kit or a hand edit), so the audit is stale'
                            % ('on' if enabled else 'off',
                               'on' if last['review_workflow_writes'] else 'off',
                               last['set_by'],last['set_at']))
        return {'review_workflow_writes':enabled,'audit':last,'audit_history':history,
                'audit_agrees':agrees},warnings
    # One hold of the deployment lock for the whole read-modify-write, so the audit
    # history records the value that was actually replaced (item 3).
    with review_writes_lock(root):
        cfg=config(root)
        previous=review_workflow_writes(root)
        enabled=action=='on'
        if enabled==previous:
            # An on that changes nothing is not a flip: it must not add an entry or
            # overwrite the history (item 3).
            return {'review_workflow_writes':previous,'changed':False},[]
        # A DAMAGED history is kept aside under a dated name before it is replaced, and
        # the caller is told; it used to be silently destroyed (kittrial-5bb.110 item 3).
        warnings=[]
        aside=keep_damaged_review_writes_audit(root)
        if aside is not None:
            warnings.append('WARNING: the review-writes audit file was damaged and has been kept aside '
                            'as %s; this flip starts a fresh history' % aside)
        # OFF is the absent key, so a deployment that never turned it on and one
        # that turned it back off read identically.
        if enabled:cfg['review_workflow_writes']=True
        else:cfg.pop('review_workflow_writes',None)
        atomic_private_write(marker,json.dumps(cfg))
        write_review_writes_audit(root,enabled,actor,previous,entries=review_writes_audit(root))
    return {'review_workflow_writes':review_workflow_writes(root),'changed':True},warnings


def stored_operators(cfg):
    """The deployment allowlist as a list of identity strings.

    ``deployment.private.json`` is hand-editable and ``operators()`` already
    treats a bare string as a one-element allowlist, so a string is normalised
    here rather than iterated: ``list('alice')`` is exactly what rewrote a string
    allowlist as the single letters of its name while dropping the real identity
    and granting five one-letter operators. Every writer of the allowlist uses
    this one normalisation point, and a value that is neither a list nor a string
    is refused before any write instead of being silently coerced.
    """
    from recovery import identity
    value=cfg.get('operators')
    if value is None:return []
    if isinstance(value,str):value=[value]
    elif not isinstance(value,list):raise ValueError('deployment operators must be a list of actor identities')
    return [identity(item,'Invalid operator identity in the deployment allowlist') for item in value]

def verifiers(root, strict=False):
    """Server-side `verifiers` list for capability verifications (.60 section 5.2).

    A second, narrow deployment-wide authority beside `operators`: an actor listed here
    may record a capability check that readers count as `verified`
    (`capability-verify`), and nothing else. `deployment.private.json`'s `verifiers` is
    the single authority source, and the list is empty by default. Like `operators`,
    `ORCHESTRA_VERIFIERS` is never an authority source: with `strict=True` (host-side
    write commands) a shell value that disagrees with the file is refused loudly.
    """
    found=[]
    marker=root/'deployment.private.json'
    if marker.is_file():
        value=deployment_document(marker).get('verifiers')
        if isinstance(value,list):found.extend(value)
        elif isinstance(value,str):found.append(value)
        elif value is not None:raise ConfigurationUnreadable('deployment verifiers must be a list of actor identities')
    from recovery import configured_operators
    allowed=configured_operators(found)
    if strict:
        shell=configured_operators((os.environ.get('ORCHESTRA_VERIFIERS') or '').replace(',',' ').split())
        if shell and shell!=allowed:
            raise ValueError('ORCHESTRA_VERIFIERS is set in this shell but is not an authority source; '
                             'deployment.private.json verifiers is. Add the actor with `admin.py verifiers add` '
                             'or unset ORCHESTRA_VERIFIERS before this command.')
    return allowed

def stored_verifiers(cfg):
    """The deployment verifiers list as identity strings, normalised like `stored_operators`."""
    from recovery import identity
    value=cfg.get('verifiers')
    if value is None:return []
    if isinstance(value,str):value=[value]
    elif not isinstance(value,list):raise ValueError('deployment verifiers must be a list of actor identities')
    return [identity(item,'Invalid verifier identity in the deployment verifiers list') for item in value]

def revoked_verifications(root,actor,limit=5):
    """Name the capability verifications one verifier's revocation changes.

    `verifiers remove ACTOR --confirm-revoke` makes every verification that actor
    recorded read `reported`, and drift that only their passes had cleared reappears.
    The answer is the difference between each capability's `verification` under the
    live lists and under the verifiers list without `actor`, read by the reader every
    other read uses. An actor who is also on the operator allowlist stays trusted, so
    nothing changes for them. Read-only and best-effort, like `revoked_revert_records`.
    """
    import capability_records
    authority=operators(root);listed=verifiers(root)
    remaining=frozenset(item for item in listed if item!=actor)
    changed=[];unreadable=0
    for name in initialized_projects(root):
        path=project_dir(root,name)
        try:
            rows=record_json.loads_rows(run_bd(root,name,['export','--all']))
            entries,_=capability_records.catalog(rows,authority)
            before=capability_records.Trust(None,authority,listed,path,export_rows=rows)
            after=capability_records.Trust(None,authority,remaining,path,export_rows=rows)
            for entry in entries:
                if entry['state'] in ('malformed','unsupported'):continue
                was=capability_records.verification_of(entry,before)['state']
                now=capability_records.verification_of(entry,after)['state']
                if was!=now:changed.append('%s/%s %s -> %s'%(name,entry['key'],was,now))
        except (OSError,ValueError,TypeError,KeyError):
            unreadable+=1
    changed.sort()
    shown=', '.join(changed[:limit])+(' (+%d more)'%(len(changed)-limit) if len(changed)>limit else '')
    return ' (capabilities whose verification changes: %s; projects that could not be read: %d)'%(shown or 'none',unreadable)

def atomic_private_write(path, text):
    """Write a private config file atomically at mode 0600.

    `deployment.private.json` also holds the Dolt password, so it must never be
    truncated in place or left group/world readable. The new bytes go to a
    sibling temporary file created 0600, are fsynced, and replace the target in
    one step; an existing permissive mode cannot survive because the
    replacement is a fresh inode.
    """
    import tempfile
    path=Path(path)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp,0o600)
        os.replace(tmp,path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
    try:  # POSIX: make the rename durable; not available on every platform.
        dir_fd=os.open(str(path.parent),os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
    except OSError:
        pass
    return path


def environment(root):
    """Environment for the runtime's own binaries; every path stays under ``root``.

    ``HOME`` is scoped here as well as ``XDG_CONFIG_HOME``. bd 1.2.2 resolves its
    user-level config with ``UserConfigYamlPath()``: it prefers
    ``$HOME/.config/bd/config.yaml`` and consults ``os.UserConfigDir()``
    (``$XDG_CONFIG_HOME``) only when that file already exists, so a normal ``HOME``
    lets every bd command - including ``bd metrics off`` in ``prepare`` - create or
    rewrite ``~/.config/bd/config.yaml`` outside the runtime
    (``internal/config/yaml_config.go``, ``internal/metrics/userconfig.go``).
    Scoping ``HOME`` keeps that write, and bd's event data, inside the runtime so
    several runtimes can share a login user. Dolt's own global config is already
    pinned by ``DOLT_ROOT_PATH``.

    ``BD_DISABLE_METRICS`` is the durable guard for an UPGRADED runtime. A runtime
    prepared before this change (or by ``admin.install``) keeps its metrics-off
    setting only in the login user's ``~/.config/bd/config.yaml`` and has no
    ``<root>/home``. ``prepare`` then takes the existing-deployment early-return
    path and never runs ``bd metrics off``, so the new ``HOME`` - which lacks that
    config - would let bd re-enable usage metrics, queue
    ``home/.beads/eventsData/*.evtq`` and start a ``bd send-metrics`` child.
    Every bd child inherits this variable instead, which keeps a runtime that was
    prepared the old way metrics-off without touching anything outside ``root``.
    """
    env=os.environ.copy()
    password=deployment_password(root)
    # The account's own home, recorded before HOME is scoped into the runtime below: the
    # scheduled-backup units are installed there, and the web service and the endpoint
    # it starts run under this environment and must still find them (kittrial-5bb.118).
    # The kit SETS the variable, from the home it is about to replace; a value a caller
    # put in the environment is overwritten. Only a process that is already under the
    # scoped home (a child of one that ran this) keeps the value its parent set.
    if env.get('HOME') and Path(env['HOME'])!=root/'home':
        env[ACCOUNT_HOME_ENV]=env['HOME']
    elif not env.get('HOME'):
        env.pop(ACCOUNT_HOME_ENV,None)
    env.update({'HOME':str(root/'home'),
                'PATH':str(root/'bin')+os.pathsep+env.get('PATH',''),
                'DOLT_ROOT_PATH':str(root/'dolt-home'),'XDG_CONFIG_HOME':str(root/'config'),
                'BEADS_DOLT_PASSWORD':password,'DOLT_CLI_PASSWORD':password,
                'BD_NON_INTERACTIVE':'1','BEADS_NO_DAEMON':'1','BD_DISABLE_METRICS':'1'})
    return env

def sql(root,query,password=None,timeout=None):
    cfg=config(root);env=environment(root)
    if password is not None: env['DOLT_CLI_PASSWORD']=password
    return checked([root/'bin/dolt','--host','127.0.0.1','--port',cfg['port'],'--no-tls','--user','root','sql','--result-format','csv'],input=query,env=env,cwd=root,timeout=timeout).stdout

def project_dir(root,name):
    validate_name(name)
    path=root/'projects'/name
    if path.is_symlink(): raise ValueError('Project directory must not be a symlink')
    return path

def run_bd(root,name,args):
    path=project_dir(root,name)
    location=[] if args and args[0]=='init' else ['--directory',path]
    return checked([root/'bin/bd',*location,'--sandbox',*args],env=environment(root),cwd=path).stdout

def project_server_metadata(root,name):
    """(host,port,user,database) from the project's ``.beads/metadata.json``, or None.

    ``bd init --server`` records the loopback Dolt server coordinates there. The
    password is deliberately not part of this tuple: ``environment(root)`` supplies it
    through ``DOLT_CLI_PASSWORD``, so a credential never reaches a command line or a
    recorded failure reason. A project without those keys (an older or partial
    project) has no SQL coordinates, and the caller keeps the pre-existing native path.
    """
    try:
        data=json.loads((project_dir(root,name)/'.beads'/'metadata.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return None
    if not isinstance(data,dict):return None
    keys=('dolt_server_host','dolt_server_port','dolt_server_user','dolt_database')
    if not all(data.get(key) for key in keys):return None
    return tuple(data[key] for key in keys)

def project_metadata_state(root,name):
    """What ``.beads/metadata.json`` says about a project, for the checks that must not guess.

    ``server`` (it records the Dolt server coordinates), ``absent`` (no such file: the
    project was never initialized), or ``unreadable`` (the file is there but cannot be
    opened, is not JSON, or does not record the coordinates). Every project this kit
    creates is a server project (``bd init --server``), so ``unreadable`` never means "an
    embedded project": bd run there would fall back to an embedded database, CREATE
    ``.beads/embeddeddolt`` inside the project and report zero issues.
    """
    path=project_dir(root,name)/'.beads'/'metadata.json'
    try:
        os.lstat(path)
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'unreadable'
    return 'server' if project_server_metadata(root,name) is not None else 'unreadable'

def project_backup_record(root,name):
    """The parsed ``.beads/dolt-backup.json`` a project records, or None.

    ``bd backup init`` writes ``backup_name`` (the Dolt backup entry a sync names) and
    ``backup_url`` (the destination that entry pushes to), so this file is the kit's local
    view of a project's native backup target. An absent, unreadable or non-object file
    reads as None, which keeps a project with no recorded target on its pre-existing path.
    """
    try:
        data=json.loads((project_dir(root,name)/'.beads'/'dolt-backup.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return None
    return data if isinstance(data,dict) else None

def project_backup_name(root,name):
    """The Dolt backup name recorded in the project's ``.beads/dolt-backup.json``."""
    record=project_backup_record(root,name)
    if record is None:return None
    value=record.get('backup_name')
    return value if isinstance(value,str) and value else None

def local_backup_path(url,base=None):
    """The local path a recorded ``backup_url`` names, or None when it names no local path.

    ``bd backup init`` records a filesystem destination as a ``file://`` URL (a bare path is
    accepted too). A DoltHub remote, a malformed URL, or a ``file:`` URL carrying a host is
    not this runtime's ``backups/<name>`` directory, so it resolves to None and a caller
    comparing targets refuses it. A bare relative path used to resolve against the process
    cwd, which made the same record acceptable or refused depending on where ``admin.py``
    was invoked; passing ``base`` (the project directory) makes that verdict deterministic.
    """
    if not isinstance(url,str) or not url:return None
    if url.startswith('file:'):
        parts=urlsplit(url)
        if parts.scheme!='file' or parts.netloc not in ('','localhost'):return None
        return Path(url2pathname(unquote(parts.path))).resolve()
    if '://' in url:return None
    path=Path(url).expanduser()
    if base is not None and not path.is_absolute():path=Path(base)/path
    return path.resolve()

def expected_backup_target(root,name):
    """The native backup directory a project named ``name`` must record: ``backups/<name>``."""
    return (root/'backups'/name).resolve()

def validate_backup_target(root,name):
    """Refuse a project whose recorded native backup target is not its own ``backups/<name>``.

    ``restore-new`` creates a clone with ``bd backup restore``, which brings the SOURCE
    project's ``.beads/dolt-backup.json`` and restored ``dolt_backups`` row along with the
    database: both still name ``backups/<source>``. Backing up the clone would then sync it
    into the SOURCE's directory - rewriting a generation whose complete sidecar and journal
    snapshot still describe the source - and leave the clone's own directory stale, so the
    kit refuses before any native command instead. A project that records no target at all
    keeps its pre-existing path.
    """
    record=project_backup_record(root,name)
    if record is None:return
    url=record.get('backup_url')
    if url is None:return
    expected=expected_backup_target(root,name)
    if local_backup_path(url,project_dir(root,name))!=expected:
        raise ValueError(
            'Project %s records native backup target %s, not %s; refusing to sync, because a '
            'restored clone inherits the source project\'s target and would overwrite that '
            'project\'s backup. Re-point it with `admin.py backup-repoint %s`, which runs '
            '`bd backup init` under the kit environment and verifies both the recorded file '
            'and the dolt_backups row; re-running `restore-new` onto an existing project is '
            'refused and would discard the clone.'
            %(name,url,expected,name))

def backup_rows(root,name):
    """The ``dolt_backups`` rows a project's database records, or None when they cannot be read.

    ``project_backup_record`` reads the kit's local view (``.beads/dolt-backup.json``);
    ``backup-repoint`` reads this row as well, so a re-point is confirmed in both the file
    and the database itself. A project with no loopback server coordinates has no SQL client
    to ask, so None means "could not be checked" rather than "no rows". The database name is
    validated before it reaches the statement, exactly like the backup name.
    """
    metadata=project_server_metadata(root,name)
    if metadata is None:return None
    database=metadata[3]
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',database):
        raise ValueError('The project records an unusable Dolt database name')
    rows=[]
    for row in csv.reader(io.StringIO(sql(root,"USE `%s`; SELECT name,url FROM dolt_backups;"%database))):
        if len(row)>=2 and row[0] and row[0]!='name':rows.append((row[0],row[1]))
    return rows

def repoint_backup(root,name):
    """Point a project's native backup at its own ``backups/<name>`` and verify both records.

    ``restore-new`` re-points a clone it creates, but a clone restored before that re-point
    existed still records the SOURCE project's target, and ``validate_backup_target`` then
    refuses to sync it. The refusal used to tell the operator to run a bare ``bd backup
    init``, which fails without the kit environment (and can silently re-point a live
    project). This command runs it through ``run_bd`` (the project directory,
    ``DOLT_CLI_PASSWORD`` from ``environment(root)``) and then reads back BOTH
    ``.beads/dolt-backup.json`` and the ``dolt_backups`` row, failing closed when either
    still names another target. It is idempotent: ``bd backup init`` updates an
    already-configured destination in place. Re-pointing moves no data, so the operator must
    back the SOURCE project up again afterwards - its ``backups/<source>`` may already hold
    this clone's data.
    """
    path=project_dir(root,name)
    if not (path/'.beads'/'metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
    expected=expected_backup_target(root,name)
    with backup_lock(root,name):
        run_bd(root,name,['backup','init',str(expected)])
    record=project_backup_record(root,name)
    recorded=None if record is None else record.get('backup_url')
    if recorded is None or local_backup_path(recorded,path)!=expected:
        raise ValueError(
            'backup-repoint %s did not hold: .beads/dolt-backup.json records %s, not %s'
            %(name,recorded,expected))
    backup_name=project_backup_name(root,name)
    rows=backup_rows(root,name)
    if rows is None:
        return {'project':name,'backup_name':backup_name,'backup_url':recorded,
                'row_url':None,'row_checked':False}
    row_urls=[url for row_name,url in rows if row_name==backup_name]
    if not row_urls or local_backup_path(row_urls[0],path)!=expected:
        raise ValueError(
            'backup-repoint %s did not hold: the dolt_backups row for %r records %s, not %s'
            %(name,backup_name,row_urls[0] if row_urls else None,expected))
    return {'project':name,'backup_name':backup_name,'backup_url':recorded,
            'row_url':row_urls[0],'row_checked':True}

def native_backup_sync(root,name,client=None):
    """Synchronize one project's native Dolt backup without bd's fixed read timeout.

    ``bd backup sync`` inherits a fixed client read timeout of about ten seconds, which
    a large database cannot meet on a busy or stalled server (that timeout is what
    forced the live installations onto a long-sync wrapper). Dolt's SQL client has no
    such timeout, so the native step is ``CALL DOLT_BACKUP('sync', <backup_name>)``
    over the same loopback connection ``sql()`` uses, bounded only by the explicit
    ``BACKUP_SYNC_TIMEOUT``. It runs in the caller's critical section, so the
    coordination sidecar and the native state still move as one pair.

    The SQL client is started in its own session through ``spawn_sync_client`` and its pid
    is recorded in the optional ``client`` handle, so the caller can stop the whole process
    group when the run is interrupted instead of leaving a ``dolt`` client writing the
    native backup after the lock is released. The backup name is validated before it
    reaches the statement, and the password travels in the environment, never in the
    command. A project that carries no server metadata has no SQL coordinates to use, so
    it keeps the pre-existing ``bd backup sync`` path and is not part of that session
    handling.
    """
    # A clone restored from another project records THAT project's target until it is
    # re-pointed (see validate_backup_target), and syncing into it would overwrite the
    # source project's backup directory.
    validate_backup_target(root,name)
    metadata=project_server_metadata(root,name)
    backup_name=project_backup_name(root,name)
    if metadata is None or backup_name is None:
        return run_bd(root,name,['backup','sync'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',backup_name):
        raise ValueError('The project records an unusable Dolt backup name')
    host,port,user,database=metadata
    command=[root/'bin/dolt','--host',str(host),'--port',str(port),'--no-tls','--user',str(user),
             '--use-db',str(database),'sql','-q',"CALL DOLT_BACKUP('sync', '%s')"%backup_name]
    handle=client if client is not None else SyncClientHandle()
    return spawn_sync_client(command,handle,env=environment(root),cwd=root,timeout=BACKUP_SYNC_TIMEOUT)

def native_restore_url(backup):
    """The ``file://`` URL ``CALL DOLT_BACKUP('restore', ...)`` reads ``backup`` from.

    The same form ``bd backup restore`` builds (``"file://" + absolute path``, not
    percent-encoded). The URL is embedded in a SQL string literal, so a path that would need
    quoting there (a quote, a backslash or a control character) is refused rather than
    escaped; a runtime root accepted by ``root_path`` never contains one.
    """
    path=Path(backup).resolve().as_posix()
    url='file://'+(path if path.startswith('/') else '/'+path)
    if re.search(r"['\\\x00-\x1f]",url):
        raise ValueError('The backup path cannot be named in a native restore statement')
    return url

def project_identity(root,database):
    """The ``_project_id`` a project's Dolt database records, or None when it records none."""
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',database):
        raise ValueError('The project records an unusable Dolt database name')
    rows=list(csv.reader(io.StringIO(
        sql(root,"SELECT value FROM `%s`.metadata WHERE `key`='_project_id';"%database))))
    values=[row[0].strip() for row in rows[1:] if row and row[0].strip()]
    return values[0] if values else None

def adopt_project_identity(root,name):
    """Write the restored database's ``_project_id`` into the project's ``.beads/metadata.json``.

    ``bd backup restore --force`` does this itself after its restore (``syncProjectIDFromDB``):
    the restored database carries the SOURCE project's identity, while ``metadata.json`` still
    holds the one ``bd init`` generated for the new project, and bd refuses every later command
    with ``PROJECT IDENTITY MISMATCH`` until the two agree. The SQL-client restore does not run
    bd, so the kit performs the same step. Like bd, a database that records no identity leaves
    the file unchanged. The file is replaced atomically and keeps its mode and every other key.
    Returns the adopted identity, or None when nothing changed.
    """
    metadata=project_server_metadata(root,name)
    if metadata is None:return None
    identity=project_identity(root,metadata[3])
    if identity is None:return None
    path=project_dir(root,name)/'.beads'/'metadata.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('project_id')==identity:return None
    data['project_id']=identity
    mode=path.stat().st_mode&0o777
    fd,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:
            handle.write(json.dumps(data,indent=2)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary,mode)
        os.replace(temporary,path)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise
    return identity

def native_restore(root,source,destination,client=None):
    """Restore ``backups/<source>`` into the new project ``destination``; return a report line.

    ``bd backup restore`` inherits the same fixed client read timeout of about ten seconds as
    ``bd backup sync`` (see ``native_backup_sync``): a restore drill cut a 588 MB backup at
    exactly 10 s (``i/o timeout``, ``invalid connection``), while the same restore through the
    SQL client took under a minute. So the native step is ``CALL DOLT_BACKUP('restore',
    '--force', <file URL>, <database>)`` over the loopback connection, bounded only by the
    explicit ``RESTORE_TIMEOUT``. ``--force`` replaces the empty database ``bd init`` just
    created for the destination, exactly as ``bd backup restore --force`` does.

    The client runs in its own session through ``spawn_sync_client``; ``SIGTERM`` is turned
    into ``TerminatedBySignal`` for the duration, and on every exit that is not a normally
    finished client (a stop, the ceiling, an exception) the whole process group is killed
    before the source's backup lock is released. The password travels in the environment,
    never in the command. After the restore, the restored project identity is adopted into
    ``.beads/metadata.json`` (``adopt_project_identity``), which ``bd backup restore`` would
    otherwise have done. A destination with no Dolt server metadata has no SQL coordinates,
    so it keeps the ``bd backup restore`` path, as ``backup`` keeps ``bd backup sync``.
    """
    backup=root/'backups'/source
    metadata=project_server_metadata(root,destination)
    if metadata is None:
        return run_bd(root,destination,['backup','restore',str(backup),'--force'])
    host,port,user,database=metadata
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',str(database)):
        raise ValueError('The project records an unusable Dolt database name')
    url=native_restore_url(backup)
    command=[root/'bin/dolt','--host',str(host),'--port',str(port),'--no-tls','--user',str(user),
             'sql','-q',"CALL DOLT_BACKUP('restore', '--force', '%s', '%s')"%(url,database)]
    handle=client if client is not None else SyncClientHandle()
    started=time.monotonic()
    with signal_termination_guard():
        try:
            spawn_sync_client(command,handle,env=environment(root),cwd=root,timeout=RESTORE_TIMEOUT)
        finally:
            # Inside the guard: a second stop cannot kill the interpreter before the
            # client's group is killed. A finished client is not signalled again.
            terminate_process_group(handle)
        elapsed=time.monotonic()-started
        # Still inside the guard (kittrial-5bb.82 review): a SIGTERM during the identity
        # step is an exception the caller reports, not a silent death between the
        # restored database and its metadata.
        identity=adopt_project_identity(root,destination)
    report='Restored backups/%s into %s through the Dolt SQL client in %.1f s.'%(source,destination,elapsed)
    if identity is not None:
        report+=' Adopted the restored project identity %s into .beads/metadata.json.'%identity
    return report

def restore_destination_state(root,destination):
    """``empty`` when the destination a failed restore left is still the clean project
    ``add-project`` made (bd reads it and it holds nothing but its merge slot), ``missing``
    when its directory is gone (moved or retired under the restore), ``uninitialized``
    when ``add-project`` stopped before the project was initialized (no
    ``.beads/metadata.json``), else ``partial``. A project that cannot be read is partial;
    bd is not run without server coordinates (it would create an embedded database
    inside the directory). Never raises."""
    try:
        if not project_dir(root,destination).is_dir():return 'missing'
        metadata=project_metadata_state(root,destination)
    except (OSError,ValueError):return 'partial'
    if metadata=='absent':return 'uninitialized'
    if metadata!='server':return 'partial'
    try:
        rows=json.loads(run_bd(root,destination,['list','--all','--limit','0','--json']) or '[]')
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,TypeError,RecursionError):
        return 'partial'
    if not isinstance(rows,list):return 'partial'
    slot=destination+'-merge-slot'
    return 'empty' if all(isinstance(row,dict) and row.get('id')==slot for row in rows) else 'partial'

#: The name ``finish_restore`` gives the step that provisions the clone's merge slot, so
#: ``restore_failure_notice`` can say what is true after it fails (kittrial-5bb.202 rev-2).
MERGE_SLOT_STEP='merge-slot provisioning'

def restore_failure_notice(destination,error,state='partial',step='native restore',merge_slot=None):
    """What an operator must do after the native step of ``restore-new`` did not complete.

    ``state`` is ``restore_destination_state``'s answer: the notice says "partial" only
    when the destination is partial (kittrial-5bb.82 review). ``step`` names the step
    that stopped: every step after the destination starts to exist prints this notice.
    ``merge_slot`` is ``project_merge_slot_state``'s answer for the destination, read AFTER
    a failure of the provisioning step: the step now runs after the re-point, the
    coordination sidecar and the journals, so the notice may say those are in place, and it
    must not call the slot unprovisioned or "named missing by the report" unless this fresh
    read of the clone actually says so (kittrial-5bb.202 review of revision 2, F1).
    """
    if isinstance(error,subprocess.TimeoutExpired):
        cause='the native restore reached its %d s ceiling and its client was stopped'%RESTORE_TIMEOUT
    elif isinstance(error,(TerminatedBySignal,KeyboardInterrupt)):
        cause='the restore was interrupted'+(' and its client was stopped' if step=='native restore' else
                                             ' during the %s step'%step)
    else:
        cause='the %s step failed'%step if step!='native restore' else 'the native restore failed'
    retire=('admin.py retire-project %s --actor OPERATOR --reason TEXT'%destination)
    if state=='missing':
        return ('restore-new did not complete: %s. The directory of project %s is no longer there (it was moved '
                'or retired while the restore ran), so nothing more was written: no re-point, no coordination '
                'sidecar, no journals. Its database may hold restored data. Run restore-new again into another '
                'unused destination name; the source backup was not modified.'%(cause,destination))
    if state=='uninitialized':
        return ('restore-new did not complete: %s. The directory projects/%s exists but the project was not '
                'initialized, so it is not a working project: nothing was restored into it, and its database '
                'may or may not have been created. Do not use it as a tracker. Retire it (%s) and run '
                'restore-new again into another unused destination name; the source backup was not modified.'
                %(cause,destination,retire))
    if state=='empty' and step!=MERGE_SLOT_STEP:
        return ('restore-new did not complete: %s. Project %s exists as an empty, working project: nothing '
                'was restored into it (and its coordination sidecar, journals and operation journal were NOT '
                'restored). Do not use it as a tracker. Retire it (%s) and run restore-new again into another '
                'unused destination name; the source backup was not modified.'%(cause,destination,retire))
    if step==MERGE_SLOT_STEP:
        # This step runs LAST, after the re-point, the coordination sidecar and the journals
        # (kittrial-5bb.202 rev-3 item 1, review F1): so the clone really does hold the
        # restored tracker, its own backup target and its journals here, and the notice says
        # that. Whether the slot itself is there is re-read from the clone (``merge_slot``)
        # rather than assumed: the step can fail on its own check with a slot that came with
        # the source, and then "not provisioned" and "the report names it missing" would both
        # be untrue (the reviewer measured exactly that; merge-check rc 0, report healthy).
        # This branch comes BEFORE the "empty, working project" one on purpose: a clone from an
        # empty slotless source reads as that shape (bd lists no rows at all), and the generic
        # sentence there would then claim the sidecar and journals were NOT restored, which is
        # exactly what the reordering made false.
        in_place=('Project %s exists and holds the restored tracker: its data, its re-pointed backup target and '
                  'its journals are in place, and a backup of it taken now is expected to succeed.'%destination)
        where=(merge_slot or {}).get('state')
        detail=(merge_slot or {}).get('detail')
        if where in ('missing','damaged'):
            return ('restore-new did not complete: %s. %s Its merge slot is still %s: %s Run the merge-create '
                    'coordination operation for %s, or retire it (%s) and run restore-new again into another '
                    'unused destination name; the source backup was not modified.'
                    %(cause,in_place,where,detail or 'the host read it as unusable.',destination,retire))
        if where=='healthy':
            return ('restore-new did not complete: %s. %s Its merge slot reads healthy, so the failure was in the '
                    '%s step and not in the slot: nothing is left to mend, and merge-check and the read-only '
                    'merge-slot-report are expected to agree. Confirm with merge-check before using it, or retire '
                    'it (%s) and run restore-new again into another unused destination name; the source backup was '
                    'not modified.'%(cause,in_place,MERGE_SLOT_STEP,retire))
        return ('restore-new did not complete: %s. %s Whether it has a usable merge slot could not be read: the '
                'same fault stopped the %s step, so this notice does not claim it is missing. Run merge-check for '
                '%s, or the read-only merge-slot-report, and if it names the merge-create operation run that; else '
                'retire it (%s) and run restore-new again into another unused destination name. The source backup '
                'was not modified.'%(cause,in_place,MERGE_SLOT_STEP,destination,retire))
    return ('restore-new did not complete: %s. Project %s exists but holds a partial restore (its '
            'coordination sidecar, journals and operation journal were NOT restored). Preserve it for '
            'inspection, do not use or back it up as a tracker, and run restore-new again into another '
            'unused destination name; the source backup was not modified. While it stays in the runtime '
            'its backup fails and the backup gate reports the runtime incomplete: retire it with %s.'
            %(cause,destination,retire))

def provision_merge_slot(root,name):
    """Create the project's merge slot once, tolerating an existing slot.

    bd 1.2.2 reports a missing slot as ``{"available": false, "error": "not
    found", "id": "<project>-merge-slot"}`` with exit code 0, so a missing slot
    is detected with ``coordination.merge_slot_missing`` instead of by an absent
    ``available`` key (which is always present). A missing, unparseable or empty
    check result means there is nothing usable, so create: ``bd merge-slot
    create`` is idempotent (an existing slot returns ``status: open`` with no
    refusal). A create refusal naming an existing slot is still tolerated
    defensively, so an operator retry stays idempotent without depending on the
    exact native error text.
    """
    from coordination import merge_slot_missing
    try:
        state=json.loads(run_bd(root,name,['merge-slot','check','--json']))
    except (TypeError,ValueError,RecursionError):
        state=None
    if not merge_slot_missing(state):return
    try:
        run_bd(root,name,['merge-slot','create','--json'])
    except subprocess.CalledProcessError as refusal:
        if 'exist' not in (refusal.stderr or '').lower():raise

def service(root,action):
    cfg=config(root)
    return checked(['systemctl','--user',action,cfg['unit']]).stdout

def install(root,port,unit):
    if not re.fullmatch(r'beads-[a-z0-9-]+\.service',unit): raise ValueError('Unit must be beads-NAME.service')
    if not 1024<=port<=65535: raise ValueError('Use an unprivileged port')
    marker=root/'deployment.private.json'
    if marker.exists():
        cfg=config(root)
        if (cfg['port'],cfg['unit'])!=(port,unit): raise ValueError('Existing deployment has different settings')
        install_binaries(root)
        print('Existing deployment preserved; checking authenticated connection')
        sql(root,'SELECT 1;')
        return
    with socket.socket() as s: s.bind(('127.0.0.1',port))
    root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
    unit_path=Path.home()/'.config/systemd/user'/unit
    if unit_path.exists(): raise ValueError('Refusing to replace an existing service unit')
    install_binaries(root)
    for name in ('data','projects','backups','config','dolt-home','home'):(root/name).mkdir(exist_ok=True)
    cfg={'port':port,'unit':unit,'password':secrets.token_hex(24),'schema':1}
    fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(cfg,f)
    env=environment(root)
    checked([root/'bin/dolt','config','--global','--add','metrics.disabled','true'],env=env)
    checked([root/'bin/dolt','config','--global','--add','user.name','Beads team service'],env=env)
    checked([root/'bin/dolt','config','--global','--add','user.email','beads@localhost'],env=env)
    checked([root/'bin/bd','metrics','off'],env=env)
    server={'log_level':'warning','behavior':{'autocommit':True,'auto_gc_behavior':{'enable':False}},
            'listener':{'host':'127.0.0.1','port':port,'socket':str(root/'mysql.sock')},
            'data_dir':str(root/'data'),'cfg_dir':str(root/'data/.doltcfg'),'metrics':{'port':-1}}
    (root/'server.json').write_text(json.dumps(server,indent=2)+'\n')
    unit_path.parent.mkdir(parents=True,exist_ok=True)
    unit_path.write_text(f'''# Managed by beads-team-kit; deployment {root}
[Unit]
Description=Beads team coordination database
After=network.target

[Service]
Type=simple
WorkingDirectory={root}
Environment=DOLT_ROOT_PATH={root}/dolt-home
ExecStart={root}/bin/dolt sql-server --config {root}/server.json
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
''')
    checked(['systemctl','--user','daemon-reload'])
    checked(['systemctl','--user','enable','--now',unit])
    try:
        for i in range(30):
            try:
                users=sql(root,'SELECT User,Host FROM mysql.user;',password='')
                break
            except subprocess.CalledProcessError: time.sleep(.5)
        else: raise RuntimeError('Server did not become ready')
        rows=list(csv.DictReader(io.StringIO(users)))
        roots=[r for r in rows if r.get('User')=='root']
        if not roots: raise RuntimeError('No initial root account found')
        changes=[]
        for r in roots:
            host=r['Host'].replace("'","''")
            changes.append(f"ALTER USER 'root'@'{host}' IDENTIFIED BY '{cfg['password']}';")
        sql(root,'\n'.join(changes),password='')
        sql(root,'SELECT 1;')
    except Exception:
        service(root,'stop')
        raise RuntimeError('Initialization failed; service stopped. Preserve runtime and inspect journal; do not overwrite the deployment.') from None
    print(f'Installed {unit}, authenticated loopback port {port}')

def install_current_link(path):
    """The ``current`` link that exposes ``path``, or None when this install has none.

    A release is a folder under a folder literally named ``releases`` whose sibling
    ``current`` link resolves to it. Both the path itself and every parent are tested:
    with the kit at a release root (``X/releases/R1`` plus ``X/current -> releases/R1``)
    the path IS the release directory, and a scan of ``Path.parents`` alone never tests
    it, so add-project and authorized-keys would disagree on that layout
    (kittrial-5bb.182 items 1 and 3). Only a folder named ``releases`` counts, so a
    folder that merely has a sibling ``current`` link is never rewritten.
    """
    resolved=Path(os.path.realpath(str(path)))
    for release in (resolved,*resolved.parents):
        install=release.parent
        if install.name!='releases':continue
        link=install.parent/'current'
        if link.is_symlink() and Path(os.path.realpath(str(link)))==release:
            return link
    return None

def install_current_path(path):
    """``path`` as this installation's ``current`` link exposes it, or unchanged.

    An office installation keeps every release under ``releases/<ID>`` and points
    ``current`` at the one in use. ``__file__`` and ``sys.executable`` resolve that
    link, so a client config printed from them pins the worker to the release that
    printed it: after an upgrade the old ``releases/<ID>`` folder remains, and
    ``endpoint.py`` run from it is the OLD kit against the new runtime
    (kittrial-5bb.182). Print the ``current`` spelling whenever the path lies inside
    a release this installation's ``current`` link names, so an upgrade moves the
    printed line with the service. A path that is not such a release (a plain
    checkout, ``/usr/bin/python3``) comes back exactly as it was given.
    """
    link=install_current_link(path)
    if link is None:return Path(str(path))
    resolved=Path(os.path.realpath(str(path)))
    return link/resolved.relative_to(Path(os.path.realpath(str(link))))

def office_bundled_python():
    """The bundled interpreter of the office installation this kit belongs to, or None.

    The release manifest names the interpreter's path inside ``python-runtime``
    (``office_release`` writes it at build and verifies it at install). Returning it
    through :func:`install_current_path` means a printed client config or forced
    command follows an upgrade instead of naming the release that printed it. None
    outside an office installation, where there is no manifest and no bundled
    interpreter. Read through ``record_json.loads``: a manifest made unreadable or
    nested too deeply is a reason to fall back, not a traceback from add-project.
    """
    release=Path(os.path.realpath(__file__)).parent.parent
    manifest=release/'manifest.json'
    if not manifest.is_file():return None
    try:
        document=record_json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError):
        return None
    relative=document.get('python_executable') if isinstance(document,dict) else None
    if not isinstance(relative,str) or not relative:return None
    inside=Path(relative)
    if inside.is_absolute() or '..' in inside.parts:return None
    executable=release/'python-runtime'/inside
    if not executable.is_file():return None
    return install_current_path(executable)

def worker_client_setup(root,name):
    """Exact worker client configuration and bootstrap command for one project.

    The endpoint is this kit's generic ``endpoint.py`` (the file beside this
    module), which serves every project of the deployment. The host is a
    placeholder because the kit is public and each worker supplies its own SSH
    alias; never point a new project at a project-specific wrapper endpoint.

    The endpoint and the interpreter are printed through the installation's
    ``current`` link where it has one, and ``python`` names the interpreter that
    runs the endpoint on the server: a bare ``python3`` is platform-python 3.6 on
    RHEL 8, which cannot run the endpoint, and a host with no python3 on PATH fails
    outright (kittrial-5bb.182). A second example uses the local transport, for an
    agent that runs on the server itself.

    The sentence that introduces each example says what was ACTUALLY printed: on an
    installation with no ``current`` link (the live kits keep
    ``<base>/kit -> <base>/releases/<ID>``) the printed paths name the release, so the
    text says they must be printed again after an upgrade instead of claiming an
    upgrade moves them (kittrial-5bb.182 item 1).
    """
    source=Path(__file__).resolve().with_name('endpoint.py')
    endpoint=install_current_path(source)
    python=str(office_bundled_python() or default_authorized_key_python())
    config=json.dumps({'host':'WORKER_SSH_HOST','endpoint':str(endpoint),'root':str(root),
                       'python':python},indent=2)
    local=json.dumps({'transport':'local','python':python,'endpoint':str(endpoint),
                      'root':str(root)},indent=2)
    if install_current_link(source) is not None:
        endpoint_note=('The endpoint and the interpreter go through install/current, so an upgrade '
                       'moves them with the service')
        local_note='the same install/current paths, so it also follows an upgrade'
    else:
        endpoint_note=('This installation has no install/current link, so the endpoint and the '
                       'interpreter below name the release that printed them and must be printed '
                       'again after an upgrade')
        local_note=('the same paths, which name this release, so print them again after an upgrade')
    # The server's own host name is what an operator types first, and it need not resolve from
    # the worker's network (kittrial-5bb.191: the first use of this route from another machine).
    host_note=('WORKER_SSH_HOST must lead to a name or address the worker\'s machine can reach; this\n'
               'server\'s own host name may not resolve from the worker\'s network.')
    return (f'Worker client configuration for {name} (save as client.local.json in the worker\'s own\n'
            f'directory and replace WORKER_SSH_HOST with that worker\'s SSH alias; this kit endpoint serves\n'
            f'every project, so do not point it at a project-specific wrapper). {endpoint_note}:\n{config}\n'
            f'{host_note}\n'
            f'Bootstrap command (replace ACTOR with the actor returned by worker.py start or session\n'
            f'register):\n  python client.py --config client.local.json --project {name} --actor ACTOR -- onboard\n'
            f'An agent that runs on the server itself uses the local transport instead (no SSH and no\n'
            f'host; {local_note}):\n{local}\n'
            f'Host project not on the web yet: nothing of {name} appears in the web interface until a\n'
            f'superuser registers it there (New project, with this name, or POST /v1/projects without\n'
            f'"create").')

#: Set by ``environment`` to the account's home when it scopes HOME into the runtime.
ACCOUNT_HOME_ENV='ORCHESTRA_ACCOUNT_HOME'

def scheduled_backup_unit_dir():
    """The user systemd unit directory an operator installs the schedule into."""
    return Path.home()/'.config/systemd/user'

def account_unit_dir(root):
    """The account's unit directory, also for a process under the runtime's scoped home.

    From a shell this is ``scheduled_backup_unit_dir()`` and the environment variable is
    not looked at. Only when HOME is the runtime's own ``<root>/home`` (the web service
    and the endpoint it starts run that way, and that home holds no units) is the
    account's home taken from ``ORCHESTRA_ACCOUNT_HOME``, which ``environment`` set.
    """
    scoped=bool(os.environ.get('HOME')) and Path(os.environ['HOME'])==Path(root)/'home'
    if not scoped:return scheduled_backup_unit_dir()
    home=os.environ.get(ACCOUNT_HOME_ENV)
    # Under the scoped home with no record of the account's own: the units cannot be
    # found from here, and the scoped home never holds any. None, not a wrong directory.
    return Path(home)/'.config/systemd/user' if home else None

def scheduled_backup_unit_paths():
    """Every installed scheduled-backup candidate unit, sorted by path.

    ``beads-backup.service`` (the template's name) is only ONE candidate: a deployment
    may install the schedule under any ``beads-*backup*.service`` name, so every
    matching unit file is read instead of assuming the historical one. Only the unit
    files themselves are inspected; systemd drop-ins (``*.service.d/*.conf``) can
    override them and are NOT read, which the coverage report states plainly.
    """
    directory=scheduled_backup_unit_dir()
    try:
        return sorted(path for path in directory.glob('beads-*backup*.service') if path.is_file())
    except OSError:
        return []

def scheduled_backup_execstart(root):
    """The exact ``ExecStart`` line that covers every project of this runtime.

    The interpreter and this module are both printed through the installation's
    ``install/current`` link where it has one, so an upgrade moves the schedule with the
    service. A line whose interpreter came through ``current`` but whose ``admin.py``
    named ``releases/<ID>`` kept running the release that printed it after the next
    upgrade (kittrial-5bb.182 item 2).
    """
    python=install_current_path(sys.executable)
    module=install_current_path(Path(__file__).resolve())
    return (f'ExecStart={python} {module} '
            f'--root {root} backup --all')

def host_command(root,*words):
    """A host command as the service user pastes it into a shell: interpreter, this module,
    ``--root``, then ``words``; every word quoted for a shell.

    ``admin.py`` alone is not a command: it is not on PATH and, in a release, not executable,
    and a set-up step that showed ``admin.py set-guidance ...`` or the unit-file line
    ``ExecStart=...`` could not be pasted (kittrial-5bb.200: "Permission denied"). The
    interpreter and the module go through ``install/current`` where there is one, as the
    schedule line does.
    """
    import shlex
    python=install_current_path(sys.executable)
    module=install_current_path(Path(__file__).resolve())
    return ' '.join(shlex.quote(str(word)) for word in (python,module,'--root',root,*words))

def backup_now_command(root):
    """The command that backs up every project of this runtime now."""
    return host_command(root,'backup','--all')

def coordination_command(root,*words):
    """A host coordination command as the service user pastes it: interpreter, the installed
    kit's ``coordination.py``, then ``words``.

    ``coordination.py`` is ``admin.py``'s sibling and takes ``--config`` (the operator's own
    client configuration), not ``--root``, so it has its own beginning. The set-up page's
    merge-slot step shows one for the merge-create operation (kittrial-5bb.202 review item 4).
    """
    import shlex
    python=install_current_path(sys.executable)
    module=install_current_path(Path(__file__).resolve().with_name('coordination.py'))
    return ' '.join(shlex.quote(str(word)) for word in (python,module,*words))

def schedule_text(root):
    """The two things an operator needs for backups, each under its own label: the command
    that runs one now, and the line a schedule's unit file carries (which is NOT a command)."""
    return (f'To run a backup of every project now, as this account (a shell command):\n  {backup_now_command(root)}\n'
            f'The line for a schedule (a line of a systemd unit file, not a shell command; it goes in the '
            f'[Service] section of a beads-*backup*.service unit in {scheduled_backup_unit_dir()}, run by its '
            f'timer):\n  {scheduled_backup_execstart(root)}\n'
            f'To check afterwards: `systemctl --user list-timers`, and\n  '
            f'{host_command(root,"backup-status","--require-complete")}')

def _execstart_values(text):
    """The command of each ``ExecStart=`` line in a unit file, systemd prefix stripped.

    A oneshot service may carry several ``ExecStart`` lines (the template uses one) and
    each may start with systemd's ``-``/``+``/``!``/``:`` prefix. Comments and every
    other directive are ignored; drop-ins are not read here at all.
    """
    for raw in text.splitlines():
        line=raw.strip()
        if not line.startswith('ExecStart='):continue
        value=line[len('ExecStart='):].strip()
        while value[:1] in ('-','+','!',':'):value=value[1:].lstrip()
        if value:yield value

def _mentions_root(tokens,root):
    """True when a parsed command line names this runtime root."""
    for index,token in enumerate(tokens):
        if token=='--root' and index+1<len(tokens):candidate=tokens[index+1]
        elif token.startswith('--root='):candidate=token.split('=',1)[1]
        elif token==str(root):return True
        else:continue
        try:
            if Path(candidate).expanduser().resolve()==root:return True
        except OSError:pass
    return False

def _project_arguments(tokens):
    """The project names a command line names with ``--project``/``--project=``.

    Used for a long-sync wrapper, whose own command line is the only statement of
    which projects it backs up: the kit cannot know that a wrapper "covers every
    project" merely because it names the runtime root, so coverage is read from the
    project arguments it actually carries. A wrapper that names none covers none.
    """
    found=[]
    for index,token in enumerate(tokens):
        if token=='--project' and index+1<len(tokens):found.append(tokens[index+1])
        elif token.startswith('--project='):found.append(token.split('=',1)[1])
    return [name for name in found if name]

def scheduled_backup_unit_report(text,root):
    """Classify one unit file's ``ExecStart`` lines against one runtime root.

    ``ours`` is True when a line runs this kit's ``admin.py`` for THIS runtime, with
    ``all_line`` the durable ``backup --all`` form and ``named`` the projects a line of
    this runtime lists individually. ``wrapper`` is the command line of a recognised
    long-sync wrapper of this runtime (a line that runs a script other than
    ``admin.py`` and names this runtime) and ``wrapper_projects`` the projects those
    wrapper lines name with ``--project``; a wrapper is treated as covering only the
    projects it names, never every project. ``other_runtime`` is any line that belongs
    to a different runtime.
    """
    import shlex
    report={'all_line':False,'named':[],'ours':False,'wrapper':None,'wrapper_projects':[],
            'other_runtime':False}
    for value in _execstart_values(text):
        try:tokens=shlex.split(value)
        except ValueError:continue
        if not tokens:continue
        if any(Path(token).name=='admin.py' for token in tokens):
            if not _mentions_root(tokens,root):
                report['other_runtime']=True;continue
            report['ours']=True
            try:index=tokens.index('backup',next(i for i,token in enumerate(tokens) if Path(token).name=='admin.py'))
            except (StopIteration,ValueError):continue
            tail=tokens[index+1:]
            if '--all' in tail:report['all_line']=True
            else:report['named']+=[token for token in tail if not token.startswith('-')]
            continue
        if _mentions_root(tokens,root):
            if report['wrapper'] is None:report['wrapper']=value
            report['wrapper_projects']+=_project_arguments(tokens)
        else:
            report['other_runtime']=True
    return report

def scheduled_backup_covers(root,name):
    """Whether the INSTALLED schedule backs up one project: True, False or None (unknown).

    The same unit files and the same classification as ``scheduled_backup_coverage``,
    reduced to one answer for the project setup page (kittrial-5bb.118): True when a
    unit runs ``backup --all`` for this runtime or names this project, False when units
    were read and none does (or none is installed), None when a unit could not be read
    and nothing that was read covers the project. Drop-ins are not inspected.
    """
    # The same candidates as scheduled_backup_unit_paths(), looked up in account_unit_dir.
    directory=account_unit_dir(root)
    if directory is None:return None
    try:
        paths=sorted(path for path in directory.glob('beads-*backup*.service') if path.is_file())
    except OSError:
        return None
    unreadable=False
    for path in paths:
        try:text=path.read_text(encoding='utf-8')
        except OSError:
            unreadable=True;continue
        report=scheduled_backup_unit_report(text,root)
        if report['all_line'] or name in report['named'] or name in report['wrapper_projects']:
            return True
    return None if unreadable else False

def project_merge_slot_state(root,name,path=None):
    """The project's merge slot as the host reads it, for the setup page and the report.

    Read-only (kittrial-5bb.202 item 2). ``healthy`` (bd reports a free or held slot),
    ``missing`` (bd reports no slot for this project), ``damaged`` (the row is there but it
    is not available and names no holder) or ``unknown`` (nothing was read: the metadata
    does not record the Dolt server coordinates, where bd would create an embedded database
    and answer nothing, or bd could not run or answer). ``detail`` is a sentence for a
    person; for missing and damaged it names the merge-create operation and uses the kit's
    existing words (``coordination.merge_slot_missing``, ``SLOT_DAMAGED``), and it is None
    for a healthy or unreadable slot.
    """
    from coordination import merge_slot_damaged,merge_slot_missing,SLOT_DAMAGED
    path=project_dir(root,name) if path is None else Path(path)
    # bd is never run without the server coordinates: it would fall back to an embedded
    # database and CREATE .beads/embeddeddolt here (see project_metadata_state).
    try:
        metadata=json.loads((path/'.beads'/'metadata.json').read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError):
        metadata=None
    keys=('dolt_server_host','dolt_server_port','dolt_server_user','dolt_database')
    if not isinstance(metadata,dict) or not all(metadata.get(key) for key in keys):
        return {'state':'unknown','detail':None}
    try:
        state=json.loads(run_bd(root,name,['merge-slot','check','--json']) or 'null')
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,TypeError,RecursionError):
        return {'state':'unknown','detail':None}
    if merge_slot_missing(state):
        return {'state':'missing','detail':'Merge slot does not exist for this project; run the merge-create '
                                           'operation to create it.'}
    if merge_slot_damaged(state):
        return {'state':'damaged','detail':SLOT_DAMAGED}
    return {'state':'healthy','detail':None}

def project_setup_status(root,name,path=None):
    """What the host knows about one project's setup, for the web setup page.

    Read-only, and no more than its caller may already read (kittrial-5bb.118): guidance
    and onboarding as set or not set with a version or a time, never their text; backup
    as covered, not covered or unknown, the line an operator would install, and how the
    last backup run recorded the project. Each part is answered on its own: one that
    cannot be read says ``unknown`` and never fails the others.
    """
    from datetime import datetime, timezone
    path=project_dir(root,name) if path is None else Path(path)
    result={'schema_version':1,'project':name}
    try:
        from guidance import state as guidance_state
        block=guidance_state(path)
        result['guidance']={'state':('unreadable' if block.get('unreadable') else 'unbound' if block.get('unbound')
                                     else 'set' if block.get('present') else 'not-set'),
                            'version':block.get('version'),'set_at':block.get('set_at')}
    except (OSError,ValueError):
        result['guidance']={'state':'unknown','version':None,'set_at':None}
    try:
        entry=path/'ONBOARDING.md'
        if entry.is_symlink():raise ValueError('symlink')
        present=entry.is_file() and bool(entry.read_text(encoding='utf-8').strip())
        result['onboarding']={'state':'set' if present else 'not-set',
                              'updated_at':(datetime.fromtimestamp(entry.stat().st_mtime,timezone.utc)
                                            .strftime('%Y-%m-%dT%H:%M:%SZ') if present else None)}
    except (OSError,ValueError,UnicodeError):
        result['onboarding']={'state':'unknown','updated_at':None}
    try:
        covers=scheduled_backup_covers(root,name)
    except OSError:
        covers=None
    backup={'scheduled':'covered' if covers else 'unknown' if covers is None else 'not-covered',
            'line':scheduled_backup_execstart(root),'last_run':None,
            # What can be pasted into a shell, beside the unit-file line that cannot (kittrial-5bb.200).
            'run_now':backup_now_command(root),
            'check':host_command(root,'backup-status','--require-complete')}
    try:
        directory=account_unit_dir(root)
        backup['unit_directory']=None if directory is None else str(directory)
    except OSError:
        backup['unit_directory']=None
    # The words every host command of this runtime begins with, for the steps an operator
    # does on the server.
    result['admin']=host_command(root)
    # The same for a coordination command (``coordination.py``, which takes ``--config``):
    # the merge-slot step's merge-create command begins this way (kittrial-5bb.202 rev-2).
    result['coordination']=coordination_command(root)
    # Why the schedule could not be checked, when it could not: this process runs under
    # the runtime's scoped home and was not told the account's own home (the service was
    # started without a usable HOME), or a unit file could not be read.
    if covers is None:
        backup['reason']='no-account-home' if account_unit_dir(root) is None else 'unreadable'
    try:
        record=read_backup_status(root)
        for item in record['projects']:
            if item['name']==name:
                backup['last_run']={'status':item['status'],'completed_at':item.get('completed_at'),
                                    'degraded':bool(item.get('degraded')),'scope':record.get('scope')}
    except (ValueError,OSError):
        pass
    result['backup']=backup
    # The project's merge slot, so the setup page can name a missing or damaged one as a
    # step (kittrial-5bb.202 item 2). Unknown when bd cannot say; never a write.
    result['merge_slot']=project_merge_slot_state(root,name,path)
    # How full the server is (kittrial-5bb.118 part 2 revision): every project database on it
    # counts, and every bd write gets slower as they grow. The web service shows it to a
    # superuser only.
    try:
        import project_creation
        result['project_databases']=project_creation.server_usage(root)
    except (ValueError,OSError):
        result['project_databases']=None
    # Why the web service may NOT register this project, or None (kittrial-5bb.149). The
    # endpoint serves a project whose creation record is damaged, so the register route no
    # longer learns of one from a refused read: it asks here. A sentence, never a path.
    try:
        import project_creation
        result['creation_record']=project_creation.registrable(root,name)
    except (ValueError,OSError):
        result['creation_record']='The creation record of project %s could not be checked; an operator must look at it first'%name
    return result

def scheduled_backup_coverage(root,name):
    """(durably_covers_every_project, message) for the INSTALLED schedule.

    ``True`` only for a schedule that durably covers every project of this runtime: an
    ``admin.py ... backup --all`` unit. A unit that names projects individually —
    whether through ``admin.py ... backup PROJECT`` or through a long-sync wrapper's
    ``--project`` arguments — is reported as covering exactly those projects and no
    others, because the next project added would need another edit; a wrapper is never
    reported as something to replace with ``backup --all``, and a wrapper that names no
    project is not full coverage either.

    Every ``beads-*backup*.service`` unit installed for the account is read, not only
    the historical ``beads-backup.service``, and the report names the units it read and
    says that systemd drop-ins (``*.service.d/*.conf``) are not inspected. ``--all`` is
    never suggested next to named project lines (naming projects and ``--all`` in one
    command is refused). It is a report, not a gate: this never edits, installs or
    enables a unit.
    """
    directory=scheduled_backup_unit_dir()
    line=scheduled_backup_execstart(root)
    both=schedule_text(root)
    dropins=(f'Systemd drop-ins ({directory}/*.service.d/*.conf) are not inspected, so this reports the unit '
             f'files themselves.')
    paths=scheduled_backup_unit_paths()
    if not paths:
        return False,(f'No installed scheduled backup unit matching beads-*backup*.service was found in '
                      f'{directory}, so no project of this runtime is on a schedule. A schedule that covers '
                      f'every project, including {name}, needs the line below.\n{both}')
    read=[];unreadable=[];durable=[];wrappers=[];named={};wrapper_projects={};foreign=[]
    for path in paths:
        try:text=path.read_text(encoding='utf-8')
        except OSError as error:
            unreadable.append('%s (%s)'%(path,error));continue
        read.append(str(path))
        report=scheduled_backup_unit_report(text,root)
        if report['all_line']:durable.append(str(path))
        if report['wrapper']:
            wrappers.append(str(path))
            wrapper_projects[str(path)]=sorted(set(report['wrapper_projects']))
        if report['ours'] and not report['all_line']:
            named[str(path)]=sorted(set(report['named']))
        if report['other_runtime']:foreign.append(str(path))
    if durable:
        note=(' Read: '+', '.join(read)+'.') if read else ''
        if unreadable:note+=' Not readable: '+'; '.join(unreadable)+'.'
        return True,(f'The installed scheduled backup unit(s) read for this runtime cover every project, '
                      f'including {name}, so no change is needed (durable backup --all: '+', '.join(durable)+
                      ').'+note+' '+dropins)
    covered=sorted({item for names in named.values() for item in names} |
                   {item for names in wrapper_projects.values() for item in names})
    individual=list(named)+list(wrapper_projects)
    if individual:
        note=' Read: '+', '.join(read)+'.'
        if foreign:note+=' Units for another runtime were also read and are not changed: '+', '.join(foreign)+'.'
        if unreadable:note+=' Not readable: '+'; '.join(unreadable)+'.'
        units=', '.join(individual)
        projectless=any(not projects for projects in wrapper_projects.values())
        if name in covered:
            message=(f'The installed scheduled backup unit(s) read for this runtime already include {name} '
                     f'(projects: {", ".join(covered)}) at {units}, but they name projects individually, so the '
                     f'next project added needs the same edit.')
            if wrappers:
                message+=(f' The long-sync wrapper is the timeout-safe path and is not replaced; add a line naming '
                          f'the new project to the same wrapper schedule ({", ".join(wrappers)}). Do not combine '
                          f'named projects with --all in one command.')
            else:
                message+=(f' Replace that project list with the durable form (do not combine named projects with '
                          f'--all in one command); it is a line of the unit file, not a shell command:\n  {line}')
            return False,message+note+' '+dropins
        message=(f'The installed scheduled backup unit(s) read for this runtime cover only '
                 f'{", ".join(covered) if covered else "no project"}, so they do not include {name}.')
        if wrappers:
            if projectless:
                message+=(f' The long-sync wrapper names no project, so it is not coverage of {name}; add a line '
                          f'naming each project, including {name}, to the same wrapper schedule '
                          f'({", ".join(wrappers)}). Do not combine named projects with --all in one command.')
            else:
                message+=(f' The long-sync wrapper is the timeout-safe path and is not replaced; add a line naming '
                          f'{name} to the same wrapper schedule ({", ".join(wrappers)}). Do not combine named '
                          f'projects with --all in one command.')
        else:
            message+=(f' Add {name} there, or replace the project list with the durable form (do not combine named '
                      f'projects with --all in one command); it is a line of the unit file, not a shell command:\n  {line}')
        return False,message+note+' '+dropins
    details=[]
    if foreign:details.append('these units do not back up this runtime: '+', '.join(foreign))
    if unreadable:details.append('could not be read: '+'; '.join(unreadable))
    if not read:
        return False,(f'The installed scheduled backup unit(s) found in {directory} could not be read ('
                      +'; '.join(unreadable)+f'), so schedule coverage of {name} cannot be confirmed. Use a '
                      f'schedule that covers every project.\n{both}')
    return False,(f'The installed scheduled backup unit(s) read ('+', '.join(read)+f') do not cover {name}'
                  +(' ('+'; '.join(details)+')' if details else '')
                  +f'. A schedule that covers every project needs the line below. '+dropins+f'\n{both}')

#: Where ``retire-project`` moves a project directory, and its append-only journal.
RETIRED_DIR='retired'
RETIRE_JOURNAL='journal.jsonl'
#: The receipt journals whose ``pending`` entries are reservations still in flight.
RESERVATION_JOURNALS=('.coordination-requests','.requirement-requests','.handoff-requests',
                      '.reference-requests','.proposal-requests','.capability-requests',
                      '.open-item-requests','.requirement-owner-requests')

def retired_entries(root):
    """``[(project name, entry directory name)]`` for every retired project, sorted.

    An entry is ``retired/<name>-<UTC stamp>``, exactly what ``retire-project`` creates;
    anything else in that directory is ignored. Read-only.
    """
    base=root/RETIRED_DIR
    if base.is_symlink() or not base.is_dir():return []
    found=[]
    for path in sorted(base.iterdir(),key=lambda item:item.name):
        match=re.fullmatch(r'([a-z][a-z0-9]{1,23})-[0-9]{8}T[0-9]{6}Z',path.name)
        if match and path.is_dir() and not path.is_symlink():found.append((match.group(1),path.name))
    return found

def refuse_retired_name(root,name):
    """A retired name is never reused: its Dolt database is still on the server, so a new
    project of that name would silently adopt it."""
    if (root/RETIRED_DIR).is_symlink():
        # retired_entries sees nothing through a symlink, so no name could be checked.
        raise ValueError('The %s directory is a symlink, so retired project names cannot be checked; replace '
                         'it with a real directory before adding or restoring a project.'%RETIRED_DIR)
    held=[entry for project,entry in retired_entries(root) if project==name]
    if held:
        raise ValueError('Project name %s is retired (%s/%s): its database is still on the server, so the '
                         'name is not reused. Choose another name.'%(name,RETIRED_DIR,held[-1]))

def restore_lock_path(root,name):
    """The lock ``restore-new`` holds for its DESTINATION for its whole run.

    A separate file from ``backups/NAME.lock``: restore-new's own ``add-project`` step
    takes that one for the first backup, and a second flock of it from the same process
    would block for ever. ``retire-project`` tests this lock without waiting.
    """
    validate_name(name)
    return root/'backups'/(name+'.restore.lock')

#: Ceiling for the ``SELECT 1`` probe ``retire-project`` sends the Dolt server. Retire holds
#: the restore, backup and coordination locks while it probes, so a frozen server must not
#: hold them forever; no answer in this time is "could not be checked".
RETIRE_PROBE_TIMEOUT=15

def retire_findings(root,name):
    """What ``retire-project`` checks before it moves a project. Never raises.

    The rule is fail-closed (kittrial-5bb.85 review 01a10219): what cannot be read is
    treated as the dangerous answer.

    * ``metadata`` is ``project_metadata_state``'s answer. bd runs only for ``server``
      (review 01a1026a): with ``unreadable`` nothing is known and bd would create an
      embedded database inside the project, so ``bd`` is ``unreachable``; with ``absent``
      the project was never initialized and ``bd`` is ``uninitialized``.
    * ``bd`` is ``reads`` (bd lists the project; ``issues`` counts what it holds besides
      its merge slot), ``rejects`` (the server answers and bd refuses the project, which
      is what a stopped restore leaves) or ``unreachable`` (the Dolt server, or bd
      itself, could not be reached or did not answer within ``RETIRE_PROBE_TIMEOUT``, so
      nothing is known: the project may be a healthy tracker).
    * ``merge_slot`` is ``held``, ``free``, ``missing``, ``unreadable`` (bd reads the
      project but not its slot, or nothing could be reached: treated as held) or
      ``not-applicable`` (bd rejects the project, so nothing holds a slot through it).
    * ``pending_reservations`` counts pending receipts per request journal, and
      ``unreadable_reservations`` the receipts that could not be read (treated as pending).
    * ``backup_pair`` and ``last_backup_run`` are reported for the journal; they no longer
      decide anything, because a healthy project whose last backup failed is still a
      healthy project.
    """
    path=project_dir(root,name)
    complete,reason=backup_pair_state(root,name)
    recorded=None
    try:
        recorded=next((entry['status'] for entry in read_backup_status(root)['projects']
                       if entry['name']==name),None)
    except (OSError,ValueError,KeyError,TypeError):pass
    metadata=project_metadata_state(root,name)
    server='not-used'
    if metadata=='server':
        try:
            sql(root,'SELECT 1;',timeout=RETIRE_PROBE_TIMEOUT);server='up'
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,KeyError,TypeError):
            server='unreachable'
    bd='unreachable';issues=None;holder=None;slot='unreadable'
    if metadata=='absent':
        bd='uninitialized';slot='not-applicable'
    elif server=='up':
        try:
            rows=json.loads(run_bd(root,name,['list','--all','--limit','0','--json']) or '[]')
            if not isinstance(rows,list):raise ValueError('unexpected list output')
            bd='reads';issues=sum(1 for row in rows if not (isinstance(row,dict) and row.get('id')==name+'-merge-slot'))
        except (subprocess.CalledProcessError,ValueError,TypeError,RecursionError):
            bd='rejects';slot='not-applicable'
        except (subprocess.TimeoutExpired,OSError):
            bd='unreachable'
    if bd=='reads':
        try:
            state=json.loads(run_bd(root,name,['merge-slot','check','--json']) or 'null')
            if isinstance(state,dict) and 'available' in state and not state.get('error'):
                holder=state.get('holder');slot='held' if holder else 'free'
            elif isinstance(state,dict):slot='missing'
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,TypeError,RecursionError):pass
    pending={};unreadable={}
    for journal in RESERVATION_JOURNALS:
        directory=path/journal
        if not directory.exists() and not directory.is_symlink():continue
        if directory.is_symlink() or not directory.is_dir():
            unreadable[journal]=unreadable.get(journal,0)+1;continue
        for receipt in directory.glob('*.json'):
            try:
                status=json.loads(receipt.read_text(encoding='utf-8'))['status']
                if not isinstance(status,str):raise ValueError('status')
                if status=='pending':pending[journal]=pending.get(journal,0)+1
            except (OSError,ValueError,KeyError,TypeError):
                unreadable[journal]=unreadable.get(journal,0)+1
    try:initialized=(path/'.beads'/'metadata.json').is_file()
    except OSError:initialized=False   # .beads itself cannot be entered: `metadata` says unreadable
    return {'initialized':initialized,'metadata':metadata,'server':server,'bd':bd,
            'issues':issues,
            'backup_pair':'complete' if complete else reason,'last_backup_run':recorded,
            'merge_slot':slot,'merge_slot_holder':holder,'pending_reservations':pending,
            'unreadable_reservations':unreadable}

def retire_blockers(findings):
    """Why a retire needs ``--force``, in the operator's words; empty when it does not."""
    counts=lambda found:', '.join('%d in %s'%(count,journal) for journal,count in sorted(found.items()))
    blockers=[]
    if findings.get('metadata')=='unreadable':
        blockers.append('its .beads/metadata.json is there but could not be read (or does not record the Dolt '
                        'server), so the project could not be checked: it may be a healthy tracker')
    elif findings['bd']=='unreachable':
        blockers.append('the Dolt server (or bd) could not be reached, so the project could not be checked: it '
                        'may be a healthy tracker')
    elif findings['bd']=='reads' and findings['issues']:
        blockers.append('bd reads it and it holds %d issue(s), so it looks like a working tracker'%findings['issues'])
    if findings['merge_slot']=='held':
        blockers.append('its merge slot is held by %s'%findings['merge_slot_holder'])
    elif findings['merge_slot']=='unreadable':
        blockers.append('its merge slot could not be read, so it is treated as held')
    if findings['pending_reservations']:
        blockers.append('it has pending reservations (%s)'%counts(findings['pending_reservations']))
    if findings['unreadable_reservations']:
        blockers.append('its reservations could not all be read, so they are treated as pending (%s)'
                        %counts(findings['unreadable_reservations']))
    return blockers

def retire_project(root,name,actor,reason,force=False,creation_locked=False):
    """Retire one project: move its directory aside. Nothing is deleted.

    For a project a stopped ``restore-new`` left behind, or a drill project. One rename
    moves ``projects/NAME`` to ``retired/NAME-<UTC stamp>``, so the project is no longer
    initialized: the backup gate stops counting it and the endpoint answers
    "Unknown/uninitialized project". ``backups/`` is never touched (this project's or any
    other's) and the Dolt database is not dropped; moving the directory back undoes it.
    The name stays reserved (``refuse_retired_name``).

    Refused, naming what was found, unless ``force`` (``retire_blockers``): bd reads the
    project and it holds issues; the Dolt server or bd could not be reached; its merge
    slot is held or could not be read; it has pending reservations or receipts that could
    not be read. Refused even with ``force`` while a ``restore-new`` into the name is
    running. The operator allowlist is checked first, strictly. Both steps are journaled in
    ``retired/journal.jsonl`` (intent before the move, the result after it).

    It holds the project creation lock (kittrial-5bb.118 part 2, review 01a109cc), without
    waiting: retiring under a creation in flight pulled the directory away from it, and the
    creation then deleted the record this had just marked. ``creation_locked`` is for
    ``remove-creation``, which holds that lock already.
    """
    import fcntl
    from keyed_records import require_configured_operator
    require_configured_operator(actor,operators(root,strict=True),'retire a project')
    import project_creation
    # Where no creation was ever started there is no lock to take, and a refusal must leave
    # nothing behind, not even the lock file.
    if not creation_locked and project_creation.records_dir(root).is_dir():
        try:
            with project_creation.creation_lock(root,wait=0):
                return retire_project(root,name,actor,reason,force=force,creation_locked=True)
        except project_creation.Busy:
            raise ValueError('Refusing to retire %s: a project is being created on this server (%s). Wait for it '
                             'to finish, then retry. Nothing was changed.'
                             %(name,project_creation.running_name(root) or 'unknown')) from None
    path=project_dir(root,name)
    if not path.is_dir():raise ValueError('Unknown project: there is no projects/%s'%name)
    if not isinstance(reason,str) or not reason.strip():raise ValueError('A reason is required')
    if (root/RETIRED_DIR).is_symlink():raise ValueError('The retired directory must not be a symlink')
    (root/'backups').mkdir(exist_ok=True)
    # A restore-new into this name holds this lock for its whole run. It is tested without
    # waiting and it is NOT overridden by --force: retiring under a running restore pulls
    # the destination away from it.
    with restore_lock_path(root,name).open('a') as restoring:
        try:
            fcntl.flock(restoring,fcntl.LOCK_EX|getattr(fcntl,'LOCK_NB',4))
        except (BlockingIOError,PermissionError):
            raise ValueError('Refusing to retire %s: a restore-new into it is running. Wait for it to finish (or '
                             'stop it), then retry. Nothing was changed.'%name) from None
        with backup_lock(root,name):
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                findings=retire_findings(root,name)
                blockers=retire_blockers(findings)
                if blockers and not force:
                    raise ValueError('Refusing to retire %s: %s. Nothing was changed. Resolve that first, or pass '
                                     '--force to retire it anyway.'%(name,'; '.join(blockers)))
                entry='%s-%s'%(name,time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
                destination=root/RETIRED_DIR/entry
                (root/RETIRED_DIR).mkdir(mode=0o700,exist_ok=True)
                if destination.exists():raise ValueError('A retired entry %s already exists; retry in a second'%entry)
                record={'project':name,'actor':actor,'reason':reason.strip(),'forced':bool(force),
                        'overrode':blockers,'findings':findings,'destination':'%s/%s'%(RETIRED_DIR,entry)}
                append_retire_journal(root,dict(record,event='intent',at=utc_stamp()))
                try:
                    os.rename(path,destination)
                except OSError as error:
                    # The intent line must not stand alone: record that nothing moved.
                    append_retire_journal(root,dict(record,event='failed',at=utc_stamp(),
                                                    error=error.__class__.__name__+': '+str(error.strerror or error)))
                    raise ValueError('Could not move projects/%s to %s/%s (%s). Nothing was changed; %s must be a real '
                                     'directory on the same filesystem as projects/.'
                                     %(name,RETIRED_DIR,entry,error.strerror or error.__class__.__name__,RETIRED_DIR)) from None
            append_retire_journal(root,dict(record,event='retired',at=utc_stamp()))
    # A web-started creation that stopped half way and is retired here holds its
    # creator's place no longer (kittrial-5bb.118 part 2). The name stays retired.
    import project_creation
    try:
        if project_creation.mark_removed(root,name,actor):record['creation']='removed'
    except (ValueError,OSError):
        pass
    return record

def append_retire_journal(root,record):
    """Append one line to ``retired/journal.jsonl`` (0600), flushed to disk."""
    path=root/RETIRED_DIR/RETIRE_JOURNAL
    if path.is_symlink():raise ValueError('The retire journal must not be a symlink')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a',encoding='utf-8') as handle:
        handle.write(json.dumps(record,sort_keys=True,ensure_ascii=False)+'\n')
        handle.flush();os.fsync(handle.fileno())

PROJECT_SETTINGS=[('no-git-ops','true'),('dolt.auto-push','false'),('dolt.auto-commit','on'),('backup.git-push','false')]

#: What ``bd init`` runs while it makes a project. ``bd init`` creates the project's
#: git repository, so a host without git fails part way through and used to leave a
#: half-made ``projects/NAME`` that the next ``add-project`` reads as "already exists"
#: and that ``backup --all`` cannot see. The list is deliberately small: each entry is
#: a program the kit has watched a plain install need, and the check runs before
#: anything is created.
BD_INIT_TOOLS=('git',)

def missing_bd_init_tools(root):
    """The programs ``bd init`` needs that are not on the runtime's PATH, in order.

    The PATH is composed as ``environment`` composes it - the runtime's own ``bin``
    first, then the caller's PATH - without reading the runtime's private config, so
    the check works before a runtime exists.
    """
    from shutil import which
    path=str(Path(root)/'bin')+os.pathsep+os.environ.get('PATH','')
    return [name for name in BD_INIT_TOOLS if which(name,path=path) is None]

def require_bd_init_tools(root):
    """Refuse, before anything is created, when a program ``bd init`` needs is missing.

    The sentence names what is missing so an operator can install it; the caller has
    not created the project directory yet, so a refusal leaves nothing behind.
    """
    missing=missing_bd_init_tools(root)
    if missing:
        names=' and '.join(missing)
        raise ValueError('This host has no %s on PATH, which bd init needs to create a project. '
                         'Install %s and run this command again; nothing was created.'%(names,names))

def require_creatable_project(root,name):
    """The checks a creation needs before anything is made; returns the project directory.

    ``add-project`` runs this before it writes its creation record, and
    ``initialize_project`` runs it again: every refusal here must leave no directory, no
    database and no record, so the operator's route never has to take a record back
    (kittrial-5bb.176). The order is the order ``add-project`` has always used.
    """
    import project_creation
    path=project_dir(root,name)
    if name in project_creation.RESERVED_NAMES:
        raise ValueError('Project name %s is used by the database server itself: choose another name'%name)
    refuse_retired_name(root,name)
    if path.exists() and any(path.iterdir()): raise ValueError('Project already exists; use it rather than initializing again')
    require_bd_init_tools(root)
    return path

def initialize_requirements_governance(root,name):
    """Only a durable, unfinished creation may install the new-project default."""
    import project_creation
    import requirement_governance
    record=project_creation.read_record(root,name)
    if record is None or record['state'] not in ('started','incomplete'):
        raise ValueError('Requirements governance initialization needs an unfinished project creation')
    if record.get('requirements_governance') != 'simple':
        return requirement_governance.current(project_dir(root,name),name)
    # Host creations predate operation_id; their durable start identity is stable
    # across finish-project, unlike the mutable stage and last-error fields.
    operation=record.get('operation_id') or content_hash({
        'project':name,'by':record['by'],'started_at':record['started_at']})
    return requirement_governance.initialize(project_dir(root,name),name,record['by'],operation)

def initialize_project(root,name,stage=None):
    """The work of ``add-project``: database, settings, backup target, merge slot, first backup.

    ``add-project`` calls this and then prints its advice. The web service's project
    creation calls it through ``project_creation.create`` (kittrial-5bb.118 part 2), which
    passes ``stage`` to record which step was running if the work stops.
    """
    at=stage or (lambda label:None)
    path=require_creatable_project(root,name)
    # Deliberately NO clean-up of a failed creation here. An earlier revision removed
    # ``projects/NAME`` whenever ``.beads/metadata.json`` was not there yet, so that a
    # failed creation would "leave nothing". The review (kittrial-5bb.162 items
    # cleanup-deletes-concurrent-creation and cleanup-hides-half-made-database) showed
    # that was worse than main in two ways: it deleted a directory a concurrent
    # ``add-project NAME`` was still filling (both calls then failed and the database
    # stayed on the server), and when ``bd init`` was killed part way it hid a
    # half-made database - the directory went while the database stayed, so the web
    # route answered "nothing was made" and every retry then failed on the half-made
    # database. A failure here now leaves the directory exactly as the failure left it,
    # which is how main behaves and what docs/HTTP_DEPLOYMENT.md describes.
    # ``project_creation.work`` is the web path's own, older clean-up and is unchanged:
    # under the creation lock it ``rmdir``s only a genuinely empty directory (and
    # tolerates failure), so it cannot remove another creation's work.
    path.mkdir(exist_ok=True)
    cfg=config(root)
    at('init')
    run_bd(root,name,['init','--server','--external','--server-host','127.0.0.1','--server-port',str(cfg['port']),
                      '--server-user','root','--prefix',name,'--database',name,'--skip-agents','--skip-hooks','--non-interactive'])
    at('configure')
    for key,value in PROJECT_SETTINGS:
        run_bd(root,name,['config','set',key,value])
    at('requirements-governance')
    initialize_requirements_governance(root,name)
    at('backup-target')
    run_bd(root,name,['backup','init',str(root/'backups'/name)])
    at('merge-slot')
    provision_merge_slot(root,name)
    at('first-backup')
    backup_project(root,name)

def finish_project_steps(root,name):
    """Complete an initialized project whose creation stopped: every step is safe to repeat.

    The settings are set again, the backup target is initialized only if it is not there
    yet, the merge slot is created only if missing, and a backup is taken.
    """
    path=project_dir(root,name)
    if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
    for key,value in PROJECT_SETTINGS:
        run_bd(root,name,['config','set',key,value])
    initialize_requirements_governance(root,name)
    target=root/'backups'/name
    if not (target.is_dir() and any(target.iterdir())):
        run_bd(root,name,['backup','init',str(target)])
    provision_merge_slot(root,name)
    backup_project(root,name)

def add_project(root,name,requirements_default=True):
    """Initialize one project, provision its merge slot and back it up once.

    The operator's route keeps the same creation record the web route keeps
    (``project_creation``, kittrial-5bb.176): a run that stops after ``bd init`` leaves an
    ``incomplete`` record, so ``finish-project`` and ``remove-creation`` act on it instead
    of refusing with "no project creation record", and a re-run that meets one is told
    those two commands rather than only "Project already exists".

    The schedule guidance this prints (``scheduled_backup_coverage``) is deliberately
    CONSERVATIVE about a unit whose ``ExecStart`` runs the backup through ``sh -c``: such a
    line is reported as not backing up this runtime, because only a recognised ``admin.py``
    or long-sync-wrapper command line can be attributed to this runtime with certainty. A
    ``sh -c`` line may well run our ``admin.py backup --all``, but proving that means
    parsing a shell command inside a shell command, and a wrong "already covered" answer
    would leave a project silently off the schedule. Reporting it as uncovered is the safe
    direction: it can only prompt an operator to double-check, never hide a gap. This is a
    deliberate choice, not a missed case, and it never edits, installs or enables a unit.
    """
    import project_creation
    try:
        if requirements_default:
            project_creation.host_create(root,name,initialize_project,require_creatable_project)
        else:
            project_creation.host_create(root,name,initialize_project,require_creatable_project,
                                         requirements_default=False)
    except ValueError as error:
        # A name held by a creation that stopped is not a dead end: the record names the
        # commands that act on it (kittrial-5bb.176).
        hint=project_creation.unfinished_sentence(root,name) if 'Project already exists' in str(error) else ''
        if hint:raise ValueError('%s %s'%(error,hint)) from None
        raise
    print(f'Created project {name}')
    print(scheduled_backup_coverage(root,name)[1])
    print(worker_client_setup(root,name))

@contextmanager
def backup_lock(root,name):
    import fcntl
    validate_name(name)
    with (root/'backups'/(name+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield

# The idempotency journals of the accepted record designs (kittrial-5bb.64, the
# shared slice 0): .41 `.reference-requests`, .58 `.proposal-requests` and .60
# `.capability-requests`. No writer exists in this kit, but a backup taken by a
# later slice must restore here (a rollback target), so each is whitelisted,
# backed up, and validated with its frozen receipt schema before any write.
# kittrial-5bb.126 (open items design, slice 0) adds `.open-item-requests`, which no
# kit writes yet either. The host-issued `.owner-answers` journal is not a receipt
# journal: see OWNER_ANSWERS_JOURNAL.
RECORD_JOURNALS=('.reference-requests','.proposal-requests','.capability-requests','.open-item-requests','.requirement-owner-requests')
#: The host-issued owner answer and owner decision journal of the open items design
#: (kittrial-5bb.126, slice 0). Nothing writes it yet. It is backed up when present and
#: validated by its reader's own validator, open_items.validate_owner_entry, exactly
#: like `.integration-reverts`.
OWNER_ANSWERS_JOURNAL='.owner-answers'

def validate_record_receipt(name,record):
    """Frozen slice-0 receipt schema for one record-journal entry.

    The schema is requirement_records.validate_receipt's (.41 10.3, .58 8.4, .60 9):
    a 64-hex `sha256`, a known `status`, optional nonempty `actor`/`id`, an optional
    `revision >= 1` and an optional bound `acceptance`. It is a validated minimum,
    not a closed set: unknown keys (.58's `operation`, .60's batch `operation_id` and
    `key`) are read, never refused. .58 additionally freezes `operation` as a
    nonempty string when present.
    """
    from requirement_records import validate_receipt
    validate_receipt(record)
    if name.startswith('.proposal-requests/') and 'operation' in record and (
            not isinstance(record['operation'],str) or not record['operation'].strip()):
        raise ValueError('Invalid proposal receipt operation')
    return record

def validate_coordination_files(files):
    if not isinstance(files,dict):raise ValueError('Invalid coordination files map')
    for name,record in files.items():
        quarantine = isinstance(name,str) and re.fullmatch(r'\.feedback\.jsonl\.(?:[a-f0-9]{16}|[a-f0-9]{64})\.incomplete',name)
        journal = isinstance(name,str) and re.fullmatch(r'(?:\.coordination-requests|\.handoffs|\.handoff-requests|\.handoff-recoveries|\.requirement-requests|\.requirement-owner-requests|\.requirement-backfills|\.integration-reverts|\.reference-requests|\.proposal-requests|\.capability-requests|\.open-item-requests|\.owner-answers)/[a-f0-9]{64}\.json',name)
        if name not in ('.merge-context.json','ONBOARDING.md','GUIDANCE.md','.guidance.json','.guidance-clear.json','.sessions.json','.feedback.jsonl','.requirements-governance.json','.requirements-governance-source.json') and not quarantine and not journal:raise ValueError('Invalid coordination backup path')
        if not isinstance(record,dict):raise ValueError('Invalid coordination record')
        if name=='.sessions.json':
            from sessions import validate
            validate(record)
        if name.startswith('.handoffs/'):
            from handoff import validate_receipt
            validate_receipt(record)
            if name!='.handoffs/'+content_hash({'operation_id':record['identity']['payload']['operation_id']})+'.json':raise ValueError('Handoff receipt path mismatch')
        if name.startswith('.handoff-requests/'):
            from handoff import validate_request_record
            validate_request_record(record)
            if name!='.handoff-requests/'+content_hash({'request_id':record['request_id']})+'.json':
                raise ValueError('Handoff request path mismatch')
        if name.startswith('.handoff-recoveries/'):
            from handoff import validate_recovery
            validate_recovery(record)
            if name!='.handoff-recoveries/'+content_hash({'request_id':record['request_id']})+'.json':raise ValueError('Handoff recovery path mismatch')
        if name.startswith('.requirement-requests/'):
            from requirement_records import validate_receipt
            validate_receipt(record)
        if name.startswith('.requirement-backfills/'):
            from requirement_records import validate_receipt
            validate_receipt(record,backfill=True)
        if name.startswith(tuple(journal+'/' for journal in RECORD_JOURNALS)):
            validate_record_receipt(name,record)
        if name.startswith('.integration-reverts/'):
            # The host-issued revert journal (kittrial-5bb.52 P1). Its record shape
            # and its <sha256>.json path/hash binding are validated by the reader's
            # own validator, so a backup can never carry an entry the reader would
            # have to guess about.
            from review_workflow import JOURNAL_DIR, validate_revert_journal_entry
            validate_revert_journal_entry(record,name.partition('/')[2])
            if not name.startswith(JOURNAL_DIR+'/'):raise ValueError('Invalid coordination backup path')
        if name.startswith(OWNER_ANSWERS_JOURNAL+'/'):
            from open_items import validate_owner_entry
            validate_owner_entry(record,name.partition('/')[2])
        if name=='ONBOARDING.md' and (set(record)!={'text'} or not isinstance(record['text'],str) or not record['text'].strip() or len(record['text'].encode('utf-8'))>8000):raise ValueError('Invalid onboarding backup')
        if name=='GUIDANCE.md':
            from guidance import validate_text
            if set(record)!={'text'}:raise ValueError('Invalid guidance backup')
            validate_text(record['text'])
        if name=='.guidance.json':
            from guidance import validate_meta
            validate_meta(record)
        if name=='.guidance-clear.json':
            # Accepted and validated so a backup that carries the clear record restores
            # (kittrial-5bb.105). `backup` does not write it yet: a kit before this one
            # refuses a whole restore on a sidecar path it does not know, so the writer
            # waits until every installation runs a kit that accepts it.
            from guidance import validate_clear_record
            validate_clear_record(record)
        if name=='.feedback.jsonl':
            from feedback import validate_feed_text
            if set(record) != {'text'}:raise ValueError('Invalid feedback backup')
            validate_feed_text(record['text'])
        if quarantine:
            from feedback import validate_quarantine_record
            validate_quarantine_record(name,record)
    from requirement_governance import validate_files as validate_governance_files
    validate_governance_files(files)
    # The guidance text and its audit record are one generation: a backup that
    # carries the text but not the matching record (or a record whose version is not
    # the hash of the text) is refused rather than restored as a mismatched pair
    # (kittrial-5bb.99 review `get-misattributes-on-mismatch`).
    if 'GUIDANCE.md' in files or '.guidance.json' in files:
        if 'GUIDANCE.md' not in files or '.guidance.json' not in files:
            raise ValueError('Invalid guidance backup: the text and its audit record must be backed up together')
        from guidance import version_of as guidance_version
        if files['.guidance.json'].get('version')!=guidance_version(files['GUIDANCE.md']['text']):
            raise ValueError('Invalid guidance backup: the audit record does not match the guidance text')

def journal_snapshot_path(root,name):
    """Where ``backup_project`` stores the project's operation-journal snapshot."""
    validate_name(name)
    return root/'backups'/(name+JOURNAL_STORE_NAME)

def staged_journal_snapshot_path(root,name):
    """Where this run stages the journal snapshot before the native sync succeeds."""
    validate_name(name)
    return root/'backups'/(name+JOURNAL_STORE_NAME+'.staging')

def stage_journal_snapshot(root,name):
    """Snapshot the live journal to a staging path (or None when there is no journal).

    The snapshot is NOT yet the one ``restore-new`` consumes: it is promoted to
    ``journal_snapshot_path`` only after the native sync and the complete sidecar are
    durable, so a failed or interrupted run cannot leave a newer journal beside the
    previous complete pair (see ``promote_journal_snapshot``).
    """
    staging=staged_journal_snapshot_path(root,name)
    if staging.is_symlink():raise ValueError('Journal snapshot path must not be a symlink')
    return snapshot_journal(project_dir(root,name)/JOURNAL_STORE_NAME,staging)

def discard_stale_journal_staging(root,name):
    """Remove a staged or temporary journal snapshot an earlier hard kill left behind.

    ``stage_journal_snapshot`` writes ``backups/<name>.http-operations.sqlite3.staging`` and
    ``snapshot_journal`` writes a ``.tmp`` sibling; a ``kill -9`` during the native sync can
    run no cleanup, so without this the file survives until the next successful run (the
    reviewer observed exactly that). The caller holds the project's backup lock for its whole
    critical section, which is what makes cleaning here safe: a concurrent ``backup`` of the
    same project is excluded by that lock, and ``backup-copy`` only ever reads the promoted
    snapshot, never a staging path, so no in-flight run can lose the file it is using. A
    symlink is removed as a link and never followed.
    """
    staging=staged_journal_snapshot_path(root,name)
    for path in (staging,Path(str(staging)+'.tmp')):
        try:
            if path.exists() or path.is_symlink():path.unlink()
        except OSError:
            pass

def promote_journal_snapshot(root,name,staging):
    """Publish ``staging`` as the project's journal snapshot, atomically.

    Only called after the native sync and the new complete sidecar are durable. A run
    with no live journal removes any previous snapshot, so the published snapshot always
    belongs to the same generation as the native backup and the complete sidecar.
    """
    final=journal_snapshot_path(root,name)
    if final.is_symlink():raise ValueError('Journal snapshot path must not be a symlink')
    if staging is None or not Path(staging).is_file():
        if final.is_file():final.unlink()
        return None
    os.replace(staging,final)
    return final

def snapshot_journal(source,destination):
    """Consistent SQLite snapshot of ``source`` into ``destination`` (or None).

    Uses the stdlib ``sqlite3.Connection.backup()`` API, which copies a live database
    page-by-page inside SQLite itself, so the snapshot is consistent even while a
    keyed mutation holds the project coordination lock. A missing source is not an
    error: a project that has never run a keyed operation has no journal yet.
    """
    source=Path(source)
    if not source.is_file():
        return None
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.tmp')
    if temporary.exists():temporary.unlink()
    _copy_sqlite(source,temporary)
    os.replace(temporary,destination)
    return destination

def restore_journal(snapshot,destination):
    """Restore a journal snapshot into ``destination`` (or None when there is none).

    Restores through the same sqlite backup API into a temporary file and then
    replaces the destination, so a half-written file can never become the live store;
    any stale ``-wal``/``-shm`` sidecars of the destination are removed so the restored
    database is authoritative. A snapshot that is not a readable journal database is
    refused before the destination is touched.
    """
    snapshot=Path(snapshot)
    if not snapshot.is_file():
        return None
    _check_journal_database(snapshot)
    destination=Path(destination)
    temporary=destination.with_name(destination.name+'.restore')
    if temporary.exists():temporary.unlink()
    _copy_sqlite(snapshot,temporary)
    os.replace(temporary,destination)
    for suffix in ('-wal','-shm'):
        sidecar=destination.with_name(destination.name+suffix)
        if sidecar.exists():sidecar.unlink()
    return destination

def _copy_sqlite(source,destination):
    from_connection=sqlite3.connect('file:%s?mode=ro'%source.as_posix(),uri=True)
    try:
        to_connection=sqlite3.connect(str(destination))
        try:
            from_connection.backup(to_connection)
            # The copy inherits the live store's WAL header; a snapshot is a single
            # self-contained file, so switch it to rollback-journal mode (no -wal/-shm
            # left in backups/). A restored store is reopened in WAL by the journal.
            to_connection.execute('PRAGMA journal_mode = DELETE').fetchone()
        finally:
            to_connection.close()
    finally:
        from_connection.close()

#: Tables a journal snapshot must contain to be restorable.
JOURNAL_REQUIRED_TABLES=('operations','meta')

def _check_journal_database(path):
    """Refuse a journal snapshot that is not a sound operation-journal database.

    Opens the snapshot read-only and runs ``PRAGMA quick_check`` plus a schema check
    (the required tables), so a truncated or corrupt snapshot is refused before
    anything is created or replaced.
    """
    try:
        connection=sqlite3.connect('file:%s?mode=ro'%Path(path).as_posix(),uri=True)
    except sqlite3.Error as error:
        raise ValueError('Journal snapshot is not a readable SQLite database: %s'%error) from None
    try:
        try:
            check=[row[0] for row in connection.execute('PRAGMA quick_check')]
            tables={row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as error:
            raise ValueError('Journal snapshot is not a readable SQLite database: %s'%error) from None
    finally:
        connection.close()
    if check!=['ok']:
        raise ValueError('Journal snapshot failed PRAGMA quick_check: %s'%'; '.join(map(str,check[:3])))
    missing=[name for name in JOURNAL_REQUIRED_TABLES if name not in tables]
    if missing:
        raise ValueError('Journal snapshot does not contain the operation-journal schema (missing %s)'%', '.join(missing))

def _atomic_write_bytes(path,content):
    fd,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.restore',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        try:
            directory_fd=os.open(str(path.parent),os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

def last_complete_sidecar_path(root,name):
    """The durable copy of this project's last COMPLETE coordination sidecar."""
    validate_name(name)
    return root/'backups'/(name+LAST_COMPLETE_SUFFIX)

def complete_sidecar(path):
    """The complete coordination record at ``path``, or None.

    Missing, symlinked, unreadable, wrong-schema and non-``complete`` sidecars all
    return None: every caller treats "not proven complete" as unusable rather than
    guessing, so a half-written or still-``pending`` marker is never restored from.
    """
    data,_=read_sidecar(path)
    return data

def read_sidecar(path):
    """``(record, None)`` for a complete coordination sidecar, else ``(None, problem)``.

    ``problem`` is None when the copy is absent and otherwise says why it cannot be used,
    for ``backup-authority`` (kittrial-5bb.145); ``complete_sidecar`` keeps only the record.
    """
    data,problem=read_sidecar_json(path)
    if problem is not None or data is None:return None,problem
    if not isinstance(data,dict) or data.get('schema_version')!=1:return None,'it is not a coordination sidecar of schema 1'
    if data.get('status')!='complete':return None,'its status is %s, not complete'%json.dumps(data.get('status'))
    return data,None

def read_sidecar_json(path):
    """``(parsed JSON, None)`` for a sidecar copy, ``(None, None)`` when it is absent, else
    ``(None, problem)``. The one place a sidecar is opened: never through a symlink, never
    blocking on a FIFO or device, and only a regular file is read (kittrial-5bb.150/152)."""
    path=Path(path)
    if path.is_symlink():return None,'it is a symlink'
    if not path.exists():return None,None
    if not path.is_file():return None,'it is not a regular file'
    # Opened without following a symlink and without blocking, then checked to be a regular
    # file on the open descriptor: a FIFO or device put in place between the checks above
    # and the open answers at once instead of hanging the read (kittrial-5bb.150).
    flags=os.O_RDONLY|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0)
    try:
        fd=os.open(str(path),flags)
    except OSError as error:
        return None,'it cannot be read (%s)'%error.__class__.__name__
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):return None,'it is not a regular file'
        with os.fdopen(fd,'rb') as handle:
            fd=None
            raw=handle.read()
    except OSError as error:
        return None,'it cannot be read (%s)'%error.__class__.__name__
    finally:
        if fd is not None:os.close(fd)
    try:return json.loads(raw.decode('utf-8')),None
    except ValueError:return None,'it is not valid JSON'

def native_backup_manifest(directory):
    """``(manifest, None)`` for a native backup directory, or ``(None, reason)``.

    The manifest is a stat-only walk - sorted relative path, size and ``mtime_ns`` for
    every regular file, plus the name of every directory and symlink target - hashed with
    SHA-256. No file contents are read, so the cost is one ``stat`` per entry and it is
    safe on a large backup; any rewrite of a chunk changes the digest, and so does an added
    or removed entry. A same-size rewrite that also lands on the same timestamp tick (a
    coarse-grained filesystem) is the one change this cannot see, which is the deliberate
    price of never reading the backup's contents. A missing, symlinked or unreadable
    directory is REPORTED rather than raising: the caller decides what to tell the
    operator, and this never turns a reporting step into a failure.
    """
    directory=Path(directory)
    if directory.is_symlink():return None,'the native backup directory is a symlink'
    if not directory.is_dir():return None,'the native backup directory is missing'
    digest=hashlib.sha256();files=0
    def add(kind,entry,extra=''):
        digest.update(('%s\0%s\0%s\0'%(kind,entry,extra)).encode('utf-8','surrogateescape'))
    try:
        for base,dirnames,filenames in os.walk(directory):
            dirnames.sort();filenames.sort()
            relative=Path(base).relative_to(directory).as_posix()
            relative='' if relative=='.' else relative
            for name in list(dirnames):
                path=Path(base)/name
                entry=(relative+'/'+name) if relative else name
                if path.is_symlink():
                    add('link',entry,os.readlink(path));dirnames.remove(name)
                else:
                    add('dir',entry)
            for name in filenames:
                path=Path(base)/name
                entry=(relative+'/'+name) if relative else name
                if path.is_symlink():
                    add('link',entry,os.readlink(path));continue
                info=path.stat()
                add('file',entry,'%d/%d'%(info.st_size,info.st_mtime_ns))
                files+=1
    except OSError as error:
        return None,'the native backup directory could not be read: %s'%error
    return {'schema_version':1,'algorithm':'sha256-path-size-mtime_ns','files':files,
            'digest':digest.hexdigest()},None

def native_backup_change(root,source,record):
    """``(state, detail)`` for the native directory against a sidecar's recorded manifest.

    ``state`` is ``'unchanged'`` when the directory still matches the manifest recorded
    with ``record``, ``'changed'`` when it does not, and ``'unknown'`` when the check
    cannot be performed at all: a sidecar written by an older revision records no manifest,
    and a missing or unreadable directory cannot be walked. ``unknown`` is reported as
    unknown rather than as clean, so an operator is never told a pair was verified when it
    was not.
    """
    recorded=(record or {}).get(NATIVE_MANIFEST_KEY) if isinstance(record,dict) else None
    if not isinstance(recorded,dict) or not isinstance(recorded.get('digest'),str):
        return 'unknown',('the coordination sidecar records no native-backup manifest, so the check could not '
                          'be performed')
    manifest,problem=native_backup_manifest(root/'backups'/source)
    if manifest is None:
        return 'unknown',problem
    if manifest['digest']==recorded['digest']:
        return 'unchanged','the native backup directory matches the manifest recorded with this generation'
    return 'changed',('the native backup directory no longer matches the manifest recorded with this '
                      'generation')

def report_native_backup_change(root,source):
    """Print the truth about the native directory the restore is about to pair up.

    A restore serves a complete sidecar (the canonical one, or the durable last-complete
    copy when the canonical sidecar is still ``pending`` after a run that could not clean
    up) together with the operation-journal snapshot of that same generation. The native
    directory, however, is written by the ``dolt`` client outside that pair, so a run that
    was killed outright or interrupted can leave it partly rewritten: the restored Dolt
    could then hold an effect whose receipt the restored journal does not have, and an
    exact retry could repeat it. Recomputing the recorded manifest is how that is detected;
    a sidecar with no manifest is reported as unverifiable rather than clean.
    """
    record=resolved_coordination_sidecar(root,source)
    state,detail=native_backup_change(root,source,record)
    if state=='changed':
        print('WARNING: %s. The restore uses the previous complete coordination sidecar and its '
              'operation-journal snapshot, but the native backup under backups/%s changed after that '
              'generation completed (an interrupted or killed backup run can leave the native directory '
              'partly rewritten). Effects present in the restored Dolt may therefore have NO receipt in the '
              'restored journal, and an exact retry of one could repeat it. Inspect the restored project '
              'before accepting writes, and take a fresh complete backup (admin.py backup %s) from the '
              'original runtime before relying on this pair.'%(detail,source,source))
    elif state=='unknown':
        print('Note: the native backup under backups/%s could not be checked against the coordination '
              'sidecar being restored (%s), so this restore cannot claim the native directory is the one '
              'that belongs to it.'%(source,detail))

def _atomic_copy(source,destination):
    """Replace ``destination`` with a byte-for-byte copy of ``source``, atomically."""
    _atomic_write_bytes(Path(destination),Path(source).read_bytes())

@contextmanager
def last_complete_guard(root,name):
    """Keep the previous complete sidecar restorable across one backup run.

    The complete sidecar is saved aside (durably) before the caller replaces it with
    the ``pending`` marker, and restored if the run fails or is interrupted, so a
    failed run never destroys the pair ``restore-new`` needs. A run with no previous
    complete pair leaves the honest ``pending`` marker in place: there is nothing to
    degrade. The caller's critical section still starts before the ``pending`` write;
    this guard only performs atomic file copies of an atomically-replaced file.

    The yielded ``state`` carries one flag the caller raises once the NEW generation is
    durable (native sync done, complete sidecar written, journal snapshot promoted):
    after that point the old sidecar must NOT be put back, because doing so would pair
    the previous sidecar with the new native directory if a later promotion step (for
    example refreshing the last-complete copy) fails.
    """
    bundle=root/'backups'/(name+'.coordination.json')
    last_complete=last_complete_sidecar_path(root,name)
    # A directory where a sidecar copy belongs is not something this kit wrote, so it is
    # neither replaced nor deleted: the run refuses before writing anything, naming it and
    # what to do (kittrial-5bb.157). It used to end in a raw "Is a directory" error.
    for copy in (bundle,last_complete):
        if copy.is_dir() and not copy.is_symlink():
            raise ValueError('%s is a directory where the coordination sidecar copy belongs, so this backup '
                             'cannot keep its pair restorable; the pair was not touched. It is not a file this kit '
                             'writes: look at what it holds, move it out of backups/, then run backup again.'
                             %copy.relative_to(root).as_posix())
    state={'promoted':False}
    if complete_sidecar(bundle) is not None:
        _atomic_copy(bundle,last_complete)
    try:
        yield bundle,last_complete,state
    except BaseException:
        if not state['promoted']:
            try:
                if last_complete.is_file() and not last_complete.is_symlink():
                    _atomic_copy(last_complete,bundle)
            except OSError:
                pass
        raise

def guidance_backup_pair(path):
    """The guidance fragment for one project's coordination sidecar, and any fault.

    Returns ``({'GUIDANCE.md': ..., '.guidance.json': ...}, None)`` for a healthy
    pair, ``({}, None)`` when the project carries no guidance, and ``({}, message)``
    when the pair is mismatched or unreadable. The caller leaves the bad pair out and
    marks the project degraded instead of skipping its tracker backup
    (kittrial-5bb.99 review `guidance-fault-skips-the-project-backup`).
    """
    if not ((path/'GUIDANCE.md').exists() or (path/'GUIDANCE.md').is_symlink()):
        return {}, None
    from guidance import (META_NAME as GUIDANCE_META, read_meta as read_guidance_meta,
                          read_text as read_guidance_text, validate_meta as validate_guidance_meta,
                          version_of as guidance_version)
    try:
        guidance_text=read_guidance_text(path)
        guidance_meta=read_guidance_meta(path)
        if guidance_meta is None:
            raise ValueError('the guidance text has no readable audit metadata')
        validate_guidance_meta(guidance_meta)
        if guidance_meta['version']!=guidance_version(guidance_text):
            raise ValueError('the audit record does not match the guidance text (a hand edit or a crashed set)')
    except ValueError as error:
        return {}, ('guidance is degraded: %s. The tracker backup is complete but carries no guidance pair; '
                    'ask the operator to repair it with `admin.py set-guidance PROJECT --actor OPERATOR '
                    '--file FILE` and then take a fresh backup. A set with the same text repairs a record that '
                    'is missing, does not match or is not valid; a guidance file that cannot be read needs a '
                    'set with clean text, which replaces it.'%(error,))
    return {'GUIDANCE.md': {'text': guidance_text}, GUIDANCE_META: guidance_meta}, None

def backup_project(root,name):
    import fcntl
    from coordination import atomic
    path=project_dir(root,name)
    # Refuse a project whose recorded target is not its own backups/<name> BEFORE the
    # sidecar is replaced by a `pending` marker, so a clone that was never re-pointed
    # cannot leave a failed marker beside a foreign target (and cannot overwrite the
    # source project's backup). native_backup_sync repeats the check for direct callers.
    validate_backup_target(root,name)
    with (path/'.coordination.lock').open('a') as lock, backup_lock(root,name), \
            last_complete_guard(root,name) as (bundle,last_complete,guard_state):
        fcntl.flock(lock,fcntl.LOCK_EX)
        atomic(bundle,{'schema_version':1,'status':'pending'})
        files={}
        from requirement_governance import snapshot as governance_snapshot
        files.update(governance_snapshot(path,name))
        if (path/'.coordination-requests').is_symlink() or (path/'.merge-context.json').is_symlink():raise ValueError('Coordination paths must not be symlinks')
        for record in sorted((path/'.coordination-requests').glob('*.json')):
            if record.is_symlink():raise ValueError('Coordination receipt must not be a symlink')
            files['.coordination-requests/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        context=path/'.merge-context.json'
        if context.exists():files[context.name]=json.loads(context.read_text(encoding='utf-8'))
        registry=path/'.sessions.json'
        if registry.is_symlink():raise ValueError('Session registry must not be a symlink')
        if registry.exists():files[registry.name]=json.loads(registry.read_text(encoding='utf-8'))
        handoffs=path/'.handoffs'
        if handoffs.is_symlink():raise ValueError('Handoff journal must not be a symlink')
        for record in handoffs.glob('*.json'):
            if record.is_symlink():raise ValueError('Handoff receipt must not be a symlink')
            files['.handoffs/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        requests=path/'.handoff-requests'
        if requests.is_symlink():raise ValueError('Handoff request journal must not be a symlink')
        for record in requests.glob('*.json'):
            if record.is_symlink():raise ValueError('Handoff request record must not be a symlink')
            files['.handoff-requests/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        recoveries=path/'.handoff-recoveries'
        if recoveries.is_symlink():raise ValueError('Handoff recovery journal must not be a symlink')
        for record in recoveries.glob('*.json'):
            if record.is_symlink():raise ValueError('Handoff recovery record must not be a symlink')
            files['.handoff-recoveries/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        # The requirement journals are the operator's recovery cache for
        # requirement-apply/backfill: back them up so a restore keeps the F3
        # acceptance evidence and the pending/reconciled operation IDs.
        requirement_requests=path/'.requirement-requests'
        if requirement_requests.is_symlink():raise ValueError('Requirement request journal must not be a symlink')
        for record in requirement_requests.glob('*.json'):
            if record.is_symlink():raise ValueError('Requirement request receipt must not be a symlink')
            files['.requirement-requests/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        requirement_backfills=path/'.requirement-backfills'
        if requirement_backfills.is_symlink():raise ValueError('Requirement backfill journal must not be a symlink')
        for record in requirement_backfills.glob('*.json'):
            if record.is_symlink():raise ValueError('Requirement backfill receipt must not be a symlink')
            files['.requirement-backfills/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        # The host-issued integration revert journal (kittrial-5bb.52 P1). It is the
        # proof a revert was issued on this host, so a restore that dropped it would
        # silently un-revert every reverted contribution: it is backed up, validated
        # and restored exactly like the requirement journals. The path is also what
        # the deployment order in docs/REVIEWS.md requires to be covered before the
        # first revert is recorded.
        revert_journal=path/'.integration-reverts'
        if revert_journal.is_symlink():raise ValueError('Integration revert journal must not be a symlink')
        for record in revert_journal.glob('*.json'):
            if record.is_symlink():raise ValueError('Integration revert journal entry must not be a symlink')
            files['.integration-reverts/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        # The owner answers journal (kittrial-5bb.126): written by no kit yet, collected
        # only when present, so a backup of a runtime with open-item writes off is
        # byte-for-byte what the previous kit takes.
        owner_answers=path/OWNER_ANSWERS_JOURNAL
        if owner_answers.is_symlink():raise ValueError('Owner answers journal must not be a symlink')
        for record in owner_answers.glob('*.json'):
            if record.is_symlink():raise ValueError('Owner answers journal entry must not be a symlink')
            files[OWNER_ANSWERS_JOURNAL+'/'+record.name]=record_json.loads(record.read_text(encoding='utf-8'))
        # The record journals (kittrial-5bb.64). This kit writes none, but after a
        # rollback from a later slice they exist and must round-trip.
        for journal in RECORD_JOURNALS:
            folder=path/journal
            if folder.is_symlink():raise ValueError('Record journal %s must not be a symlink'%journal)
            for record in folder.glob('*.json'):
                if record.is_symlink():raise ValueError('Record journal receipt must not be a symlink')
                files[journal+'/'+record.name]=json.loads(record.read_text(encoding='utf-8'))
        if (path/'ONBOARDING.md').exists() or (path/'ONBOARDING.md').is_symlink():
            from onboarding import read_document, PROJECT_LIMIT
            files['ONBOARDING.md']={'text':read_document(path,'ONBOARDING.md',PROJECT_LIMIT)}
        if (path/'GUIDANCE.md').exists() or (path/'GUIDANCE.md').is_symlink():
            # The standing guidance channel (kittrial-5bb.99): the text and its audit
            # record are one generation. A mismatched or unreadable pair is a two-file
            # sidecar fault and must not stop the tracker backup (live installations
            # run `backup --all` on a daily timer): leave the pair out, mark the
            # project degraded with an actionable message, and keep this project's
            # tracker backup complete and usable
            # (kittrial-5bb.99 review `guidance-fault-skips-the-project-backup`).
            fragment,fault=guidance_backup_pair(path)
            files.update(fragment)
            if fault:
                print('backup degraded for %s: %s'%(name,fault),file=sys.stderr)
        feedback=path/'.feedback.jsonl'
        if feedback.exists() or feedback.is_symlink():
            if feedback.is_symlink():raise ValueError('Feedback feed must not be a symlink')
            from feedback import FEED_NAME, QUARANTINE_SUFFIX, _read as read_feedback, validate_quarantine_record
            read_feedback(feedback)
            files['.feedback.jsonl']={'text':feedback.read_text(encoding='utf-8')}
            for record in sorted(path.glob(FEED_NAME+'.*'+QUARANTINE_SUFFIX)):
                if record.is_symlink():raise ValueError('Feedback quarantine must not be a symlink')
                quarantine_name=record.name
                content=record.read_bytes()
                backup={'base64':base64.b64encode(content).decode('ascii')}
                validate_quarantine_record(quarantine_name,backup)
                files[quarantine_name]=backup
        validate_coordination_files(files)
        # The idempotency journal is a second local store inside the project directory;
        # take its consistent snapshot in the same critical section as the coordination
        # sidecar so a backup never pairs one store's state with the other's. It is
        # STAGED here and promoted only after the native sync and the new complete
        # sidecar are durable, so a failed run leaves the previous snapshot (which
        # belongs to the previous complete pair) in place for restore-new.
        staged=None
        client=SyncClientHandle()
        # SIGTERM is turned into an exception for exactly this section (SIGINT already
        # raises KeyboardInterrupt) and later SIGTERMs are ignored, so the cleanup below
        # still runs on a normal operator stop instead of the interpreter dying with the
        # dolt client still writing. The cleanup runs INSIDE the guard, while its handler
        # is still installed: outside it, a second stop would kill the interpreter before
        # terminate_process_group could kill the client group.
        with signal_termination_guard():
            try:
                # A staging file from an earlier hard kill is cleaned at the start of the run,
                # under the backup lock (see discard_stale_journal_staging).
                discard_stale_journal_staging(root,name)
                staged=stage_journal_snapshot(root,name)
                output=native_backup_sync(root,name,client=client)
                # The deployment operator allowlist travels with the project sidecar so a
                # restore can TELL the operator which recorded authority is missing on the
                # destination host. It is not applied automatically: the allowlist is
                # deployment-wide authority, so `restore-new` only re-grants it with an
                # explicit --restore-operators. Native backup preserves the void comments
                # and this preserves the record of the authority the reads would need.
                # The `verifiers` list travels the same way and under the same rule
                # (--restore-verifiers); older kits ignore the unknown key.
                record={'schema_version':1,'status':'complete','files':files,
                        'operators':sorted(operators(root)),'verifiers':sorted(verifiers(root))}
                # A manifest of the native directory as this generation completed it. A
                # restore recomputes it and warns when the directory no longer matches, so
                # a partly rewritten native backup is never paired silently with the
                # previous complete sidecar and its journal snapshot (see
                # report_native_backup_change). It is additive: the schema stays 1.
                manifest,problem=native_backup_manifest(root/'backups'/name)
                if manifest is not None:record[NATIVE_MANIFEST_KEY]=manifest
                atomic(bundle,record)
                # The new generation is now native + complete sidecar + promoted journal;
                # from here the guard must not put the previous sidecar back. SIGTERM is
                # held across the promotion and the flag together: a stop landing between
                # them would restore the previous sidecar beside an already-promoted
                # journal, pairing two generations (see sigterm_blocked).
                with sigterm_blocked():
                    promote_journal_snapshot(root,name,staged)
                    staged=None
                    guard_state['promoted']=True
                # Refresh the durable last-complete copy so the next run has a restorable
                # pair to protect even if it is interrupted before it can write anything.
                # A failure here leaves the new complete generation in place (the guard no
                # longer rolls it back), and the next run's guard refreshes this copy.
                _atomic_copy(bundle,last_complete)
            finally:
                # On ANY path that is not a normally-finished client - SIGTERM/SIGINT, the
                # explicit ceiling, an exception - stop the whole process group here, while
                # the backup lock and the SIGTERM guard are both still held, so the dolt
                # client and anything it spawned cannot keep writing backups/<name> past the
                # lock (the reviewer reproduced that after SIGTERM) and a second stop cannot
                # kill the interpreter before the group is killed. A successful run is a
                # no-op: the client already exited.
                terminate_process_group(client)
                if staged is not None:
                    try:
                        if Path(staged).exists():Path(staged).unlink()
                    except OSError:
                        pass
        return output

def utc_stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())

def utc_timestamp(value):
    """True for the exact second-precision UTC form this file writes."""
    return isinstance(value,str) and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',value))

def open_item_labelled(labels):
    """The open items family or state labels among ``labels``: ``open-item`` and ``open-item:*``."""
    return sorted(label for label in labels or [] if isinstance(label,str)
                  and (label=='open-item' or label.startswith('open-item:')))

def open_item_label_check(root,names=None):
    """Read-only deploy-time check of the open items design (kittrial-5bb.126, slice 0).

    The family label ``open-item`` is an exact label that a project may already use; a
    later slice would read such a row as a record anchor once it also carries an
    open-item record. ``open-item:`` became a reserved prefix in this slice, so a row
    already carrying one can no longer have it changed by a contributor. This lists,
    for every initialized project (or the ones named), the rows carrying either, so the
    operator knows before open-item writes are turned on. No lock, no write: bd runs
    only for a project whose metadata records the server coordinates
    (``project_metadata_state`` is ``server``); any other project is reported unreadable.
    Returns ``(report, clean)``; ``clean`` is false when a project uses a label or
    could not be read.
    """
    report={}
    for name in (names or initialized_projects(root)):
        try:
            state=project_metadata_state(root,name)
        except (ValueError,OSError):state='absent'
        if state=='absent':
            report[name]={'error':'unknown or uninitialized project'};continue
        if state!='server':
            # bd would fall back to an embedded database here: it would CREATE
            # .beads/embeddeddolt inside the project and list nothing, which would read
            # as clean (kittrial-5bb.126 review). Nothing is run for such a project.
            report[name]={'error':'.beads/metadata.json does not record the Dolt server coordinates, so bd '
                                   'was not run (it would create an embedded database here and list nothing)'}
            continue
        try:
            rows=json.loads(run_bd(root,name,['list','--all','--limit','0','--json']) or '[]')
            if not isinstance(rows,list):raise ValueError('unexpected list output')
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,TypeError) as error:
            report[name]={'error':'could not list the project: %s'%type(error).__name__};continue
        found=[{'id':row.get('id'),'labels':open_item_labelled(row.get('labels'))} for row in rows
               if isinstance(row,dict) and open_item_labelled(row.get('labels'))]
        report[name]={'rows':found}
    using=sorted(name for name,entry in report.items() if entry.get('rows'))
    unreadable=sorted(name for name,entry in report.items() if 'error' in entry)
    return {'projects':report,'using':using,'unreadable':unreadable},not using and not unreadable

def initialized_projects(root):
    """Every initialized project name in this runtime, sorted.

    ``backup --all`` must enumerate real projects only, so a name counts when its
    directory carries the native marker ``.beads/metadata.json`` that the other
    host commands already require. A stray or half-created directory, a symlinked
    directory and a name that could never be a project are not targets.
    """
    projects=root/'projects'
    if not projects.is_dir():return []
    found=[]
    for path in projects.iterdir():
        if path.is_symlink() or not path.is_dir():continue
        if not re.fullmatch(r'[a-z][a-z0-9]{1,23}',path.name):continue
        if (path/'.beads'/'metadata.json').is_file():found.append(path.name)
    return sorted(found)

def merge_slot_report(root,names=None):
    """Read-only report of the projects whose merge slot is not healthy (kittrial-5bb.202 item 4).

    Every initialized project of this runtime (or the ones named) is read once through
    ``project_merge_slot_state``, which never writes. A project is listed under exactly one
    of ``missing``, ``damaged``, ``unreadable`` or ``healthy``. ``missing``/``damaged`` name
    the merge-create repair in the project's ``detail``; ``unreadable`` is a project whose
    slot could not be read at all. Returns ``(report, healthy)``: ``healthy`` is true only
    when at least one project was read and every one of them is healthy, so a caller can use
    it as a gate. No lock and no write: bd runs only for a project whose metadata records
    the Dolt server coordinates.
    """
    report={}
    for name in (names or initialized_projects(root)):
        report[name]=project_merge_slot_state(root,name)
    groups={state:sorted(name for name,entry in report.items() if entry['state']==state)
            for state in ('missing','damaged','unknown','healthy')}
    return ({'schema_version':1,'projects':report,
             'missing':groups['missing'],'damaged':groups['damaged'],
             'unreadable':groups['unknown'],'healthy':groups['healthy'],
             'not_healthy':sorted(groups['missing']+groups['damaged']+groups['unknown'])},
            bool(report) and not groups['missing'] and not groups['damaged'] and not groups['unknown'])

def _revoked_list(found,limit):
    """One revocation list: the first `limit` entries, or every entry when limit is None.

    `operators remove` truncates at 5 with `(+N more)`, which left an operator no way to
    see the rest; `--all-revoked` passes limit=None and names them all
    (kittrial-5bb.92 item 3).
    """
    if limit is None:
        return ', '.join(found) or 'none'
    return (', '.join(found[:limit])+(' (+%d more)'%(len(found)-limit) if len(found)>limit else '')) or 'none'

def revoked_proposal_records(root,actor,limit=5):
    """Name the requirement-proposal effects of one operator's revocation.

    A proposal disposition, an owner decision and a contribution-settings record count
    only while their native author is on the operator allowlist. Removing an operator
    therefore moves every proposal they decided back to its earlier trusted state, and
    can make the settings chain read empty (the actor map and the deciders), which stops
    triage. Both are named here, from the difference between a read under the live
    allowlist and one without `actor`, with the reader every other read uses. Read-only
    and best-effort, like `revoked_revert_records`.
    """
    import proposal_records
    authority=operators(root)
    changed=[];settings=[];unreadable=0
    for name in initialized_projects(root):
        path=project_dir(root,name)
        try:
            rows=record_json.loads_rows(run_bd(root,name,['export','--all']))
            moved,setting=proposal_records.revocation_effects(rows,authority,actor,path)
            changed+=['%s/%s'%(name,item) for item in moved]
            if setting:settings.append('%s: %s'%(name,setting))
        except (OSError,ValueError,TypeError,KeyError,subprocess.CalledProcessError):
            unreadable+=1
    def listed(found):
        return _revoked_list(found,limit)
    return (' Requirement proposals: dispositions, owner decisions and contribution settings they recorded stop '
            'counting too (proposals whose state changes: %s; contribution settings that change: %s; projects that '
            'could not be read: %d). Re-adding the operator restores them; otherwise re-enter the settings with '
            'admin.py proposal-settings, and note that %s.'
            %(listed(changed),listed(settings),unreadable,proposal_records.NO_REPAIR[0].lower()+proposal_records.NO_REPAIR[1:]))

def revoked_keyed_voids(root,actor,limit=5):
    """Name the reference and capability repairs one operator's revocation undoes.

    A void of a reference or capability record (kittrial-5bb.74) applies only while its
    native author is on the operator allowlist, so removing that operator brings back
    the record it voided: the entry reads malformed again, or an anchor whose every
    record was voided holds a record again. The count is the voids the actor authored
    that apply today; the entries are those whose reading changes between the live
    allowlist and the allowlist without `actor`, read by the reader every other read
    uses. Read-only and best-effort, like `revoked_revert_records`.
    """
    import capability_records,reference_records
    authority=operators(root)
    remaining=frozenset(item for item in authority if item!=actor)
    count=0;changed=[];unreadable=0
    for name in initialized_projects(root):
        try:
            rows=record_json.loads_rows(run_bd(root,name,['export','--all']))
        except (OSError,ValueError,TypeError,KeyError,subprocess.CalledProcessError):
            unreadable+=1
            continue
        for kind in (reference_records.KIND,capability_records.KIND):
            for row in rows:
                if not isinstance(row,dict) or kind.type_label not in (row.get('labels') or []):continue
                mine=[payload for payload,comment in kind.applied_voids(row,authority)[0] if comment.get('author')==actor]
                if not mine:continue
                count+=len(mine)
                def reading(allowed):
                    if not kind.has_live_record(row,allowed):return None,'no record'
                    view=kind.entry_view(row,allowed)
                    return view['key'],view['state']
                (key,before),(_,after)=reading(authority),reading(remaining)
                if before!=after:
                    changed.append('%s/%s %s %s -> %s'%(name,kind.noun,key or row.get('id'),before,after))
    changed.sort()
    shown=_revoked_list(changed,limit)
    return (' Voids of reference and capability records they authored stop applying too (%d void%s; entries '
            'whose reading changes: %s; projects that could not be read: %d).'
            %(count,'' if count==1 else 's',shown,unreadable))

def revoked_revert_records(root,actor,limit=5):
    """Name the host-issued records one operator's revocation changes.

    ``operators remove ACTOR --confirm-revoke`` makes every record that operator
    authored stop applying: voids (the pre-existing warning) and, since
    kittrial-5bb.52, the host-issued integration revert records and RETRACTIONS
    too. Those pull in opposite directions and both must be reported: a revert the
    actor issued stops applying, while a retraction the actor issued also stops
    applying, which RE-APPLIES the revert it retracted (kittrial-5bb.52 review item
    ``smaller``).

    The answer is the difference between the reverts honoured under the LIVE
    deployment allowlist and under that allowlist without ``actor``, read by the
    same reader every other read uses. That is what makes retractions count: a
    revert already retracted by another operator is reported as neither stopping
    nor re-applying, and the scan never invents a single-actor authority by passing
    the revoked actor alone. Read-only and best-effort: each initialized project is
    exported, its host journal is consulted, and a project that cannot be read is
    reported as unreadable rather than failed -- the allowlist change must not
    depend on one broken runtime.
    """
    from review_workflow import revert_records
    def listed(found):
        return _revoked_list(found,limit)
    authority=operators(root)
    remaining=frozenset(item for item in authority if item!=actor)
    stopped=[];reapplied=[];unreadable=0
    for name in initialized_projects(root):
        path=project_dir(root,name)
        try:
            rows=record_json.loads_rows(run_bd(root,name,['export','--all']))
        except (OSError,ValueError,TypeError,KeyError):
            unreadable+=1
            continue
        for row in rows:
            if not isinstance(row,dict) or row.get('issue_type')=='event':continue
            try:
                before,_=revert_records(row,authority,path)
                after,_=revert_records(row,remaining,path)
            except (ValueError,TypeError,KeyError):
                unreadable+=1
                continue
            task=str(row.get('id') or '?')
            was={record['comment_id'] for record in before}
            now={record['comment_id'] for record in after}
            stopped.extend(task+'/'+cid for cid in sorted(was-now))
            reapplied.extend(task+'/'+cid for cid in sorted(now-was))
    stopped=sorted(set(stopped));reapplied=sorted(set(reapplied))
    parts=[]
    if stopped:
        parts.append('host-issued integration revert records that stop applying: '+listed(stopped))
    if reapplied:
        parts.append('host-issued retractions that stop applying, so these reverted integrations '
                     're-apply: '+listed(reapplied))
    if not parts:
        parts.append('no host-issued integration revert record stops or re-applies')
    parts.append('projects that could not be read: %d'%unreadable)
    return ' ('+'; '.join(parts)+')'


def backup_pair_state(root,name):
    """(complete, reason) for one project's last backup pair, read from disk.

    The pair is the native backup directory plus its coordination sidecar, and the
    sidecar's own ``status`` is the record of whether the native sync finished, so
    the state is re-derived from the files rather than assumed from a successful
    call: an absent directory, an absent or still ``pending`` sidecar and an
    unreadable record all mean NOT complete, with the reason to report.
    """
    native=root/'backups'/name
    sidecar=root/'backups'/(name+'.coordination.json')
    if not native.is_dir():return False,'native backup directory is missing'
    # Through read_sidecar_json, never a plain open: a FIFO in place of the sidecar made
    # `backup-status --require-complete` wait until it was killed (kittrial-5bb.152).
    data,problem=read_sidecar_json(sidecar)
    if problem=='it is a symlink':return False,'coordination sidecar must not be a symlink'
    if problem=='it is not a regular file':return False,'coordination sidecar is not a regular file'
    if problem is not None:return False,'coordination sidecar is not readable JSON'
    if data is None:return False,'coordination sidecar is missing'
    if not isinstance(data,dict) or data.get('schema_version')!=1:
        return False,'coordination sidecar has an unexpected schema'
    if data.get('status')!='complete':
        return False,'coordination sidecar status is %r, not complete'%(data.get('status'),)
    return True,None

def backup_status_record(results,scope,generated_at):
    """Build the schema-1 status record for one run (see validate_backup_status)."""
    projects=[]
    for item in results:
        entry={'name':item['name'],'status':item['status']}
        if item.get('degraded'):
            entry['degraded']=item['degraded']
        if item['status']=='complete':
            entry['completed_at']=item['completed_at']
            entry['pair']={'native':'backups/'+item['name'],
                           'coordination':'backups/'+item['name']+'.coordination.json'}
        else:
            entry['reason']=item['reason']
        projects.append(entry)
    return {'schema_version':1,'scope':scope,'generated_at':generated_at,
            'status':'complete' if projects and all(p['status']=='complete' for p in projects) else 'incomplete',
            'projects':projects}

def validate_backup_status(record):
    """Validate a scheduled-backup status record (schema 1).

    The file is the operator's machine-readable statement of which projects have a
    complete backup pair, so it is validated with the same strictness as the
    coordination records and before it is written or trusted. A record that could
    claim completeness it cannot support is refused: an unknown status or scope, a
    ``complete`` entry without a timestamp or without the exact pair paths for its
    own name, a non-complete entry without a reason, a repeated or invalid project
    name, an empty project list, and a run status that disagrees with its entries.
    """
    if not isinstance(record,dict):raise ValueError('Backup status must be an object')
    if record.get('schema_version')!=1:raise ValueError('Backup status schema_version must be 1')
    if record.get('scope') not in ('all','named'):raise ValueError('Backup status scope must be all or named')
    if not utc_timestamp(record.get('generated_at')):
        raise ValueError('Backup status generated_at must be a UTC timestamp')
    projects=record.get('projects')
    if not isinstance(projects,list) or not projects:
        raise ValueError('Backup status must list at least one project')
    seen=set();complete=0
    for entry in projects:
        if not isinstance(entry,dict):raise ValueError('Backup status project entry must be an object')
        name=entry.get('name')
        try:validate_name(name)
        except (TypeError,ValueError):raise ValueError('Backup status project name is invalid: %r'%(name,)) from None
        if name in seen:raise ValueError('Backup status repeats project '+name)
        seen.add(name)
        status=entry.get('status')
        if status not in ('complete','failed','skipped'):
            raise ValueError('Backup status for %s must be complete, failed or skipped'%name)
        if status=='complete':
            complete+=1
            if not utc_timestamp(entry.get('completed_at')):
                raise ValueError('Complete backup status for %s needs a UTC completed_at timestamp'%name)
            expected={'native':'backups/'+name,'coordination':'backups/'+name+'.coordination.json'}
            if entry.get('pair')!=expected:
                raise ValueError('Complete backup status for %s must name the pair %s'%(name,expected))
            if 'reason' in entry:
                raise ValueError('Complete backup status for %s must not carry a failure reason'%name)
            degraded=entry.get('degraded')
            if degraded is not None and (not isinstance(degraded,str) or not degraded.strip()):
                raise ValueError('Degraded backup status for %s needs a nonempty message'%name)
        else:
            reason=entry.get('reason')
            if not isinstance(reason,str) or not reason.strip():
                raise ValueError('Backup status for %s must say why it is not complete'%name)
            if 'completed_at' in entry or 'pair' in entry:
                raise ValueError('Incomplete backup status for %s must not carry a completion'%name)
            if 'degraded' in entry:
                raise ValueError('Incomplete backup status for %s must not carry a degraded message'%name)
    expected='complete' if complete==len(projects) else 'incomplete'
    if record.get('status')!=expected:
        raise ValueError('Backup status run status must be %s for its project entries'%expected)

def write_backup_status(root,record):
    from coordination import atomic
    validate_backup_status(record)
    atomic(root/'backups'/BACKUP_STATUS_NAME,record)

def merged_backup_status(root,record):
    """``record`` merged with the last known state of the projects it did not touch.

    A named or single-project run must not erase the last known state of the other
    projects, so their entries are carried forward from the previous file. `scope` and
    `generated_at` always describe THIS run truthfully, and a carried-forward entry
    keeps the completion time or failure reason from when it was last observed, so the
    merge cannot dress a stale entry up as this run's result. ``--require-complete``
    still refuses a record whose scope is not ``all`` and still re-checks every pair on
    disk, so a merged record cannot pass the completeness gate on a stale entry alone.
    """
    try:
        previous=read_backup_status(root)
    except ValueError:
        previous=None
    if previous is None:return record
    # A project retired since the previous run is not carried forward: its last entry
    # (usually the failure that led to retiring it) would otherwise keep every later
    # record, and the health line built on it, incomplete for good.
    # An entry for a name that is not an initialized project at all (a mistyped name an
    # older kit recorded as skipped, or a directory removed by hand) is dropped for the
    # same reason; the gate checks every initialized project on disk, not this list.
    known=set(initialized_projects(root))
    entries={entry['name']:entry for entry in previous['projects'] if entry['name'] in known}
    for entry in record['projects']:entries[entry['name']]=entry
    merged=dict(record)
    merged['projects']=[entries[name] for name in sorted(entries)]
    complete=sum(1 for entry in merged['projects'] if entry['status']=='complete')
    merged['status']='complete' if complete==len(merged['projects']) else 'incomplete'
    return merged

def failure_reason(error):
    """One-line, size-bounded reason for a failed project, with the native stderr.

    The exception text alone (``Command '[...]' returned non-zero exit status 1.``)
    hides the diagnostic that says WHY the native command refused, so the captured
    stderr (or stdout) is appended when it adds anything. This is the native command's
    own output, never the environment or a command line, so no credential is echoed.
    """
    parts=[' '.join(str(error).split()) or error.__class__.__name__]
    extra=getattr(error,'stderr',None) or getattr(error,'stdout',None)
    if isinstance(extra,bytes):extra=extra.decode('utf-8','replace')
    extra=' '.join(str(extra or '').split())
    if extra and extra not in parts[0]:parts.append(extra)
    return ' '.join(parts)[:400]

def read_backup_status(root):
    path=root/'backups'/BACKUP_STATUS_NAME
    if path.is_symlink():raise ValueError('Backup status file must not be a symlink')
    if not path.is_file():raise ValueError('No backup status file; run a backup first')
    try:record=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('Backup status file is not readable JSON') from None
    validate_backup_status(record)
    return record

def degraded_projects(root,record):
    """``[(name, message)]``: the projects the last run recorded complete but degraded.

    A degraded project has a restorable tracker backup that is missing something it
    should carry (today: a GUIDANCE pair that was mismatched or unreadable when the
    backup ran). It does not fail ``--require-complete`` or the daily timer; it is what
    ``--require-clean`` refuses, and what the summary lines name, so it cannot be missed
    (kittrial-5bb.105). A project retired since the run is left out, as in the gate.
    """
    retired={name for name,_ in retired_entries(root)}-set(initialized_projects(root))
    return [(entry['name'],entry['degraded']) for entry in record['projects']
            if entry.get('degraded') and entry['name'] not in retired]

def restore_degraded_note(root,project,destination):
    """The sentence ``restore-new`` prints when the last run recorded its source degraded.

    Read from the run record itself, whether or not the source project still exists (a
    restore is often of a project that is gone). None when the record is missing or
    unreadable, or does not name the project degraded.
    """
    try:record=read_backup_status(root)
    except (ValueError,OSError):return None
    for entry in record['projects']:
        if entry['name']==project and entry.get('degraded'):
            return ('Note: the last backup run recorded %s degraded, so what it names was not in this backup and '
                    'was not restored into %s: %s'%(project,destination,entry['degraded']))
    return None

def degraded_summary(degraded):
    """'2 degraded (alpha, beta)', or '' when none."""
    if not degraded:return ''
    return '%d degraded (%s)'%(len(degraded),', '.join(name for name,_ in degraded))

def require_complete_problems(root,record):
    """Why the last run does not cover every initialized project with a complete pair.

    This is the gate in front of both the operator's off-machine copy and the
    ``backup-copy`` helper, so it is one implementation. Beyond the run having used
    ``--all`` and every recorded pair still being complete on disk, the record is
    compared with ``initialized_projects(root)``: a project added after the last run is
    absent from the record and is reported by name instead of being silently ignored.
    A project is named at most once (the most concrete reason wins).
    """
    problems=[];seen=set()
    def add(key,text):
        if key in seen:return
        seen.add(key);problems.append(text)
    if record.get('scope')!='all':
        add('scope','the last run was a named run, so it does not cover every initialized project')
    entries={entry['name']:entry for entry in record['projects']}
    live=set(initialized_projects(root))
    retired={name for name,_ in retired_entries(root)}-live
    for entry in record['projects']:
        # A project retired since the run is no longer part of the runtime: the entry the
        # run recorded for it (often the failure that led to retiring it) is not a gap.
        if entry['name'] in retired:continue
        complete,reason=backup_pair_state(root,entry['name'])
        if not complete:add(entry['name'],'%s: %s'%(entry['name'],reason))
    for name in initialized_projects(root):
        entry=entries.get(name)
        if entry is None:
            add(name,'%s: initialized project is absent from the last run record (added after it, or not on '
                    'the schedule); run backup --all'%name)
        elif entry['status']!='complete':
            add(name,'%s: the last run recorded %s, not complete'%(name,entry['status']))
    return problems

def copy_destination_path(value):
    """Validate the operator's off-machine copy destination.

    The copy is a plain mirror an operator can copy back into a runtime's ``backups/``
    directory, so the destination must be an explicit, absolute, non-root path with no
    whitespace: an ambiguous path is refused rather than guessed at.
    """
    path=Path(value).expanduser()
    if not path.is_absolute():raise ValueError('Destination must be an absolute path')
    path=path.resolve()
    if path==Path(path.anchor):raise ValueError('Destination must not be the filesystem root')
    if any(character.isspace() for character in str(path)):
        raise ValueError('Destination path must not contain whitespace')
    return path

def path_is_within(path,directory):
    """Whether ``path`` names ``directory`` itself or a location inside it.

    ``Path.resolve`` collapses 8.3 short names (``C:/Users/RUNNER~1`` on a Windows
    runner), symlinks and ``..`` so one runtime cannot be spelled two ways, and
    ``normcase`` folds the case difference Windows ignores. Comparing the literal
    spelling instead let a destination inside the runtime pass the containment
    refusal whenever the root was spelled with its short name.
    """
    path=Path(path).resolve();directory=Path(directory).resolve()
    return (os.path.normcase(str(path))==os.path.normcase(str(directory))
            or any(os.path.normcase(str(parent))==os.path.normcase(str(directory))
                   for parent in path.parents))

def _replace_with(staged,target):
    """Move ``staged`` onto ``target``, replacing it fully instead of merging.

    A directory is swapped by moving the old one aside first and removing it only after
    the new one is in place, so a failure cannot leave the destination as a mixture of
    two generations; a file is replaced in one step. The temporary ``.previous`` name is
    removed on success and put back if the swap of the new content fails.
    """
    import shutil
    target=Path(target)
    previous=None
    if target.is_symlink() or target.exists():
        previous=target.with_name(target.name+'.previous')
        if previous.is_symlink():
            previous.unlink()
        elif previous.is_dir():
            shutil.rmtree(previous)
        elif previous.exists():
            previous.unlink()
        os.replace(target,previous)
    try:
        os.replace(staged,target)
    except BaseException:
        if previous is not None and not (target.exists() or target.is_symlink()):
            os.replace(previous,target)
        raise
    if previous is not None:
        if previous.is_dir() and not previous.is_symlink():
            shutil.rmtree(previous,ignore_errors=True)
        else:
            try:previous.unlink()
            except OSError:pass

def backup_copy(root,destination,require_clean=False):
    """Reference off-machine copy of every project's last complete backup pair.

    The gate is exactly ``backup-status --require-complete``: every initialized project
    must be in the last run record with a complete pair on disk, or this refuses and
    names what is missing, copying nothing. Each project is written as
    ``<DEST>/<name>`` (its native backup directory), ``<DEST>/<name>.coordination.json``
    (its complete sidecar) and — when the project has one — its operation-journal
    snapshot under the same file name the runtime's ``backups/`` directory uses
    (``<name>`` plus the journal store name), the same shape a runtime's ``backups/``
    directory has, so the copy can be copied back and restored; the operation journal is
    what keeps an acknowledged retry from re-executing after an off-machine restore.
    Each project's pair is copied into a per-run staging directory and only then moved into
    place by rename, so a concurrent ``--all`` cannot yield a mixed native directory beside
    a complete sidecar, a stale destination file is replaced rather than merged, and a
    failure cannot leave a half-refreshed generation. That project's backup lock is held for
    its whole staging (the same lock ``backup`` takes), while the project coordination lock
    is held only long enough to re-check the pair and copy the sidecar plus the journal
    snapshot: the long native ``copytree`` runs after the coordination lock is released, so
    copying a large database cannot block endpoint writes on it. Both commands take the two
    locks in the same order, so no deadlock is possible. Each project's native directory is
    also checked against the manifest its complete sidecar records, before and after that
    project's ``copytree``: a directory that no longer matches — a killed or interrupted run
    can leave it partly rewritten — is refused instead of copied, and a sidecar that records
    no manifest is reported as unverifiable rather than called clean. The completeness record
    that gated the copy is published last; if anything fails, the destination is left without
    it and the command exits non-zero with a clear error. The scheduled, encrypted
    off-machine system, its retention and its encryption stay the operator's: this is a
    generic, credential-free reference the operator can gate and schedule. It reads and
    copies files only; it never touches a unit, timer or schedule.
    """
    import fcntl
    import shutil
    record=read_backup_status(root)
    problems=require_complete_problems(root,record)
    if problems:
        raise SystemExit('backup-copy refused: not every initialized project has a complete backup pair '
                         'on disk: '+'; '.join(problems))
    # A degraded project copies (its tracker backup is restorable), but never silently:
    # each one is named with its message, and --require-clean refuses instead.
    degraded=degraded_projects(root,record)
    if degraded and require_clean:
        raise SystemExit('backup-copy refused (--require-clean): %s. %s'%(
            degraded_summary(degraded),' '.join('%s: %s'%item for item in degraded)))
    for name,message in degraded:
        print('Degraded, copied as it is: %s: %s'%(name,message))
    try:
        destination=copy_destination_path(destination)
    except ValueError as error:
        raise SystemExit('backup-copy refused: '+str(error)) from None
    if path_is_within(destination,root):
        raise SystemExit('backup-copy refused: destination %s is inside the runtime %s; use an off-machine '
                         'location'%(destination,Path(root).resolve()))
    backups=root/'backups'
    destination.mkdir(parents=True,exist_ok=True)
    target_status=destination/BACKUP_STATUS_NAME
    if target_status.is_symlink():
        raise SystemExit('backup-copy refused: %s must not be a symlink'%target_status)
    staging=destination/('.backup-copy-staging-'+secrets.token_hex(8))
    if staging.exists():shutil.rmtree(staging)
    staging.mkdir()
    staged=[];journals=0;copied=[]
    try:
        # Stage every project first, each under its locks, so a copy error touches
        # nothing in the destination.
        retired={name for name,_ in retired_entries(root)}-set(initialized_projects(root))
        for entry in sorted(record['projects'],key=lambda item:item['name']):
            name=entry['name']
            # A project retired since the run is not part of the runtime any more: its
            # recorded entry is neither a gap (the gate) nor something to copy.
            if name in retired:continue
            native=backups/name
            sidecar=backups/(name+'.coordination.json')
            journal=journal_snapshot_path(root,name)
            if native.is_symlink() or sidecar.is_symlink() or journal.is_symlink():
                raise ValueError('Backup pair paths must not be symlinks')
            project=root/'projects'/name
            # This project's backup lock is held for its whole staging; it is the same lock
            # ``backup_project`` takes, so no concurrent backup can rewrite the native
            # directory or the sidecar underneath us. The project's coordination lock is
            # taken only long enough to re-check the pair and copy the two small
            # coordination files - it is what keeps an endpoint keyed write from landing
            # between the sidecar and the journal snapshot that must belong to it - and the
            # long native ``copytree`` then runs AFTER it is released, so copying a large
            # database cannot pin the coordination lock and delay endpoint writes.
            # Lock ORDER is the same in both commands: ``backups/<name>.lock`` is acquired
            # first and the project coordination lock second. ``backup_project`` merely
            # OPENS the coordination lock file first (in its ``with`` header); its ``flock``
            # runs in the body, after ``backup_lock`` is already held. A single consistent
            # order means no deadlock is possible between a backup and a copy.
            staged_journal=None
            with backup_lock(root,name):
                with contextlib.ExitStack() as stack:
                    if project.is_dir():
                        handle=stack.enter_context((project/'.coordination.lock').open('a'))
                        fcntl.flock(handle,fcntl.LOCK_EX)
                    complete,reason=backup_pair_state(root,name)
                    if not complete:
                        raise RuntimeError('%s is no longer a complete pair on disk: %s'%(name,reason))
                    # The complete sidecar records the native directory as its generation
                    # finished. Copying on regardless would propagate a pair whose Dolt and
                    # journal disagree (a killed or interrupted run can leave the native
                    # directory partly rewritten); a sidecar that records no manifest is
                    # reported as unverifiable, never called clean.
                    sidecar_record=complete_sidecar(sidecar)
                    state,detail=native_backup_change(root,name,sidecar_record)
                    if state=='changed':
                        raise SystemExit('backup-copy refused: the native backup of %s no longer matches '
                                         'the manifest its complete sidecar records, so a copy would pair '
                                         'two generations: %s. Take a fresh complete backup of %s (or '
                                         'restore it) before copying.'%(name,detail,name))
                    if state=='unknown':
                        print('Note: the native backup under backups/%s could not be checked against its '
                              'complete sidecar, because %s; this copy cannot claim the directory is the '
                              'one that sidecar belongs to.'%(name,detail))
                    _atomic_copy(sidecar,staging/(name+'.coordination.json'))
                    if journal.is_file():
                        staged_journal=staging/journal.name
                        _atomic_copy(journal,staged_journal)
                        journals+=1
                shutil.copytree(native,staging/name)
                # The long copytree runs with the project's backup lock held, so the kit
                # cannot rewrite the directory underneath it; a direct native writer bypasses
                # that lock, so re-check and refuse rather than move a mixed directory into
                # the destination.
                state,detail=native_backup_change(root,name,sidecar_record)
                if state=='changed':
                    raise SystemExit('backup-copy refused: the native backup of %s changed while it was '
                                     'being copied: %s. No completeness record was published under %s.'%(
                                         name,detail,destination))
            staged.append((name,staging/name,staging/(name+'.coordination.json'),staged_journal))
        # Every project staged: move each pair into place, replacing (not merging) the
        # destination, then publish the completeness record last.
        for name,native_staged,sidecar_staged,journal_staged in staged:
            _replace_with(native_staged,destination/name)
            _replace_with(sidecar_staged,destination/(name+'.coordination.json'))
            if journal_staged is not None:
                _replace_with(journal_staged,destination/journal_staged.name)
            copied.append(name)
            print('Copied %s: %s -> %s'%(name,backups/name,destination/name))
            print('Copied %s: %s -> %s'%(name,backups/(name+'.coordination.json'),
                                         destination/(name+'.coordination.json')))
            if journal_staged is not None:
                print('Copied %s journal snapshot: %s -> %s'%(
                    name,journal_snapshot_path(root,name),destination/journal_staged.name))
        _atomic_copy(backups/BACKUP_STATUS_NAME,staging/BACKUP_STATUS_NAME)
        _replace_with(staging/BACKUP_STATUS_NAME,target_status)
        print('Copied the completeness record: %s -> %s'%(backups/BACKUP_STATUS_NAME,target_status))
    except Exception as error:
        # A copy that did not finish must not leave a destination that looks complete.
        try:
            if target_status.is_file() and not target_status.is_symlink():target_status.unlink()
        except OSError:
            pass
        raise SystemExit('backup-copy failed: %s; no completeness record was published under %s, so the '
                         'destination does not look complete. Re-run after fixing the cause.'%(error,destination))
    finally:
        shutil.rmtree(staging,ignore_errors=True)
    if retired_entries(root):
        print('Retired projects (not initialized, not copied): %s'%', '.join(entry for _,entry in retired_entries(root)))
    print('Copied %d complete project pair(s) and %d operation-journal snapshot(s) of %d initialized to %s.'
          %(len(copied),journals,len(initialized_projects(root)),destination))
    return copied

def backup_projects(root,names,all_projects=False):
    """Back up one or more projects in one run and publish the run's status file.

    ``admin.py backup PROJECT`` keeps its existing output and exit behaviour: the
    native command output is printed and the process exits 0 on success. Several
    names, or ``--all`` (every initialized project in this runtime), are attempted
    in one run, each project's native output is printed in turn, one failing
    project does not stop the others, and the run exits non-zero when any target's
    pair is not complete. The status file is written (and validated) before that
    decision, so a failed or skipped project is recorded NOT complete instead of
    being lost with the process. A project whose GUIDANCE pair is mismatched or
    unreadable is recorded COMPLETE plus ``degraded``: the tracker backup is
    restorable and usable, so it does not fail the run or the daily timer, and the
    actionable repair message is in the entry and on stderr.
    """
    if all_projects:
        targets=initialized_projects(root);scope='all'
        if names:raise ValueError('backup --all already covers every project; do not also name projects')
        if not targets:raise ValueError('No initialized projects in this runtime; add one before backup --all')
    else:
        if not names:raise ValueError('backup needs at least one project, or --all')
        for name in names:validate_name(name)
        targets=sorted(dict.fromkeys(names));scope='named'
        # A name that is not an initialized project is refused BEFORE anything is backed
        # up or recorded (review 01a1026a): a recorded "skipped" entry for a mistyped
        # name was carried forward by every later run, so the --require-complete gate
        # and backup-copy failed for good.
        unknown=[name for name in targets if not (project_dir(root,name)/'.beads'/'metadata.json').is_file()]
        if unknown:
            raise ValueError('Not an initialized project in this runtime: %s. Nothing was backed up or recorded.'
                             %', '.join(unknown))
    generated_at=utc_stamp();results=[];incomplete=[]
    for name in targets:
        path=project_dir(root,name)
        if not (path/'.beads'/'metadata.json').is_file():
            results.append({'name':name,'status':'skipped','reason':'not an initialized project in this runtime'})
            incomplete.append(name);continue
        # One project's failure must not abandon the rest of the run, and the run's
        # status file must still record the truth; the reason is reported and the
        # process exits non-zero below. KeyboardInterrupt/SystemExit still propagate.
        try:
            native=backup_project(root,name)
        except Exception as error:
            results.append({'name':name,'status':'failed','reason':failure_reason(error)})
            incomplete.append(name);continue
        complete,reason=backup_pair_state(root,name)
        if complete:
            entry={'name':name,'status':'complete','completed_at':utc_stamp()}
            _,degraded_fault=guidance_backup_pair(path)
            if degraded_fault:entry['degraded']=degraded_fault
            results.append(entry)
            print(native)
        else:
            results.append({'name':name,'status':'failed','reason':reason})
            incomplete.append(name)
    write_backup_status(root,merged_backup_status(root,backup_status_record(results,scope,generated_at)))
    for entry in results:
        if entry['status']!='complete':
            print('backup %s for %s: %s'%(entry['status'],entry['name'],entry['reason']),file=sys.stderr)
    degraded=[(entry['name'],entry['degraded']) for entry in results if entry.get('degraded')]
    if len(targets)>1:
        # The count of degraded projects is IN the summary line: a run that is complete
        # and degraded used to read exactly like a clean one (kittrial-5bb.105).
        print('Backed up %d of %d project(s)%s; %s is %s.'%(
            len(targets)-len(incomplete),len(targets),
            ', '+degraded_summary(degraded) if degraded else '',root/'backups'/BACKUP_STATUS_NAME,
            'complete' if not incomplete else 'incomplete'))
    elif degraded:
        print('Backup of %s is complete but degraded: %s'%degraded[0])
    if incomplete:
        raise SystemExit('backup incomplete for: '+' '.join(incomplete)+
                         ' (see %s)'%(root/'backups'/BACKUP_STATUS_NAME)+unfinished_creation_hint(root,incomplete))

def unfinished_creation_hint(root,names):
    """What to do when a project that failed its backup is a web creation that did not finish.

    Such a project is initialized (so ``backup --all`` covers it) and has no backup target
    yet, so the run is incomplete and the nightly gate is red until an operator finishes
    or removes it (kittrial-5bb.118 part 2, review 01a109cc). Never raises.
    """
    try:
        import project_creation
        waiting=[record['project'] for record in project_creation.records(root)
                 if record['project'] in names and record['effective'] in ('incomplete',project_creation.STALLED)]
    except (ValueError,OSError):
        return ''
    if not waiting:return ''
    return ('. '+'; '.join('%s is a project creation from the web interface that did not finish: finish it (admin.py '
                           'finish-project %s) or remove it (admin.py remove-creation %s --actor OPERATOR --reason REASON)'
                           %(name,name,name) for name in waiting)+'. Until then every backup --all is incomplete.')

def validate_coordination_operators(value,noun='operators'):
    """Validate the optional operator (or verifier) snapshot carried by a backup sidecar."""
    if value is None:return []
    if not isinstance(value,list):raise ValueError('Coordination backup %s must be a list'%noun)
    from recovery import identity
    allowed=[]
    for item in value:
        try:allowed.append(identity(item,'Invalid %s identity in coordination backup'%noun[:-1]))
        except ValueError:raise ValueError('Invalid %s identity in coordination backup'%noun[:-1]) from None
    return allowed

def coordination_sidecar_source(root,source):
    """(path, record) of the complete coordination sidecar a restore should use.

    The canonical ``backups/<name>.coordination.json`` is preferred. A run that failed
    or was interrupted after writing its ``pending`` marker leaves that marker behind,
    so the durable last-complete copy is used instead: it is what keeps the previous
    restorable pair usable by ``restore-new``. Every path is refused if it is a
    symlink, exactly like the canonical sidecar. Returns ``(None, None)`` when neither
    is complete, so the caller can distinguish a legacy backup from an incomplete one.
    """
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    if bundle.is_symlink():raise ValueError('Coordination backup must not be a symlink')
    data=complete_sidecar(bundle)
    if data is not None:return bundle,data
    fallback=last_complete_sidecar_path(root,source)
    if fallback.is_symlink():raise ValueError('Coordination backup must not be a symlink')
    data=complete_sidecar(fallback)
    if data is not None:return fallback,data
    return None,None

def sidecar_outcome(root,source):
    """What a backup's coordination sidecar allows: ``(outcome, path, record, damaged)``.

    ``outcome`` is ``'answer'`` (``path``/``record`` are the copy ``coordination_sidecar_source``
    chooses), ``'refuse'`` (no copy is usable but at least one exists: ``damaged`` lists each
    with why), or ``'legacy'`` (no copy at all). ``restore-new``, ``coordination_backup`` and
    ``backup-authority`` all decide from this, so they cannot disagree (kittrial-5bb.152).
    ``damaged`` also lists a damaged copy the answer passed over.
    """
    validate_name(source)
    copies=[root/'backups'/(source+'.coordination.json'),last_complete_sidecar_path(root,source)]
    damaged=[(copy,problem) for copy,problem in ((copy,read_sidecar(copy)[1]) for copy in copies) if problem]
    try:
        path,data=coordination_sidecar_source(root,source)
    except ValueError:      # a symlink the restore reaches: no usable copy
        path,data=None,None
    if path is not None:return 'answer',path,data,damaged
    return ('refuse' if damaged else 'legacy'),None,None,damaged

def unusable_sidecar_message(root,source,damaged):
    """The refusal for a backup whose sidecar copies exist but none can be used."""
    named='; '.join('%s: %s'%(copy.relative_to(root).as_posix(),problem) for copy,problem in damaged)
    symlink=' A coordination backup must not be a symlink.' if any(problem=='it is a symlink' for _,problem in damaged) else ''
    return ('Incomplete coordination backup: no copy of the coordination sidecar of backup %s can be used (%s).%s '
            'Recover or reconcile the source first, or restore the native tracker data alone with '
            '`restore-new %s DEST --without-coordination`: its sessions, handoffs, requests, merge context and the '
            'other coordination records, and the operators and verifiers it records, are then not restored.'
            %(source,named,symlink,source))

def resolved_coordination_sidecar(root,source):
    """The complete coordination sidecar for a project, or None when there is none.

    The canonical ``backups/<name>.coordination.json`` is preferred; the durable
    last-complete copy is the fallback (see ``coordination_sidecar_source``).
    """
    return coordination_sidecar_source(root,source)[1]

def coordination_backup(root,source):
    # None only for a truly legacy backup (no sidecar copy at all). A copy that exists but
    # cannot be used refuses, the last-complete copy included: with the canonical copy
    # absent and that copy damaged, the backup used to restore as legacy, silently without
    # any coordination data (kittrial-5bb.152).
    outcome,_,data,damaged=sidecar_outcome(root,source)
    if outcome=='legacy':return None
    if outcome=='refuse':raise ValueError(unusable_sidecar_message(root,source,damaged))
    validate_coordination_files(data.get('files'))
    validate_coordination_operators(data.get('operators'))
    validate_coordination_operators(data.get('verifiers'),'verifiers')
    return data['files']

def coordination_operators(root,source):
    """Operator allowlist snapshot in a project sidecar, or [] when absent.

    Reads the same sidecar ``coordination_backup`` restores (the canonical one, else the
    durable last-complete copy), so the "recorded but not listed here" report and the
    restore itself cannot disagree.
    """
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    if bundle.is_symlink():return []
    data=complete_sidecar(bundle)
    if data is None:data=complete_sidecar(last_complete_sidecar_path(root,source))
    if data is None:return []
    return validate_coordination_operators(data.get('operators'))

def coordination_verifiers(root,source):
    """Verifiers list snapshot in a project sidecar, or [] when absent (see `coordination_operators`)."""
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    if bundle.is_symlink():return []
    data=complete_sidecar(bundle)
    if data is None:data=complete_sidecar(last_complete_sidecar_path(root,source))
    if data is None:return []
    return validate_coordination_operators(data.get('verifiers'),'verifiers')

def authority_change_restore_reason(source,reason):
    """The reason recorded for a name ``restore-new`` re-grants (kittrial-5bb.192 review item 2).

    It always names ``restore-new`` and the source project, so a reader of the trail can tell a
    re-grant from a listing done with ``operators add``; the operator's own sentence follows it.
    The 400-character ceiling is on ``--reason``; the recorded reason also carries this prefix.
    """
    base='restore-new %s'%source
    return ('%s: %s'%(base,reason) if reason else
            '%s re-granted this name from the backup (--restore-operators/--restore-verifiers)'%base)

def _restore_authority_reason(source,reason):
    """Validate ``restore-new --reason`` before anything is restored; ``None`` when it was not given.

    The audit holds the COMPOSED reason (``authority_change_restore_reason``), so the 400-character
    ceiling is checked on that, before the destination exists: the re-grant runs last, and a value
    refused there would fail a restore that had already happened.
    """
    if reason is None:return None
    reason=reason.strip()
    if not reason:raise ValueError('--reason must be a sentence, not blank')
    room=AUTHORITY_CHANGES_REASON_MAX-len('restore-new %s: '%source)
    if len(reason)>room:
        raise ValueError('--reason must be at most %d characters here: the recorded reason also carries '
                         '"restore-new %s: ", so a reader of the authority-changes audit can tell a re-grant '
                         'from an `operators add`'%(room,source))
    return reason

def merge_authority(root,operators=(),verifiers=(),actor=None,reason=None,source=None):
    """Add missing operators and verifiers under ONE wait for the deployment lock.

    Returns ``(added_operators, added_verifiers)``. Additive only; ``restore_authority``
    calls it with exactly the lists the explicit restore flags asked for, so a busy lock
    refuses both at once with nothing changed (kittrial-5bb.144). Every name it re-grants is
    RECORDED in the authority-changes audit, one entry per name, under the same lock and before
    the configuration (kittrial-5bb.192 review item 2): this is the one route in the kit that
    undoes a revocation, so the trail must carry its last word on the name. ``actor`` is the
    ``--actor`` the restore was given (null when it was not) and the reason names ``restore-new``
    and the source. A damaged audit refuses the re-grant because it is an ADD; nothing is changed
    and the caller reports what was not re-granted. A re-grant nobody was named for prints the same
    one stderr sentence the four list commands print (round-2 review item 3), so a bare re-grant is
    never silently unattributed.
    """
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    from recovery import identity
    if isinstance(operators,str):operators=[operators]
    if isinstance(verifiers,str):verifiers=[verifiers]
    wanted_operators=[identity(item,'Invalid operator identity') for item in operators]
    wanted_verifiers=[identity(item,'Invalid verifier identity') for item in verifiers]
    with deployment_config_lock(root):
        cfg=config(root)
        current_operators=stored_operators(cfg)
        current_verifiers=stored_verifiers(cfg)
        added_operators=[item for item in wanted_operators if item not in current_operators]
        added_verifiers=[item for item in wanted_verifiers if item not in current_verifiers]
        if not added_operators and not added_verifiers:return [],[]
        for noun,added in (('operators',added_operators),('verifiers',added_verifiers)):
            for name in added:
                record_authority_change(root,noun,'add',name,actor,
                                        authority_change_restore_reason(source,reason))
        if added_operators:cfg['operators']=current_operators+added_operators
        if added_verifiers:cfg['verifiers']=current_verifiers+added_verifiers
        atomic_private_write(marker,json.dumps(cfg))
    if actor is None or reason is None:
        print(authority_change_notice('restore-new','re-grant',actor,reason),file=sys.stderr)
    return added_operators,added_verifiers

def merge_verifiers(root,actors,actor=None,reason=None):
    """Add missing verifiers to the deployment list; return the added names.

    Additive only, and only ever called by `restore_coordination` when the operator
    explicitly passed `--restore-verifiers`: the list is deployment-wide authority, so
    a backup taken before `verifiers remove ACTOR --confirm-revoke` must not silently
    undo that revocation. Every name it adds is recorded in the authority-changes audit,
    one entry per name, under the same lock and before the configuration, so no route in
    this kit changes a list without an entry (kittrial-5bb.192 review item 2). ``restore-new``
    itself uses ``merge_authority``, because kittrial-5bb.144 requires both lists under ONE wait
    for the lock; this single-list helper stays because it is the writer the lock matrix
    (``tests/test_deployment_config_lock.py``) and the recovery tests (``tests/test_recovery.py``)
    hold directly, and it is what an operator script that merges ONE list uses (round-2 review
    item 4 asks why it stays: that is why).
    """
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    from recovery import identity
    wanted=[identity(item,'Invalid verifier identity') for item in actors]
    with deployment_config_lock(root):
        cfg=config(root)
        current=stored_verifiers(cfg)
        added=[item for item in wanted if item not in current]
        if not added:return []
        for name in added:
            record_authority_change(root,'verifiers','add',name,actor,
                                    reason or 'admin.py merge_verifiers added this name to the capability '
                                              'verifiers list')
        cfg['verifiers']=current+added
        atomic_private_write(marker,json.dumps(cfg))
    return added

def missing_verifiers(root,source):
    """Verifiers recorded in a project backup sidecar that this host does not list."""
    listed=verifiers(root)
    return [item for item in coordination_verifiers(root,source) if item not in listed]

def merge_operators(root,actors,actor=None,reason=None):
    """Add missing operators to the deployment allowlist; return the added names.

    Additive only, and only ever called by `restore_coordination` when the
    operator explicitly passed `--restore-operators`. The deployment allowlist is
    authority for EVERY project, so re-adding an entry from a backup is a
    deployment-wide grant: a backup taken before `operators remove ACTOR
    --confirm-revoke` must not silently undo that revocation. The added names are
    returned so the caller can report exactly what was re-granted, and every one of
    them is recorded in the authority-changes audit, one entry per name, under the
    same lock and before the configuration (kittrial-5bb.192 review item 2). ``restore-new``
    itself uses ``merge_authority``, because kittrial-5bb.144 requires both lists under ONE wait
    for the lock; this single-list helper stays because it is the writer the lock matrix
    (``tests/test_deployment_config_lock.py``) and the recovery tests (``tests/test_recovery.py``)
    hold directly, and it is what an operator script that merges ONE list uses (round-2 review
    item 4 asks why it stays: that is why).
    """
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    from recovery import identity
    if isinstance(actors,str):actors=[actors]
    wanted=[identity(item,'Invalid operator identity') for item in actors]
    with deployment_config_lock(root):
        cfg=config(root)
        current=stored_operators(cfg)
        added=[item for item in wanted if item not in current]
        if not added:return []
        for name in added:
            record_authority_change(root,'operators','add',name,actor,
                                    reason or 'admin.py merge_operators added this name to the operator allowlist')
        cfg['operators']=current+added
        atomic_private_write(marker,json.dumps(cfg))
    return added


def missing_operators(root,source):
    """Operators recorded in a project backup sidecar that this host does not list."""
    snapshot=coordination_operators(root,source)
    listed=operators(root)
    return [item for item in snapshot if item not in listed]


def using_last_complete_sidecar(root,source):
    """True when ``coordination_backup`` had to use the durable last-complete copy.

    ``complete_sidecar`` treats a symlinked or unreadable path as unusable rather than
    raising, so this is a pure report: it decides whether ``restore_coordination``
    should tell the operator which generation it is restoring.
    """
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    fallback=last_complete_sidecar_path(root,source)
    return complete_sidecar(bundle) is None and complete_sidecar(fallback) is not None

def restore_coordination(root,source,destination,restore_operators=False,restore_verifiers=False,authority=True,
                         actor=None,reason=None):
    from coordination import atomic
    path=project_dir(root,destination)
    files=coordination_backup(root,source)
    if files is None:
        print('Legacy backup has no coordination journal. Reconcile outstanding child requests and merge ownership before accepting writes.')
        return False
    from requirement_governance import restored_files as restored_governance_files
    files=restored_governance_files(files,source,destination)
    if using_last_complete_sidecar(root,source):
        print('The canonical coordination sidecar backups/%s.coordination.json is not complete, so this restore '
              'uses the durable last-complete copy %s (the previous complete generation, restored with the '
              'operation-journal snapshot that belongs to it).'
              %(source,last_complete_sidecar_path(root,source)))
    # The sidecar and journal snapshot are one generation by construction; the native
    # directory is not. Report it before any coordination or journal write, so the operator
    # learns here whether the restored Dolt really is the generation the journal describes.
    report_native_backup_change(root,source)
    for name in files:
        target=path/name
        if target.is_symlink() or target.parent.is_symlink() or target.with_suffix('.tmp').is_symlink():raise ValueError('Coordination restore paths must not be symlinks')
    # Validate every requirement receipt before the first write, so a malformed
    # one cannot create a journal directory or a partial restore. The host-issued
    # integration revert journal (kittrial-5bb.52 P1) is validated the same way:
    # a bad entry must refuse the whole restore rather than silently un-revert.
    for name,record in files.items():
        if name.startswith('.requirement-requests/'):
            from requirement_records import validate_receipt
            validate_receipt(record)
        elif name.startswith('.requirement-backfills/'):
            from requirement_records import validate_receipt
            validate_receipt(record,backfill=True)
        elif name.startswith('.integration-reverts/'):
            from review_workflow import validate_revert_journal_entry
            validate_revert_journal_entry(record,name.partition('/')[2])
        elif name.startswith(OWNER_ANSWERS_JOURNAL+'/'):
            from open_items import validate_owner_entry
            validate_owner_entry(record,name.partition('/')[2])
        elif name.startswith(tuple(journal+'/' for journal in RECORD_JOURNALS)):
            validate_record_receipt(name,record)
        elif name=='GUIDANCE.md':
            from guidance import validate_text
            if set(record)!={'text'}:raise ValueError('Invalid guidance backup')
            validate_text(record['text'])
        elif name=='.guidance.json':
            from guidance import validate_meta
            validate_meta(record)
        elif name=='.guidance-clear.json':
            from guidance import validate_clear_record
            validate_clear_record(record)
    # The guidance text and its audit record are one generation: refuse to restore a
    # mismatched pair rather than installing text that cannot be attributed.
    if 'GUIDANCE.md' in files or '.guidance.json' in files:
        if 'GUIDANCE.md' not in files or '.guidance.json' not in files:
            raise ValueError('Invalid guidance backup: the text and its audit record must be restored together')
        from guidance import version_of as guidance_version
        if files['.guidance.json'].get('version')!=guidance_version(files['GUIDANCE.md']['text']):
            raise ValueError('Invalid guidance backup: the audit record does not match the guidance text')
    for name,record in files.items():
        target=project_dir(root,destination)/name
        target.parent.mkdir(exist_ok=True)
        if name.startswith('.handoff-recoveries/'):
            from handoff import validate_recovery
            validate_recovery(record)
            if name!='.handoff-recoveries/'+content_hash({'request_id':record['request_id']})+'.json':raise ValueError('Handoff recovery path mismatch')
        if name=='ONBOARDING.md':
            from onboarding import write_project
            write_project(target,record['text'])
        elif name=='GUIDANCE.md':
            from guidance import write_text as write_guidance_text
            write_guidance_text(target,record['text'])
        elif name=='.guidance.json':
            from guidance import validate_meta
            validate_meta(record)
            atomic(target,record)
        elif name=='.feedback.jsonl':
            temporary=target.with_suffix('.tmp')
            temporary.write_text(record['text'],encoding='utf-8',newline='\n')
            os.replace(temporary,target)
        elif re.fullmatch(r'\.feedback\.jsonl\.(?:[a-f0-9]{16}|[a-f0-9]{64})\.incomplete',name):
            from feedback import validate_quarantine_record
            _atomic_write_bytes(target,validate_quarantine_record(name,record))
        else:atomic(target,record)
    if authority:
        warning=restore_authority(root,source,restore_operators,restore_verifiers,actor,reason)
        if warning:print(warning,file=sys.stderr)
    return True

#: ``restore-new`` exit status when the restore is complete but ``--restore-operators`` or
#: ``--restore-verifiers`` could not re-grant what the backup records (kittrial-5bb.144).
RESTORE_AUTHORITY_NOT_REGRANTED=3

def restore_authority(root,source,restore_operators=False,restore_verifiers=False,actor=None,reason=None):
    """Report, and with the explicit flags re-grant, the deployment authority a backup records.

    ``restore-new`` runs this LAST, after the coordination files and the operation journal
    are in place (kittrial-5bb.142). Both requested lists are merged under ONE wait for the
    deployment lock (kittrial-5bb.144), so a busy lock costs one wait, not one per list, and
    every name the merge re-grants is recorded in the authority-changes audit (kittrial-5bb.192
    review item 2), with ``actor``/``reason`` from the new ``restore-new --actor``/``--reason``.
    A merge refused because another change still holds the lock, or because the audit is damaged
    and a re-grant is an ADD, leaves a completed restore: the warning naming what was not
    re-granted, with the exact commands to re-grant it, is RETURNED (``None`` when there is
    nothing to say) so the caller prints it last and exits ``RESTORE_AUTHORITY_NOT_REGRANTED``.
    """
    # The native and coordination records (original comment plus its void
    # disposition) are restored by the writes above. Operator AUTHORITY is not:
    # the deployment allowlist is authority for every project, so a stale backup
    # must never silently re-grant an operator the deployment has since revoked.
    # Re-adding entries recorded in the backup is an explicit operator decision
    # (`--restore-operators`), and what it re-grants is reported either way.
    missing=missing_operators(root,source)
    if missing and not restore_operators:
        print('NOT restored: the backup records operator allowlist entries this host does not list: '
              + ', '.join(missing) + '. Restoring them would re-grant deployment-wide authority for every project, '
              'so they stay revoked here and void records they authored stay inert. Re-grant one deliberately with '
              '`admin.py --root ROOT operators add ACTOR`, or re-run this restore with --restore-operators to '
              're-establish the whole recorded allowlist.')
    # The verifiers list is the second deployment-wide authority (.60 section 5.2) and
    # follows the same rule: never re-granted by a restore on its own.
    unlisted=missing_verifiers(root,source)
    if unlisted and not restore_verifiers:
        print('NOT restored: the backup records capability verifiers this host does not list: '
              + ', '.join(unlisted) + '. They stay unlisted here, so capability verifications they recorded read '
              '`reported`, not `verified`, and drift only their passes had cleared reappears. Re-grant one '
              'deliberately with `admin.py --root ROOT verifiers add ACTOR`, or re-run this restore with '
              '--restore-verifiers to re-establish the whole recorded list.')
    wanted_operators=missing if restore_operators else []
    wanted_verifiers=unlisted if restore_verifiers else []
    if not wanted_operators and not wanted_verifiers:return None
    try:
        added_operators,added_verifiers=merge_authority(root,wanted_operators,wanted_verifiers,actor,reason,source)
    except DeploymentLockBusy:
        return authority_not_regranted(root,source,wanted_operators,wanted_verifiers,actor,reason)
    except AuthorityAuditDamaged as error:
        # The re-grant is an ADD, and an ADD is refused on a damaged audit. The restore itself
        # is complete, so this is the same shape as a busy lock: report what was not re-granted
        # and exit 3 rather than fail the whole restore at its last step.
        return authority_not_regranted_damaged_audit(root,source,wanted_operators,wanted_verifiers,error,actor,reason)
    if added_operators:
        print('Re-granted operator allowlist entries from the backup (--restore-operators): ' + ', '.join(added_operators))
    if added_verifiers:
        print('Re-granted capability verifiers from the backup (--restore-verifiers): ' + ', '.join(added_verifiers))
    return None

def authority_regrant_commands(root,operators,verifiers,actor=None,reason=None):
    """The exact commands that re-grant what a refused restore could not, with the recording flags.

    A printed ``operators add NAME`` without ``--actor``/``--reason`` would record a null operator
    and print the warning saying so: the remedy would leave the audit less complete than the
    restore tried to (round-2 review item 3). The operator and reason the restore was GIVEN are
    used where it had them, and the literal placeholders ``OPERATOR``/``TEXT`` where it had none;
    every word is shell-quoted as printed. The placeholders are refused when they are run
    unchanged (kittrial-5bb.229 finding 3): these commands are ADDs, and
    ``authority_change_arguments`` raises for ``--actor OPERATOR``/``--reason TEXT`` on an add, so a
    command copied from a warning cannot record an operator named OPERATOR with the entry counted
    as attributed. (On a REMOVAL the same placeholders are not a refusal: the entry is recorded
    without them and a sentence says so - rev-2 item 3.) The warnings that print these commands say
    so.
    """
    import shlex
    flags=' --actor %s'%(shlex.quote(actor) if actor else AUTHORITY_CHANGE_PLACEHOLDER_ACTOR)
    flags+=' --reason %s'%(shlex.quote(reason) if reason else AUTHORITY_CHANGE_PLACEHOLDER_REASON)
    return ['admin.py --root %s %s add %s%s'%(shlex.quote(str(root)),kind,shlex.quote(name),flags)
            for kind,actors in (('operators',operators),('verifiers',verifiers)) for name in actors]

def authority_regrant_replace_note(actor=None,reason=None):
    """The line beside printed re-grant commands whose flags are placeholders (finding 3).

    Empty when the restore was given both flags. It is placed between the commands and the
    ``backup-authority`` line, so the warning still ends with its exit-status sentence.
    """
    missing=[]
    if actor is None:missing.append('%s in --actor'%AUTHORITY_CHANGE_PLACEHOLDER_ACTOR)
    if reason is None:missing.append('%s in --reason'%AUTHORITY_CHANGE_PLACEHOLDER_REASON)
    if not missing:return ''
    return ('Replace %s in the command(s) above before running them: these are ADDs, and a literal placeholder is '
            'refused on an add, so the audit does not count an operator named OPERATOR.\n'%' and '.join(missing))

def authority_not_regranted_damaged_audit(root,source,operators,verifiers,error,actor=None,reason=None):
    """The warning for a restore whose re-grant a DAMAGED authority-changes audit refused.

    The restore is complete; nothing was re-granted and nothing was written to the audit. The
    recovery is the one the audit's own refusal names (move the file aside, then re-grant), so
    this says that instead of the busy-lock remedy of retrying the command.
    """
    import shlex
    named=[]
    if operators:named.append('operators (--restore-operators): '+', '.join(operators))
    if verifiers:named.append('verifiers (--restore-verifiers): '+', '.join(verifiers))
    commands=authority_regrant_commands(root,operators,verifiers,actor,reason)
    return ('WARNING: the restore is complete, but deployment authority the backup records was NOT re-granted: '
            '%s. The authority-changes audit refuses an ADD while it is damaged: %s Do not repeat the restore; '
            'move the damaged file aside, then re-grant them with:\n  %s\n%sCompare what the backup records with '
            'this installation:\n  admin.py --root %s backup-authority %s\nrestore-new exits %d: the restore is '
            'complete, but the authority above was NOT re-granted.'
            %('; '.join(named),error,'\n  '.join(commands),authority_regrant_replace_note(actor,reason),
              shlex.quote(str(root)),shlex.quote(source),RESTORE_AUTHORITY_NOT_REGRANTED))

def authority_not_regranted(root,source,operators,verifiers,actor=None,reason=None):
    """The warning for a restore whose authority merge was refused by a busy lock.

    One block for both lists, ending with the exit status, so it can be printed as the last
    thing the restore says. Every command is shell-quoted as printed.
    """
    import shlex
    named=[]
    if operators:named.append('operators (--restore-operators): '+', '.join(operators))
    if verifiers:named.append('verifiers (--restore-verifiers): '+', '.join(verifiers))
    commands=authority_regrant_commands(root,operators,verifiers,actor,reason)
    # Not the refusal's own text: it says to run the command again, and a second
    # restore-new into this destination is refused because the destination now exists.
    return ('WARNING: the restore is complete, but deployment authority the backup records was NOT re-granted: '
            '%s. Another change to deployment.private.json held its lock (%s) for more than %d s. Do not repeat '
            'the restore; re-grant them with:\n  %s\n%sCompare what the backup records with this installation:\n'
            '  admin.py --root %s backup-authority %s\nrestore-new exits %d: the restore is complete, but the authority '
            'above was NOT re-granted.'
            %('; '.join(named),REVIEW_WRITES_LOCK,DEPLOYMENT_LOCK_WAIT_SECONDS,'\n  '.join(commands),
              authority_regrant_replace_note(actor,reason),shlex.quote(str(root)),shlex.quote(source),
              RESTORE_AUTHORITY_NOT_REGRANTED))

def backup_authority(root,source):
    """Read only: the deployment authority a project backup records against this installation.

    The same complete sidecar ``restore-new`` would use (the canonical one, else the durable
    last-complete copy). For each list: what the backup records, what this installation
    lists now, and the recorded entries it does not list, which ``--restore-operators`` /
    ``--restore-verifiers`` (or ``operators add`` / ``verifiers add``) would re-grant.

    Three cases that once all read as empty lists are told apart (kittrial-5bb.145): a name
    with no backup is refused; a backup whose sidecar copies exist but none is usable is
    refused, naming each copy and why; a backup with no sidecar at all says so in ``note``.
    A copy passed over for the fallback is listed in ``unusable``.
    """
    validate_name(source)
    if not (root/'backups'/source).is_dir():
        raise ValueError('No such backup: backups/%s does not exist. Check the project name '
                         '(`backup-status` lists the backups this installation has).'%source)
    # Exactly what restore-new decides (kittrial-5bb.150/152): the same outcome, from the
    # same copy. A symlinked copy the restore never reaches (the last-complete copy behind a
    # good canonical one) is only listed as unusable.
    outcome,path,data,damaged=sidecar_outcome(root,source)
    if outcome=='refuse':
        raise ValueError('The coordination sidecar of backup %s is damaged: %s. What this backup records '
                         'cannot be read, so no operator or verifier list from it can be trusted.'
                         %(source,'; '.join('%s: %s'%(copy.relative_to(root).as_posix(),problem) for copy,problem in damaged)))
    def compare(recorded,listed):
        return {'recorded':recorded,'listed_here':sorted(listed),
                'not_listed_here':[item for item in recorded if item not in listed]}
    result={'project':source,
            'sidecar':None if path is None else path.relative_to(root).as_posix(),
            'operators':compare([] if data is None else validate_coordination_operators(data.get('operators')),
                                operators(root)),
            'verifiers':compare([] if data is None else validate_coordination_operators(data.get('verifiers'),'verifiers'),
                                verifiers(root))}
    if damaged:
        result['unusable']=[{'copy':copy.relative_to(root).as_posix(),'problem':problem} for copy,problem in damaged]
    if path is None:
        result['note']=('This backup has no coordination sidecar (a legacy backup): it records no operators '
                        'or verifiers.')
    return result

def record_store_path(state):
    """The HTTP record store beside the service state document (``http_auth.Store``)."""
    from http_auth import RECORD_STORE_SUFFIX
    state=Path(state)
    return state.with_name(state.name+RECORD_STORE_SUFFIX)

def existing_record_store(state):
    """Open the EXISTING record store beside the EXISTING service state document.

    ``http_auth.RecordStore`` is the running service's write path: its constructor runs
    ``_ensure()``, which creates the parent directory and the SQLite file. That is exactly
    the wrong failure mode for an operator command - a typo in ``--state`` fabricates an
    empty store beside it, reports a successful reset (rc=0) and leaves the real store
    pinned, so the operator sees success and no effect. This guard therefore requires the
    state document AND its sidecar to already exist, and proves the sidecar really is a
    record store by opening it read-only (``mode=ro`` cannot create a missing file) before
    the service's own open path runs. Nothing is created here; ``ValueError`` names the
    missing path.
    """
    import sqlite3
    from http_auth import RecordStore
    document=Path(state)
    path=record_store_path(document)
    if not document.is_file():
        raise ValueError('No HTTP service state document at '+str(document)+': refusing to '
                         'create one. Point --state at the running service\'s real --state path.')
    if not path.is_file():
        raise ValueError('No record store at '+str(path)+': refusing to create one. The store '
                         'is created by the HTTP service itself; check --state.')
    try:
        probe=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    except sqlite3.Error as error:
        raise ValueError('Cannot read the record store at '+str(path)+' (not a SQLite store): '
                         +str(error))
    try:
        tables={row[0] for row in probe.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as error:
        raise ValueError('Cannot read the record store at '+str(path)+': '+str(error))
    finally:
        probe.close()
    if 'records' not in tables:
        raise ValueError('The file at '+str(path)+' is not an HTTP record store (no records '
                         'table): refusing to create one over it.')
    return RecordStore(path),document,path

def record_store_reset(state):
    """Operator recovery for the service record store's monotone auth clock.

    Auth expiry is ``max(raw now, high_water)``; after a forward jump that is later
    corrected, ``high_water`` stays ahead until the raw clock passes it, so every
    session, credential and reset value issued in that period is stamped on the pinned
    timeline and lives late. This sets the floor to the corrected clock
    (``http_auth.RecordStore.reset_high_water``) and keeps ``jump_credit``, so records
    already ageing on the confirmed timeline keep their real expiry. The matching
    operation-journal command is ``admin.py journal <PROJECT> --reset-high-water``.
    The state document and its record store must already exist; a missing path is
    refused instead of being created (``existing_record_store``).
    """
    store,document,path=existing_record_store(state)
    report={'state':str(document),'record_store':str(path),
            'high_water':store.reset_high_water()}
    report['stats']=store.stats()
    return report

def record_store_stats(state):
    """Inspection: the EXISTING store's clock state and record counts, no clock change.

    The same guard as the reset path, so an inspection cannot create a store either.
    """
    store,document,path=existing_record_store(state)
    return {'state':str(document),'record_store':str(path),'stats':store.stats()}

# ---------------------------------------------------------------------------
# Confined contributor keys (kittrial-5bb.89).
#
# The endpoint takes its HTTP authority from its own launch flags and enforces every
# authority rule in the kit, so those rules bind only a caller who cannot choose the
# remote command. `ssh_forced_command.py` is the authorized_keys `command=` wrapper that
# makes that true for a contributor key; this helper prints the exact lines to install.
# ---------------------------------------------------------------------------

# One plain public key line is accepted; anything else (options, a private key, several
# keys) is refused rather than concatenated into a line nobody can audit.
AUTHORIZED_KEY_TYPES=('ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256','ecdsa-sha2-nistp384',
                      'ecdsa-sha2-nistp521','sk-ssh-ed25519@openssh.com',
                      'sk-ecdsa-sha2-nistp256@openssh.com','ssh-dss')
# `restrict` comes first: OpenSSH documents it as switching off pty, port forwarding, agent
# forwarding, X11 forwarding *and* the user rc file (~/.ssh/rc), so a capability OpenSSH adds
# later is off for this key by default rather than granted until someone edits this list. The
# four explicit options follow so a stock sshd that predates `restrict` still gets them, and
# so a reader sees exactly what the entry closes.
CONTRIBUTOR_KEY_OPTIONS=('restrict','no-pty','no-port-forwarding','no-agent-forwarding',
                         'no-X11-forwarding')
# The character class --root, the kit directory and --python must all satisfy: sshd hands
# `command=` to the account shell, so anything a shell would expand (`$`, backtick, `;`, `|`,
# `&`, `(`, `)`), any whitespace and any quote must never reach the printed line. A bare
# interpreter name (`python3`) or an absolute path are the only two accepted shapes.
AUTHORIZED_KEY_PATH=re.compile(r'/[A-Za-z0-9_./-]+')
AUTHORIZED_KEY_PYTHON=re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*|/[A-Za-z0-9_./-]+)')
# The flags the contributor line runs the interpreter with: -E ignores PYTHON* environment
# variables and -s drops the user site directory. Not -I, which also removes the script's own
# directory from sys.path and would stop the kit's modules importing each other.
AUTHORIZED_KEY_PYTHON_FLAGS=('-E','-s')
OPERATOR_KEY_NOTE=('Unrestricted service-account shell access: this key can run admin.py, bd '
                   'and anything else the account can. It is deliberately not confined. '
                   'Grant it only to an allowlisted operator.')

def public_key_line(text,source='key file'):
    """The one plain public-key line in `text` as (type, base64 body, comment).

    A line that already carries authorized_keys options, a private key, a second key line
    or no key at all is refused: the helper must never nest a `command=`, grant more than
    the one key the operator read, or silently ignore a key the operator did not see.
    """
    found=None
    for raw in str(text).splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):continue
        if 'PRIVATE KEY' in line:
            raise ValueError('%s: that is a private key; install only its .pub public key'%source)
        parts=line.split()
        if len(parts)<2 or parts[0] not in AUTHORIZED_KEY_TYPES:
            raise ValueError('%s: expected one plain public key line (<type> <base64> [comment]); '
                             'remove any authorized_keys options and pass exactly one key'%source)
        if found is not None:
            raise ValueError('%s: expected exactly one public key line but found a second one; '
                             'pass one key per entry, one entry per key'%source)
        try:payload=base64.b64decode(parts[1],validate=True)
        except Exception:
            raise ValueError('%s: the key body is not valid base64'%source) from None
        if not payload:
            raise ValueError('%s: the key body is empty'%source)
        found=(parts[0],parts[1],' '.join(parts[2:]))
    if found is None:
        raise ValueError('%s: no public key line found'%source)
    return found

def _authorized_key_path(value,label):
    text=str(value)
    if not AUTHORIZED_KEY_PATH.fullmatch(text):
        raise ValueError('%s must be an absolute Linux path without spaces or quotes to be '
                         'usable inside an authorized_keys command='%label)
    return text

def default_authorized_key_python():
    """The interpreter the contributor line runs: this interpreter, else `/usr/bin/python3`.

    Absolute on purpose. A bare `python3` is resolved by the account shell through PATH,
    which PermitUserEnvironment or an AcceptEnv forwarding the caller's PATH can move, so
    the printed line names the interpreter the deployment actually tested. Inside an
    office installation the path goes through `install/current`, so an upgrade moves the
    line with the service instead of leaving it on the release that printed it
    (kittrial-5bb.182).
    """
    executable=getattr(sys,'executable','') or ''
    if executable.startswith('/') and AUTHORIZED_KEY_PYTHON.fullmatch(executable):
        return str(install_current_path(executable))
    return '/usr/bin/python3'

def _authorized_key_python(value):
    """One bare interpreter name (`python3`) or absolute path - never a name plus flags.

    The same character class as --root and the kit directory. sshd hands `command=` to the
    account shell, so `--python '$(touch${IFS}/tmp/canary)python3'` would run the substitution
    on every connection; a value outside this class is refused rather than printed.
    """
    text=str(value)
    if not AUTHORIZED_KEY_PYTHON.fullmatch(text):
        raise ValueError('--python must be one interpreter name or absolute path using only '
                         'letters, digits, dot, underscore, dash and slash, to be usable '
                         'inside an authorized_keys command=')
    return text

def _authorized_key_comment(comment):
    text=str(comment)
    if any(character in text for character in '\0\r\n'):
        raise ValueError('--comment must be one line without control characters')
    return text.strip()

#: What a bound line's comment carries, so that authorized_keys can be read by eye.
KEY_PROJECTS_COMMENT='orchestra-projects='
#: The principal a bound line names, in the same comment (kittrial-5bb.194).
KEY_PRINCIPAL_COMMENT='orchestra-principal='

def key_projects(root,names):
    """The projects a key is to be bound to, checked: names of this runtime's projects, none twice.

    A name that is no project would bind the key to nothing, and a typo is the likely
    reason, so it is refused here and not printed (kittrial-5bb.193).
    """
    projects=[]
    for name in names or []:
        validate_name(name)
        if name in projects:raise ValueError('--project names %s twice'%name)
        if not (project_dir(Path(root),name)/'.beads/metadata.json').is_file():
            raise ValueError('--project %s: no such project in this runtime. A key bound to a name that is not '
                             'a project reaches nothing; create the project first, or check the name'%name)
        projects.append(name)
    return projects

def key_principal(value):
    """The principal a key is to name, checked (kittrial-5bb.194, rule 2).

    A lane, spelled `lane:NAME` or `person:NAME` (the coordinator decision of 2026-10-08:
    the two prefixes are different principals). One token with no space because the value is
    an argument of the authorized_keys command line. ``--principal`` is an append argument,
    so a value given twice is refused rather than silently taking the last.
    """
    if isinstance(value,list):
        if len(value)>1:
            raise ValueError('--principal names %s twice; a key may name at most one principal'
                             %', '.join(str(item) for item in value))
        value=value[0] if value else None
    if value is None:return None
    from sessions import valid_principal
    return valid_principal(value,'--principal')

def authorized_key_lines(root,kit,key_type,key_body,key_comment='',comment=None,python=None,projects=(),principal=None):
    """The exact contributor (confined) and operator (unrestricted) authorized_keys lines.

    The contributor line runs `ssh_forced_command.py` - under an absolute interpreter with
    `-E -s` - with the deployment's fixed root and the endpoint path, plus the options that
    close the interactive, forwarding, agent, X11 and user-rc paths. The operator line is the
    bare key: an operator needs the service account's shell for the host commands, and
    pretending otherwise would be a false guarantee.
    """
    root=_authorized_key_path(root,'--root')
    kit=_authorized_key_path(kit,'the kit directory')
    python=_authorized_key_python(default_authorized_key_python() if python is None else python)
    endpoint=kit+'/endpoint.py'
    wrapper=kit+'/ssh_forced_command.py'
    text=_authorized_key_comment(comment) if comment is not None else key_comment
    if any(character in text for character in '\0\r\n'):
        raise ValueError('the key comment must be one line without control characters')
    key=' '.join(part for part in (key_type,key_body,text) if part)
    bound=tuple(part for name in projects for part in ('--project',name))
    if principal is not None:
        bound=bound+('--principal',principal)
    command=' '.join((python,)+AUTHORIZED_KEY_PYTHON_FLAGS+(wrapper,'--root',root,
                                                           '--endpoint',endpoint)+bound)
    # The bound line says in its comment what it is bound to; the operator line stays the bare key.
    marks=[]
    if projects:marks.append(KEY_PROJECTS_COMMENT+','.join(projects))
    if principal is not None:marks.append(KEY_PRINCIPAL_COMMENT+principal)
    bound_key=' '.join(part for part in (key_type,key_body,text,*marks) if part) if marks else key
    contributor='command="%s",%s %s'%(command,','.join(CONTRIBUTOR_KEY_OPTIONS),bound_key)
    return {'root':root,'kit':kit,'endpoint':endpoint,'wrapper':wrapper,'python':python,
            'python_flags':list(AUTHORIZED_KEY_PYTHON_FLAGS),
            'contributor_options':list(CONTRIBUTOR_KEY_OPTIONS),
            'contributor':contributor,'operator':key}

def _key_line_options(line):
    """Split one authorized_keys line into (options text, the rest), as sshd reads it.

    Options end at the first blank outside double quotes; inside quotes a backslash keeps
    the next quote. A line that begins with a key type has no options.
    """
    first=line.split(None,1)[0]
    if first in AUTHORIZED_KEY_TYPES:return '',line
    quoted=False;index=0
    while index<len(line):
        character=line[index]
        if character=='\\' and quoted and index+1<len(line) and line[index+1]=='"':index+=2;continue
        if character=='"':quoted=not quoted
        elif character in ' \t' and not quoted:break
        index+=1
    if quoted:raise ValueError('a quote is not closed')
    return line[:index],line[index:].strip()

def _key_line_command(options):
    """The value of ``command=`` among a line's options, or None."""
    parts=[];current='';quoted=False;index=0
    while index<len(options):
        character=options[index]
        if character=='\\' and quoted and index+1<len(options) and options[index+1]=='"':
            current+='"';index+=2;continue
        if character=='"':quoted=not quoted
        elif character==',' and not quoted:parts.append(current);current='';index+=1;continue
        else:current+=character
        index+=1
    parts.append(current)
    for part in parts:
        name,equals,value=part.partition('=')
        if equals and name.strip().lower()=='command':return value
    return None

def key_line(line,root,kit):
    """What one line of authorized_keys is, for this runtime and this kit; None for a blank or # line.

    Read only. ``kind`` is ``bound`` (this kit's forced command with projects), ``confined``
    (the forced command with none: any project), ``unrestricted`` (no command: the account's
    shell), ``other-command`` (a command that is not the kit's wrapper; said, not judged) or
    ``unreadable``.
    """
    import shlex
    text=line.strip()
    if not text or text.startswith('#'):return None
    try:
        options,rest=_key_line_options(text)
        parts=rest.split(None,2)
        if len(parts)<2 or parts[0] not in AUTHORIZED_KEY_TYPES:raise ValueError('no public key after the options')
        body=base64.b64decode(parts[1],validate=True)
        if not body:raise ValueError('the key body is empty')
    except Exception as error:
        return {'kind':'unreadable','reason':str(error)[:120]}
    entry={'key_type':parts[0],
           'fingerprint':'SHA256:'+base64.b64encode(hashlib.sha256(body).digest()).decode('ascii').rstrip('='),
           'comment':parts[2] if len(parts)>2 else ''}
    command=_key_line_command(options)
    if command is None:
        entry['kind']='unrestricted'
        return entry
    try:tokens=shlex.split(command)
    except ValueError:tokens=[]
    at=next((index for index,token in enumerate(tokens) if token.rsplit('/',1)[-1]=='ssh_forced_command.py'),None)
    if at is None:
        entry['kind']='other-command'
        return entry
    wrapper=tokens[at];values={'--root':[],'--endpoint':[],'--python':[],'--project':[],'--principal':[]};unknown=[]
    arguments=tokens[at+1:];index=0
    while index<len(arguments):
        flag,equals,joined=arguments[index].partition('=')
        if flag in values and equals:values[flag].append(joined)
        elif flag in values and index+1<len(arguments) and not arguments[index+1].startswith('-'):
            index+=1;values[flag].append(arguments[index])
        else:unknown.append(arguments[index])
        index+=1
    here=Path(os.path.realpath(str(kit)))
    endpoints=values['--endpoint'] or [wrapper.rsplit('/',1)[0]+'/endpoint.py']
    line_root=values['--root'][0] if values['--root'] else None
    principals=values['--principal']
    entry.update({
        # A line bound to a principal is bound, not merely confined (kittrial-5bb.194 review,
        # finding 3): the summary counts the binding and the notes name both kinds.
        'kind':'bound' if (values['--project'] or principals) else 'confined',
        'projects':values['--project'],
        # Slice 2 of the design adds --principal; the field is here so that the shape of the
        # listing does not change with it.
        'principal':principals[0] if principals else None,
        'root':line_root,'wrapper':wrapper,'endpoints':endpoints,
        # A line names its wrapper and endpoint by path. One printed by an earlier release of an
        # office installation still runs THAT release's kit, which knows none of the rules of
        # this one, for as long as its folder is there (the design, Migration, step 6).
        'other_kit':(Path(os.path.realpath(wrapper))!=here/'ssh_forced_command.py'
                     or any(Path(os.path.realpath(endpoint))!=here/'endpoint.py' for endpoint in endpoints)),
        'other_root':line_root is None or Path(os.path.realpath(line_root))!=Path(os.path.realpath(str(root))),
        'missing':not Path(wrapper).is_file(),
        'unknown_arguments':unknown})
    # It is this kit today and names its release folder: after the next upgrade it is another kit.
    link=install_current_link(here)
    entry['names_release']=bool(link is not None and not entry['other_kit']
                                and not all(str(path).startswith(str(link)+'/') for path in [wrapper,*endpoints]))
    if not entry['other_root']:
        entry['unknown_projects']=[name for name in values['--project']
                                   if not re.fullmatch(r'[a-z][a-z0-9]{1,23}',name)
                                   or not (Path(root)/'projects'/name/'.beads/metadata.json').is_file()]
    if principals:
        # A repeated principal would be served as the last one while the listing showed the
        # first; an ill-formed one refuses every request of that key. Both go under attention.
        from sessions import PRINCIPAL as PRINCIPAL_FORM
        entry['principal_repeated']=len(principals)>1
        entry['principal_ill_formed']=any(not isinstance(item,str) or not PRINCIPAL_FORM.fullmatch(item)
                                          for item in principals)
    if values['--project'] and len(values['--project'])!=len(set(values['--project'])):
        # A project named twice (``--project pa --project pa``) is refused by the wrapper, as
        # a repeated principal is; the listing must say so too (kittrial-5bb.223, finding 6).
        entry['project_repeated']=True
    return entry

def authorized_keys_listing(root,file=None):
    """Every line of an authorized_keys file and what it may do here. Reads; never writes.

    The kit cannot audit sshd's file, only read it (kittrial-5bb.193): which keys have the
    account's shell, which are confined to the endpoint, which are bound to projects, and
    which point at a kit other than the installed one.
    """
    path=Path(file) if file else Path.home()/'.ssh'/'authorized_keys'
    kit=Path(__file__).resolve().parent
    try:text=path.read_text(encoding='utf-8',errors='replace')
    except OSError as error:
        raise ValueError('Cannot read %s: %s'%(path,error.strerror or error)) from None
    lines=[];summary={}
    for number,raw in enumerate(text.splitlines(),1):
        entry=key_line(raw,root,kit)
        if entry is None:continue
        lines.append({'line':number,**entry})
        summary[entry['kind']]=summary.get(entry['kind'],0)+1
        # Both kinds of binding are counted and named (kittrial-5bb.194 review, finding 3):
        # `bound` counts every bound line, and `principal-bound` names the principal binding
        # on its own. The key appears only when such a line exists.
        if entry.get('principal') and not str(entry['principal']).startswith('-'):
            summary['principal-bound']=summary.get('principal-bound',0)+1
    attention=[entry['line'] for entry in lines
               if entry.get('other_kit') or entry.get('missing') or entry.get('unknown_arguments')
               or entry.get('unknown_projects') or entry.get('names_release') or entry['kind']=='unreadable'
               or entry.get('principal_repeated') or entry.get('principal_ill_formed')
               or entry.get('project_repeated')]
    return {'schema_version':1,'file':str(path),'root':str(root),'kit':str(kit),'lines':lines,'summary':summary,
            'attention':attention,
            'notes':['unrestricted: the key has this account\'s shell and is outside every rule of the kit, on every project.',
                     'confined: the key runs only the endpoint and may name any project and any actor.',
                     'bound: the key runs only the endpoint and only for its projects and/or its principal - the '
                     '`projects` and `principal` fields say which. A principal-bound line is `bound`, not `confined`.',
                     'principal-bound: counted separately for the lines that name a principal, so a line bound only to '
                     'a principal is not read as merely confined.',
                     'principal_ill_formed: the line names a principal that is not `lane:NAME` or `person:NAME`; the '
                     'wrapper refuses every request of that key. principal_repeated: the line names more than one '
                     'principal (the wrapper refuses it). Both are under attention and must be reprinted.',
                     'project_repeated: the line names the same project twice (`--project pa --project pa`); the '
                     'wrapper refuses the line. It is under attention and must be reprinted.',
                     'other_kit: the line runs a wrapper or an endpoint that is not this kit\'s file. Such a line is '
                     'served by that other kit, whatever its text says: a kit older than this one binds nothing. '
                     'Print the line again with this kit (authorized-keys) and replace it.',
                     'names_release: the line is this kit today but names its release folder, so after the next '
                     'upgrade it is other_kit. Print it again; it then goes through install/current.',
                     'other_root: the line serves another runtime than --root (or names none); its projects were not looked up here.',
                     'This command reads the file and changes nothing.']}

def authorized_keys(root,key_file,role='both',python=None,comment=None,projects=None,principal=None):
    """Print the installable lines for one public key as JSON (see authorized_key_lines).

    The kit directory is taken through the installation's `install/current` link where it
    has one: a forced command that names `releases/<ID>` keeps running the old kit after an
    upgrade while the service runs the new one, and its exact `--endpoint` string is what
    the wrapper compares the caller's config against (kittrial-5bb.182).
    """
    kit=install_current_path(Path(__file__).resolve().parent)
    for name in ('ssh_forced_command.py','endpoint.py'):
        if not (kit/name).is_file():
            raise ValueError('This kit copy has no %s; run the helper from the installed kit directory'%name)
    projects=key_projects(root,projects)
    principal=key_principal(principal)
    if (projects or principal is not None) and role=='operator':
        raise ValueError('--project and --principal bind the confined contributor line; an unrestricted operator '
                         'key has a shell and cannot be bound to projects or to a principal')
    path=Path(key_file)
    lines=authorized_key_lines(root,kit,*public_key_line(path.read_text(encoding='utf-8-sig'),str(path)),
                               comment=comment,python=python,projects=projects,principal=principal)
    payload={'schema_version':1,'root':lines['root'],'kit':lines['kit'],'endpoint':lines['endpoint'],
             'wrapper':lines['wrapper'],'python':lines['python'],
             'contributor_options':lines['contributor_options'],
             'operator_note':OPERATOR_KEY_NOTE,
             'notes':['The contributor line needs "forced_command": true in that contributor\'s '
                      'client config; without it the client sends a --root the wrapper refuses.',
                      'The confined key and that client flag are a coupled pair per contributor: '
                      'the flag without the confined entry makes the connection fail '
                      '(Permission denied 126, the account shell cannot execute the path).',
                      'Verify the operator key in a second SSH session before closing the one '
                      'used to edit authorized_keys, so a bad edit cannot lock everyone out.',
                      'Confinement binds the key to the endpoint, not to an actor: a confined key '
                      'still self-declares its actor on every request, and what it protects is the '
                      'operator-gated and reserved operations, not the actor name.',
                      'Install one entry per key: both lines are alternatives for different keys, '
                      'never two entries for the same key.',
                      'The endpoint in the contributor line goes through the installation\'s '
                      'install/current link where it has one. Put that exact path in the contributor\'s '
                      'client config: the wrapper compares it as one token, and a releases/<ID> spelling '
                      'would keep that key on the release that printed it.']}
    if projects or principal is not None:
        # Rules 1 and 2 of docs/COORDINATORS_PER_PROJECT_DESIGN.md. Only the bound line is
        # printed: the operator line is a shell, and a shell is every project.
        role='contributor'
        if projects:
            payload['projects']=projects
            payload['notes'].append(
                'This line is bound to the projects above: the endpoint refuses every request of this key '
                'that names another project, with the answer it gives for a project that does not exist. '
                'The binding is the --project arguments of the line; the comment only repeats them.')
        if principal is not None:
            payload['principal']=principal
            payload['notes'].append(
                'This line is bound to the principal above (a lane): the endpoint refuses every request of '
                'this key whose actor the project\'s registry does not give to that principal. Registering '
                'a new session under this key makes the new actor that principal\'s. The binding is the '
                '--principal argument of the line; the comment only repeats it. A lane is spelled lane:NAME '
                '(or person:NAME, which is a different principal).')
            payload['notes'].append(
                'A bound key owns no actors in the project before its first registration, so its first kit '
                'call must be session register. No other command (such as docs or ready) can precede '
                'registration or be used as a check of the key: client.py requires --actor, and the '
                'endpoint\'s principal gate refuses any actor until registered. To verify the key without '
                'registering (or without making an actor), check the line on the server with admin.py '
                '--root RUNTIME authorized-keys-list, or test client SSH access with plain ssh HOST exit or '
                'ssh -T HOST (refused on stderr with status 2 by the forced command wrapper, confirming '
                'connection and confinement).')
        bound_note='--project or --principal' if principal is not None else '--project'
        payload['notes'].extend([
            'The operator line is not printed with %s: an unrestricted key has the account\'s shell and '
            'cannot be bound. A key that already has an unrestricted or an unbound line in authorized_keys '
            'is not bound by adding this one: replace that line.'%bound_note,
            'admin.py --root RUNTIME authorized-keys-list shows every line of authorized_keys, what it is '
            'bound to and whether it still points at the installed kit.'])
    if role in ('contributor','both'):payload['contributor']=lines['contributor']
    if role in ('operator','both'):payload['operator']=lines['operator']
    print(json.dumps(payload,ensure_ascii=True,indent=2))
    if role in ('operator','both'):
        print('warning: the operator line is unrestricted service-account shell access; '+OPERATOR_KEY_NOTE,
              file=sys.stderr)

#: The adoption audit (kittrial-5bb.194, rule 2): every give-an-actor-to-a-principal a host
#: command made. The registry's owners map is written by the server (session register under a
#: bound key) and by this host command; this audit says who ran the host command, when and
#: why. Runtime-level, beside deployment.private.json, and never part of a project's
#: coordination backup.
ACTOR_ADOPTIONS_AUDIT='actor-adoptions.audit.json'
ACTOR_ADOPTIONS_SCHEMA=1
#: A short history, like review-writes.audit.json: the audit answers "who adopted whom, when,
#: and why", not "every adoption since the installation was made".
ACTOR_ADOPTIONS_MAX=200
ACTOR_ADOPTIONS_FIELDS=frozenset({'at','operator','project','actor','principal','previous','reason'})
#: A MOVE (the actor already belonged to another principal, named with --from) is marked in
#: the entry. Entries written before the marker existed are still read, so the audit stays
#: forward-compatible with this kit's own history.
ACTOR_ADOPTIONS_MOVE='moved'
ACTOR_ADOPTIONS_FIELDS_WITH_MOVE=ACTOR_ADOPTIONS_FIELDS|{ACTOR_ADOPTIONS_MOVE}

def adoption_entry(item):
    """Whether one entry has the shape ``adopt-actor`` writes (with or without the move marker)."""
    return (isinstance(item,dict) and set(item) in (ACTOR_ADOPTIONS_FIELDS,ACTOR_ADOPTIONS_FIELDS_WITH_MOVE)
            and all(isinstance(item[field],str) and item[field] for field in
                    ('at','operator','project','actor','principal','reason'))
            and (item['previous'] is None or isinstance(item['previous'],str))
            and (ACTOR_ADOPTIONS_MOVE not in item or isinstance(item[ACTOR_ADOPTIONS_MOVE],bool)))

def actor_adoptions(root,project=None):
    """The recorded adoptions, oldest first, for one project or all of them. Reads only.

    An absent file is an empty history. A file this kit cannot read as its own history is a
    refusal, not an empty history: a caller must not be told "nobody was adopted" by a
    damaged audit.
    """
    path=root/ACTOR_ADOPTIONS_AUDIT
    if not path.is_file():return []
    try:record=record_json.loads(path.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError) as error:
        raise ValueError('The actor-adoptions audit %s cannot be read: %s'%(path,error)) from None
    entries=record.get('entries') if isinstance(record,dict) and record.get('schema_version')==ACTOR_ADOPTIONS_SCHEMA else None
    if not isinstance(entries,list) or any(not adoption_entry(item) for item in entries):
        raise ValueError('The actor-adoptions audit %s is not the history this kit writes; nothing was changed'%path)
    return [item for item in entries if project is None or item['project']==project]

def principal_conflicts(root,actor,principal,exclude):
    """Other projects' owners maps that give ``actor`` to a principal other than ``principal``.

    Read before an adoption. The design (rule 2) keeps one operator list for the whole
    installation, so a name on that list must mean the same principal everywhere; a name not
    on the list may differ from project to project. A registry that cannot be read is a whole
    refusal, never "no conflict".
    """
    from sessions import owners
    found=[]
    projects=root/'projects'
    if not projects.is_dir():return found
    for entry in sorted(projects.iterdir()):
        if not entry.is_dir() or entry.name==exclude or not (entry/'.sessions.json').is_file():continue
        try:other=owners(entry).get(actor)
        except ValueError as error:
            raise ValueError('Cannot read the session registry of project %s while adopting %s: %s'
                             %(entry.name,actor,error)) from None
        if other is not None and other!=principal:found.append((entry.name,other))
    return found

def project_actor_names(root,project,path):
    """The actor names the project already holds: session registrations, owners-map keys and
    the names its tracker rows use.

    Read before an adoption so that a name that appears nowhere in the project is refused
    rather than adopted into a fictitious owner (kittrial-5bb.194 review, finding 10). The
    tracker is read because the actors this command exists for are the legacy ones: they
    predate the registry and are visible only in the rows they wrote. A tracker that cannot
    be read is a refusal, never "the name is unknown". Rows are parsed with the row bound
    of kittrial-5bb.141 (``record_json.loads_rows``, ``ROW_NESTING_MAX`` 750): rows nested
    up to 750 levels read normally, and ANY row that cannot be parsed -- unparseable text,
    deeper than 750, a line cut short -- refuses the whole command as a tracker that could
    not be read, exactly as before (kittrial-5bb.221 revision 2): the unreadable row may be
    the one that names the actor, and a name that cannot be shown to be held must not be
    adopted either.
    """
    from sessions import read_registry, owner_map, used_actors
    data=read_registry(path)
    names={record['actor'] for record in data['records'].values()}|set(owner_map(data))
    try:
        rows=record_json.loads_rows(run_bd(root,project,['export','--all']))
    except (subprocess.SubprocessError,OSError,ValueError,RecursionError):
        raise ValueError('Cannot read the tracker of project %s to check whether that actor exists there; '
                         'nothing was changed'%project) from None
    if any(not isinstance(row,dict) or row.get('malformed') for row in rows):
        raise ValueError('Cannot read the tracker of project %s: it holds unreadable row(s) (nested deeper '
                         'than the row bound or not parseable), so the names it holds cannot be told; '
                         'nothing was changed'%project)
    names|=used_actors(rows)
    return names

def adopt_actor(root,project,actor,principal,operator,reason,from_principal=None):
    """Give an existing actor to a principal in one project, and record it in the audit.

    Rule 2 of docs/COORDINATORS_PER_PROJECT_DESIGN.md: actors that existed before a key was
    bound are given to a principal once, by a host command, so they keep their names and
    their history. The name must appear somewhere in the project the kit can read (a session
    registration, an owner entry or a tracker row): a name that appears nowhere is refused.
    It refuses a name on the operator allowlist that another principal owns in another
    project: the one operator list must mean one lane everywhere.

    An actor this project already gives to ANOTHER principal is not moved by the same plain
    command: ``from_principal`` (``--from``) must name the owner it has now, and the audit
    entry is marked ``moved``. Writing nothing when it already gives it to this one.

    The same-operator-name check, the existence check and both writes run under the
    deployment lock (kittrial-5bb.223, finding 1): two ``adopt-actor`` commands for one
    operator-listed name in two projects serialize there, so the second reads the first's
    owner entry and refuses instead of both exiting 0. Within that lock the registry is
    written under the project's coordination lock. A damaged audit refuses the whole command
    before the registry is touched. The audit is appended BEFORE the registry mutation, so a
    host crash between the two leaves an audit entry with no adoption - the safer mistake
    (review, item 6).
    """
    path=project_dir(root,project)
    if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
    from sessions import validate as validate_sessions, read_registry, owner_map, valid_actor, valid_principal
    from recovery import identity
    actor=valid_actor(actor,'actor')
    principal=valid_principal(principal,'--principal')
    if from_principal is not None:from_principal=valid_principal(from_principal,'--from')
    operator=identity(operator,'Invalid operator identity')
    from keyed_records import require_configured_operator
    require_configured_operator(operator,operators(root,strict=True),'adopt an actor')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('A reason is required (--reason)')
    reason=reason.strip()
    if len(reason)>400:raise ValueError('--reason must be at most 400 characters')
    with deployment_config_lock(root):
        if actor in operators(root):
            conflicts=principal_conflicts(root,actor,principal,project)
            if conflicts:
                raise ValueError('Refusing to adopt %s for %s: it is on the operator allowlist and project %s '
                                 'already gives it to %s. One operator name must mean one principal on this '
                                 'installation; adopt it there first, or take it off the operator list.'
                                 %(actor,principal,conflicts[0][0],conflicts[0][1]))
        actor_adoptions(root)                   # a damaged audit refuses before anything is written
        if actor not in project_actor_names(root,project,path):
            raise ValueError('Refusing to adopt %s: no session registration, owner entry or tracker row in project %s '
                             'names that actor, so this project holds nothing to give to %s. Check the name '
                             '(session show ACTOR, or the project rows); nothing was changed.'%(actor,project,principal))
        try:
            import fcntl
        except ImportError:                     # a platform without flock: the atomic write still stands
            fcntl=None
        with (path/'.coordination.lock').open('a') as lock:
            if fcntl is not None:fcntl.flock(lock,fcntl.LOCK_EX)
            from coordination import atomic
            data=read_registry(path)
            current=dict(owner_map(data))
            previous=current.get(actor)
            if previous is not None and previous!=principal:
                # An actor belongs to one principal: moving it out of another's hands is not the
                # same plain command that gives a legacy actor its first owner (review, item 2).
                if from_principal is None:
                    raise ValueError('Refusing to move %s: project %s already gives it to %s. One actor belongs to one '
                                     'principal; to move it, name the owner it has now with --from %s. Nothing was changed.'
                                     %(actor,project,previous,previous))
                if from_principal!=previous:
                    raise ValueError('--from names %s, but project %s gives %s to %s; nothing was changed.'
                                     %(from_principal,project,actor,previous))
            elif from_principal is not None and from_principal!=previous:
                raise ValueError('--from names %s, but project %s does not give %s to it; nothing was changed.'
                                 %(from_principal,project,actor))
            changed=previous!=principal
            if changed:
                entry={'at':utc_stamp(),'operator':operator,'project':project,'actor':actor,'principal':principal,
                       'previous':previous,'reason':reason,'moved':previous is not None}
                history=actor_adoptions(root)
                history.append(entry)
                atomic_private_write(root/ACTOR_ADOPTIONS_AUDIT,
                                     json.dumps({'schema_version':ACTOR_ADOPTIONS_SCHEMA,
                                                 'entries':history[-ACTOR_ADOPTIONS_MAX:]}))
                current[actor]=principal
                data['owners']=current
                validate_sessions(data)
                atomic(path/'.sessions.json',data)
    if not changed:
        return {'schema_version':1,'project':project,'actor':actor,'principal':principal,
                'previous':previous,'changed':False,'moved':False,'audit_records':len(actor_adoptions(root))}
    return {'schema_version':1,'project':project,'actor':actor,'principal':principal,
            'previous':previous,'changed':True,'moved':entry['moved'],
            'audit_records':len(actor_adoptions(root))}

#: The deployment-authority audit (kittrial-5bb.192, slice 5 of
#: docs/COORDINATORS_PER_PROJECT_DESIGN.md): every change of the operator and verifier lists
#: made by ``operators add|remove`` and ``verifiers add|remove``. Where the adoption audit
#: above records who gave an actor to a principal, this one records who changed the
#: installation's authority and why. It is runtime-level, beside ``deployment.private.json``
#: and ``actor-adoptions.audit.json``, and never part of a project's coordination backup.
AUTHORITY_CHANGES_AUDIT='authority-changes.audit.json'
AUTHORITY_CHANGES_SCHEMA=1
#: A short history, like actor-adoptions.audit.json: the audit answers "who changed the
#: lists, when and why", not "every change since the installation was made".
AUTHORITY_CHANGES_MAX=200
AUTHORITY_CHANGES_FIELDS=frozenset({'at','operator','list','actor','change','reason'})
AUTHORITY_CHANGES_LISTS=('operators','verifiers')
AUTHORITY_CHANGES_ACTIONS=('add','remove')
#: The ceiling on a recorded ``--reason``, the same one ``adopt-actor`` uses.
AUTHORITY_CHANGES_REASON_MAX=400
#: What a damaged audit file is KEPT BESIDE the runtime as before a fresh history starts, the shape
#: ``review-writes.audit.json`` already uses (kittrial-5bb.192 review item 1).
AUTHORITY_CHANGES_DAMAGED='.damaged-'
#: How many free names or temporary names one set-aside tries before it gives up. A name is only
#: taken when ``os.path.lexists`` says it is free, so this is reached only on a race.
AUTHORITY_CHANGE_ASIDE_ATTEMPTS=64
#: The name a copy of a damaged audit leaves behind when the process is killed inside the copy:
#: ``.authority-changes.audit.json.damaged-<STAMP>[.N].tmp-<16 hex>``. It is the alternation
#: ``remove_private_write_leftovers`` adds for this audit alone, so the leftover of a kill inside
#: the copy is cleaned by the next locked write like any other (kittrial-5bb.229 rev-2 item 4).
AUTHORITY_CHANGE_COPY_LEFTOVER=r'\.damaged-[0-9A-Za-z]+(?:\.[0-9]+)?\.tmp-[0-9a-f]{16}'
#: The literal words the printed re-grant commands carry when the restore was given no
#: ``--actor``/``--reason``. ``authority_change_arguments`` refuses them on an ADD, so a command
#: copied from a warning and run unchanged is refused instead of recording an operator named
#: OPERATOR (kittrial-5bb.229 findings 3 and rev-2 item 3). A REMOVAL is never made harder for
#: them: the placeholder is dropped, the entry is recorded unattributed, and one sentence says so.
#: ``OPERATOR`` is a placeholder only while no listed operator carries that name, so a real listed
#: operator named OPERATOR is not locked out of attributing a change (rev-2 item 3).
AUTHORITY_CHANGE_PLACEHOLDER_ACTOR='OPERATOR'
AUTHORITY_CHANGE_PLACEHOLDER_REASON='TEXT'
#: The baseline a NEW history begins with: the operator and verifier lists as they stood when the
#: trail began (round-2 review item 1, the coordinator's decision). It is one record beside the
#: capped ``entries``, not one entry per name: the cap is 200 ENTRIES, and a per-name baseline
#: costs one entry for every name it folds, so a history that lists ~200 names could never be
#: replayed within the cap (baseline plus kept would stay at 201 for ever). ``at`` is the moment
#: it was written, ``operator`` the operator of the change that started the history (null when
#: that change named nobody) and ``reason`` says why it was written.
AUTHORITY_CHANGES_BASELINE_FIELDS=frozenset({'at','operator','reason','lists'})

class AuthorityAuditDamaged(ValueError):
    """A list change or a read was refused because the authority-changes audit is unusable."""

def authority_change_entry(item):
    """Whether one entry has the shape ``operators``/``verifiers`` add|remove writes.

    ``operator`` and ``reason`` are null when the caller did not say them. A call without
    the new flags keeps working - the office wrapper ``coord.sh`` runs the bare
    ``admin.py --root RT operators add ACTOR`` - and the entry then says plainly that the
    change was not attributed instead of pretending somebody was named. The other fields
    are always present, so the reader can always show what changed. ``at`` must be the exact
    UTC stamp ``utc_stamp`` writes: a hand-written entry whose ``at`` is any other text is not
    this kit's history, and neither is one that carries a field this kit does not write
    (kittrial-5bb.192 review item 4).
    """
    return (isinstance(item,dict) and set(item)==AUTHORITY_CHANGES_FIELDS
            and utc_timestamp(item['at'])
            and isinstance(item['actor'],str) and bool(item['actor'])
            and item['list'] in AUTHORITY_CHANGES_LISTS
            and item['change'] in AUTHORITY_CHANGES_ACTIONS
            and all(item[field] is None or (isinstance(item[field],str) and bool(item[field]))
                    for field in ('operator','reason')))

def authority_change_baseline(lists, operator, reason, at=None):
    """The baseline a NEW history begins with: the operator and verifier lists as they stand now.

    It is written (a) by the first change on an installation whose audit holds no baseline, which
    includes an installation from before this kit and a file a hand edit started, (b) by the first
    change after a damaged audit was set aside, and (c) carried forward by
    ``record_authority_change`` when a change would push the trail past its cap, where the entries
    dropped for room are folded into the fresh one. ``lists`` is either the
    ``{noun: [names]}`` mapping ``authority_changes_current_lists`` answers or the same mapping
    ``authority_change_fold`` answers. Reads nothing and writes nothing.

    A list that could not be read is recorded as ``None`` (UNKNOWN), never as an empty list
    (kittrial-5bb.229 rev-2 item 1, the coordinator's decision): a baseline holding ``[]`` for a
    list nobody could read would read every name that list really holds as one the trail never
    mentions, for good. A REMOVAL is never refused to avoid that: it starts the trail and records
    the list it could not read as ``None``, and the reader then says the trail is incomplete for
    that list. Only an ADD may be refused while a list cannot be read.
    """
    return {'at':at or utc_stamp(),'operator':operator,'reason':reason,
            'lists':{noun:(None if (lists or {}).get(noun) is None else sorted(lists[noun]))
                     for noun in AUTHORITY_CHANGES_LISTS}}

def authority_change_baseline_record(item):
    """Whether ``item`` is a baseline this kit writes.

    ``lists`` must carry both lists and nothing else, each a list of non-empty names or ``null``
    (UNKNOWN: the list could not be read when that history began, kittrial-5bb.229 rev-2 item 1);
    the same strictness ``authority_change_entry`` applies to an entry, so a hand-written baseline
    this kit does not write is "not the history this kit writes" rather than something silently
    obeyed.
    """
    if not isinstance(item,dict) or set(item)!=AUTHORITY_CHANGES_BASELINE_FIELDS:return False
    if not utc_timestamp(item['at']):return False
    if item['operator'] is not None and not (isinstance(item['operator'],str) and item['operator']):return False
    if not (isinstance(item['reason'],str) and item['reason']) or len(item['reason'])>AUTHORITY_CHANGES_REASON_MAX:
        return False
    lists=item['lists']
    if not isinstance(lists,dict) or set(lists)!=set(AUTHORITY_CHANGES_LISTS):return False
    return all(lists[noun] is None or (isinstance(lists[noun],list)
               and all(isinstance(name,str) and bool(name) for name in lists[noun]))
               for noun in AUTHORITY_CHANGES_LISTS)

def authority_changes_document(record):
    """``(entries, baseline, damage)`` for a parsed audit document; ``([], None, (kind, phrase))`` when it is not ours.

    The ONE place the document's shape is judged, so the reader, the set-aside and the four
    commands cannot disagree. ``schema_version`` is compared strictly: JSON ``true`` equals 1 in
    Python and a hand-set ``1.0`` reads as 1, and neither is this kit's history (kittrial-5bb.192
    review item 4). An unknown top-level key is a refusal, not something quietly dropped at the
    next write, and every entry must carry exactly ``AUTHORITY_CHANGES_FIELDS``. The optional
    ``baseline`` (round-2 review item 1) must be one this kit writes; a document without it is
    still this kit's history - an older revision wrote none, and a hand-written file may have
    none - and the reader then says the trail does not begin with a baseline.
    """
    if (not isinstance(record,dict) or not {'schema_version','entries'}<=set(record)
            or set(record)-{'schema_version','entries','baseline'}):
        return [],None,('not-this-history','is not the history this kit writes')
    if type(record['schema_version']) is not int or record['schema_version']!=AUTHORITY_CHANGES_SCHEMA:
        return [],None,('not-this-history','is not the history this kit writes: its schema_version is %r, not %d'
                        %(record['schema_version'],AUTHORITY_CHANGES_SCHEMA))
    entries=record['entries']
    if not isinstance(entries,list) or any(not authority_change_entry(item) for item in entries):
        return [],None,('not-this-history','is not the history this kit writes')
    baseline=record.get('baseline')
    if baseline is not None and not authority_change_baseline_record(baseline):
        return [],None,('not-this-history','is not the history this kit writes: its baseline is not one this kit writes')
    return entries,baseline,None

def read_authority_changes(root):
    """``(entries, baseline, damage)`` for the audit file; ``damage`` is None when it is absent or readable.

    ``damage`` is ``(kind, phrase)``. ``not-a-regular-file``: the path is a directory, a fifo or a
    symlink - a dangling symlink is not a file at all, so it used to read as an empty history and
    then be replaced by a regular file. ``unreadable``: the bytes cannot be read as this kit's
    JSON (not JSON, empty, a BOM, non-UTF-8, a bare list or ``null``, nested past the guard, a
    ``NaN``, unreadable mode ``000``). ``not-this-history``: the document is JSON but is not the
    history this kit writes (another schema, an unknown top-level key, an entry or a baseline with
    an unknown or missing field, an ``at`` that is not this kit's stamp). An absent file is an
    empty history with no baseline, never damage.
    """
    path=root/AUTHORITY_CHANGES_AUDIT
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return [],None,('not-a-regular-file','is not a regular file (a directory, a fifo or a symlink)')
    if not path.exists():return [],None,None
    try:record=record_json.loads(path.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError) as error:
        return [],None,('unreadable','cannot be read: %s'%error)
    return authority_changes_document(record)

def authority_change_damaged_files(root):
    """The ``.damaged-*`` audit files kept beside this runtime, oldest name first. Reads only.

    The kit never removes one (round-2 review item 2), so the reader lists them: nothing else
    would tell an operator that the history they are reading was restarted beside a kept file.
    Only a REGULAR, NON-SYMLINK file is one kept here (kittrial-5bb.229 finding 1): ``is_file``
    follows a symlink, so a ``.damaged-*`` link to any outside file used to read as bytes the
    kit had kept inside the runtime.
    """
    prefix=AUTHORITY_CHANGES_AUDIT+AUTHORITY_CHANGES_DAMAGED
    try:names=sorted(os.listdir(str(root)))
    except OSError:return []
    kept=[]
    for name in names:
        if not name.startswith(prefix):continue
        try:mode=os.lstat(str(root/name)).st_mode
        except OSError:continue
        if stat.S_ISREG(mode):kept.append(name)
    return kept

def authority_change_aside_name(root):
    """The name a damaged audit would be kept under right now: a real stamp, free at this moment.

    "Free" is ``os.path.lexists``, not ``Path.exists`` (kittrial-5bb.229 finding 1): a DANGLING
    symlink at the name is taken, where ``exists`` read it as free and the fallback copy then
    followed the link and wrote the damaged bytes outside the runtime.
    """
    from datetime import datetime,timezone
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    aside=root/('%s%s%s'%(AUTHORITY_CHANGES_AUDIT,AUTHORITY_CHANGES_DAMAGED,stamp))
    number=1
    while os.path.lexists(str(aside)):
        aside=root/('%s%s%s.%d'%(AUTHORITY_CHANGES_AUDIT,AUTHORITY_CHANGES_DAMAGED,stamp,number));number+=1
    return aside

def authority_change_aside_hint(root):
    """The ``mv`` command that gets a damaged audit out of the way, as the refusals print it.

    The name is a real, current UTC stamp, not the literal word ``STAMP``: a template two
    different set-asides both follow overwrites the first kept file (round-2 review item 4).
    """
    path=root/AUTHORITY_CHANGES_AUDIT
    return 'mv %s %s'%(path,authority_change_aside_name(root))

def authority_audit_refusal(root,damage,mode):
    """The sentence for an unusable audit; ``mode`` is ``read``, ``add`` or ``remove``.

    Every refusal names the path, what is wrong with it and the recovery, so an operator is never
    told only that the command was refused (kittrial-5bb.192 review items 1 and 4).
    """
    kind,phrase=damage
    path=root/AUTHORITY_CHANGES_AUDIT
    hint=authority_change_aside_hint(root)
    head='The authority-changes audit %s %s'%(path,phrase)
    # A read changes nothing at all, so it says that rather than "nothing was changed".
    tail='; nothing was read' if mode=='read' else '; nothing was changed'
    if kind=='not-a-regular-file':
        return (head+tail+'. The kit only sets a damaged regular FILE aside by itself. Move it aside by hand (%s) '
                'and run the command again.'%hint)
    if mode=='add':
        return (head+tail+'. This command ADDS to the deployment authority, and the kit does not start a fresh history '
                'for a grant. Move the file aside by hand (%s) and run the command again: the change is then '
                'recorded in a fresh history.'%hint)
    if mode=='read':
        return (head+tail+'. Move the file aside by hand (%s) and read it again; a REMOVAL made with this kit keeps a '
                'damaged file beside the runtime by itself and starts a fresh history.'%hint)
    return head+tail+'. Move it aside by hand (%s) and run the command again.'%hint

def authority_change_baseline_problem_refusal(problem):
    """The sentence for an ADD that would start a new history while a list cannot be read.

    A new history begins with a baseline of the lists as they stand, so a baseline that held an
    empty list for a list nobody could read would read every name that list really holds as one the
    trail never mentions, for good. That is why an ADD is refused here - and ONLY an add: a REMOVAL
    is never refused for the audit (kittrial-5bb.192 review item 1). A removal starts the trail and
    records the list it could not read as ``null`` (UNKNOWN) instead (kittrial-5bb.229 rev-2 item
    1). The change is refused BEFORE the configuration is written, so nothing changed.
    """
    return ('The authority-changes audit would start a new history with a baseline of the current operator and '
            'verifier lists, but they cannot both be read: %s. Nothing was changed: a baseline never holds an empty '
            'list for a list that could not be read, and this command ADDS to the deployment authority, so the ADD '
            'is the one refused. Repair deployment.private.json and run the command again; a REMOVAL is not refused '
            'for it - it starts the trail and records the unreadable list as null (UNKNOWN).'%problem)

def authority_changes(root):
    """The recorded operator/verifier list changes, oldest first. Reads only.

    An absent file is an empty history. A file this kit cannot read as its own history is a
    refusal, not an empty history, exactly as ``actor_adoptions``: a caller must not be told
    "nobody changed the lists" by a damaged audit.
    """
    entries,_,damage=read_authority_changes(root)
    if damage is not None:raise AuthorityAuditDamaged(authority_audit_refusal(root,damage,'read'))
    return entries

def authority_change_same_bytes(first,second):
    """Whether two regular files hold exactly the same bytes, never following a symlink. Reads only."""
    try:
        if os.lstat(str(first)).st_size!=os.lstat(str(second)).st_size:return False
        with open(first,'rb') as left,open(second,'rb') as right:
            while True:
                chunk=left.read(131072)
                if chunk!=right.read(131072):return False
                if not chunk:return True
    except OSError:
        return False

def authority_change_copy_bytes(source,destination):
    """Copy ``source``'s bytes to ``destination``; ``False`` when the name is taken.

    The bytes go to a fresh temporary name in the same directory and are renamed into place
    (kittrial-5bb.229 finding 4): a kill inside the copy leaves the temporary name, never a
    partial file under a ``.damaged-*`` name. The temporary file is opened
    ``O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`` (finding 1), and ``destination`` is only renamed onto
    when ``os.path.lexists`` says it is free, so a symlink there is never followed or overwritten;
    the caller takes the next free name when this returns ``False``.

    ``O_EXCL`` is the flag that does the work: it refuses a name that is already taken, a symlink
    in the final component included, so ``O_NOFOLLOW`` adds nothing beside it (kittrial-5bb.229
    rev-2 item 4). It is kept anyway - it says the intent, it is read with ``getattr`` because a
    platform may not have it, and it costs nothing - but no test or comment should claim it is
    what stops the write through a symlink. When every one of the ``AUTHORITY_CHANGE_ASIDE_ATTEMPTS``
    temporary names is taken, this refuses with a sentence instead of a traceback (item 4).
    """
    import shutil
    directory=str(destination.parent)
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
    temporary=None;descriptor=None
    for _ in range(AUTHORITY_CHANGE_ASIDE_ATTEMPTS):
        candidate=os.path.join(directory,'.%s.tmp-%s'%(destination.name,secrets.token_hex(8)))
        try:descriptor=os.open(candidate,flags,0o600)
        except FileExistsError:continue
        temporary=candidate;break
    if temporary is None:
        raise AuthorityAuditDamaged(
            'The kit could not write a temporary copy of %s beside it: every one of the %d names it tried in %s was '
            'taken. Nothing was changed. Move the damaged file aside by hand and run the command again.'
            %(source,AUTHORITY_CHANGE_ASIDE_ATTEMPTS,destination.parent))
    try:
        with os.fdopen(descriptor,'wb') as out, open(source,'rb') as src:
            shutil.copyfileobj(src,out)
        if os.path.lexists(str(destination)):return False
        os.rename(temporary,destination)
        temporary=None
        return True
    finally:
        # The temporary copy never outlives this call, whether the copy failed or the name was
        # taken: a leftover here is exactly what the widened cleanup must not have to guess at
        # (kittrial-5bb.229 rev-2 item 4).
        if temporary is not None:
            try:os.unlink(temporary)
            except OSError:pass

def authority_change_keep_bytes(root,path):
    """Keep ``path``'s bytes beside the runtime under a fresh, free ``.damaged-*`` name; return it.

    ``os.link`` is the first choice, so the bytes are one inode under two names and the retry
    finds them with ``os.samefile``. A filesystem without hard links gets a copy that is written
    through a temporary name and renamed (kittrial-5bb.229 findings 1 and 4). A name is only used
    when ``os.path.lexists`` says it is free, and ``EEXIST`` on the link is never a reason to copy
    - a copy would follow a link that appeared at that name - so the next free name is taken.

    Every attempt failing is a REFUSAL with a sentence naming the recovery, not a traceback
    (kittrial-5bb.229 rev-2 item 4): 64 attempts was reached only by injecting ``EEXIST`` on a
    name ``os.path.lexists`` had just called free, and a Python traceback told an operator
    nothing.
    """
    for _ in range(AUTHORITY_CHANGE_ASIDE_ATTEMPTS):
        aside=authority_change_aside_name(root)
        try:
            os.link(path,aside)
        except FileExistsError:
            continue
        except OSError:
            if authority_change_copy_bytes(path,aside):return aside
            continue
        else:
            return aside
    raise AuthorityAuditDamaged(
        'The authority-changes audit %s is damaged, and the kit could not keep its bytes beside the runtime: every '
        'one of the %d names it tried was taken. Nothing was changed. Move the file aside by hand (%s) and run the '
        'command again.'%(root/AUTHORITY_CHANGES_AUDIT,AUTHORITY_CHANGE_ASIDE_ATTEMPTS,authority_change_aside_hint(root)))

def set_aside_damaged_authority_changes(root,damage):
    """Keep the DAMAGED audit's bytes beside the runtime under a dated name; return the path kept.

    A REMOVAL is never refused for the audit (kittrial-5bb.192 review item 1, the coordinator's
    decision). The bytes are put at the aside name FIRST - a hard link, or a copy where the
    filesystem has none - and ``record_authority_change`` then lets the atomic write of the new
    history REPLACE the audit path, so the path is never absent (round-2 review item 2): a kill, or
    a write that fails, at any point leaves the damaged file exactly where it was plus one extra
    name, and the retry reuses that name instead of filling the runtime with copies of the same
    bytes.

    It does NOT print the sentence saying a fresh history starts: ``record_authority_change``
    prints that once the fresh history is actually written, so the kit never says a fresh history
    starts and then refuses (kittrial-5bb.229 rev-2 item 1).

    Only a regular, non-symlink ``.damaged-*`` file is a candidate (kittrial-5bb.229 finding 1):
    a symlink at the name, even one pointing at the audit path, is neither reused nor listed, and
    an existing regular copy holding the same bytes is reused (finding 4) so a filesystem without
    hard links does not accumulate one copy per attempt. "The same bytes" is the BYTES, not the
    size: a same-size file holding something else is not the kept copy and its bytes are not taken
    for this audit's (rev-2 item 4).
    """
    path=root/AUTHORITY_CHANGES_AUDIT
    aside=None
    for name in authority_change_damaged_files(root):
        candidate=root/name
        try:
            if os.path.samefile(path,candidate):aside=candidate;break
        except OSError:
            pass
        if authority_change_same_bytes(path,candidate):aside=candidate;break
    if aside is None:
        aside=authority_change_keep_bytes(root,path)
    return aside

def authority_change_set_aside_reason(aside,reason):
    """The reason on the change that starts a fresh history after a damaged audit.

    The entry field set is closed, so the one place that change can record the set-aside is its
    reason; it is written BEFORE the operator's own sentence so the fact cannot be lost. The
    baseline written with it names the same file in the same way.
    """
    note='the previous audit was damaged and was kept beside the runtime as %s'%aside.name
    return '%s; %s'%(note,reason) if reason else note

def authority_change_baseline_reason(aside=None,dropped=0,late=False,unknown=()):
    """Why a baseline was written: the trail's start, a late start, a set-aside, or the cap making room.

    ``late`` says this history already held entries but no baseline - a file written by hand, or by
    a kit older than this one - so the baseline holds the lists as they stand at THIS change, and
    says plainly that the trail before it is incomplete instead of claiming to hold the lists "as
    they stood when this history began" (kittrial-5bb.229 finding 6).

    ``unknown`` names the lists this baseline holds as ``null`` because they could not be read
    (kittrial-5bb.229 rev-2 item 1). The sentence then says so and says the trail is incomplete for
    them, so the one place the closed entry field set can record WHY - the baseline's reason -
    carries it. With the cap also folding entries in, the wording claims only the lists it could
    read: the fold cannot invent the names a list held when nobody could read it.
    """
    if dropped:
        if unknown:
            note=('baseline: the %d entr%s the trail dropped at its %d-entry cap are folded into the lists it could '
                  'read; the %s list could not be read and stays null (UNKNOWN), so the trail is incomplete for it'
                  %(dropped,'y' if dropped==1 else 'ies',AUTHORITY_CHANGES_MAX,' and '.join(unknown)))
        else:
            note=('baseline: the lists as the trail held them where it was cut to its %d-entry cap; the %d entr%s '
                  'dropped are folded in here, so the trail still leads to the lists'
                  %(AUTHORITY_CHANGES_MAX,dropped,'y' if dropped==1 else 'ies'))
    elif late:
        note=('baseline: the lists as they stand at this change; this history held entries but no baseline, so the '
              'trail before it is incomplete for these lists and nothing recorded what they were when it began')
    else:
        note='baseline: the lists as they stood when this history began'
    if unknown:
        note+=('; the %s list could not be read, so the baseline holds null (UNKNOWN) for it and the trail is '
               'incomplete for it'%' and '.join(unknown))
    if aside is not None:
        note+='; the previous audit was damaged and was kept beside the runtime as %s'%aside.name
    return note

def authority_change_fold(baseline,entries):
    """The ``{noun: [names]}`` a baseline holds after every entry in ``entries`` is applied to it.

    The trail is replayed exactly as the reader replays it (the last word on a name wins, and a
    baseline name is listed), so the state a cut trail is folded into is the state the reader would
    have computed for those names. A list the baseline holds as ``None`` (UNKNOWN) STAYS ``None``:
    the fold cannot invent the names a list held when nobody could read it, and an empty list there
    would be exactly the claim finding 2 forbids (kittrial-5bb.229 rev-2 item 1).
    """
    held={noun:(baseline or {}).get('lists',{}).get(noun) for noun in AUTHORITY_CHANGES_LISTS}
    lists={noun:set(held[noun] or ()) for noun in AUTHORITY_CHANGES_LISTS}
    for item in entries:
        if item['change']=='remove':lists[item['list']].discard(item['actor'])
        else:lists[item['list']].add(item['actor'])
    return {noun:(sorted(lists[noun]) if held[noun] is not None else None) for noun in AUTHORITY_CHANGES_LISTS}

def authority_change_precheck(root,action,changes=True):
    """Refuse an ADD that WOULD change the list on a damaged audit; refuse any command on a path that is not a regular file.

    A REMOVAL is NOT refused for a damaged file: ``record_authority_change`` keeps it beside the
    runtime under a dated name and starts a fresh history with the removal in it (kittrial-5bb.192
    review item 1). ``changes`` says whether this command would really change the list: a no-op ADD
    (the name is already listed) is NOT refused, because the release before this audit exited 0 for
    it and a wrapper that re-runs its bare ``operators add`` must keep working (round-2 review item
    4); only a grant that would change something pays the price of moving the file by hand. Called
    BEFORE the lock, so a refusal costs nothing at all: no lock file, nothing written.
    """
    _,_,damage=read_authority_changes(root)
    if damage is None:return
    if damage[0]=='not-a-regular-file':
        raise AuthorityAuditDamaged(authority_audit_refusal(root,damage,'remove' if action=='remove' else 'add'))
    if action=='add' and changes:
        raise AuthorityAuditDamaged(authority_audit_refusal(root,damage,'add'))

def record_authority_change(root,noun,action,actor,operator,reason):
    """Append one recorded list change and return the entry written.

    Called only when the list really changes, from inside ``deployment_config_lock`` and
    BEFORE the configuration is written: a host crash between the two leaves an audit entry
    with no change - the safer mistake, the same ordering ``adopt_actor`` uses.

    A damaged FILE is handled here (kittrial-5bb.192 review item 1, the coordinator's decision):
    a REMOVAL keeps the file beside the runtime and starts a fresh history, an ADD is refused, and
    a path that is not a regular file is refused for every command. A history with no baseline gets
    one here - the lists as they stand, read under the lock and before this change - so the replay
    starts from a known state and a listed-but-never-mentioned name really is a hand edit or an
    older kit (round-2 review item 1). A REMOVAL is never refused because one of the two lists
    cannot be read: that list is recorded as ``null`` (UNKNOWN) in the baseline and the removal
    goes on; only an ADD is refused while a list cannot be read (kittrial-5bb.229 rev-2 item 1).
    When a change needs room in the cap, the entries that make room are folded into a fresh
    baseline rather than lost, and the drop is named on stderr (review item 3e).

    The sentence saying a fresh history starts after a damaged audit is printed AFTER that fresh
    history has actually been written, so the kit never says it and then refuses (rev-2 item 1).
    """
    entries,baseline,damage=read_authority_changes(root)
    aside=None
    if damage is not None:
        if damage[0]=='not-a-regular-file' or action!='remove':
            raise AuthorityAuditDamaged(authority_audit_refusal(root,damage,'add' if action!='remove' else 'remove'))
        aside=set_aside_damaged_authority_changes(root,damage)
        reason=authority_change_set_aside_reason(aside,reason)
        entries=[];baseline=None
    entry={'at':utc_stamp(),'operator':operator,'list':noun,'actor':actor,'change':action,'reason':reason}
    if baseline is None:
        listed,problem=authority_changes_current_lists(root)
        if problem is not None and action!='remove':
            raise ValueError(authority_change_baseline_problem_refusal(problem))
        unknown=[] if problem is None else [name for name in AUTHORITY_CHANGES_LISTS
                                            if listed is None or listed.get(name) is None]
        baseline=authority_change_baseline(listed,operator,
                                          authority_change_baseline_reason(aside,late=bool(entries),
                                                                           unknown=unknown),at=entry['at'])
    history=list(entries)
    history.append(entry)
    dropped=len(history)-AUTHORITY_CHANGES_MAX
    if dropped>0:
        # The oldest entries must go, and a trail cut at the head can no longer be replayed: the
        # state they recorded is carried forward in a fresh baseline, and the entries over the cap
        # are the newest ones (round-2 review item 1).
        baseline=authority_change_baseline(authority_change_fold(baseline,history[:dropped]),operator,
                                           authority_change_baseline_reason(
                                               dropped=dropped,
                                               unknown=[name for name in AUTHORITY_CHANGES_LISTS
                                                        if baseline['lists'].get(name) is None]),
                                           at=entry['at'])
        history=history[dropped:]
        print('WARNING: %d older entr%s dropped from %s: it keeps the last %d entries, and what they recorded is '
              'folded into the baseline, so the trail still leads to the lists.'
              %(dropped,'y was' if dropped==1 else 'ies were',root/AUTHORITY_CHANGES_AUDIT,AUTHORITY_CHANGES_MAX),
              file=sys.stderr)
    document={'schema_version':AUTHORITY_CHANGES_SCHEMA,'entries':history}
    if baseline is not None:document['baseline']=baseline
    atomic_private_write(root/AUTHORITY_CHANGES_AUDIT,json.dumps(document))
    if aside is not None:
        # Only now is it true (rev-2 item 1): the fresh history is on disk, so the sentence cannot
        # be followed by a refusal that leaves it false.
        print('The authority-changes audit %s is damaged (%s); it was kept beside the runtime as %s, and a fresh '
              'history starts with the change that follows.'%(root/AUTHORITY_CHANGES_AUDIT,damage[1],aside),
              file=sys.stderr)
    return entry

def authority_change_notice(noun,action,operator,reason):
    """The one sentence a list change without the new flags prints on stderr.

    It names exactly what to add, so the bare form the office wrapper uses keeps working and is
    never silently unattributed. With one flag given it names that flag's holder and asks for the
    other alone: a change made by a named operator is not called unattributed (kittrial-5bb.192
    review item 3c). ``restore-new``'s re-grant prints the same sentence with
    ``('restore-new','re-grant')`` when ``--actor`` or ``--reason`` was not given (round-2 review
    item 3), so an unattributed re-grant is as loud as an unattributed list change.
    """
    if operator is None and reason is None:
        return ('WARNING: this %s %s was recorded in %s without --actor OPERATOR and --reason TEXT, so the audit '
                'reads it as unattributed. Add --actor OPERATOR and --reason TEXT to say who changed the '
                'deployment authority and why.'%(noun,action,AUTHORITY_CHANGES_AUDIT))
    if reason is None:
        return ('WARNING: this %s %s by %s was recorded in %s without --reason TEXT, so the audit names %s but '
                'does not say WHY the change was made. Add --reason TEXT to say why the deployment authority was '
                'changed.'%(noun,action,operator,AUTHORITY_CHANGES_AUDIT,operator))
    return ('WARNING: this %s %s was recorded in %s without --actor OPERATOR, so the audit does not say WHO made '
            'the change (the reason you gave is recorded). Add --actor OPERATOR to name the operator who changed '
            'the deployment authority.'%(noun,action,AUTHORITY_CHANGES_AUDIT))

def authority_change_placeholder_notice(noun,action,dropped,operator):
    """The one sentence a REMOVAL prints when it accepted a printed placeholder instead of refusing.

    A removal must not be harder WITH the flags than without (kittrial-5bb.229 rev-2 item 3), so the
    literal ``--actor OPERATOR``/``--reason TEXT`` the printed re-grant commands carry are not a
    refusal on a removal: the placeholder is dropped, the entry is recorded without it, and this
    sentence says exactly what was recorded. Nothing is silent, and an ADD is still refused
    (finding 3), so a command copied from a warning and run unchanged does not record an operator
    named OPERATOR on a grant.
    """
    labels={'actor':'--actor OPERATOR','reason':'--reason TEXT'}
    given=' and '.join(labels[field] for field in dropped)
    recorded=('unattributed (operator null)' if operator is None else 'by %s'%operator)
    return ('WARNING: %s %s the placeholder the printed re-grant commands carry, not a real value. A REMOVAL is '
            'never refused for it, and the placeholder is not recorded as a real value: this %s %s is recorded %s. '
            'Replace it with the real value and run the command again to record it.'
            %(given,'is' if len(dropped)==1 else 'are',noun,action,recorded))

def authority_change_listed_operators(cfg):
    """The names the deployment lists as operators, or ``()`` when that value cannot be read.

    The literal ``--actor OPERATOR`` is a real name only while an operator this deployment lists
    carries it (kittrial-5bb.229 rev-2 item 3). A ``verifiers`` command needs that answer too, and
    an unusable ``operators`` value must NOT make it fail here: it lists nobody, so the literal is
    the placeholder, and the removal the reviewer's mirror case exercises goes on (rev-2 item 1).
    """
    try:return list(stored_operators(cfg))
    except (ValueError,TypeError,OSError,UnicodeError):return []

def authority_change_arguments(args,noun,listed_operators=()):
    """Validate ``--actor``/``--reason`` for one list change; returns ``(operator,reason,notice)``.

    Both are optional, because the bare form ``admin.py --root RT operators add ACTOR`` (the
    office wrapper ``coord.sh``) must keep working. A value that IS given is normalised and
    checked here like ``adopt-actor``'s, and ``notice`` is the one sentence to print on
    stderr when one or both were not recorded, so the bare form is never silently unattributed.
    ``--actor``/``--reason`` may be given at most once: the parser refuses a repeat instead of
    recording the last value silently (kittrial-5bb.192 review item 4).

    The literal placeholders the printed re-grant commands carry are refused on an ADD
    (kittrial-5bb.229 finding 3) and never on a REMOVAL (rev-2 item 3): a removal must not be
    harder WITH the flags than without, so the placeholder is dropped, the entry is recorded
    without it and ``authority_change_placeholder_notice`` says so. ``OPERATOR`` is a placeholder
    only while no listed operator carries that name: ``operators add OPERATOR`` is still accepted,
    so a real operator named OPERATOR is not locked out of attributing a change (rev-2 item 3).
    """
    from recovery import identity
    listed=set(listed_operators)
    operator=args.operator
    dropped=[]
    if operator is not None:
        operator=identity(operator,'Invalid operator identity')
        if operator==AUTHORITY_CHANGE_PLACEHOLDER_ACTOR and operator not in listed:
            if args.action!='remove':
                raise ValueError('--actor %s is the placeholder the printed re-grant commands carry; replace it with '
                                 'the operator who is making the change, or the audit would record OPERATOR as a real '
                                 'name'%AUTHORITY_CHANGE_PLACEHOLDER_ACTOR)
            dropped.append('actor');operator=None
    reason=args.reason
    if reason is not None:
        reason=reason.strip()
        if not reason:raise ValueError('--reason must be a sentence, not blank')
        if reason==AUTHORITY_CHANGE_PLACEHOLDER_REASON:
            if args.action!='remove':
                raise ValueError('--reason %s is the placeholder the printed re-grant commands carry; replace it with '
                                 'a sentence saying why the deployment authority is changing'
                                 %AUTHORITY_CHANGE_PLACEHOLDER_REASON)
            dropped.append('reason');reason=None
        elif len(reason)>AUTHORITY_CHANGES_REASON_MAX:
            raise ValueError('--reason must be at most %d characters'%AUTHORITY_CHANGES_REASON_MAX)
    if dropped:
        notice=authority_change_placeholder_notice(noun,args.action,dropped,operator)
    else:
        notice=(authority_change_notice(noun,args.action,operator,reason)
                if operator is None or reason is None else None)
    return operator,reason,notice

def authority_change_list_arguments(args,noun):
    """Refuse the recording flags on ``list``: they were accepted and ignored (review item 4)."""
    given=[flag for flag,value in (('--actor',args.operator),('--reason',args.reason)) if value is not None]
    if given:
        raise ValueError('%s list takes no %s: it changes nothing, so the flag would be accepted and ignored. '
                         'The recording flags are for %s add and %s remove.'
                         %(noun,' or '.join(given),noun,noun))

def authority_changes_current_lists(root):
    """``(lists, problem)``: each list as the deployment holds it, or ``None`` when it cannot be read.

    Reads each list ON ITS OWN (kittrial-5bb.229 finding 2): a ``deployment.private.json`` whose
    ``verifiers`` value cannot be used no longer hides the readable ``operators`` list. ``lists``
    maps each noun to its names, or to ``None`` when that one alone could not be read; a file that
    cannot be read at all makes ``lists`` ``None``. ``problem`` names each unreadable list (or the
    whole file) and is ``None`` when both could be read. Reads only, and never raises: the reader
    must still show the trail when the lists cannot be read, saying beside it that they could not.
    """
    try:
        cfg=config(root)
    except (OSError,UnicodeError,ValueError,TypeError) as error:
        return None,'%s'%error
    lists={};problems=[]
    for noun,reader in (('operators',stored_operators),('verifiers',stored_verifiers)):
        try:lists[noun]=list(reader(cfg))
        except (OSError,UnicodeError,ValueError,TypeError) as error:
            lists[noun]=None;problems.append('%s: %s'%(noun,error))
    return lists,('; '.join(problems) if problems else None)

def authority_change_replay(entries,listed,noun,baseline=None):
    """Replay one list's trail against that list as it is now (kittrial-5bb.192 review item 3a).

    An entry holds no before/after of the list itself, so the trail can only be replayed: the last
    entry the trail holds for a name is the trail's last word on it, and a name the baseline holds
    is the trail's word for the state its history began in. With a baseline to replay from, a name
    the trail never mentions IS a hand edit or a change made by an older kit; without one - a file
    written before this kit, or by hand - the trail may simply be older than the lists, and the
    note says so (round-2 review item 1).

    ``None`` when the baseline holds THIS list as ``null`` (UNKNOWN): it could not be read when that
    history began, so there is no known state to replay from and the reader must say the trail is
    incomplete for the list rather than replay it from an empty one (kittrial-5bb.229 rev-2 item 1).
    """
    if baseline is not None and baseline['lists'].get(noun) is None:return None
    last={}
    if baseline is not None:
        for name in baseline['lists'].get(noun) or ():last[name]='baseline'
    for item in entries:
        if item['list']==noun:last[item['actor']]=item['change']
    written=list(listed.get(noun,[]))
    listed_twice=sorted({name for name in written if written.count(name)>1})
    current=sorted(set(written))
    expected=sorted(name for name,change in last.items() if change!='remove')
    return {'agrees':current==expected,
            'current':current,
            'trail_expects':expected,
            'listed_but_last_removed':sorted(name for name in current if last.get(name)=='remove'),
            'listed_but_not_in_trail':sorted(name for name in current if name not in last),
            'trail_added_but_not_listed':sorted(set(expected)-set(current)),
            'listed_more_than_once':listed_twice}

def authority_changes_lists_phrase(nouns):
    """``the current <noun> list``/``the current lists``, for a note that must not overclaim.

    With one list unreadable the note can only speak for the lists it could compare: saying "the
    current lists" would claim the other one too (kittrial-5bb.229 rev-2 item 2).
    """
    if len(nouns)==len(AUTHORITY_CHANGES_LISTS):return 'the current lists'
    return 'the current %s list'%' and '.join(nouns)

def authority_changes_incomplete_sentence(incomplete,problem,unknown_in_baseline=()):
    """The sentence for each list this read cannot compare, the unreadable list named FIRST.

    ``incomplete`` are the lists with no replay, in ``AUTHORITY_CHANGES_LISTS`` order. A list is in
    it because the deployment could not be read now (``problem`` names it) or because the baseline
    this trail begins with holds it as ``null`` (UNKNOWN): it could not be read when that history
    began (kittrial-5bb.229 rev-2 item 1). The caller puts this sentence BEFORE anything about the
    lists that could be compared, so the unreadable list is named first (rev-2 item 2).
    """
    reasons=[]
    for noun in incomplete:
        if noun in unknown_in_baseline:
            reasons.append('the baseline this trail begins with holds the %s list as null (UNKNOWN), because it could '
                           'not be read when that history began'%noun)
        else:
            reasons.append(problem or 'it could not be read')
    return ('The trail is incomplete for %s: %s. This read cannot say whether the trail leads to that list.'
            %(' and '.join(incomplete),'; '.join(reasons)))

def authority_changes_replay_note(replay,entries,capped,problem,baseline=None,unknown_in_baseline=()):
    """The plain sentence beside the entries: does the trail lead to the lists as they are now?

    ``capped`` says only that the trail HOLDS its cap (200 entries). It must never say entries were
    dropped: at exactly 200 with nothing dropped yet, that was untrue (round-2 review item 1). What
    the cap does with the entries that make room is said as the policy it is. A list that could not
    be read has no replay (``None``): the note then says the trail is incomplete for that list and
    that this read cannot say whether it leads to it (kittrial-5bb.229 finding 2), exactly as it
    says the trail is incomplete when the history has no baseline (finding 6) or when the baseline
    holds the list as ``null`` (rev-2 item 1). The incomplete sentence comes FIRST, so the
    unreadable list is named first (rev-2 item 2), and everything after it speaks only for the
    lists this read could compare.
    """
    if problem is not None and replay is None:
        return ('WARNING: the trail cannot be compared with the current lists: %s. The audit records only the '
                'changes the four list commands made here, so this read cannot say whether the trail leads to '
                'the lists.'%problem)
    incomplete=[noun for noun in AUTHORITY_CHANGES_LISTS if replay is None or replay.get(noun) is None]
    readable=[noun for noun in AUTHORITY_CHANGES_LISTS if noun not in incomplete]
    note=[]
    if incomplete:
        note.append(authority_changes_incomplete_sentence(incomplete,problem,unknown_in_baseline))
    if not entries and baseline is None:
        if incomplete:
            note.append('No trail yet: no operator or verifier list change has been recorded here, so there is '
                        'nothing to replay. The lists above are the ones this deployment holds and could be read; '
                        'the first change that CAN start the trail does. A REMOVAL is never refused for a list that '
                        'cannot be read: it starts the trail and the baseline records that list as null (UNKNOWN), '
                        'so the trail is incomplete for it. A GRANT (an add) is refused while a list cannot be read. '
                        'Reading again after a change says whether the trail leads to the lists.')
        else:
            note.append('No trail yet: no operator or verifier list change has been recorded here, so there is '
                        'nothing to replay. The lists above are the ones this deployment holds; the first change '
                        'starts the trail with a baseline of them, and reading again after it says whether the trail '
                        'leads to them.')
        return ' '.join(note)
    parts=[];listed_twice=[]
    for noun in readable:
        item=None if replay is None else replay.get(noun)
        if item is None:
            continue
        if item['listed_more_than_once']:
            listed_twice.append('%s in %s'%(', '.join(item['listed_more_than_once']),noun))
        if item['agrees']:continue
        detail=[]
        if item['listed_but_last_removed']:
            detail.append('the list holds %s, whose last recorded change is a remove'
                          %', '.join(item['listed_but_last_removed']))
        if item['trail_added_but_not_listed']:
            detail.append('the trail adds %s but the list does not hold it'
                          %', '.join(item['trail_added_but_not_listed']))
        if item['listed_but_not_in_trail']:
            detail.append('the list holds %s, which the trail never mentions'
                          %', '.join(item['listed_but_not_in_trail']))
        parts.append('%s: %s'%(noun,'; '.join(detail)))
    if readable and parts:
        note.append('WARNING: the trail does not lead to %s - '%authority_changes_lists_phrase(readable)
                    +' | '.join(parts)+'.')
        if baseline is None:
            note.append('A hand edit of deployment.private.json leaves no entry here; so does a list change made by a '
                        'kit older than this one. This trail does not begin with a baseline either, so it may simply '
                        'be older than the lists: nothing here can tell those apart. The trail is incomplete for '
                        'these lists.')
        else:
            note.append('A hand edit of deployment.private.json leaves no entry here; so does a list change made by a '
                        'kit older than the baseline this trail begins with (at %s).'%baseline['at'])
    elif readable and baseline is None:
        note.append('The trail leads to %s, but it does not begin with a baseline, so it was written '
                    'before this kit or by hand and may be older than the lists. The trail is incomplete for these '
                    'lists.'%authority_changes_lists_phrase(readable))
    elif readable:
        note.append('The trail leads to %s: every name listed now was put there by an entry or by the '
                    'baseline this trail begins with (at %s), and every name the trail adds is listed.'
                    %(authority_changes_lists_phrase(readable),baseline['at']))
    if listed_twice:
        note.append('WARNING: deployment.private.json lists %s more than once; the trail compares names as a set.'
                    %'; '.join(listed_twice))
    if capped:
        note.append('The trail is at its cap of %d entries: the newest are kept, and when the next change needs room '
                    'the oldest entries are folded into the baseline rather than lost, so the trail still leads to '
                    'the lists.'%AUTHORITY_CHANGES_MAX)
    return ' '.join(note)

def authority_changes_report(root):
    """``(report, note)`` for the ``authority-changes`` reader: the trail, the lists and the replay.

    The report prints the current lists beside the entries, counts and marks the entries that name
    nobody, prints the baseline the trail begins with and any damaged audit kept beside the
    runtime, and replays the trail against the lists (kittrial-5bb.192 review items 3a and 3b,
    round-2 review items 1 and 2). ``replay.agrees`` is ``true`` when the trail leads to the lists,
    ``false`` when it does not, and ``null`` when there is no trail yet or when the trail is
    INCOMPLETE for a list (it could not be read now, or the baseline holds it as ``null``/UNKNOWN
    because it could not be read when that history began; kittrial-5bb.229 rev-2 item 1); ``replay``
    itself is ``null`` when the lists cannot be read at all. The exit code is 0 in all of those
    cases, so a script must read ``replay.agrees`` rather than the exit status (round-2 review item
    4).
    """
    entries,baseline,damage=read_authority_changes(root)
    if damage is not None:raise AuthorityAuditDamaged(authority_audit_refusal(root,damage,'read'))
    listed,problem=authority_changes_current_lists(root)
    capped=len(entries)>=AUTHORITY_CHANGES_MAX
    kept=authority_change_damaged_files(root)
    # A list the baseline holds as null (UNKNOWN) could not be read when that history began: it has
    # no replay either, and the note says so in those words (kittrial-5bb.229 rev-2 item 1).
    unknown_in_baseline=[noun for noun in AUTHORITY_CHANGES_LISTS
                         if baseline is not None and baseline['lists'].get(noun) is None]
    report={'schema_version':AUTHORITY_CHANGES_SCHEMA,
            'entries':[dict(item,unattributed=True) if item['operator'] is None else item for item in entries],
            'unattributed_entries':sum(1 for item in entries if item['operator'] is None),
            'baseline':None if baseline is None else
                       {'at':baseline['at'],'operator':baseline['operator'],'reason':baseline['reason'],
                        'entries':sum(len(baseline['lists'][noun]) for noun in AUTHORITY_CHANGES_LISTS
                                      if baseline['lists'][noun] is not None),
                        'unknown_lists':list(unknown_in_baseline),
                        'lists':{noun:(None if baseline['lists'][noun] is None else list(baseline['lists'][noun]))
                                 for noun in AUTHORITY_CHANGES_LISTS}},
            'damaged_files':kept,
            'current_lists':listed,
            'current_lists_problem':problem}
    no_trail=not entries and baseline is None
    if listed is None:
        report['replay']=None
        note=authority_changes_replay_note(None,entries,capped,problem,baseline,unknown_in_baseline)
    else:
        replay={noun:(None if listed[noun] is None else authority_change_replay(entries,listed,noun,baseline))
                for noun in AUTHORITY_CHANGES_LISTS}
        note=authority_changes_replay_note(replay,entries,capped,problem,baseline,unknown_in_baseline)
        readable=all(replay[noun] is not None for noun in AUTHORITY_CHANGES_LISTS)
        if not readable:
            # One list could not be read (now, or when the baseline was written): the trail is
            # incomplete for it, so this read cannot say whether it leads to the lists
            # (kittrial-5bb.229 finding 2 and rev-2 item 1). With no trail at all the state is still
            # no-trail; the note says which list could not be read, and says it first (rev-2 item 2).
            agrees=None;state='no-trail' if no_trail else 'incomplete'
        else:
            agrees=None if no_trail else all(replay[noun]['agrees'] for noun in AUTHORITY_CHANGES_LISTS)
            state='no-trail' if no_trail else ('agrees' if agrees else 'mismatch')
        report['replay']={'agrees':agrees,'state':state,'lists':replay,'note':note}
    if kept:
        note+=' The kit has set %d damaged audit file(s) aside beside this runtime - %s - and never removes them.'\
              %(len(kept),', '.join(kept))
    if report['replay'] is not None:report['replay']['note']=note      # the JSON and stderr say the same thing
    return report,note

def credential_actors(root,state_path,service_namespace=None):
    """Every worker credential of the web service with the name it writes under, and whether
    that name is somebody else's on this host (kittrial-5bb.184). Reads; changes nothing.

    A worker credential writes under the actor namespace its issuer chose. Before
    kittrial-5bb.184 an owner could choose a registered session actor, a name on the operator
    or verifier list or the service's own namespace, and the credential then acted as that
    actor. Such a credential is refused when it writes from that kit on; this lists them, so
    an operator can tell their owners. Nothing is revoked here: revoking is the owner's, in
    the web interface.

    ``tracker_rows`` says whether the project's tracker already has rows under the name (an
    assignee, a creator, a comment author). For a name that collides they may be the real
    actor's or the credential's: the rows cannot tell. For a name that does not, they are
    what a credential under that name wrote, or an old actor from before sessions were
    registered: worth a look when nobody remembers issuing it.

    ``collides`` now holds the rows in too (kittrial-5bb.188 item 1): a plain name the tracker
    was already holding before the credential was issued is refused when it writes, and the
    credential is listed here. A credential issued after that rule carries
    ``actor_rows_checked`` and is judged only against the rows older than its own issuance, so
    the rows it wrote itself are not held against it. A credential issued before the rule is
    judged by the rows once, and the outcome is kept on it (``actor_rows_checked`` or
    ``actor_rows_refused``; item 5). A name a superuser waived carries ``actor_waived``
    (item 4): it is listed as allowed -- ``collides`` null, ``refused_when_it_writes`` false,
    with ``waived``, ``waived_by_username``, ``waived_at``, ``waived_reason`` and an
    ``actor_allowed`` sentence -- because it writes. Rows inside an earlier same-name,
    same-owner credential's lifetime are not held either (item 3). An export that answers no
    rows is said as ``tracker_rows`` null, not false: the tracker was not read. The rows are
    parsed with the row bound of kittrial-5bb.141 (``record_json.loads_rows``,
    ``ROW_NESTING_MAX`` 750): rows nested up to 750 levels read normally, and an export that
    holds ANY unreadable row is one the tracker could not be read from -- ``tracker_rows``
    null for every credential of that project, with the unreadable row ids named beside it
    (``unreadable_rows``), so an operator sees there is something unread rather than "no
    such rows" (kittrial-5bb.221 revision 2).
    ``service_namespace`` is the namespace the web service was started with (its
    ``--actor-namespace``), when the operator says so: a credential named under it is refused
    at use too (kittrial-5bb.188 item 3).
    """
    import actor_names
    from datetime import datetime,timezone
    from sessions import registered_actors
    source=Path(state_path)
    if not source.is_file():raise ValueError('No web service state at %s'%source)
    try:state=json.loads(source.read_text(encoding='utf-8'))
    except (ValueError,RecursionError):raise ValueError('The web service state at %s is not readable as JSON'%source) from None
    if not isinstance(state,dict) or not isinstance(state.get('credentials'),dict):
        raise ValueError('The file at %s is not a web service state document'%source)
    users=state.get('users') if isinstance(state.get('users'),dict) else {}
    listed_operators,listed_verifiers=sorted(operators(root)),sorted(verifiers(root))
    def moment(value):
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            return datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return value if isinstance(value,str) else None
    projects={}
    def project(name):
        if name not in projects:
            found={'on_host':False,'sessions':[],'marks':None}
            try:path=project_dir(root,name)
            except ValueError:path=None
            if path is not None and (path/'.beads/metadata.json').is_file():
                found['on_host']=True
                found['sessions']=registered_actors(path)
                try:
                    text=run_bd(root,name,['export','--all'])
                    rows=record_json.loads_rows(text)
                    unreadable=[row.get('id') if isinstance(row,dict) else None
                                for row in rows if not isinstance(row,dict) or row.get('malformed')]
                    if unreadable:
                        # kittrial-5bb.221 revision 2: an unreadable row may be the row that holds the
                        # name, so no name of this project can be judged; said as null with the row ids
                        # named, never as "no such rows".
                        found['unreadable_rows']=unreadable
                        raise ValueError('the export holds %d unreadable row(s)'%len(unreadable))
                    if not rows or not any(isinstance(row,dict) for row in rows):
                        raise ValueError('the export answered no rows, or something that is not rows')
                    # A listing, and main's answer here: an export that answered nothing is
                    # ``tracker_rows`` null ("the tracker was not read"), never false ("the
                    # tracker has no names"), which an operator would take as a read. This read
                    # does NOT run the merge-slot decision ``endpoint.tracker_actors`` runs, so
                    # the empty-project decision does not reach it (kittrial-5bb.202 review of
                    # revision 2, F3: revision 2 had changed it to false).
                    found['marks']=actor_names.tracker_marks(rows)
                except (subprocess.CalledProcessError,OSError,ValueError,RecursionError):
                    found['marks']=None                # the tracker could not be read: said as null, not as "no rows"
            projects[name]=found
        return projects[name]
    out=[]
    for identifier in sorted(state['credentials']):
        credential=state['credentials'][identifier]
        if not isinstance(credential,dict) or credential.get('agent_id'):continue
        namespace=credential.get('actor')
        if not isinstance(namespace,str) or not namespace:continue         # it writes under its issuer's own account id
        name=credential.get('project_id')
        host=project(name) if isinstance(name,str) else {'on_host':False,'sessions':[],'marks':None}
        issued=credential.get('created_at')
        before=issued if isinstance(issued,(str,int,float)) and not isinstance(issued,bool) else None
        authors=None if host['marks'] is None else actor_names.tracker_names(host['marks'],before)
        reason=actor_names.collision(namespace,sessions=host['sessions'],operators=listed_operators,
                                     verifiers=listed_verifiers,authors=authors or (),
                                     service=(service_namespace,actor_names.SERVICE_NAMESPACE)
                                     if service_namespace else actor_names.SERVICE_NAMESPACE)
        issuer=users.get(credential.get('user_id')) if isinstance(users.get(credential.get('user_id')),dict) else {}
        waived=credential.get('actor_waived') if isinstance(credential.get('actor_waived'),dict) else None
        if waived is not None:
            # A superuser allowed this name on purpose (kittrial-5bb.188 item 4): it is not
            # colliding and it is not refused when it writes; the listing says who allowed it
            # and when, in plain words as well as in fields.
            reason=None
        item={'credential':identifier,'project':name,'project_on_host':host['on_host'],'label':credential.get('label'),
              'actor':namespace,'collides':reason,'revoked':bool(credential.get('revoked')),
              'refused_when_it_writes':reason is not None,
              'tracker_rows':None if host['marks'] is None else actor_names.head(namespace) in {n for n,_ in host['marks']},
              'issued_by':credential.get('user_id'),'issued_by_username':issuer.get('username'),
              'created_at':moment(credential.get('created_at')),'last_used':moment(credential.get('last_used')),
              'expires_at':moment(credential.get('expires_at'))}
        if waived is not None:
            item['waived']=True
            item['waived_by']=waived.get('by')
            item['waived_by_username']=issuer.get('username')
            item['waived_at']=moment(waived.get('at'))
            item['waived_reason']=waived.get('reason')
            item['actor_allowed']='allowed by %s on %s' % (issuer.get('username') or waived.get('by') or 'a superuser',
                                                            item['waived_at'] or 'an unrecorded date')
        if host.get('unreadable_rows') is not None:
            # kittrial-5bb.221 revision 2: this project's tracker holds unreadable row(s), so
            # tracker_rows is null (could not be read) and the row ids are named, so an
            # operator sees there is something unread rather than "no such rows".
            item['unreadable_rows']=host['unreadable_rows']
        out.append(item)
    colliding=[item for item in out if item['collides'] is not None and not item['revoked']]
    return {'schema_version':1,'state':str(source),'worker_credentials_with_a_name':len(out),
            'colliding_and_not_revoked':len(colliding),'credentials':out}

class _OnceFlag(argparse.Action):
    """A flag that may be given at most once.

    argparse's default ``store`` keeps the LAST value silently, so ``--actor a --actor b``
    recorded ``b`` with nothing said (kittrial-5bb.192 review item 4). The second occurrence is
    refused instead: whose change it was must never depend on the order of the arguments. An
    ABBREVIATED spelling never reaches this action: the subparsers that take these flags
    (``operators``, ``verifiers`` and, since round-2 review item 3, ``restore-new``) are built
    with ``allow_abbrev=False``, so argparse refuses ``--act``/``--reas`` itself with exit 2.
    """
    def __call__(self,parser,namespace,values,option_string=None):
        if getattr(namespace,self.dest,None) is not None:
            raise ValueError('%s was given more than once; give it once. The value recorded would otherwise be the '
                             'last one, silently.'%(option_string or self.dest))
        setattr(namespace,self.dest,values)

def one_flag_value(values,label):
    """The one value of a CLI flag that may be named at most once.

    ``adopt-actor``'s ``--principal``, ``--from``, ``--actor`` and ``--reason`` are ``append``
    arguments, so a flag named twice is refused here rather than silently taking the last
    (kittrial-5bb.223, finding 4); the parser stores one or more values in a list.
    """
    if isinstance(values,list):
        if len(values)>1:
            raise ValueError('%s given more than once; name it once (got %s)'
                             %(label,', '.join(str(value) for value in values)))
        return values[0] if values else None
    return values

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('install');a.add_argument('--port',type=int,default=13317);a.add_argument('--unit',default='beads-team.service')
    a=sub.add_parser('add-project');a.add_argument('project')
    a=sub.add_parser('finish-project',help='complete a project creation the web interface started and that stopped half way')
    a.add_argument('project')
    a=sub.add_parser('project-creations',help='list the project creations the web interface started (JSON), or set the limit of project databases on this server')
    a.add_argument('--attention',action='store_true',help='only the ones that are running or that an operator must finish or remove')
    a.add_argument('--usage',action='store_true',help='how many project databases this server holds, and its limit')
    a.add_argument('--set-server-limit',type=int,metavar='N',help='set the limit of project databases (operator allowlist, audited); needs --actor')
    a.add_argument('--actor')
    a=sub.add_parser('remove-creation',help='remove a project creation that did not finish (operator allowlist); refuses a finished project and a running creation')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a=sub.add_parser('set-onboarding');a.add_argument('project');a.add_argument('--file',required=True)
    a=sub.add_parser('set-guidance',help='set the standing coordinator guidance every actor reads each run (operator allowlist, audited)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('guidance-status',help='which actors have acknowledged which guidance version (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True)
    a=sub.add_parser('clear-guidance',help='remove the project standing guidance and its audit record (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True)
    a=sub.add_parser('compact-guidance-acks',help='drop guidance acknowledgements for versions no longer current or previous (operator allowlist, audited)')
    a.add_argument('project');a.add_argument('--actor',required=True)
    a=sub.add_parser('handoff');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('requirement-backfill');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('requirement-apply');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('requirement-reconcile');a.add_argument('project');a.add_argument('--operation-id',required=True)
    a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a.add_argument('--disposition',choices=['failed','released','complete'],default='released')
    a.add_argument('--issue-id',dest='issue_id',default=None,
                   help='with --disposition complete, the exact native record to confirm')
    a=sub.add_parser('capability-apply',help='accept a batch of capabilities, or a direct revision 1 (operator allowlist, F3 evidence)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('capability-retire',help='retire a capability in favour of a successor key (operator allowlist, F3 evidence)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('capability-alias-reject',help='reject a pending capability alias (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('capability-alias-propose',help='propose a capability alias as a verified operator (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('capability-misses-clear',help='delete a project\'s capability lookup-miss log (telemetry; not backed up)')
    a.add_argument('project')
    a=sub.add_parser('reference-misses-clear',help='delete a project\'s reference lookup-miss log (telemetry; not backed up)')
    a.add_argument('project')
    a=sub.add_parser('reference-apply',help='accept a reference catalog entry, or a batch of them with items (operator allowlist, F3 evidence)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('proposal-review',help='record a coordinator disposition on a requirement proposal (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('proposal-decide',help='record the owner decision on an escalated requirement proposal (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('proposal-http-records',help='list the proposal revisions and dispositions whose native author has an HTTP account or agent id shape, with their native creation time (read-only; run after an upgrade and after a rollback)')
    a.add_argument('project');a.add_argument('--before',default=None,help='only records natively created before this UTC stamp (YYYY-MM-DDTHH:MM:SSZ), the deploy time of the kit with the reservation')
    a.add_argument('--after',default=None,help='only records natively created at or after this UTC stamp; with --before, the window in which an older kit was the endpoint')
    a=sub.add_parser('proposal-settings',help='read or change the contribution settings: the actor map and the owner deciders (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True)
    a.add_argument('--map-actor',dest='map_actor',metavar='ACTOR',help='map a session actor to a person, with --to')
    a.add_argument('--namespace',metavar='NAME',help='map a session name (and NAME/..., NAME-...) to a person, with --to')
    a.add_argument('--to',metavar='IDENTITY',help='account:<uid> or person:<name>')
    a.add_argument('--unmap-actor',dest='unmap_actor',metavar='ACTOR');a.add_argument('--unmap-namespace',dest='unmap_namespace',metavar='NAME')
    a.add_argument('--add-decider',dest='add_decider',metavar='IDENTITY');a.add_argument('--remove-decider',dest='remove_decider',metavar='IDENTITY')
    for name in ('reference-reconcile','capability-reconcile','proposal-reconcile','record-reconcile'):
        a=sub.add_parser(name);a.add_argument('project');a.add_argument('--operation-id',required=True)
        a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
        a.add_argument('--disposition',choices=['failed','released','complete'],default='released')
        a.add_argument('--issue-id',dest='issue_id',default=None,
                       help='with --disposition complete, the exact native record to confirm')
        if name=='record-reconcile':a.add_argument('--kind',choices=['requirement','reference','capability','proposal'],required=True)
    a=sub.add_parser('void-record');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('anchor-release',help='close a reference or capability anchor that holds no record and free its key, or with --duplicate release a named duplicate anchor of a key (operator allowlist)')
    a.add_argument('project');a.add_argument('--kind',choices=['reference','capability'],required=True)
    a.add_argument('--issue-id',dest='issue_id',required=True);a.add_argument('--actor',required=True)
    a.add_argument('--reason',required=True)
    a.add_argument('--duplicate',action='store_true',
                   help='release a NAMED anchor of a duplicated key although it holds well-formed records; another anchor of the key must remain')
    a.add_argument('--set-aside-evidence',action='store_true',dest='set_aside_evidence',
                   help='with --duplicate: release an anchor that carries acceptance evidence; with live evidence, a remaining anchor must have live evidence too')
    a=sub.add_parser('revert-record');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('operators',allow_abbrev=False,
                     help='the installation operator allowlist: who may run the operator-gated host '
                          'commands; add and remove are recorded in the authority-changes audit')
    a.add_argument('action',choices=['list','add','remove']);a.add_argument('actor',nargs='?')
    a.add_argument('--actor',dest='operator',default=None,metavar='OPERATOR',action=_OnceFlag,
                   help='with add/remove: the operator making this change, recorded in the authority-changes '
                        'audit (the change still applies without it, and the entry records null). Given twice, '
                        'refused')
    a.add_argument('--reason',default=None,action=_OnceFlag,
                   help='with add/remove: why the list is changed, recorded in the '
                        'authority-changes audit (at most %d characters). Given twice, refused'
                        %AUTHORITY_CHANGES_REASON_MAX)
    a.add_argument('--confirm','--confirm-revoke',action='store_true',dest='confirm_revoke',
                   help='with remove: acknowledge that this operator\'s earlier operator voids stop applying')
    a.add_argument('--a','--all','--all-revoked',action='store_true',dest='all_revoked',
                   help='with remove: name every affected entry instead of the first 5 (the warning truncates otherwise)')
    a=sub.add_parser('verifiers',allow_abbrev=False,
                     help='the capability verifiers list: actors whose capability-verify records read verified')
    a.add_argument('action',choices=['list','add','remove']);a.add_argument('actor',nargs='?')
    a.add_argument('--actor',dest='operator',default=None,metavar='OPERATOR',action=_OnceFlag,
                   help='with add/remove: the operator making this change, recorded in the authority-changes '
                        'audit (the change still applies without it, and the entry records null). Given twice, '
                        'refused')
    a.add_argument('--reason',default=None,action=_OnceFlag,
                   help='with add/remove: why the list is changed, recorded in the '
                        'authority-changes audit (at most %d characters). Given twice, refused'
                        %AUTHORITY_CHANGES_REASON_MAX)
    a.add_argument('--confirm','--confirm-revoke',action='store_true',dest='confirm_revoke',
                   help='with remove: acknowledge that this verifier\'s capability verifications stop reading verified')
    a=sub.add_parser('review-writes',help='read or set the per-installation switch that allows WRITING the new review-workflow record shapes (readers understand them either way; OFF by default)')
    a.add_argument('action',choices=['status','on','off'])
    a.add_argument('--actor',required=True,help='an actor on the deployment operator allowlist')
    a=sub.add_parser('checkpoint-provenance-writes',help='operator-audited reader-first checkpoint rollout switch; OFF by default, existing provenance tasks refuse legacy writes')
    a.add_argument('action',choices=['status','on','off'])
    a.add_argument('--actor',required=True,help='an actor on the deployment operator allowlist')
    a=sub.add_parser('capability-verify',help='record verified capability checks (operator allowlist or verifiers list)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('authorized-keys',help='print the confined contributor and unrestricted operator authorized_keys lines for one public key')
    a.add_argument('--key-file',required=True,help='a file holding one plain OpenSSH public key line')
    a.add_argument('--role',choices=['contributor','operator','both'],default='both',
                   help='which line(s) to print (default: both, for different keys)')
    a.add_argument('--python',default=None,help='interpreter in the contributor forced command (default: this interpreter, or /usr/bin/python3)')
    a.add_argument('--comment',default=None,help='replace the key line comment')
    a.add_argument('--project',action='append',default=None,metavar='NAME',
                   help='bind the contributor line to this project (repeatable): the endpoint then refuses every '
                        'request of that key for another project. Without it the key may name any project')
    a.add_argument('--principal',action='append',default=None,metavar='NAME',
                   help='bind the contributor line to this principal (a lane, lane:NAME or person:NAME): the '
                        'endpoint then refuses every request of that key whose actor that principal does not own '
                        'in the project. Without it the key acts as any actor. Given twice, refused')
    a=sub.add_parser('authorized-keys-list',help='read-only: every line of authorized_keys, what it may do here, the '
                     'projects and the principal it is bound to, and whether it points at the installed kit')
    a.add_argument('--file',default=None,help='the authorized_keys file to read (default: ~/.ssh/authorized_keys of this account)')
    a=sub.add_parser('adopt-actor',help='give an existing actor to a principal (a lane) in one project, so a key '
                                        'bound to that principal may act as it; recorded in the adoption audit '
                                        '(operator allowlist)',allow_abbrev=False)
    a.add_argument('project',help='the project whose registry records the actor')
    a.add_argument('actor',metavar='ACTOR',help='the actor name to give to the principal, as it already appears in '
                                                'the project (a session actor, or an older name with tracker rows)')
    a.add_argument('--principal',required=True,action='append',default=None,metavar='NAME',
                   help='the principal (a lane, lane:NAME or person:NAME) that actor is to belong to (given twice, refused)')
    a.add_argument('--from',dest='from_principal',action='append',default=None,metavar='NAME',
                   help='the principal that currently owns the actor, required to MOVE an actor another principal '
                        'already owns: without it the move is refused (given twice, refused)')
    a.add_argument('--actor',required=True,dest='operator',action='append',default=None,metavar='OPERATOR',
                   help='the actor performing the adoption, on the deployment operator allowlist (given twice, refused)')
    a.add_argument('--reason',required=True,action='append',default=None,
                   help='why this actor is being adopted (recorded in the audit; given twice, refused)')
    a=sub.add_parser('actor-adoptions',help='read-only: the recorded actor adoptions (who gave which actor to which '
                                            'principal, when and why)')
    a.add_argument('project',nargs='?',help='only the adoptions of this project')
    a=sub.add_parser('authority-changes',help='read-only: the recorded changes of the installation operator and '
                                              'verifier lists (which list changed, who changed it, when and why)')
    a=sub.add_parser('backup');a.add_argument('projects',nargs='*',metavar='project')
    a.add_argument('--all',action='store_true',dest='all_projects',
                   help='back up every initialized project in this runtime in one run')
    a=sub.add_parser('retire-project',help='retire a partial or drill project: move it to retired/; deletes nothing (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a.add_argument('--force',action='store_true',
                   help='retire it although it looks like a working tracker (or could not be checked), holds the merge slot or has pending reservations')
    a=sub.add_parser('backup-authority',help='read only: the operators and verifiers a project backup records, '
                                             'against what this installation lists now')
    a.add_argument('project')
    a=sub.add_parser('open-item-label-check',help='read only, at deploy time: list the projects whose rows already carry '
                                                 'the label open-item or an open-item: label (exit 1 when any does, or '
                                                 'cannot be read)')
    a.add_argument('projects',nargs='*',metavar='project')
    a=sub.add_parser('merge-slot-report',help='read only: the projects whose merge slot is missing, damaged or cannot '
                                              'be read, with the merge-create repair (exit 1 when any is not healthy)')
    a.add_argument('projects',nargs='*',metavar='project')
    a=sub.add_parser('backup-status')
    a.add_argument('--require-complete',action='store_true',dest='require_complete',
                   help='exit non-zero unless the last run covered every project (--all) and every initialized '
                        'project has a complete pair on disk')
    a.add_argument('--require-clean',action='store_true',dest='require_clean',
                   help='everything --require-complete checks, and also exit non-zero when any project is '
                        'recorded degraded (complete, but missing something it should carry); for a release gate')
    a=sub.add_parser('backup-copy');a.add_argument('destination',metavar='DEST',
                   help='copy every project\'s last complete backup pair under this off-machine directory; '
                        'refuses unless backup-status --require-complete would pass')
    a.add_argument('--require-clean',action='store_true',dest='require_clean',
                   help='also refuse when any project is recorded degraded; without it a degraded project is '
                        'copied and named')
    a=sub.add_parser('backup-repoint');a.add_argument('project')
    a=sub.add_parser('restore-new',allow_abbrev=False,
                     help='restore a backup into a new project; --restore-operators/--restore-verifiers '
                          're-grant the authority the backup records, recorded in the authority-changes audit')
    a.add_argument('project');a.add_argument('destination')
    a.add_argument('--restore-operators',action='store_true',dest='restore_operators',
                   help='explicitly re-grant the operator allowlist entries the backup records that this '
                        'host no longer lists; off by default because the allowlist is deployment-wide '
                        'authority for every project and a stale backup must not undo a revocation')
    a.add_argument('--restore-verifiers',action='store_true',dest='restore_verifiers',
                   help='explicitly re-grant the capability verifiers the backup records that this host no '
                        'longer lists; off by default for the same reason as --restore-operators')
    a.add_argument('--actor',default=None,metavar='OPERATOR',action=_OnceFlag,
                   help='with --restore-operators/--restore-verifiers: the operator making the re-grant, recorded '
                        'in the authority-changes audit (the re-grant still applies without it, and the entry '
                        'records null, with one sentence on stderr). Given twice, or abbreviated, refused')
    a.add_argument('--reason',default=None,action=_OnceFlag,
                   help='with --restore-operators/--restore-verifiers: why the re-grant is made; the recorded '
                        'reason names restore-new and the source project and then this sentence, which must leave '
                        'room for that prefix inside the %d-character ceiling. Given twice, or abbreviated, refused'
                        %AUTHORITY_CHANGES_REASON_MAX)
    a.add_argument('--without-coordination',action='store_true',dest='without_coordination',
                   help='restore only the native tracker data of a backup whose coordination sidecar exists but '
                        'cannot be used (restore-new refuses such a backup without this flag); its sessions, '
                        'handoffs, requests, merge context and recorded operators and verifiers are not restored')
    a.epilog=('Exit status: 0 restored; 3 restored, but --restore-operators/--restore-verifiers could not '
              're-grant (another change held the deployment lock, or the authority-changes audit is damaged): '
              'the last lines say what, with the commands to re-grant it; 1 failed. --actor OPERATOR and '
              '--reason TEXT are recorded on every re-grant in the authority-changes audit, and one sentence on '
              'stderr says so when they were not given. Compare a backup with this installation: '
              'backup-authority PROJECT.')
    a=sub.add_parser('reconcile-request');a.add_argument('project');a.add_argument('--request-id',required=True)
    a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a.add_argument('--disposition',choices=['failed','released','complete'],default='released')
    a.add_argument('--issue-id',dest='issue_id',default=None,
                   help='with --disposition complete, the exact labelled native issue to confirm and attach')
    a.add_argument('--any-actor',action='store_true',dest='any_actor',
                   help='with --disposition released (or failed on a receipt with no recorded actor), open the request ID to any actor')
    a=sub.add_parser('service');a.add_argument('action',choices=['start','stop','restart','status'])
    a=sub.add_parser('credential-actors',help='list the web service\'s worker credentials and whether the name each writes under is somebody else\'s on this host (reads only)')
    a.add_argument('--service-namespace',default=None,metavar='NS',dest='service_namespace',
                   help='the namespace the web service was started with (its --actor-namespace): a credential named '
                        'under it is listed as refused too. Without it only http is judged')
    a.add_argument('--state',required=True,help='the web service state document (the --state of http_service.py)')
    a=sub.add_parser('record-store')
    a.add_argument('--state',required=True,
                   help='the HTTP service state document (its --state); the record store is '
                        '<state>.records.sqlite3. Both must already exist: a missing path is '
                        'refused, never created')
    a.add_argument('--reset-high-water',action='store_true',dest='reset_high_water',
                   help='set the record store high-water mark to the current clock and clear suspicion '
                        '(keeps jump_credit); the auth-clock recovery after a corrected forward jump')
    a=sub.add_parser('journal');a.add_argument('project')
    a.add_argument('--retention',type=float,default=None,
                   help='uncertain-reservation window in seconds for this command (default 7 days)')
    a.add_argument('--committed-retention',type=float,dest='committed_retention',default=None,
                   help='replayable committed-receipt window in seconds (default 1 day)')
    a.add_argument('--reclaim-expired',action='store_true',
                   help='compact identities whose receipt window has closed into tombstones')
    a.add_argument('--prune-before',type=float,default=None,
                   help='hard-remove identities last touched before this epoch second (after reconciling)')
    a.add_argument('--reset-high-water',action='store_true',dest='reset_high_water',
                   help='set the high-water mark to the current clock and clear clock suspicion (after a clock correction)')
    a.add_argument('--stats',action='store_true',
                   help='report the journal size (rows by state, bytes on disk, bounds); this is the default inspection')
    args=p.parse_args();root=root_path(args.root)
    if args.command=='install':install(root,args.port,args.unit)
    elif args.command=='add-project':add_project(root,args.project)
    elif args.command=='finish-project':
        import project_creation
        result=project_creation.finish(root,args.project)
        already=result.pop('already',False)
        print(json.dumps(result,sort_keys=True))
        if already:
            print('%s is complete on the server already: there was nothing to finish. If it is not in the web '
                  'interface, the account that started it creates it again there (the same name), or a superuser '
                  'registers it.'%args.project,file=sys.stderr)
        else:
            print('Finished %s on the server. If it is not in the web interface yet, the account that started it '
                  'creates it again there (the same name), or a superuser registers it.'%args.project,file=sys.stderr)
    elif args.command=='project-creations':
        import project_creation
        if args.set_server_limit is not None:
            if not args.actor:raise ValueError('--set-server-limit requires --actor (an operator on the allowlist)')
            from recovery import identity
            print(json.dumps(project_creation.set_server_limit(root,args.set_server_limit,identity(args.actor,'Invalid actor identity')),sort_keys=True))
            print('The limit counts every project database on this server: archived and retired projects and '
                  'unfinished creations too. Every bd write gets slower as their number grows; see OPERATIONS, '
                  '"The cost of many projects on one server".',file=sys.stderr)
        elif args.usage:
            print(json.dumps(project_creation.server_usage(root),sort_keys=True))
        else:
            found=project_creation.attention(root) if args.attention else project_creation.records(root)
            print(json.dumps(found,sort_keys=True,indent=1))
    elif args.command=='remove-creation':
        import project_creation
        result=project_creation.remove(root,args.project,args.actor,args.reason)
        print(json.dumps(result,sort_keys=True))
        if result['removed']=='damaged-record':
            print('The creation record of %s could not be read. It is kept as project-creations/%s and nothing else '
                  'was touched. %s'%(args.project,result['kept_as'],
                  'Nothing is under projects/%s, so the name is free again.'%args.project if result['name']=='free' else
                  'projects/%s exists and is now a project with no creation record: register it in the web interface '
                  'if it is complete, or retire it (admin.py retire-project %s --actor OPERATOR --reason REASON).'
                  %(args.project,args.project)),file=sys.stderr)
        elif result['name']=='free':
            print('Removed the creation record of %s. Nothing had been made for it, so the name is free again.'
                  %args.project,file=sys.stderr)
        else:
            print('Removed the unfinished creation of %s: its directory is retired and nothing was deleted. The '
                  'name stays retired, because a database of that name may be on the server.'%args.project,file=sys.stderr)
    elif args.command=='set-onboarding':
        import fcntl
        from onboarding import probe_endpoints, write_project
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            text=Path(args.file).read_text(encoding='utf-8-sig')
            write_project(path/'ONBOARDING.md',text)
        # Warn after the atomic write: an endpoint that refuses this project must never
        # be installed silently, but a warning must not block the operator's update.
        for warning in probe_endpoints(text,args.project,Path(__file__).resolve().parent):
            print(warning,file=sys.stderr)
        print('Project onboarding installed; back up the project after changes.')
    elif args.command=='set-guidance':
        import fcntl
        from guidance import write_guidance
        from keyed_records import require_configured_operator
        # The operator allowlist is checked before the project is read or any file
        # is written, so a contributor actor cannot set guidance even if it reaches
        # the host command line.
        require_configured_operator(args.actor,operators(root,strict=True),'set the project guidance')
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        text=Path(args.file).read_text(encoding='utf-8-sig')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=write_guidance(path,text,args.actor)
        outcome='installed' if result['changed'] else ('repaired' if result.get('repaired') else 'unchanged')
        print('Project guidance %s (version %s); back up the project after changes.'
              %(outcome,result['version']))
        if result.get('repaired') and not result['changed']:
            print('The audit record was missing or did not match the text; it is now bound to the text you set.')
        if result.get('warning'):
            print(result['warning']+'.')
        if result.get('replaced_unreadable'):
            print('The guidance file that was on disk could not be read as guidance (a refused character, over the '
                  'limit, or not UTF-8) and was replaced. Nothing of it was kept in the record.')
        print(json.dumps(result,sort_keys=True))
    elif args.command=='clear-guidance':
        import fcntl
        from guidance import clear as guidance_clear
        from keyed_records import require_configured_operator
        require_configured_operator(args.actor,operators(root,strict=True),'clear the project guidance')
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=guidance_clear(path,args.actor)
        print('Project guidance cleared (%s); back up the project after changes. A small local record in %s keeps '
              'who cleared it, when and the cleared version (guidance-status shows it; it is not in the backup); '
              'the removed guidance record itself stays in the most recent coordination backup, if one was taken.'
              %(', '.join(result['removed']) or 'nothing was set',result.get('clear_record','the project directory')))
        if result.get('invalid_record_kept_as'):
            print('The clear record that was already there was not a valid record this kit wrote. It was kept as %s '
                  'and a new record was started.'%result['invalid_record_kept_as'])
        print(json.dumps(result,sort_keys=True))
    elif args.command=='compact-guidance-acks':
        import fcntl
        from guidance import compact as guidance_compact
        from keyed_records import require_configured_operator
        require_configured_operator(args.actor,operators(root,strict=True),'compact the project guidance acks')
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=guidance_compact(path,args.actor)
        print('Guidance acknowledgements compacted: removed %d for versions no longer current or previous.'
              %result['removed_total'])
        print(json.dumps(result,sort_keys=True))
    elif args.command=='guidance-status':
        import fcntl
        from guidance import status as guidance_status
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            guidance_report=guidance_status(path,args.actor,operators(root,strict=True),host=True)
        print(json.dumps(guidance_report,sort_keys=True,indent=2))
    elif args.command=='service':print(service(root,args.action))
    elif args.command=='credential-actors':
        report=credential_actors(root,args.state,getattr(args,'service_namespace',None))
        print(json.dumps(report,indent=2,sort_keys=True))
        if report['colliding_and_not_revoked']:
            print('%d worker credential(s) write under a name that is somebody else\'s on this host. Each is refused '
                  'when it writes; its owner revokes it in the web interface and issues one under another name. '
                  'Nothing was changed by this command.'%report['colliding_and_not_revoked'],file=sys.stderr)
    elif args.command=='record-store':
        try:
            report=record_store_reset(args.state) if args.reset_high_water \
                else record_store_stats(args.state)
        except ValueError as error:
            # A typo in --state must never fabricate an empty store beside the real one and
            # report success; refuse before anything is opened or created.
            raise SystemExit('record-store refused: '+str(error))
        print(json.dumps(report,sort_keys=True))
        if not args.reset_high_water:
            print('Inspection only: the record store monotone auth floor is unchanged. Use '
                  '--reset-high-water after correcting a clock that ran ahead, so sessions, worker '
                  'credentials and reset values issued while the floor was pinned stop being stamped '
                  'on it; jump_credit is kept either way.',file=__import__('sys').stderr)
    elif args.command=='reconcile-request':
        import fcntl
        from coordination import reconcile_request
        from keyed_records import require_configured_operator
        # Strict allowlist first (kittrial-5bb.85): every host command that writes on
        # another actor's behalf checks it. The actor-binding rules still apply on top.
        require_configured_operator(args.actor,operators(root,strict=True),'reconcile a coordination request')
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=reconcile_request(path,args.request_id,args.actor,args.reason,args.disposition,
                                     lambda argv: run_bd(root,args.project,argv),any_actor=args.any_actor,
                                     issue_id=args.issue_id)
        print(json.dumps(result,ensure_ascii=False))
    elif args.command=='handoff':
        import fcntl
        from handoff import execute as handoff
        path=project_dir(root,args.project)
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(handoff(path,args.actor,payload,run,operator=True)))
    elif args.command=='requirement-backfill':
        import fcntl
        from requirement_records import backfill
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        authority=operators(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(backfill(payload,args.actor,run,path,operators=authority)))
    elif args.command=='requirement-apply':
        import fcntl
        from requirement_records import apply_native
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        authority=operators(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(apply_native(payload,args.actor,run,path,operator=True,operators=authority)))
    elif args.command=='requirement-reconcile':
        import fcntl
        from requirement_records import reconcile
        from keyed_records import require_configured_operator
        require_configured_operator(args.actor,operators(root,strict=True),'reconcile a requirement operation')
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(reconcile(path,args.operation_id,args.actor,args.reason,
                                       args.disposition,run,issue_id=args.issue_id)))
    elif args.command=='reference-apply':
        import contextlib
        import fcntl
        import reference_records
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        authority=operators(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        @contextlib.contextmanager
        def held():
            # One hold of the project's coordination lock; closing the file releases it.
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                yield
        if isinstance(payload,dict) and 'items' in payload:
            # A batch (kittrial-5bb.98) takes the lock once per item and releases it between
            # items, exactly as capability-apply does.
            print(json.dumps(reference_records.apply_batch(payload,args.actor,run,path,operators=authority,lock=held)))
        else:
            if isinstance(payload,dict):payload.setdefault('operation','accept')
            with held():
                print(json.dumps(reference_records.apply_native(payload,args.actor,run,path,operator=True,operators=authority)))
    elif args.command in ('capability-apply','capability-retire','capability-alias-reject','capability-alias-propose'):
        import contextlib
        import fcntl
        import capability_records
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        authority=operators(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        @contextlib.contextmanager
        def held():
            # One hold of the project's coordination lock; closing the file releases it.
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                yield
        if args.command=='capability-apply' and isinstance(payload,dict) and 'items' in payload:
            # A batch takes the lock once per item and releases it between items, so
            # another writer waits behind at most one item (kittrial-5bb.67 review 01a0fc55).
            result=capability_records.apply_batch(payload,args.actor,run,path,operators=authority,lock=held)
        else:
            with held():
                if args.command=='capability-alias-reject':
                    result=capability_records.reject_alias(payload,args.actor,run,path,operators=authority)
                elif args.command=='capability-alias-propose':
                    if not isinstance(payload,dict) or set(payload)-{'schema_version','key','alias','evidence'} or payload.get('schema_version')!=1:
                        raise ValueError('alias payload must be {schema_version: 1, key, alias, evidence?}')
                    result=capability_records.propose_alias(payload.get('key'),payload.get('alias'),args.actor,run,authority,
                                                            evidence=payload.get('evidence'),operator=True)
                elif args.command=='capability-retire':
                    if isinstance(payload,dict):payload.setdefault('operation','retire')
                    result=capability_records.apply_native(payload,args.actor,run,path,operator=True,operators=authority)
                else:
                    result=capability_records.apply_native(payload,args.actor,run,path,operator=True,operators=authority)
        print(json.dumps(result))
    elif args.command in ('capability-misses-clear','reference-misses-clear'):
        # Telemetry only (kittrial-5bb.77; the reference log, kittrial-5bb.98): no tracker
        # write, no coordination lock, no bd call and no allowlist. The log is not in any
        # backup, so nothing else changes.
        import capability_misses
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        which=capability_misses.REFERENCE if args.command=='reference-misses-clear' else capability_misses.CAPABILITY
        print(json.dumps(dict(capability_misses.clear(path,which),project=args.project)))
    elif args.command=='capability-verify':
        import contextlib
        import fcntl
        import capability_verification
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        # Both lists are read strictly: a shell value that disagrees with the file is refused.
        authority=operators(root,strict=True);listed=verifiers(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        @contextlib.contextmanager
        def held():
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                yield
        print(json.dumps(capability_verification.verify_batch(payload,args.actor,run,operators=authority,
                                                              verifiers=listed,journal=path,lock=held)))
    elif args.command=='proposal-http-records':
        import proposal_records
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        for flag,value in (('--before',args.before),('--after',args.after)):
            if value is not None and not proposal_records.STAMP.fullmatch(value):
                raise ValueError('%s is a UTC stamp, YYYY-MM-DDTHH:MM:SSZ'%flag)
        if args.before is not None and args.after is not None and args.after>=args.before:
            raise ValueError('--after must be earlier than --before')
        # Read-only: no lock, no actor. It reads every proposal anchor with its comments.
        rows=proposal_records.read_rows(lambda argv:run_bd(root,args.project,argv))
        print(json.dumps(proposal_records.http_authored(rows,args.before,args.after),ensure_ascii=False))
    elif args.command in ('proposal-review','proposal-decide','proposal-settings'):
        # Requirement proposals (.58 slice 1a, kittrial-5bb.68). Everything that rests on
        # the operator allowlist is a host command, because over SSH the actor is
        # self-declared: the allowlist is read strictly here and checked before any read.
        import fcntl
        import proposal_records
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        authority=operators(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if args.command=='proposal-settings':
                changes={name:getattr(args,name) for name in proposal_records.SETTINGS_CHANGES}
                result=proposal_records.change_settings(changes,args.actor,run,operators=authority)
            else:
                payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
                result=proposal_records.dispose(payload,args.actor,run,path,operators=authority,
                                                route='decide' if args.command=='proposal-decide' else 'review')
        print(json.dumps(result))
    elif args.command in ('reference-reconcile','capability-reconcile','proposal-reconcile','record-reconcile'):
        import fcntl
        kind={'reference-reconcile':'reference','capability-reconcile':'capability','proposal-reconcile':'proposal'}.get(args.command) or args.kind
        if kind=='reference':from reference_records import reconcile as record_reconcile
        elif kind=='capability':from capability_records import reconcile as record_reconcile
        elif kind=='proposal':from proposal_records import reconcile as record_reconcile
        else:from requirement_records import reconcile as record_reconcile
        from keyed_records import require_configured_operator
        # Strict allowlist before the receipt is read (kittrial-5bb.85).
        require_configured_operator(args.actor,operators(root,strict=True),'reconcile a %s operation'%kind)
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # A proposal reconcile checks the operator allowlist strictly (kittrial-5bb.68
            # review 01a10180). A reference or capability reconcile reads it strictly too,
            # only to decide which operator voids apply when `complete` confirms the anchor
            # holds a live record (kittrial-5bb.74 review); requirements do not take it.
            extra={'operators':operators(root,strict=True)} if kind in ('proposal','reference','capability') else {}
            print(json.dumps(record_reconcile(path,args.operation_id,args.actor,args.reason,
                                              args.disposition,run,issue_id=args.issue_id,**extra)))
    elif args.command=='anchor-release':
        # kittrial-5bb.74: an anchor whose propose stopped before its first record, or whose
        # every record an operator void names, holds its key for good once the original
        # payload is lost. Host route only: the allowlist is the authority.
        import fcntl
        if args.kind=='reference':import reference_records as records
        else:import capability_records as records
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        authority=operators(root,strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(records.KIND.release(path,args.issue_id,args.actor,args.reason,run,operators=authority,
                                                  duplicate=args.duplicate,set_aside_evidence=args.set_aside_evidence)))
    elif args.command=='void-record':
        import fcntl
        from recovery import KEYED_KIND_PREFIXES
        from review_workflow import apply_void
        path=project_dir(root,args.project)
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        if not isinstance(payload,dict) or not isinstance(payload.get('task'),str):raise ValueError('Void record payload must name its task')
        authority=operators(root, strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if payload.get('target_kind') in KEYED_KIND_PREFIXES:
                # A record on a reference or capability anchor (kittrial-5bb.74): the anchor's
                # kind owns the void and reads only that anchor.
                import capability_records,reference_records
                kind=next((k for k in (reference_records.KIND,capability_records.KIND)
                           if payload['target_kind'].startswith(k.family)),None)
                if kind is None:raise ValueError('Unsupported operator void target kind')
                print(json.dumps(kind.apply_void(payload,args.actor,run,authority)))
                return
            rows=record_json.loads_rows(run_bd(root,args.project,['export','--all']))
            print(json.dumps(apply_void(rows,payload['task'],args.actor,payload,run,operator=True,
                                        operators=authority,journal=path)))
    elif args.command=='revert-record':
        import fcntl
        from review_workflow import apply_revert
        path=project_dir(root,args.project)
        payload=read_json_file(args.file,'Payload file',encoding='utf-8-sig')
        if not isinstance(payload,dict) or not isinstance(payload.get('task'),str):raise ValueError('Integration revert payload must name its task')
        authority=operators(root, strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            rows=record_json.loads_rows(run_bd(root,args.project,['export','--all']))
            print(json.dumps(apply_revert(rows,payload['task'],args.actor,payload,run,operator=True,
                                          operators=authority,journal=path)))
    elif args.command=='operators':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        cfg=config(root)
        current=stored_operators(cfg)
        if args.action=='list':
            authority_change_list_arguments(args,'operators')
            print(json.dumps({'operators':current}))
            # An allowlist that already holds an HTTP account or agent id (added by hand, or
            # by an older kit following its own advice) makes that id an operator.
            from http_authority import http_shaped_names
            shaped=http_shaped_names(current)
            if shaped:
                print('Warning: the operator allowlist holds %s, which has the shape of an HTTP account or agent '
                      'id. Such an id must not be an operator; remove it with: admin.py operators remove NAME '
                      '--confirm-revoke'%', '.join(shaped),file=sys.stderr)
            return
        # Config is the single authority source; a shell-only ORCHESTRA_OPERATORS
        # that disagrees is refused before the change rather than applied here
        # and ignored by the endpoint.
        operators(root, strict=True)
        if not args.actor:raise ValueError('operators '+args.action+' requires an actor identity')
        actor=identity(args.actor,'Invalid operator identity')
        operator,reason,notice=authority_change_arguments(args,'operators',current)
        if args.action=='add':
            # An HTTP account or agent id is never an operator (kittrial-5bb.70 review
            # 01a10308): the web service acts under those ids, and an older kit's advice
            # ("operators add ACTOR" for an inert web disposition) must not allowlist one.
            from http_authority import http_actor_id
            if http_actor_id(actor) is not None:
                raise ValueError('%s has the shape of an HTTP account or agent id; such an id is never added to the '
                                 'operator allowlist. A web disposition counts through the web service, not '
                                 'through this list'%actor)
            changes=actor not in current
            if changes:current.append(actor)
        else:
            if not args.confirm_revoke:
                limit=None if args.all_revoked else 5
                raise ValueError('operators remove revokes ' + actor + ': voids they authored stop applying on '
                                 'reads, and so do the host-issued integration revert records and retractions '
                                 'they authored' + revoked_revert_records(root,actor,limit) +
                                 ' (re-add restores them).' + revoked_keyed_voids(root,actor,limit) +
                                 revoked_proposal_records(root,actor,limit) +
                                 ' Re-run with --confirm-revoke to acknowledge this.')
            changes=actor in current
        # A damaged audit refuses only an ADD that would really change the list: a no-op add is
        # rc 0 here, exactly as the release before this audit, and the refusal costs nothing at
        # all - no lock is taken (round-2 review item 4).
        authority_change_precheck(root,args.action,changes)
        # The change is applied to a fresh read under the deployment lock, so an add or
        # remove made at the same instant by another command is not lost (kittrial-5bb.136).
        changed=False
        with deployment_config_lock(root):
            cfg=config(root)
            current=stored_operators(cfg)
            changed=(actor not in current) if args.action=='add' else (actor in current)
            if changed:record_authority_change(root,'operators',args.action,actor,operator,reason)
            if args.action=='add':
                if actor not in current:current.append(actor)
            elif actor in current:current.remove(actor)
            if current:cfg['operators']=current
            else:cfg.pop('operators',None)
            atomic_private_write(marker,json.dumps(cfg))
        if changed and notice:print(notice,file=sys.stderr)
        print(json.dumps({'operators':current}))
    elif args.command=='verifiers':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        cfg=config(root)
        current=stored_verifiers(cfg)
        if args.action=='list':
            authority_change_list_arguments(args,'verifiers')
            print(json.dumps({'verifiers':current}));return
        # Config is the single authority source, exactly as for `operators`.
        verifiers(root, strict=True)
        if not args.actor:raise ValueError('verifiers '+args.action+' requires an actor identity')
        actor=identity(args.actor,'Invalid verifier identity')
        # The literal --actor placeholder is a real name for an operator the deployment lists
        # (kittrial-5bb.229 rev-2 item 3), so the operators list is passed in, exactly as above -
        # and an unusable operators value lists nobody rather than failing the command (item 1).
        operator,reason,notice=authority_change_arguments(args,'verifiers',
                                                          authority_change_listed_operators(cfg))
        if args.action=='add':
            changes=actor not in current
            if changes:current.append(actor)
        else:
            if not args.confirm_revoke:
                raise ValueError('verifiers remove revokes ' + actor + ': every capability verification they '
                                 'recorded reads `reported` instead of `verified`, and drift that only their '
                                 'passes had cleared reappears' + revoked_verifications(root,actor) +
                                 ' (re-add restores them). Re-run with --confirm-revoke to acknowledge this.')
            changes=actor in current
        # As for `operators`: only a change that would really change the list is refused here, and
        # the refusal takes no lock and writes nothing (round-2 review item 4).
        authority_change_precheck(root,args.action,changes)
        changed=False
        with deployment_config_lock(root):
            cfg=config(root)
            current=stored_verifiers(cfg)
            changed=(actor not in current) if args.action=='add' else (actor in current)
            if changed:record_authority_change(root,'verifiers',args.action,actor,operator,reason)
            if args.action=='add':
                if actor not in current:current.append(actor)
            elif actor in current:current.remove(actor)
            if current:cfg['verifiers']=current
            else:cfg.pop('verifiers',None)
            atomic_private_write(marker,json.dumps(cfg))
        if changed and notice:print(notice,file=sys.stderr)
        print(json.dumps({'verifiers':current}))
    elif args.command=='checkpoint-provenance-writes':
        print(json.dumps(checkpoint_provenance_switch(root,args.action,args.actor)))
    elif args.command=='review-writes':
        result,warnings=review_writes_command(root,args.actor,args.action)
        for line in warnings:print(line,file=sys.stderr)
        print(json.dumps(result))
    elif args.command=='authorized-keys':
        authorized_keys(root,args.key_file,args.role,args.python,args.comment,args.project,args.principal)
    elif args.command=='authorized-keys-list':
        print(json.dumps(authorized_keys_listing(root,args.file),ensure_ascii=True,indent=2))
    elif args.command=='adopt-actor':
        print(json.dumps(adopt_actor(root,args.project,args.actor,
                                     one_flag_value(args.principal,'--principal'),
                                     one_flag_value(args.operator,'--actor'),
                                     one_flag_value(args.reason,'--reason'),
                                     from_principal=one_flag_value(args.from_principal,'--from')),
                         ensure_ascii=True,sort_keys=True))
    elif args.command=='actor-adoptions':
        print(json.dumps({'schema_version':ACTOR_ADOPTIONS_SCHEMA,'entries':actor_adoptions(root,args.project)},
                         ensure_ascii=True,indent=2))
    elif args.command=='authority-changes':
        report,note=authority_changes_report(root)
        print(json.dumps(report,ensure_ascii=True,indent=2))
        if note:print(note,file=sys.stderr)
    elif args.command=='backup':backup_projects(root,args.projects,args.all_projects)
    elif args.command=='backup-copy':backup_copy(root,args.destination,require_clean=args.require_clean)
    elif args.command=='backup-repoint':print(json.dumps(repoint_backup(root,args.project),sort_keys=True))
    elif args.command=='retire-project':
        result=retire_project(root,args.project,args.actor,args.reason,force=args.force)
        print(json.dumps(result,sort_keys=True))
        print('Retired %s to %s. Nothing was deleted: backups/%s and the Dolt database are untouched, and the '
              'name stays reserved. If this project is registered in the web interface, archive it there: '
              'its task pages now answer "Unknown/uninitialized project".'
              %(args.project,result['destination'],args.project),file=sys.stderr)
    elif args.command=='backup-authority':
        print(json.dumps(backup_authority(root,args.project)))
    elif args.command=='open-item-label-check':
        report,clean=open_item_label_check(root,args.projects)
        print(json.dumps(report,sort_keys=True))
        if not clean:raise SystemExit(1)
    elif args.command=='merge-slot-report':
        report,healthy=merge_slot_report(root,args.projects)
        print(json.dumps(report,sort_keys=True))
        if not healthy:
            # stdout stays one JSON document; the plain sentences go to stderr. The
            # merge-create repair is named only for the projects that are missing or
            # damaged: an unreadable one (a dropped database, a stopped server, a name
            # that is no project) is not mended by it, so it is told what is true
            # instead (kittrial-5bb.202 review item 4; review F5).
            names=report['not_healthy']
            print('merge-slot-report: %d project(s) do not have a healthy merge slot: %s.'
                  %(len(names),', '.join(names)),file=sys.stderr)
            repairable=report['missing']+report['damaged']
            if repairable:
                print('An operator runs the merge-create coordination operation for %s (a missing or damaged '
                      'slot), then re-runs this report.'%', '.join(repairable),file=sys.stderr)
            if report['unreadable']:
                print('The merge slot of %s could not be read at all, and no merge-create run mends that: check '
                      'the project\'s database, its Dolt server coordinates and the name itself first.'
                      %', '.join(report['unreadable']),file=sys.stderr)
            raise SystemExit(1)
    elif args.command=='backup-status':
        record=read_backup_status(root)
        # Retired projects are not part of the gate; they are listed so an operator can
        # see them (the key appears only when there are any, the record is unchanged).
        retired=[entry for _,entry in retired_entries(root)]
        print(json.dumps(dict(record,retired=retired) if retired else record,sort_keys=True))
        degraded=degraded_projects(root,record)
        if degraded:
            # stdout stays one JSON document; the plain sentence goes to stderr.
            print('backup-status: %s. %s'%(degraded_summary(degraded),' '.join('%s: %s'%item for item in degraded)),
                  file=sys.stderr)
        if args.require_complete or args.require_clean:
            problems=require_complete_problems(root,record)
            if problems:
                raise SystemExit('backup-status: not every project has a complete backup pair on disk: '
                                 +'; '.join(problems))
        if args.require_clean and degraded:
            # The strict gate (for a release): complete is not enough, nothing may be degraded.
            raise SystemExit('backup-status: every pair is complete, but %s; --require-clean refuses a degraded '
                             'project. Repair it and take a fresh backup.'%degraded_summary(degraded))
    elif args.command=='journal':
        import fcntl
        from http_authority import OperationJournal, journal_path
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        if args.retention is not None and args.retention<=0:raise ValueError('Retention must be a positive number of seconds')
        if args.committed_retention is not None and args.committed_retention<=0:
            raise ValueError('Committed retention must be a positive number of seconds')
        options={}
        if args.retention is not None:options['retention']=args.retention
        if args.committed_retention is not None:options['committed_retention']=args.committed_retention
        journal=OperationJournal(str(journal_path(path)),**options)
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            report={'project':args.project}
            if args.reset_high_water:
                report['high_water']=journal.reset_high_water()
            if args.reclaim_expired:report['reclaimed']=journal.reclaim_expired()
            if args.prune_before is not None:report['pruned']=journal.prune(args.prune_before)
            report['stats']=journal.stats()
        print(json.dumps(report,sort_keys=True))
        if not args.reclaim_expired and args.prune_before is None and not args.reset_high_water:
            print('Size report (the default inspection; --stats is the same). Retention is by TIME ONLY: a '
                  'committed receipt is never compacted while its own replay window is open; the byte '
                  'budget (limit_bytes) and the advisory tombstone size (tombstone_limit) are reported '
                  '(over_bytes/over_tombstones), never enforced by eviction, and never block a write. '
                  'The only refusal is the live-identity count (limit) genuinely being reached, which fails '
                  'closed with rc=124. Use --reclaim-expired to compact closed receipt windows now, or '
                  '--prune-before EPOCH to hard-remove a still-live identity after reconciling canonical '
                  'state (an exact retry of a pruned identity can repeat its effect; a reclaimed one is '
                  'refused as expired). Clock: a step of more than 24 h since the last write makes the '
                  'journal suspect (suspect/anchor/suspect_since) for one hour; expiry is then capped at '
                  'anchor+24h and reclaim waits, and suspicion clears by itself. --reset-high-water sets '
                  'high_water to now and clears suspicion (use it after correcting a wrong clock).',
                  file=__import__('sys').stderr)
    elif args.command=='restore-new':
        validate_name(args.project);validate_name(args.destination)
        # --actor/--reason record the re-grant the two restore flags make (kittrial-5bb.192 review
        # item 2). Everything about them is checked HERE, before the destination exists: a bad
        # value must not be discovered after the native restore, when the re-grant step runs last.
        args.reason=_restore_authority_reason(args.project,args.reason)
        if args.actor is not None:
            from recovery import identity
            args.actor=identity(args.actor,'Invalid operator identity')
        if (args.actor is not None or args.reason is not None) and not (args.restore_operators or args.restore_verifiers):
            raise ValueError('--actor/--reason on restore-new record the re-grant that --restore-operators or '
                             '--restore-verifiers makes in the authority-changes audit; without one of those '
                             'flags nothing is re-granted, so they would record nothing')
        refuse_retired_name(root,args.destination)
        backup=root/'backups'/args.project
        if not backup.is_dir():raise ValueError('Source backup missing')
        if args.project==args.destination:raise ValueError('Restore requires a different destination')
        with backup_lock(root,args.project):
            # The sidecar decides first, before anything is created (kittrial-5bb.152): a copy
            # that exists but cannot be used refuses the restore unless --without-coordination
            # asks for the native tracker data alone; a backup with no copy at all is legacy.
            outcome,_,_,damaged=sidecar_outcome(root,args.project)
            args.native_only=None
            if outcome=='refuse':
                if not args.without_coordination:
                    raise ValueError(unusable_sidecar_message(root,args.project,damaged))
                if args.restore_operators or args.restore_verifiers:
                    raise ValueError('--restore-operators and --restore-verifiers re-grant what the coordination sidecar '
                                     'records, and it cannot be read; they cannot be combined with --without-coordination')
                args.native_only=damaged
            elif args.without_coordination and outcome=='answer':
                raise ValueError('--without-coordination is only for a backup whose coordination sidecar cannot be '
                                 'used; this backup\'s can be, so restore it without the flag')
            else:
                coordination_backup(root,args.project)
            args.flag_had_nothing=args.without_coordination and outcome=='legacy'
            # Validate the journal snapshot BEFORE creating anything: a corrupt snapshot
            # must fail the restore with no destination project, Dolt restore or
            # coordination files left behind.
            snapshot=journal_snapshot_path(root,args.project)
            if snapshot.is_file():_check_journal_database(snapshot)
            # Validate the deployment operator allowlist BEFORE creating anything too.
            # `restore_coordination` only consults it at its very end (inside
            # missing_operators/merge_operators), which is after the destination project
            # and its native data exist; a corrupt `operators` value or a missing
            # deployment config with --restore-operators would then leave a partial
            # destination behind. `operators(root)` raises for a corrupt value, and
            # --restore-operators needs the config file that merge_operators reads.
            operators(root);verifiers(root)
            if (args.restore_operators or args.restore_verifiers) and not (root/'deployment.private.json').is_file():
                raise ValueError('Deployment is not installed; run install first')
            # From here the destination starts to exist. Hold its restore lock to the end,
            # so retire-project cannot pull it away mid-restore, and print the notice for
            # every way the rest can stop (kittrial-5bb.85 review 01a10219): a failure, a
            # stop or Ctrl-C in add-project, in the native restore, or in what follows it.
            import fcntl
            (root/'backups').mkdir(exist_ok=True)
            restoring=restore_lock_path(root,args.destination).open('a')
            fcntl.flock(restoring,fcntl.LOCK_EX)
            # ``step`` is a one-word holder: ``finish_restore`` names the provisioning step
            # inside itself, so a failure there prints the provisioning notice and not the
            # re-point's (kittrial-5bb.202 review item 4, review F8).
            step=['add-project']
            try:
                # SIGTERM is an exception for the whole of what follows, not only inside
                # the native restore: a stop during add-project (or the re-point) used to
                # end the process with no notice at all (review 01a1026a).
                with signal_termination_guard():
                    add_project(root,args.destination,requirements_default=False)
                    # The native restore runs through the Dolt SQL client (no bd ~10 s read
                    # timeout), in its own process group, and adopts the restored project
                    # identity; a destination without server metadata keeps `bd backup restore`.
                    step[0]='native restore'
                    print(native_restore(root,args.project,args.destination))
                    step[0]='re-point and coordination'
                    warning=finish_restore(root,args,snapshot,step)
            except BaseException as error:
                # add-project's own refusals (a populated or retired destination) are raised
                # before it creates anything: they need no notice about a leftover project.
                if not (step[0]=='add-project' and isinstance(error,ValueError)):
                    destination_state=restore_destination_state(root,args.destination)
                    # The provisioning notice says what the clone now holds, so read the slot
                    # from the clone itself (read-only) instead of assuming the step's failure
                    # means a missing slot (kittrial-5bb.202 rev-3 item 1, review F1). Its
                    # "empty, working project" shape is included: a clone from an empty slotless
                    # source reads that way, and it still needs the provisioning notice.
                    slot=(project_merge_slot_state(root,args.destination)
                          if step[0]==MERGE_SLOT_STEP and destination_state in ('partial','empty') else None)
                    print(restore_failure_notice(args.destination,error,destination_state,step=step[0],
                                                 merge_slot=slot),
                          file=sys.stderr)
                raise
            finally:
                restoring.close()
        print('Restored only into the newly created project; retained original issue IDs. Never use this clone as a second live tracker.')
        # A backup recorded degraded restores its tracker, and nothing says what is
        # missing unless this does (kittrial-5bb.105). The run record is advisory here:
        # an unreadable one must not fail a restore that has already succeeded.
        noted=restore_degraded_note(root,args.project,args.destination)
        if noted:print(noted)
        if args.native_only is not None:
            # The last lines, so the operator cannot miss what this restore left out.
            print(native_only_note(root,args,args.native_only))
        elif args.flag_had_nothing:
            print('--without-coordination: this backup has no coordination sidecar at all, so there was nothing '
                  'to leave out; it was restored as a legacy backup, exactly as without the flag.')
        if warning:
            # The last thing the restore says, after everything on stdout (kittrial-5bb.144),
            # and a distinct exit status, so `restore-new ... && next-step` does not proceed.
            sys.stdout.flush()
            print(warning,file=sys.stderr)
            sys.stderr.flush()
            raise SystemExit(RESTORE_AUTHORITY_NOT_REGRANTED)

def native_only_note(root,args,damaged):
    """What a ``restore-new --without-coordination`` did NOT restore (its last lines)."""
    journal=journal_snapshot_path(root,args.project)
    return ('NOT restored (--without-coordination): the coordination sidecar of backup %s could not be used (%s). '
            '%s has none of its coordination records: no sessions, handoffs, handoff or coordination requests, '
            'merge context, guidance, onboarding, feedback or record journals; and the operators and verifiers that '
            'backup records were neither compared nor re-granted.\nThe operation journal %s.'
            %(args.project,'; '.join('%s: %s'%(copy.relative_to(root).as_posix(),problem) for copy,problem in damaged),
              args.destination,
              'was restored from %s'%journal.relative_to(root).as_posix() if getattr(args,'journal_restored',False)
              else 'was not restored: the backup has no operation-journal snapshot'))

def finish_restore(root,args,snapshot,step=None):
    """What ``restore-new`` does after the native restore: re-point, sidecar, journals, slot.

    ``step`` is the caller's one-word holder, when it has one: the merge-slot provisioning
    step names itself in it, so a failure there prints its own notice rather than the
    re-point's (kittrial-5bb.202 review item 4, review F8). A caller that passes none (the
    focused tests) gets the same work with no naming.
    """
    def at(name):
        if isinstance(step,list) and step:step[0]=name
    # The native restore brings the SOURCE project's backup configuration with the
    # restored database: `.beads/dolt-backup.json` and the restored `dolt_backups`
    # row both still name `backups/<source>`. Left there, `backup <destination>`
    # would sync the clone into the source project's directory (rewriting a
    # generation whose complete sidecar and journal snapshot describe the source)
    # and leave the clone's own directory stale, so re-point the destination at
    # `backups/<destination>` before anything else. `bd backup init` updates an
    # already-configured destination in place; validate_backup_target refuses any
    # clone that was not re-pointed this way.
    run_bd(root,args.destination,['backup','init',str(root/'backups'/args.destination)])
    at('re-point and coordination')
    # The deployment authority merges come last (kittrial-5bb.142): they are the only step
    # that waits on the deployment lock, and a refusal there must leave a complete restore.
    # --without-coordination (kittrial-5bb.152): no coordination files, no authority; the
    # operation journal is restored as before when the backup has a snapshot.
    sidecar=False if getattr(args,'native_only',None) is not None else restore_coordination(root,args.project,args.destination,authority=False)
    restored=restore_journal(snapshot,
                             project_dir(root,args.destination)/JOURNAL_STORE_NAME)
    args.journal_restored=restored is not None
    if restored is None:
        print('Backup has no operation-journal snapshot; the restored project starts with an empty identity journal.')
    # The native restore replaced the destination's rows with the SOURCE database's, so a
    # source made before the slot was provisioned leaves the clone without a merge slot.
    # This is a way of making a project that the kit controls, so close it here: provision the
    # slot idempotently, AFTER the re-point, the coordination sidecar and the journals, so a
    # failure here leaves everything else the clone needs in place and only the slot to mend
    # (kittrial-5bb.202 item 3; rev-3 item 1, review F1: it ran BEFORE them and the notice
    # then claimed journals that were never restored). It still runs for a native-only
    # restore, whose journal step above is kept.
    at(MERGE_SLOT_STEP)
    provision_merge_slot(root,args.destination)
    if getattr(args,'native_only',None) is not None:
        return None
    if not sidecar:
        # A legacy backup records no deployment authority: there is nothing to re-grant, and
        # the authority step does not run (it would read no sidecar).
        if args.restore_operators or args.restore_verifiers:
            print('This backup has no coordination sidecar, so it records no operators or verifiers: '
                  '--restore-operators/--restore-verifiers re-granted nothing.')
        return None
    return restore_authority(root,args.project,restore_operators=args.restore_operators,
                             restore_verifiers=args.restore_verifiers,actor=args.actor,reason=args.reason)

def kit_refusal(error):
    """Whether a ``ValueError`` is one of the kit's own refusals: exactly ``ValueError``
    (what the kit raises), or a subclass a kit module defines. A standard-library
    subclass (``json.JSONDecodeError``, ``UnicodeDecodeError``) is not."""
    if type(error) is ValueError:return True
    module=sys.modules.get(type(error).__module__)
    try:
        return Path(getattr(module,'__file__','') or '/nonexistent/x').resolve().parent==Path(__file__).resolve().parent
    except (OSError,ValueError):
        return False

def run_main():
    """``main()`` with the command-line exits: a failure is one line, never a traceback.

    A refusal (``ValueError``) ends with the same last line a traceback would have,
    ``ValueError: <message>``, so anything that reads that line is unaffected. Only the
    kit's own refusals are shortened (``kit_refusal``): any other error, including a
    ``ValueError`` subclass from the standard library such as ``JSONDecodeError``, keeps
    its traceback, because the traceback is the only thing that says where it came from.
    """
    try: main()
    except ValueError as e:
        if not kit_refusal(e):raise
        raise SystemExit('ValueError: %s'%e)
    except subprocess.CalledProcessError as e:
        # Never echo credential-bearing command input or the environment.
        raise SystemExit(f'Command failed ({e.returncode}): {e.stderr[:2000]}')
    except subprocess.TimeoutExpired as e:
        # The client's group was already stopped; report the ceiling, never the command.
        raise SystemExit(f'Command timed out after {e.timeout} s')
    except TerminatedBySignal as e:
        # The guarded cleanup ran (previous pair kept, sync client's group stopped); exit
        # with the conventional 128+signal status instead of a traceback.
        raise SystemExit(128+e.signum)
    except KeyboardInterrupt:
        # Ctrl-C: the command's own notice (if it has one) is already printed; exit with the
        # conventional status for SIGINT instead of a traceback.
        raise SystemExit(128+signal.SIGINT)

if __name__=='__main__':
    run_main()
