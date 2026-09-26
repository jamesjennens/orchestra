#!/usr/bin/env python3
"""Server-side session registration/onboarding without a local kit."""
import argparse
import json
import sys
import uuid
from pathlib import Path
from client import request

START_GUIDANCE = """
Worker setup:
- Keep one working directory and checkout per actor for this project. Never use
  another worker's directory or another project's checkout.
- Save the full actor and the client settings from the project entry above in a
  private, untracked file in that directory. Do not commit or share either value.
- Use only the installed client and exact project configuration printed above.
  If onboarding fails, keep this actor and rerun onboard; do not register again.
- Choose one task from `ready --json`, then claim it atomically. Do not work on a
  task that is held or already assigned, and do not claim a second task before
  delivering the first. Follow the project's Delivery section; if it is missing or
  unclear, ask the coordinator instead of guessing.
- Only when the harness explicitly permits a loop, check every N minutes for review
  feedback on your tasks with `work --mine` first, then look for the next task with
  `ready --json`. Stop at the deadline or when you have no actionable open or
  pending-review task and `ready --json` has no unheld task. Do not wait on tasks
  held by other actors. In office mode, do not poll or loop; end each turn with a
  one-line status for the person.
"""

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--actor')
    parser.add_argument('args',nargs=argparse.REMAINDER)
    a=parser.parse_args()
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    resumed=False
    started=False
    if not args or args[0] not in ('onboard','docs','start','resume','run'):
        parser.error('Use start --name NAME, resume with --actor SAVED_ACTOR, run start|heartbeat|end|status, onboard or docs [NAME]; configure client.py for normal work')
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
        started=True
        args=['onboard']
    elif args[0]=='resume':
        if not a.actor:parser.error('resume requires --actor SAVED_ACTOR; never infer ownership from a readable name')
        resume_parser=argparse.ArgumentParser(prog='worker.py resume')
        resume_parser.add_argument('--request-id')
        options=resume_parser.parse_args(args[1:])
        request_id=options.request_id or str(uuid.uuid4())
        print('Resume request-id (reuse after an uncertain response): '+request_id,file=sys.stderr,flush=True)
        r=request(cfg,a.project,a.actor,['resume','--request-id',request_id],action='session')
        if r['returncode']:
            sys.stderr.write(r['stderr']);return r['returncode']
        print('Resumed session: '+json.dumps(json.loads(r['stdout'])['resume'],ensure_ascii=False),flush=True)
        resumed=True
        args=['onboard']
    if args[0]=='run':
        if not a.actor:parser.error('run requires --actor SAVED_ACTOR')
        r=request(cfg,a.project,a.actor,args,action='session')
    else:
        try:
            r=request(cfg,a.project,a.actor,args[1:],action=args[0])
        except (ValueError,RuntimeError,OSError):
            if started:
                print('The actor is already registered. Keep it and retry onboard after fixing the reported issue; do not register another actor.',file=sys.stderr,flush=True)
            raise
    sys.stdout.write(r['stdout']);sys.stderr.write(r['stderr'])
    if started:
        if r['returncode']:
            print('The actor is already registered. Keep it and retry onboard after fixing the reported issue; do not register another actor.',file=sys.stderr,flush=True)
        else:
            print(START_GUIDANCE,flush=True)
    if r['returncode'] or not resumed:return r['returncode']
    print('\nOwned work (revision requests first)',flush=True)
    try:
        r=request(cfg,a.project,a.actor,['--mine'],action='work')
    except (ValueError,RuntimeError,OSError) as e:
        raise RuntimeError('Session resume was recorded, but owned work could not be read. Retry work --mine with the same actor. '+str(e)) from e
    sys.stdout.write(r['stdout']);sys.stderr.write(r['stderr'])
    if r['returncode']:
        print('Session resume was recorded, but owned work could not be read. Retry work --mine with the same actor.',file=sys.stderr,flush=True)
    return r['returncode']

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,RuntimeError,OSError) as e:raise SystemExit(str(e))
