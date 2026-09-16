#!/usr/bin/env python3
"""Server-side session registration/onboarding without a local kit."""
import argparse
import json
import sys
from pathlib import Path
from client import request

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--actor')
    parser.add_argument('args',nargs=argparse.REMAINDER)
    a=parser.parse_args()
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    if not args or args[0] not in ('onboard','docs','start'):
        parser.error('Use start --name NAME, onboard or docs [NAME]; configure client.py for normal work')
    cfg={'transport':'local','python':sys.executable,'endpoint':str(Path(__file__).resolve().with_name('endpoint.py')),'root':a.root}
    if args[0]=='start':
        if a.actor:parser.error('start allocates an actor; omit --actor')
        # Ensure onboarding exists before creating the durable registration.
        from onboarding import execute as onboarding
        from admin import project_dir, root_path
        onboarding(Path(__file__).resolve().parent,project_dir(root_path(a.root),a.project),a.project,'pending-registration','onboard',[])
        r=request(cfg,a.project,None,['register',*args[1:]],action='session')
        if r['returncode']:
            sys.stderr.write(r['stderr']);return r['returncode']
        record=json.loads(r['stdout'])['session'];a.actor=record['actor']
        print('Registered session: '+json.dumps(record,ensure_ascii=False),flush=True)
        print('Use --actor '+a.actor+' for this session. Resume this ID; do not share it with another worker.',flush=True)
        args=['onboard']
    r=request(cfg,a.project,a.actor,args[1:],action=args[0])
    sys.stdout.write(r['stdout']);sys.stderr.write(r['stderr']);return r['returncode']

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,RuntimeError,OSError) as e:raise SystemExit(str(e))
