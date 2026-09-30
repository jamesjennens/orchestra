#!/usr/bin/env python3
"""Operator commands for an isolated, user-systemd Beads/Dolt deployment."""
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
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from bootstrap import install as install_binaries
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

def raise_termination(signum,frame):
    """Signal handler that turns a stop into an exception so the cleanup path runs.

    The FIRST stop becomes ``TerminatedBySignal``. A second ``SIGTERM`` would otherwise
    kill the interpreter inside the cleanup the first one started - including inside
    ``terminate_process_group``, before the ``killpg`` that stops the client group - so
    this also ignores later ``SIGTERM`` for the rest of the guarded section; the guard
    restores the previous handler in its ``finally``. ``SIGKILL`` remains the operator's
    way to force a stop that runs no cleanup.
    """
    try: signal.signal(signum,signal.SIG_IGN)
    except (ValueError,OSError,RuntimeError,AttributeError): pass
    raise TerminatedBySignal(signum)

@contextmanager
def signal_termination_guard():
    """Turn ``SIGTERM`` into a ``TerminatedBySignal`` exception for the duration of the block.

    ``backup_project``'s ``last_complete_guard`` (which catches ``BaseException``) and its
    ``finally`` are what restore the previous complete sidecar, drop the staged journal
    snapshot and stop the native sync client. A ``SIGINT`` (Ctrl-C) already raises
    ``KeyboardInterrupt``, but a ``SIGTERM`` terminates the interpreter outright, so that
    cleanup never ran and the ``dolt`` client kept writing ``backups/<name>`` after the
    backup lock was released. A handler that raises puts a normal stop back on the cleanup
    path; the previous handlers are restored in a ``finally``.

    A handler can only be installed in the main thread (``signal.signal`` raises
    ``ValueError`` elsewhere), an embedded host may have its own handlers and a platform
    may refuse the signal, so an install that is not possible is skipped instead of
    failing the backup for a reason unrelated to it: the caller keeps the previous
    behaviour, which is the honest limitation this cannot remove. Once a stop has been
    turned into the exception, later ``SIGTERM`` delivery is ignored for the rest of the
    block (see ``raise_termination``), so the cleanup it started cannot itself be
    interrupted; a very short window whose state must change as one unit additionally
    holds the signal with ``sigterm_blocked``.
    """
    previous={}
    if threading.current_thread() is threading.main_thread():
        try:
            for signum in (signal.SIGTERM,):
                previous[signum]=signal.getsignal(signum)
                signal.signal(signum,raise_termination)
        except (ValueError,OSError,RuntimeError,AttributeError):
            previous={}
    try:
        yield
    finally:
        for signum,handler in previous.items():
            try: signal.signal(signum,handler)
            except (ValueError,OSError,RuntimeError,AttributeError): pass

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

def config(root):
    return json.loads((root/'deployment.private.json').read_text())

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
        value=json.loads(marker.read_text()).get('operators')
        if isinstance(value,list):found.extend(value)
        elif isinstance(value,str):found.append(value)
        elif value is not None:raise ValueError('deployment operators must be a list of actor identities')
    from recovery import configured_operators
    allowed=configured_operators(found)
    if strict:
        shell=configured_operators((os.environ.get('ORCHESTRA_OPERATORS') or '').replace(',',' ').split())
        if shell and shell!=allowed:
            raise ValueError('ORCHESTRA_OPERATORS is set in this shell but is not an authority source; '
                             'deployment.private.json operators is. Add the actor with `admin.py operators add` '
                             'or unset ORCHESTRA_OPERATORS before this command.')
    return allowed

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
    env=os.environ.copy()
    env.update({'PATH':str(root/'bin')+os.pathsep+env.get('PATH',''),
                'DOLT_ROOT_PATH':str(root/'dolt-home'),'XDG_CONFIG_HOME':str(root/'config'),
                'BEADS_DOLT_PASSWORD':config(root)['password'],'DOLT_CLI_PASSWORD':config(root)['password'],
                'BD_NON_INTERACTIVE':'1','BEADS_NO_DAEMON':'1'})
    return env

def sql(root,query,password=None):
    cfg=config(root);env=environment(root)
    if password is not None: env['DOLT_CLI_PASSWORD']=password
    return checked([root/'bin/dolt','--host','127.0.0.1','--port',cfg['port'],'--no-tls','--user','root','sql','--result-format','csv'],input=query,env=env,cwd=root).stdout

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
    except (TypeError,ValueError):
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
    for name in ('data','projects','backups','config','dolt-home'):(root/name).mkdir(exist_ok=True)
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

def worker_client_setup(root,name):
    """Exact worker client configuration and bootstrap command for one project.

    The endpoint is this kit's generic ``endpoint.py`` (the file beside this
    module), which serves every project of the deployment. The host is a
    placeholder because the kit is public and each worker supplies its own SSH
    alias; never point a new project at a project-specific wrapper endpoint.
    """
    endpoint=Path(__file__).resolve().with_name('endpoint.py')
    config=json.dumps({'host':'WORKER_SSH_HOST','endpoint':str(endpoint),'root':str(root)},indent=2)
    return (f'Worker client configuration for {name} (save as client.local.json in the worker\'s own\n'
            f'directory and replace WORKER_SSH_HOST with that worker\'s SSH alias; this kit endpoint serves\n'
            f'every project, so do not point it at a project-specific wrapper):\n{config}\n'
            f'Bootstrap command (replace ACTOR with the actor returned by worker.py start or session\n'
            f'register):\n  python client.py --config client.local.json --project {name} --actor ACTOR -- onboard')

def scheduled_backup_unit_dir():
    """The user systemd unit directory an operator installs the schedule into."""
    return Path.home()/'.config/systemd/user'

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
    """The exact ``ExecStart`` line that covers every project of this runtime."""
    return (f'ExecStart={sys.executable} {Path(__file__).resolve()} '
            f'--root {root} backup --all')

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
    dropins=(f'Systemd drop-ins ({directory}/*.service.d/*.conf) are not inspected, so this reports the unit '
             f'files themselves.')
    paths=scheduled_backup_unit_paths()
    if not paths:
        return False,(f'No installed scheduled backup unit matching beads-*backup*.service was found in '
                      f'{directory}, so no project of this runtime is on a schedule. A schedule that covers '
                      f'every project, including {name}, is:\n  {line}')
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
                          f'--all in one command):\n  {line}')
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
                      f'projects with --all in one command):\n  {line}')
        return False,message+note+' '+dropins
    details=[]
    if foreign:details.append('these units do not back up this runtime: '+', '.join(foreign))
    if unreadable:details.append('could not be read: '+'; '.join(unreadable))
    if not read:
        return False,(f'The installed scheduled backup unit(s) found in {directory} could not be read ('
                      +'; '.join(unreadable)+f'), so schedule coverage of {name} cannot be confirmed. Use a '
                      f'schedule that covers every project:\n  {line}')
    return False,(f'The installed scheduled backup unit(s) read ('+', '.join(read)+f') do not cover {name}'
                  +(' ('+'; '.join(details)+')' if details else '')
                  +f'. A schedule that covers every project is:\n  {line} '+dropins)

def add_project(root,name):
    """Initialize one project, provision its merge slot and back it up once.

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
    path=project_dir(root,name)
    if path.exists() and any(path.iterdir()): raise ValueError('Project already exists; use it rather than initializing again')
    path.mkdir(exist_ok=True)
    cfg=config(root)
    run_bd(root,name,['init','--server','--external','--server-host','127.0.0.1','--server-port',str(cfg['port']),
                      '--server-user','root','--prefix',name,'--database',name,'--skip-agents','--skip-hooks','--non-interactive'])
    for key,value in [('no-git-ops','true'),('dolt.auto-push','false'),('dolt.auto-commit','on'),('backup.git-push','false')]:
        run_bd(root,name,['config','set',key,value])
    run_bd(root,name,['backup','init',str(root/'backups'/name)])
    provision_merge_slot(root,name)
    backup_project(root,name)
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

def validate_coordination_files(files):
    if not isinstance(files,dict):raise ValueError('Invalid coordination files map')
    for name,record in files.items():
        quarantine = isinstance(name,str) and re.fullmatch(r'\.feedback\.jsonl\.(?:[a-f0-9]{16}|[a-f0-9]{64})\.incomplete',name)
        journal = isinstance(name,str) and re.fullmatch(r'(?:\.coordination-requests|\.handoffs|\.handoff-requests|\.handoff-recoveries|\.requirement-requests|\.requirement-backfills)/[a-f0-9]{64}\.json',name)
        if name not in ('.merge-context.json','ONBOARDING.md','.sessions.json','.feedback.jsonl') and not quarantine and not journal:raise ValueError('Invalid coordination backup path')
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
        if name=='ONBOARDING.md' and (set(record)!={'text'} or not isinstance(record['text'],str) or not record['text'].strip() or len(record['text'].encode('utf-8'))>8000):raise ValueError('Invalid onboarding backup')
        if name=='.feedback.jsonl':
            from feedback import validate_feed_text
            if set(record) != {'text'}:raise ValueError('Invalid feedback backup')
            validate_feed_text(record['text'])
        if quarantine:
            from feedback import validate_quarantine_record
            validate_quarantine_record(name,record)

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
    path=Path(path)
    if path.is_symlink() or not path.is_file():return None
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return None
    if not isinstance(data,dict) or data.get('schema_version')!=1 or data.get('status')!='complete':
        return None
    return data

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
        if (path/'ONBOARDING.md').exists() or (path/'ONBOARDING.md').is_symlink():
            from onboarding import read_document, PROJECT_LIMIT
            files['ONBOARDING.md']={'text':read_document(path,'ONBOARDING.md',PROJECT_LIMIT)}
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
                record={'schema_version':1,'status':'complete','files':files,
                        'operators':sorted(operators(root))}
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
    if sidecar.is_symlink():return False,'coordination sidecar must not be a symlink'
    try:data=json.loads(sidecar.read_text(encoding='utf-8'))
    except FileNotFoundError:return False,'coordination sidecar is missing'
    except (OSError,ValueError):return False,'coordination sidecar is not readable JSON'
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
        else:
            reason=entry.get('reason')
            if not isinstance(reason,str) or not reason.strip():
                raise ValueError('Backup status for %s must say why it is not complete'%name)
            if 'completed_at' in entry or 'pair' in entry:
                raise ValueError('Incomplete backup status for %s must not carry a completion'%name)
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
    entries={entry['name']:entry for entry in previous['projects']}
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
    for entry in record['projects']:
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

def backup_copy(root,destination):
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
        for entry in sorted(record['projects'],key=lambda item:item['name']):
            name=entry['name']
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
    being lost with the process.
    """
    if all_projects:
        targets=initialized_projects(root);scope='all'
        if names:raise ValueError('backup --all already covers every project; do not also name projects')
        if not targets:raise ValueError('No initialized projects in this runtime; add one before backup --all')
    else:
        if not names:raise ValueError('backup needs at least one project, or --all')
        for name in names:validate_name(name)
        targets=sorted(dict.fromkeys(names));scope='named'
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
            results.append({'name':name,'status':'complete','completed_at':utc_stamp()})
            print(native)
        else:
            results.append({'name':name,'status':'failed','reason':reason})
            incomplete.append(name)
    write_backup_status(root,merged_backup_status(root,backup_status_record(results,scope,generated_at)))
    for entry in results:
        if entry['status']!='complete':
            print('backup %s for %s: %s'%(entry['status'],entry['name'],entry['reason']),file=sys.stderr)
    if len(targets)>1:
        print('Backed up %d of %d project(s); %s is %s.'%(
            len(targets)-len(incomplete),len(targets),root/'backups'/BACKUP_STATUS_NAME,
            'complete' if not incomplete else 'incomplete'))
    if incomplete:
        raise SystemExit('backup incomplete for: '+' '.join(incomplete)+
                         ' (see %s)'%(root/'backups'/BACKUP_STATUS_NAME))

def validate_coordination_operators(value):
    """Validate the optional operator snapshot carried by a backup sidecar."""
    if value is None:return []
    if not isinstance(value,list):raise ValueError('Coordination backup operators must be a list')
    from recovery import identity
    allowed=[]
    for item in value:
        try:allowed.append(identity(item,'Invalid operator identity in coordination backup'))
        except ValueError:raise ValueError('Invalid operator identity in coordination backup') from None
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

def resolved_coordination_sidecar(root,source):
    """The complete coordination sidecar for a project, or None when there is none.

    The canonical ``backups/<name>.coordination.json`` is preferred; the durable
    last-complete copy is the fallback (see ``coordination_sidecar_source``).
    """
    return coordination_sidecar_source(root,source)[1]

def coordination_backup(root,source):
    data=resolved_coordination_sidecar(root,source)
    if data is None:
        bundle=root/'backups'/(source+'.coordination.json')
        if not bundle.exists():
            return None
        raise ValueError('Incomplete coordination backup; recover/reconcile source first')
    validate_coordination_files(data.get('files'))
    validate_coordination_operators(data.get('operators'))
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

def merge_operators(root,actors):
    """Add missing operators to the deployment allowlist; return the added names.

    Additive only, and only ever called by `restore_coordination` when the
    operator explicitly passed `--restore-operators`. The deployment allowlist is
    authority for EVERY project, so re-adding an entry from a backup is a
    deployment-wide grant: a backup taken before `operators remove ACTOR
    --confirm-revoke` must not silently undo that revocation. The added names are
    returned so the caller can report exactly what was re-granted.
    """
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    from recovery import identity
    if isinstance(actors,str):actors=[actors]
    wanted=[identity(item,'Invalid operator identity') for item in actors]
    cfg=config(root)
    current=stored_operators(cfg)
    added=[item for item in wanted if item not in current]
    if not added:return []
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

def restore_coordination(root,source,destination,restore_operators=False):
    from coordination import atomic
    path=project_dir(root,destination)
    files=coordination_backup(root,source)
    if files is None:
        print('Legacy backup has no coordination journal. Reconcile outstanding child requests and merge ownership before accepting writes.')
        return
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
    # one cannot create a journal directory or a partial restore.
    for name,record in files.items():
        if name.startswith('.requirement-requests/'):
            from requirement_records import validate_receipt
            validate_receipt(record)
        elif name.startswith('.requirement-backfills/'):
            from requirement_records import validate_receipt
            validate_receipt(record,backfill=True)
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
        elif name=='.feedback.jsonl':
            temporary=target.with_suffix('.tmp')
            temporary.write_text(record['text'],encoding='utf-8',newline='\n')
            os.replace(temporary,target)
        elif re.fullmatch(r'\.feedback\.jsonl\.(?:[a-f0-9]{16}|[a-f0-9]{64})\.incomplete',name):
            from feedback import validate_quarantine_record
            _atomic_write_bytes(target,validate_quarantine_record(name,record))
        else:atomic(target,record)
    # The native and coordination records (original comment plus its void
    # disposition) are restored by the writes above. Operator AUTHORITY is not:
    # the deployment allowlist is authority for every project, so a stale backup
    # must never silently re-grant an operator the deployment has since revoked.
    # Re-adding entries recorded in the backup is an explicit operator decision
    # (`--restore-operators`), and what it re-grants is reported either way.
    missing=missing_operators(root,source)
    if not missing:
        return
    if not restore_operators:
        print('NOT restored: the backup records operator allowlist entries this host does not list: '
              + ', '.join(missing) + '. Restoring them would re-grant deployment-wide authority for every project, '
              'so they stay revoked here and void records they authored stay inert. Re-grant one deliberately with '
              '`admin.py --root ROOT operators add ACTOR`, or re-run this restore with --restore-operators to '
              're-establish the whole recorded allowlist.')
        return
    added=merge_operators(root,missing)
    if added:
        print('Re-granted operator allowlist entries from the backup (--restore-operators): ' + ', '.join(added))

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

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('install');a.add_argument('--port',type=int,default=13317);a.add_argument('--unit',default='beads-team.service')
    a=sub.add_parser('add-project');a.add_argument('project')
    a=sub.add_parser('set-onboarding');a.add_argument('project');a.add_argument('--file',required=True)
    a=sub.add_parser('handoff');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('requirement-backfill');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('requirement-apply');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('requirement-reconcile');a.add_argument('project');a.add_argument('--operation-id',required=True)
    a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a.add_argument('--disposition',choices=['failed','released','complete'],default='released')
    a.add_argument('--issue-id',dest='issue_id',default=None,
                   help='with --disposition complete, the exact native record to confirm')
    a=sub.add_parser('void-record');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('revert-record');a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--file',required=True)
    a=sub.add_parser('operators');a.add_argument('action',choices=['list','add','remove']);a.add_argument('actor',nargs='?')
    a.add_argument('--confirm-revoke',action='store_true',dest='confirm_revoke',
                   help='with remove: acknowledge that this operator\'s earlier operator voids stop applying')
    a=sub.add_parser('backup');a.add_argument('projects',nargs='*',metavar='project')
    a.add_argument('--all',action='store_true',dest='all_projects',
                   help='back up every initialized project in this runtime in one run')
    a=sub.add_parser('backup-status')
    a.add_argument('--require-complete',action='store_true',dest='require_complete',
                   help='exit non-zero unless the last run covered every project (--all) and every initialized '
                        'project has a complete pair on disk')
    a=sub.add_parser('backup-copy');a.add_argument('destination',metavar='DEST',
                   help='copy every project\'s last complete backup pair under this off-machine directory; '
                        'refuses unless backup-status --require-complete would pass')
    a=sub.add_parser('backup-repoint');a.add_argument('project')
    a=sub.add_parser('restore-new');a.add_argument('project');a.add_argument('destination')
    a.add_argument('--restore-operators',action='store_true',dest='restore_operators',
                   help='explicitly re-grant the operator allowlist entries the backup records that this '
                        'host no longer lists; off by default because the allowlist is deployment-wide '
                        'authority for every project and a stale backup must not undo a revocation')
    a=sub.add_parser('reconcile-request');a.add_argument('project');a.add_argument('--request-id',required=True)
    a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a.add_argument('--disposition',choices=['failed','released','complete'],default='released')
    a.add_argument('--issue-id',dest='issue_id',default=None,
                   help='with --disposition complete, the exact labelled native issue to confirm and attach')
    a.add_argument('--any-actor',action='store_true',dest='any_actor',
                   help='with --disposition released (or failed on a receipt with no recorded actor), open the request ID to any actor')
    a=sub.add_parser('service');a.add_argument('action',choices=['start','stop','restart','status'])
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
    elif args.command=='service':print(service(root,args.action))
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
        payload=json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(handoff(path,args.actor,payload,run,operator=True)))
    elif args.command=='requirement-backfill':
        import fcntl
        from requirement_records import backfill
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(backfill(payload,args.actor,run,path)))
    elif args.command=='requirement-apply':
        import fcntl
        from requirement_records import apply_native
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        payload=json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(apply_native(payload,args.actor,run,path,operator=True)))
    elif args.command=='requirement-reconcile':
        import fcntl
        from requirement_records import reconcile
        path=project_dir(root,args.project)
        if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            print(json.dumps(reconcile(path,args.operation_id,args.actor,args.reason,
                                       args.disposition,run,issue_id=args.issue_id)))
    elif args.command=='void-record':
        import fcntl
        from review_workflow import apply_void
        path=project_dir(root,args.project)
        payload=json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict) or not isinstance(payload.get('task'),str):raise ValueError('Void record payload must name its task')
        authority=operators(root, strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            rows=[json.loads(line) for line in run_bd(root,args.project,['export','--all']).splitlines() if line.strip()]
            print(json.dumps(apply_void(rows,payload['task'],args.actor,payload,run,operator=True,operators=authority)))
    elif args.command=='revert-record':
        import fcntl
        from review_workflow import apply_revert
        path=project_dir(root,args.project)
        payload=json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict) or not isinstance(payload.get('task'),str):raise ValueError('Integration revert payload must name its task')
        authority=operators(root, strict=True)
        def run(argv):return run_bd(root,args.project,['--actor',args.actor,*argv])
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            rows=[json.loads(line) for line in run_bd(root,args.project,['export','--all']).splitlines() if line.strip()]
            print(json.dumps(apply_revert(rows,payload['task'],args.actor,payload,run,operator=True,operators=authority)))
    elif args.command=='operators':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        cfg=config(root)
        current=stored_operators(cfg)
        if args.action=='list':print(json.dumps({'operators':current}));return
        # Config is the single authority source; a shell-only ORCHESTRA_OPERATORS
        # that disagrees is refused before the change rather than applied here
        # and ignored by the endpoint.
        operators(root, strict=True)
        if not args.actor:raise ValueError('operators '+args.action+' requires an actor identity')
        actor=identity(args.actor,'Invalid operator identity')
        if args.action=='add':
            if actor not in current:current.append(actor)
        else:
            if not args.confirm_revoke:
                raise ValueError('operators remove revokes ' + actor + ': voids they authored stop applying on reads '
                                 '(re-add restores them). Re-run with --confirm-revoke to acknowledge this.')
            if actor in current:current.remove(actor)
        if current:cfg['operators']=current
        else:cfg.pop('operators',None)
        atomic_private_write(marker,json.dumps(cfg))
        print(json.dumps({'operators':current}))
    elif args.command=='backup':backup_projects(root,args.projects,args.all_projects)
    elif args.command=='backup-copy':backup_copy(root,args.destination)
    elif args.command=='backup-repoint':print(json.dumps(repoint_backup(root,args.project),sort_keys=True))
    elif args.command=='backup-status':
        record=read_backup_status(root)
        print(json.dumps(record,sort_keys=True))
        if args.require_complete:
            problems=require_complete_problems(root,record)
            if problems:
                raise SystemExit('backup-status: not every project has a complete backup pair on disk: '
                                 +'; '.join(problems))
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
        backup=root/'backups'/args.project
        if not backup.is_dir():raise ValueError('Source backup missing')
        if args.project==args.destination:raise ValueError('Restore requires a different destination')
        with backup_lock(root,args.project):
            coordination_backup(root,args.project)
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
            operators(root)
            if args.restore_operators and not (root/'deployment.private.json').is_file():
                raise ValueError('Deployment is not installed; run install first')
            add_project(root,args.destination)
            print(run_bd(root,args.destination,['backup','restore',str(backup),'--force']))
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
            restore_coordination(root,args.project,args.destination,
                                 restore_operators=args.restore_operators)
            restored=restore_journal(snapshot,
                                     project_dir(root,args.destination)/JOURNAL_STORE_NAME)
            if restored is None:
                print('Backup has no operation-journal snapshot; the restored project starts with an empty identity journal.')
        print('Restored only into the newly created project; retained original issue IDs. Never use this clone as a second live tracker.')

if __name__=='__main__':
    try: main()
    except subprocess.CalledProcessError as e:
        # Never echo credential-bearing command input or the environment.
        raise SystemExit(f'Command failed ({e.returncode}): {e.stderr[:2000]}')
    except TerminatedBySignal as e:
        # The guarded cleanup ran (previous pair kept, sync client's group stopped); exit
        # with the conventional 128+signal status instead of a traceback.
        raise SystemExit(128+e.signum)
