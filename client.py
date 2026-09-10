#!/usr/bin/env python3
"""Portable SSH client: only trusted endpoint paths enter the remote shell command."""
import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

def request(config,project,actor,args,action='bd',path=None):
    attachments={};converted=[];i=0
    flags={'--body-file','--design-file','--file','-f'}
    while i<len(args):
        token=args[i];flag=token.split('=',1)[0]
        if flag in flags:
            if '=' in token: value=token.split('=',1)[1]
            else:
                i+=1
                if i>=len(args):raise ValueError('File flag needs a local path')
                value=args[i]
            if value=='-':raise ValueError('Use a local UTF-8 file for attachment input')
            key=str(len(attachments));attachments[key]={'flag':flag,'text':Path(value).read_text(encoding='utf-8-sig')}
            converted.append('@attachment:'+key)
        else:converted.append(token)
        i+=1
    host=config['host']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*',host):raise ValueError('Host must be an SSH alias or user@host')
    endpoint=config['endpoint'];root=config['root']
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+',x) for x in (endpoint,root)):raise ValueError('Use absolute server paths without spaces')
    command=' '.join(shlex.quote(x) for x in ['python3',endpoint,'--root',root])
    payload={'project':project,'actor':actor,'action':action,'args':converted,'attachments':attachments}
    if path is not None:payload['path']=path
    wire=json.dumps(payload,ensure_ascii=False)
    if len(wire)>2_000_000:raise ValueError('Request exceeds 2 MB')
    try:
        p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,command],input=wire,text=True,encoding='utf-8',capture_output=True,timeout=150)
    except subprocess.TimeoutExpired:
        raise RuntimeError('SSH timed out; outcome may be uncertain. Inspect state; do not blindly retry mutations.') from None
    if p.returncode:raise RuntimeError(f'SSH failed ({p.returncode}); outcome may be uncertain. {p.stderr[:1000]}')
    try:return json.loads(p.stdout)
    except json.JSONDecodeError:raise RuntimeError('Invalid endpoint response; inspect state before retrying.') from None

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--project',required=True);p.add_argument('--actor',required=True)
    p.add_argument('args',nargs=argparse.REMAINDER);a=p.parse_args()
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    action='bd';path=None
    if args[:1]==['refresh']:action='refresh';args=[]
    elif args[:1]==['view']:
        action='view';path=args[1] if len(args)>1 else 'CURRENT.md';args=[]
    result=request(json.loads(Path(a.config).read_text()),a.project,a.actor,args,action,path)
    sys.stdout.write(result['stdout']);sys.stderr.write(result['stderr']);return result['returncode']

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,RuntimeError,OSError) as e:raise SystemExit(str(e))
