#!/usr/bin/env python3
"""Portable client: SSH by default, explicit local transport, never a silent fallback.

SSH remains the default and unchanged: the endpoint path is the only part of the
remote command line and only trusted config values enter it. Local transport is an
explicit opt-in (config "transport": "local") for a Linux endpoint on this machine;
it runs the same endpoint as an argv list with shell=False. Both transports send the
byte-identical JSON envelope, so attachments and actor semantics do not vary.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]*')
SERVER_PATH = re.compile(r'/[A-Za-z0-9_./-]+')
FILE_FLAGS = {'--body-file', '--design-file', '--file', '-f'}
TRANSPORTS = {'ssh', 'local'}   # anything else is refused: no fallback to a default
MAX_WIRE = 2_000_000
TIMEOUT = 150

def _endpoint_paths(config):
    endpoint,root = config.get('endpoint'),config.get('root')
    if any(not isinstance(x,str) or not SERVER_PATH.fullmatch(x) for x in (endpoint,root)):
        raise ValueError('Use absolute server paths without spaces')
    return endpoint,root

def _python(config):
    # One executable path only: flags or arguments here would become an unquoted command.
    value = config.get('python', 'python3')
    if not isinstance(value,str) or not value or value.startswith('-') or re.search(r'\s',value):
        raise ValueError('Configure "python" as one Python executable path without flags')
    return value

def _ssh_argv(config):
    host = config.get('host')
    if not isinstance(host,str) or not HOST.fullmatch(host):
        raise ValueError('Host must be an SSH alias or user@host')
    endpoint,root = _endpoint_paths(config)
    command = ' '.join(shlex.quote(x) for x in ['python3',endpoint,'--root',root])
    return ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,command],'SSH'

def _local_argv(config):
    # A local endpoint is a Linux Python script, so its paths stay absolute POSIX paths.
    endpoint,root = _endpoint_paths(config)
    return [_python(config),endpoint,'--root',root],'Local endpoint'

def _argv(config):
    if not isinstance(config,dict):raise ValueError('Client config must be a JSON object')
    transport = config.get('transport','ssh')
    if transport == 'ssh':return _ssh_argv(config)
    if transport == 'local':return _local_argv(config)
    raise ValueError(f'Unknown transport {transport!r}; use one of {sorted(TRANSPORTS)}')

def _attachments(args):
    attachments = {};converted = [];i = 0
    while i < len(args):
        token = args[i];flag = token.split('=',1)[0]
        if flag in FILE_FLAGS:
            if '=' in token: value = token.split('=',1)[1]
            else:
                i += 1
                if i >= len(args):raise ValueError('File flag needs a local path')
                value = args[i]
            if value == '-':raise ValueError('Use a local UTF-8 file for attachment input')
            key = str(len(attachments));attachments[key] = {'flag':flag,'text':Path(value).read_text(encoding='utf-8-sig')}
            converted.append('@attachment:'+key)
        else:converted.append(token)
        i += 1
    return converted,attachments

def _wire(project,actor,args,action,path):
    # Same actor rule as the endpoint, enforced here too: a rejected actor must not
    # reach the transport layer, and never reaches the server as a shell word.
    registering=action=='session' and args[:1]==['register']
    resuming=action=='session' and args[:1]==['resume']
    if registering or resuming:
        if not any(a=='--request-id' or a.startswith('--request-id=') for a in args):
            import uuid
            request_id=str(uuid.uuid4())
            print('Session request-id (reuse after an uncertain response): '+request_id,file=sys.stderr,flush=True)
            args=[*args,'--request-id',request_id]
        if registering:actor=actor or ''
    if not registering and (not isinstance(actor,str) or not ACTOR.fullmatch(actor)):
        raise ValueError('Supply a short contributor/session actor')
    converted,attachments = _attachments(args)
    payload = {'project':project,'actor':actor,'action':action,'args':converted,'attachments':attachments}
    if path is not None:payload['path'] = path
    wire = json.dumps(payload,ensure_ascii=False)
    if len(wire) > MAX_WIRE:raise ValueError('Request exceeds 2 MB')
    return wire

def request(config,project,actor,args,action='bd',path=None):
    argv,label = _argv(config)
    wire = _wire(project,actor,args,action,path)
    try:
        # A captured console child can still flash a window when the client is
        # launched by a GUI/agent on Windows. Preserve pipes and exit status.
        p = subprocess.run(argv,shell=False,input=wire,text=True,encoding='utf-8',capture_output=True,timeout=TIMEOUT,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{label} timed out; outcome may be uncertain. Inspect state; do not blindly retry mutations.') from None
    if p.returncode:raise RuntimeError(f'{label} failed ({p.returncode}); outcome may be uncertain. {p.stderr[:1000]}')
    try:return json.loads(p.stdout)
    except json.JSONDecodeError:raise RuntimeError('Invalid endpoint response; inspect state before retrying.') from None

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--project',required=True);p.add_argument('--actor')
    p.add_argument('args',nargs=argparse.REMAINDER);a=p.parse_args()
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    action='bd';path=None
    if args[:1]==['refresh']:action='refresh';args=[]
    elif args[:1]==['view']:
        action='view';path=args[1] if len(args)>1 else 'CURRENT.md';args=[]
    elif args[:1] in (['brief'],['history'],['checkpoint'],['onboard'],['docs'],['session'],['handoff'],['review'],['work']):action=args.pop(0)
    result=request(json.loads(Path(a.config).read_text()),a.project,a.actor,args,action,path)
    sys.stdout.write(result['stdout']);sys.stderr.write(result['stderr']);return result['returncode']

if __name__=='__main__':
    # Keep redirected document/template output UTF-8 on Windows as well as POSIX.
    # The remote transport already decodes UTF-8; the terminal pipe must not
    # silently re-encode it using the workstation's legacy code page.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:sys.exit(main())
    except (ValueError,RuntimeError,OSError) as e:raise SystemExit(str(e))
