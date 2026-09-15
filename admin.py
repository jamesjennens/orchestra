#!/usr/bin/env python3
"""Operator commands for an isolated, user-systemd Beads/Dolt deployment."""
import argparse
import csv
import io
import json
import os
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path
from contextlib import contextmanager
from bootstrap import install as install_binaries

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
    backup_project(root,name)
    print(f'Created project {name}')

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
        if name!='.merge-context.json' and not re.fullmatch(r'\.coordination-requests/[a-f0-9]{64}\.json',name):raise ValueError('Invalid coordination backup path')
        if not isinstance(record,dict):raise ValueError('Invalid coordination record')

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
        validate_coordination_files(files)
        output=run_bd(root,name,['backup','sync'])
        atomic(bundle,{'schema_version':1,'status':'complete','files':files})
        return output

def coordination_backup(root,source):
    validate_name(source)
    bundle=root/'backups'/(source+'.coordination.json')
    if not bundle.exists():
        return None
    if bundle.is_symlink():raise ValueError('Coordination backup must not be a symlink')
    data=json.loads(bundle.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('schema_version')!=1 or data.get('status')!='complete':raise ValueError('Incomplete coordination backup; recover/reconcile source first')
    validate_coordination_files(data.get('files'))
    return data['files']

def restore_coordination(root,source,destination):
    from coordination import atomic
    path=project_dir(root,destination)
    files=coordination_backup(root,source)
    if files is None:
        print('Legacy backup has no coordination journal. Reconcile outstanding child requests and merge ownership before accepting writes.')
        return
    for name in files:
        target=path/name
        if target.is_symlink() or target.parent.is_symlink():raise ValueError('Coordination restore paths must not be symlinks')
    for name,record in files.items():
        target=project_dir(root,destination)/name
        target.parent.mkdir(exist_ok=True)
        atomic(target,record)

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('install');a.add_argument('--port',type=int,default=13317);a.add_argument('--unit',default='beads-team.service')
    a=sub.add_parser('add-project');a.add_argument('project')
    for command in ('backup','restore-new'):
        a=sub.add_parser(command);a.add_argument('project')
        if command=='restore-new':a.add_argument('destination')
    a=sub.add_parser('service');a.add_argument('action',choices=['start','stop','restart','status'])
    args=p.parse_args();root=root_path(args.root)
    if args.command=='install':install(root,args.port,args.unit)
    elif args.command=='add-project':add_project(root,args.project)
    elif args.command=='service':print(service(root,args.action))
    elif args.command=='backup':print(backup_project(root,args.project))
    elif args.command=='restore-new':
        validate_name(args.project);validate_name(args.destination)
        backup=root/'backups'/args.project
        if not backup.is_dir():raise ValueError('Source backup missing')
        if args.project==args.destination:raise ValueError('Restore requires a different destination')
        with backup_lock(root,args.project):
            coordination_backup(root,args.project)
            add_project(root,args.destination)
            print(run_bd(root,args.destination,['backup','restore',str(backup),'--force']))
            restore_coordination(root,args.project,args.destination)
        print('Restored only into the newly created project; retained original issue IDs. Never use this clone as a second live tracker.')

if __name__=='__main__':
    try: main()
    except subprocess.CalledProcessError as e:
        # Never echo credential-bearing command input or the environment.
        raise SystemExit(f'Command failed ({e.returncode}): {e.stderr[:2000]}')
