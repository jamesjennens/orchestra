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

def config(root):
    return read_json_file(root/'deployment.private.json','Deployment configuration')

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
        value=read_json_file(marker,'Deployment configuration').get('operators')
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

def review_workflow_writes(root, strict=False):
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
    """
    enabled = False
    marker = root/'deployment.private.json'
    if marker.is_file():
        value = read_json_file(marker,'Deployment configuration').get('review_workflow_writes')
        if isinstance(value,bool):
            enabled = value
        elif value is not None:
            raise ValueError('deployment review_workflow_writes must be true or false')
    return enabled

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
        value=read_json_file(marker,'Deployment configuration').get('verifiers')
        if isinstance(value,list):found.extend(value)
        elif isinstance(value,str):found.append(value)
        elif value is not None:raise ValueError('deployment verifiers must be a list of actor identities')
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
            rows=[json.loads(line) for line in run_bd(root,name,['export','--all']).splitlines() if line.strip()]
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
                'BEADS_DOLT_PASSWORD':config(root)['password'],'DOLT_CLI_PASSWORD':config(root)['password'],
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
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,TypeError):
        return 'partial'
    if not isinstance(rows,list):return 'partial'
    slot=destination+'-merge-slot'
    return 'empty' if all(isinstance(row,dict) and row.get('id')==slot for row in rows) else 'partial'

def restore_failure_notice(destination,error,state='partial',step='native restore'):
    """What an operator must do after the native step of ``restore-new`` did not complete.

    ``state`` is ``restore_destination_state``'s answer: the notice says "partial" only
    when the destination is partial (kittrial-5bb.82 review). ``step`` names the step
    that stopped: every step after the destination starts to exist prints this notice.
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
    if state=='empty':
        return ('restore-new did not complete: %s. Project %s exists as an empty, working project: nothing '
                'was restored into it (and its coordination sidecar, journals and operation journal were NOT '
                'restored). Do not use it as a tracker. Retire it (%s) and run restore-new again into another '
                'unused destination name; the source backup was not modified.'%(cause,destination,retire))
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
            'line':scheduled_backup_execstart(root),'last_run':None}
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

#: Where ``retire-project`` moves a project directory, and its append-only journal.
RETIRED_DIR='retired'
RETIRE_JOURNAL='journal.jsonl'
#: The receipt journals whose ``pending`` entries are reservations still in flight.
RESERVATION_JOURNALS=('.coordination-requests','.requirement-requests','.handoff-requests',
                      '.reference-requests','.proposal-requests','.capability-requests')

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
        except (subprocess.CalledProcessError,ValueError,TypeError):
            bd='rejects';slot='not-applicable'
        except (subprocess.TimeoutExpired,OSError):
            bd='unreachable'
    if bd=='reads':
        try:
            state=json.loads(run_bd(root,name,['merge-slot','check','--json']) or 'null')
            if isinstance(state,dict) and 'available' in state and not state.get('error'):
                holder=state.get('holder');slot='held' if holder else 'free'
            elif isinstance(state,dict):slot='missing'
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError,TypeError):pass
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

def retire_project(root,name,actor,reason,force=False):
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
    """
    import fcntl
    from keyed_records import require_configured_operator
    require_configured_operator(actor,operators(root,strict=True),'retire a project')
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
    return record

def append_retire_journal(root,record):
    """Append one line to ``retired/journal.jsonl`` (0600), flushed to disk."""
    path=root/RETIRED_DIR/RETIRE_JOURNAL
    if path.is_symlink():raise ValueError('The retire journal must not be a symlink')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a',encoding='utf-8') as handle:
        handle.write(json.dumps(record,sort_keys=True,ensure_ascii=False)+'\n')
        handle.flush();os.fsync(handle.fileno())

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
    refuse_retired_name(root,name)
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

# The idempotency journals of the accepted record designs (kittrial-5bb.64, the
# shared slice 0): .41 `.reference-requests`, .58 `.proposal-requests` and .60
# `.capability-requests`. No writer exists in this kit, but a backup taken by a
# later slice must restore here (a rollback target), so each is whitelisted,
# backed up, and validated with its frozen receipt schema before any write.
RECORD_JOURNALS=('.reference-requests','.proposal-requests','.capability-requests')

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
        journal = isinstance(name,str) and re.fullmatch(r'(?:\.coordination-requests|\.handoffs|\.handoff-requests|\.handoff-recoveries|\.requirement-requests|\.requirement-backfills|\.integration-reverts|\.reference-requests|\.proposal-requests|\.capability-requests)/[a-f0-9]{64}\.json',name)
        if name not in ('.merge-context.json','ONBOARDING.md','GUIDANCE.md','.guidance.json','.guidance-clear.json','.sessions.json','.feedback.jsonl') and not quarantine and not journal:raise ValueError('Invalid coordination backup path')
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
            rows=[json.loads(line) for line in run_bd(root,name,['export','--all']).splitlines() if line.strip()]
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
            rows=[json.loads(line) for line in run_bd(root,name,['export','--all']).splitlines() if line.strip()]
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
            rows=[json.loads(line) for line in run_bd(root,name,['export','--all']).splitlines() if line.strip()]
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
                         ' (see %s)'%(root/'backups'/BACKUP_STATUS_NAME))

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

def merge_verifiers(root,actors):
    """Add missing verifiers to the deployment list; return the added names.

    Additive only, and only ever called by `restore_coordination` when the operator
    explicitly passed `--restore-verifiers`: the list is deployment-wide authority, so
    a backup taken before `verifiers remove ACTOR --confirm-revoke` must not silently
    undo that revocation.
    """
    marker=root/'deployment.private.json'
    if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
    from recovery import identity
    wanted=[identity(item,'Invalid verifier identity') for item in actors]
    cfg=config(root)
    current=stored_verifiers(cfg)
    added=[item for item in wanted if item not in current]
    if not added:return []
    cfg['verifiers']=current+added
    atomic_private_write(marker,json.dumps(cfg))
    return added

def missing_verifiers(root,source):
    """Verifiers recorded in a project backup sidecar that this host does not list."""
    listed=verifiers(root)
    return [item for item in coordination_verifiers(root,source) if item not in listed]

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

def restore_coordination(root,source,destination,restore_operators=False,restore_verifiers=False):
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
    elif missing:
        added=merge_operators(root,missing)
        if added:
            print('Re-granted operator allowlist entries from the backup (--restore-operators): ' + ', '.join(added))
    # The verifiers list is the second deployment-wide authority (.60 section 5.2) and
    # follows the same rule: never re-granted by a restore on its own.
    unlisted=missing_verifiers(root,source)
    if unlisted and not restore_verifiers:
        print('NOT restored: the backup records capability verifiers this host does not list: '
              + ', '.join(unlisted) + '. They stay unlisted here, so capability verifications they recorded read '
              '`reported`, not `verified`, and drift only their passes had cleared reappears. Re-grant one '
              'deliberately with `admin.py --root ROOT verifiers add ACTOR`, or re-run this restore with '
              '--restore-verifiers to re-establish the whole recorded list.')
    elif unlisted:
        added=merge_verifiers(root,unlisted)
        if added:
            print('Re-granted capability verifiers from the backup (--restore-verifiers): ' + ', '.join(added))

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
    the printed line names the interpreter the deployment actually tested.
    """
    executable=getattr(sys,'executable','') or ''
    if executable.startswith('/') and AUTHORIZED_KEY_PYTHON.fullmatch(executable):
        return executable
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

def authorized_key_lines(root,kit,key_type,key_body,key_comment='',comment=None,python=None):
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
    command=' '.join((python,)+AUTHORIZED_KEY_PYTHON_FLAGS+(wrapper,'--root',root,
                                                           '--endpoint',endpoint))
    contributor='command="%s",%s %s'%(command,','.join(CONTRIBUTOR_KEY_OPTIONS),key)
    return {'root':root,'kit':kit,'endpoint':endpoint,'wrapper':wrapper,'python':python,
            'python_flags':list(AUTHORIZED_KEY_PYTHON_FLAGS),
            'contributor_options':list(CONTRIBUTOR_KEY_OPTIONS),
            'contributor':contributor,'operator':key}

def authorized_keys(root,key_file,role='both',python=None,comment=None):
    """Print the installable lines for one public key as JSON (see authorized_key_lines)."""
    kit=Path(__file__).resolve().parent
    for name in ('ssh_forced_command.py','endpoint.py'):
        if not (kit/name).is_file():
            raise ValueError('This kit copy has no %s; run the helper from the installed kit directory'%name)
    path=Path(key_file)
    lines=authorized_key_lines(root,kit,*public_key_line(path.read_text(encoding='utf-8-sig'),str(path)),
                               comment=comment,python=python)
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
                      'never two entries for the same key.']}
    if role in ('contributor','both'):payload['contributor']=lines['contributor']
    if role in ('operator','both'):payload['operator']=lines['operator']
    print(json.dumps(payload,ensure_ascii=True,indent=2))
    if role in ('operator','both'):
        print('warning: the operator line is unrestricted service-account shell access; '+OPERATOR_KEY_NOTE,
              file=sys.stderr)

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('install');a.add_argument('--port',type=int,default=13317);a.add_argument('--unit',default='beads-team.service')
    a=sub.add_parser('add-project');a.add_argument('project')
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
    a=sub.add_parser('operators');a.add_argument('action',choices=['list','add','remove']);a.add_argument('actor',nargs='?')
    a.add_argument('--confirm-revoke',action='store_true',dest='confirm_revoke',
                   help='with remove: acknowledge that this operator\'s earlier operator voids stop applying')
    a.add_argument('--all-revoked',action='store_true',dest='all_revoked',
                   help='with remove: name every affected entry instead of the first 5 (the warning truncates otherwise)')
    a=sub.add_parser('verifiers',help='the capability verifiers list: actors whose capability-verify records read verified')
    a.add_argument('action',choices=['list','add','remove']);a.add_argument('actor',nargs='?')
    a.add_argument('--confirm-revoke',action='store_true',dest='confirm_revoke',
                   help='with remove: acknowledge that this verifier\'s capability verifications stop reading verified')
    a=sub.add_parser('review-writes',help='read or set the per-installation switch that allows WRITING the new review-workflow record shapes (readers understand them either way; OFF by default)')
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
    a=sub.add_parser('backup');a.add_argument('projects',nargs='*',metavar='project')
    a.add_argument('--all',action='store_true',dest='all_projects',
                   help='back up every initialized project in this runtime in one run')
    a=sub.add_parser('retire-project',help='retire a partial or drill project: move it to retired/; deletes nothing (operator allowlist)')
    a.add_argument('project');a.add_argument('--actor',required=True);a.add_argument('--reason',required=True)
    a.add_argument('--force',action='store_true',
                   help='retire it although it looks like a working tracker (or could not be checked), holds the merge slot or has pending reservations')
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
    a=sub.add_parser('restore-new');a.add_argument('project');a.add_argument('destination')
    a.add_argument('--restore-operators',action='store_true',dest='restore_operators',
                   help='explicitly re-grant the operator allowlist entries the backup records that this '
                        'host no longer lists; off by default because the allowlist is deployment-wide '
                        'authority for every project and a stale backup must not undo a revocation')
    a.add_argument('--restore-verifiers',action='store_true',dest='restore_verifiers',
                   help='explicitly re-grant the capability verifiers the backup records that this host no '
                        'longer lists; off by default for the same reason as --restore-operators')
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
            rows=[json.loads(line) for line in run_bd(root,args.project,['export','--all']).splitlines() if line.strip()]
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
            rows=[json.loads(line) for line in run_bd(root,args.project,['export','--all']).splitlines() if line.strip()]
            print(json.dumps(apply_revert(rows,payload['task'],args.actor,payload,run,operator=True,
                                          operators=authority,journal=path)))
    elif args.command=='operators':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        cfg=config(root)
        current=stored_operators(cfg)
        if args.action=='list':
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
        if args.action=='add':
            # An HTTP account or agent id is never an operator (kittrial-5bb.70 review
            # 01a10308): the web service acts under those ids, and an older kit's advice
            # ("operators add ACTOR" for an inert web disposition) must not allowlist one.
            from http_authority import http_actor_id
            if http_actor_id(actor) is not None:
                raise ValueError('%s has the shape of an HTTP account or agent id; such an id is never added to the '
                                 'operator allowlist. A web disposition counts through the web service, not '
                                 'through this list'%actor)
            if actor not in current:current.append(actor)
        else:
            if not args.confirm_revoke:
                limit=None if args.all_revoked else 5
                raise ValueError('operators remove revokes ' + actor + ': voids they authored stop applying on '
                                 'reads, and so do the host-issued integration revert records and retractions '
                                 'they authored' + revoked_revert_records(root,actor,limit) +
                                 ' (re-add restores them).' + revoked_keyed_voids(root,actor,limit) +
                                 revoked_proposal_records(root,actor,limit) +
                                 ' Re-run with --confirm-revoke to acknowledge this.')
            if actor in current:current.remove(actor)
        if current:cfg['operators']=current
        else:cfg.pop('operators',None)
        atomic_private_write(marker,json.dumps(cfg))
        print(json.dumps({'operators':current}))
    elif args.command=='verifiers':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        cfg=config(root)
        current=stored_verifiers(cfg)
        if args.action=='list':print(json.dumps({'verifiers':current}));return
        # Config is the single authority source, exactly as for `operators`.
        verifiers(root, strict=True)
        if not args.actor:raise ValueError('verifiers '+args.action+' requires an actor identity')
        actor=identity(args.actor,'Invalid verifier identity')
        if args.action=='add':
            if actor not in current:current.append(actor)
        else:
            if not args.confirm_revoke:
                raise ValueError('verifiers remove revokes ' + actor + ': every capability verification they '
                                 'recorded reads `reported` instead of `verified`, and drift that only their '
                                 'passes had cleared reappears' + revoked_verifications(root,actor) +
                                 ' (re-add restores them). Re-run with --confirm-revoke to acknowledge this.')
            if actor in current:current.remove(actor)
        if current:cfg['verifiers']=current
        else:cfg.pop('verifiers',None)
        atomic_private_write(marker,json.dumps(cfg))
        print(json.dumps({'verifiers':current}))
    elif args.command=='review-writes':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        actor=identity(args.actor,'Invalid actor identity')
        authority=operators(root, strict=True)
        if actor not in authority:
            raise ValueError('review-writes requires an actor on the deployment operator allowlist '
                             '(deployment.private.json operators); ' + actor + ' is not on it')
        cfg=config(root)
        if args.action=='status':
            print(json.dumps({'review_workflow_writes':review_workflow_writes(root)}))
            return
        enabled=args.action=='on'
        # OFF is the absent key, so a deployment that never turned it on and one
        # that turned it back off read identically.
        if enabled:cfg['review_workflow_writes']=True
        else:cfg.pop('review_workflow_writes',None)
        atomic_private_write(marker,json.dumps(cfg))
        print(json.dumps({'review_workflow_writes':review_workflow_writes(root)}))
    elif args.command=='authorized-keys':
        authorized_keys(root,args.key_file,args.role,args.python,args.comment)
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
        refuse_retired_name(root,args.destination)
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
            step='add-project'
            try:
                # SIGTERM is an exception for the whole of what follows, not only inside
                # the native restore: a stop during add-project (or the re-point) used to
                # end the process with no notice at all (review 01a1026a).
                with signal_termination_guard():
                    add_project(root,args.destination)
                    # The native restore runs through the Dolt SQL client (no bd ~10 s read
                    # timeout), in its own process group, and adopts the restored project
                    # identity; a destination without server metadata keeps `bd backup restore`.
                    step='native restore'
                    print(native_restore(root,args.project,args.destination))
                    step='re-point and coordination'
                    finish_restore(root,args,snapshot)
            except BaseException as error:
                # add-project's own refusals (a populated or retired destination) are raised
                # before it creates anything: they need no notice about a leftover project.
                if not (step=='add-project' and isinstance(error,ValueError)):
                    print(restore_failure_notice(args.destination,error,
                                                 restore_destination_state(root,args.destination),step=step),
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

def finish_restore(root,args,snapshot):
    """What ``restore-new`` does after the native restore: re-point, sidecar, journals."""
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
                         restore_operators=args.restore_operators,
                         restore_verifiers=args.restore_verifiers)
    restored=restore_journal(snapshot,
                             project_dir(root,args.destination)/JOURNAL_STORE_NAME)
    if restored is None:
        print('Backup has no operation-journal snapshot; the restored project starts with an empty identity journal.')

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
