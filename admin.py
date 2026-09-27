#!/usr/bin/env python3
"""Operator commands for an isolated, user-systemd Beads/Dolt deployment."""
import argparse
import base64
import csv
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager
from bootstrap import install as install_binaries
from requirements import content_hash

#: The live operation-journal store inside one project directory. It is included in a
#: native project backup through ``snapshot_journal``/``restore_journal`` below (`bd
#: backup` itself covers Dolt only).
JOURNAL_STORE_NAME='.http-operations.sqlite3'

#: The per-run scheduled-backup status file inside the runtime's backups directory.
#: One run writes one record covering the projects it attempted, so an operator's
#: off-machine copy can read which projects have a complete backup pair.
BACKUP_STATUS_NAME='backup-status.json'

def checked(cmd, **kwargs):
    return subprocess.run(list(map(str,cmd)),text=True,encoding='utf-8',capture_output=True,check=True,**kwargs)

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

def scheduled_backup_unit_path():
    """The installed user unit the scheduled-backup template is copied to."""
    return Path.home()/'.config/systemd/user'/'beads-backup.service'

def scheduled_backup_execstart(root):
    """The exact ``ExecStart`` line that covers every project of this runtime."""
    return (f'ExecStart={sys.executable} {Path(__file__).resolve()} '
            f'--root {root} backup --all')

def scheduled_backup_coverage(root,name):
    """(already_covers_every_project, message) for the INSTALLED schedule.

    ``True`` only for the durable ``backup --all`` form; a unit that merely lists
    ``name`` inside its project list is reported as included but not durable
    (the next project added would need another edit).

    The schedule is an operator-owned user systemd unit copied from
    ``templates/beads-backup.service``; this reads the file that is actually
    installed (not the template) and reports it factually. A unit that runs
    another runtime, one that lists projects individually, one that already uses
    ``backup --all`` and an absent unit are distinguished, and every message says
    which project is not covered and, unless the durable ``--all`` form is already
    installed, prints the exact ``ExecStart`` line to use. It is a report, not a
    gate: this never edits, installs or enables a unit.
    """
    import shlex
    path=scheduled_backup_unit_path()
    line=scheduled_backup_execstart(root)
    if not path.is_file():
        return False,(f'The installed scheduled backup unit {path} is missing, so no project is on a '
                      f'schedule. A schedule that covers every project, including {name}, is:\n  {line}')
    try:
        text=path.read_text(encoding='utf-8')
    except OSError as error:
        return False,(f'The installed scheduled backup unit {path} could not be read ({error}), so schedule '
                      f'coverage of {name} cannot be confirmed. Use a schedule that covers every project:\n  {line}')
    ours=False;all_projects=False;covered=[]
    for raw in text.splitlines():
        raw=raw.strip()
        if not raw.startswith('ExecStart='):continue
        try:tokens=shlex.split(raw[len('ExecStart='):])
        except ValueError:continue
        if not any(Path(token).name=='admin.py' for token in tokens):continue
        unit_root=[tokens[i+1] for i,token in enumerate(tokens[:-1]) if token=='--root']
        unit_root+= [token.split('=',1)[1] for token in tokens if token.startswith('--root=')]
        if not unit_root:continue
        try:
            if Path(unit_root[0]).expanduser().resolve()!=root:continue
        except OSError:continue
        ours=True
        try:index=tokens.index('backup',next(i for i,token in enumerate(tokens) if Path(token).name=='admin.py'))
        except (StopIteration,ValueError):continue
        tail=tokens[index+1:]
        if '--all' in tail:all_projects=True
        covered+=[token for token in tail if not token.startswith('-')]
    if all_projects:
        return True,(f'The installed scheduled backup unit {path} already backs up every project of this '
                     f'runtime (backup --all), so {name} is covered; no change is needed.')
    covered=sorted(set(covered))
    if not ours:
        return False,(f'The installed scheduled backup unit {path} does not back up this runtime ({root}), so '
                      f'{name} is not covered. Replace it with a schedule that covers every project:\n  {line}')
    if name in covered:
        return False,(f'The installed scheduled backup unit {path} already includes {name} (projects: '
                      f'{", ".join(covered)}), but it names projects individually, so the next project added '
                      f'needs the same edit. Replace that project list with the durable form:\n  {line}')
    return False,(f'The installed scheduled backup unit {path} covers only '
                  f'{", ".join(covered) if covered else "no project"}, so it does not include {name}. Add this '
                  f'exact line (a oneshot service may run several ExecStart lines), or replace the project '
                  f'list with --all:\n  {line}')

def add_project(root,name):
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

def backup_project(root,name):
    import fcntl
    from coordination import atomic
    path=project_dir(root,name)
    with (path/'.coordination.lock').open('a') as lock, backup_lock(root,name):
        fcntl.flock(lock,fcntl.LOCK_EX)
        bundle=root/'backups'/(name+'.coordination.json')
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
        # sidecar so a backup never pairs one store's state with the other's.
        snapshot_journal(path/JOURNAL_STORE_NAME,journal_snapshot_path(root,name))
        output=run_bd(root,name,['backup','sync'])
        # The deployment operator allowlist travels with the project sidecar so a
        # restore can TELL the operator which recorded authority is missing on the
        # destination host. It is not applied automatically: the allowlist is
        # deployment-wide authority, so `restore-new` only re-grants it with an
        # explicit --restore-operators. Native backup preserves the void comments
        # and this preserves the record of the authority the reads would need.
        atomic(bundle,{'schema_version':1,'status':'complete','files':files,
                       'operators':sorted(operators(root))})
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

def read_backup_status(root):
    path=root/'backups'/BACKUP_STATUS_NAME
    if path.is_symlink():raise ValueError('Backup status file must not be a symlink')
    if not path.is_file():raise ValueError('No backup status file; run a backup first')
    try:record=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('Backup status file is not readable JSON') from None
    validate_backup_status(record)
    return record

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
            reason=' '.join(str(error).split()) or error.__class__.__name__
            results.append({'name':name,'status':'failed','reason':reason[:400]})
            incomplete.append(name);continue
        complete,reason=backup_pair_state(root,name)
        if complete:
            results.append({'name':name,'status':'complete','completed_at':utc_stamp()})
            print(native)
        else:
            results.append({'name':name,'status':'failed','reason':reason})
            incomplete.append(name)
    write_backup_status(root,backup_status_record(results,scope,generated_at))
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

def coordination_backup(root,source):
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    if not bundle.exists():
        return None
    if bundle.is_symlink():raise ValueError('Coordination backup must not be a symlink')
    data=json.loads(bundle.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('schema_version')!=1 or data.get('status')!='complete':raise ValueError('Incomplete coordination backup; recover/reconcile source first')
    validate_coordination_files(data.get('files'))
    validate_coordination_operators(data.get('operators'))
    return data['files']

def coordination_operators(root,source):
    """Operator allowlist snapshot in a project sidecar, or [] when absent."""
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    if not bundle.is_file() or bundle.is_symlink():return []
    try:data=json.loads(bundle.read_text(encoding='utf-8'))
    except ValueError:return []
    if not isinstance(data,dict):return []
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
    wanted=[identity(item,'Invalid operator identity') for item in actors]
    cfg=config(root)
    current=list(cfg.get('operators') or [])
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


def restore_coordination(root,source,destination,restore_operators=False):
    from coordination import atomic
    path=project_dir(root,destination)
    files=coordination_backup(root,source)
    if files is None:
        print('Legacy backup has no coordination journal. Reconcile outstanding child requests and merge ownership before accepting writes.')
        return
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
    a=sub.add_parser('operators');a.add_argument('action',choices=['list','add','remove']);a.add_argument('actor',nargs='?')
    a.add_argument('--confirm-revoke',action='store_true',dest='confirm_revoke',
                   help='with remove: acknowledge that this operator\'s earlier operator voids stop applying')
    a=sub.add_parser('backup');a.add_argument('projects',nargs='*',metavar='project')
    a.add_argument('--all',action='store_true',dest='all_projects',
                   help='back up every initialized project in this runtime in one run')
    a=sub.add_parser('backup-status')
    a.add_argument('--require-complete',action='store_true',dest='require_complete',
                   help='exit non-zero unless the last run covered every project (--all) and every recorded '
                        'pair is still complete on disk')
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
    elif args.command=='operators':
        marker=root/'deployment.private.json'
        if not marker.is_file():raise ValueError('Deployment is not installed; run install first')
        from recovery import identity
        cfg=config(root)
        current=list(cfg.get('operators') or [])
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
    elif args.command=='backup-status':
        record=read_backup_status(root)
        print(json.dumps(record,sort_keys=True))
        if args.require_complete:
            problems=[]
            if record['scope']!='all':
                problems.append('the last run was a named run, so it does not cover every initialized project')
            for entry in record['projects']:
                complete,reason=backup_pair_state(root,entry['name'])
                if not complete:problems.append('%s: %s'%(entry['name'],reason))
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
