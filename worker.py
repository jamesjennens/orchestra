#!/usr/bin/env python3
"""Server-side CLI: bootstrap from any SSH-capable terminal without a local kit."""
import argparse
import json
import sys
from pathlib import Path
from client import request

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--actor',required=True)
    parser.add_argument('args',nargs=argparse.REMAINDER)
    a=parser.parse_args()
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    # This bootstrap intentionally exposes only read-only installed instructions.
    # Normal work uses client.py, including local-file attachment transport.
    if not args or args[0] not in ('onboard','docs'):
        parser.error('Use onboard or docs [NAME]; configure client.py for normal work')
    cfg={'transport':'local','python':sys.executable,'endpoint':str(Path(__file__).resolve().with_name('endpoint.py')),'root':a.root}
    r=request(cfg,a.project,a.actor,args[1:],action=args[0])
    sys.stdout.write(r['stdout']);sys.stderr.write(r['stderr']);return r['returncode']

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,RuntimeError,OSError) as e:raise SystemExit(str(e))
