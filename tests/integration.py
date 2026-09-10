"""Explicit disposable-project test using two independent SSH client processes."""
import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client import request

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--project',required=True);p.add_argument('--other-project',required=True);p.add_argument('--restore-project',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();cfg=json.loads(Path(a.config).read_text());results=[];start=time.monotonic()
    def call(args,actor='alice',project=None,action='bd',path=None,ok=True):
        r=request(cfg,project or a.project,actor,args,action,path)
        if ok and r['returncode']:raise AssertionError(r['stderr'])
        return r
    def mark(name,detail):results.append({'check':name,'result':'PASS','detail':detail});print('PASS',name,flush=True)
    def admin(*args):
        script=str(Path(cfg['endpoint']).with_name('admin.py')).replace('\\','/')
        command=' '.join(shlex.quote(x) for x in ['python3',script,'--root',cfg['root'],*args])
        r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',cfg['host'],command],capture_output=True,text=True,encoding='utf-8',timeout=120)
        if r.returncode:raise AssertionError(r.stderr)
        return r.stdout
    token=uuid.uuid4().hex[:8]
    job=json.loads(call(['create','Shared pilot '+token,'--type','epic','--json'])['stdout'])['id']
    with tempfile.TemporaryDirectory() as tmp:
        file=Path(tmp)/'plan with spaces.md';text="Intent: preserve user's correction.\nImpact: src/parser.py; quote schema unchanged.\nLiteral shell text: $(echo nope) `echo nope` 'quoted' café."
        file.write_text(text,encoding='utf-8')
        task=json.loads(call(['create','Plan roundtrip '+token,'--parent',job,'--body-file',str(file),'--json'])['stdout'])['id']
        shown=json.loads(call(['show',task,'--json'],actor='bob')['stdout'])[0]
        assert shown['description'].replace('\r\n','\n')==text
        mark('cross-client plan and quoting','Bob read exactly the UTF-8 plan written by Alice; shell metacharacters stayed data.')
    outcomes=[]
    for n in range(3):
        target=json.loads(call(['create',f'Claim race {token}-{n}','--parent',job,'--json'])['stdout'])['id']
        with ThreadPoolExecutor(2) as pool:
            rs=list(pool.map(lambda actor:call(['update',target,'--claim','--json'],actor=actor,ok=False),['alice','bob']))
        assert sum(r['returncode']==0 for r in rs)==1,rs
        owner=json.loads(call(['show',target,'--json'])['stdout'])[0]['assignee']
        outcomes.append(owner)
    mark('simultaneous claim','Exactly one winner in each of three independent two-client races: '+','.join(outcomes))
    with ThreadPoolExecutor(2) as pool:
        rs=list(pool.map(lambda actor:call(['comments','add',task,f'Kind: finding\nIndependent report from {actor} {token}','--json'],actor=actor),['alice','bob']))
    comments=json.loads(call(['comments',task,'--json'])['stdout'])
    assert sum(token in c['text'] for c in comments)==2
    old=comments[0]['id'];eid=f'{task}-c{old}'
    new=json.loads(call(['comments','add',task,f'Kind: correction\nSupersedes: {eid}\nKeep the original assertion visible; revised conclusion.','--json'],actor='bob')['stdout'])['id']
    call([],action='refresh')
    taskpage=call([],action='view',path=f'jobs/{task}.md')['stdout']
    assert eid in taskpage and f'{task}-c{new}' in taskpage and 'Later annotations' in taskpage
    mark('comments and correction backlinks','Both concurrent reports persisted; original entry links to the later correction.')
    other=json.loads(call(['list','--all','--limit','0','--json'],project=a.other_project)['stdout'])
    assert task not in {r['id'] for r in other}
    mark('project separation','Second project query did not include first-project issue IDs.')
    call(['close',task,'--reason','Synthetic acceptance satisfied','--json'],actor='bob')
    assert json.loads(call(['show',job,'--json'])['stdout'])[0]['status']!='closed'
    mark('structured completion','Task closed while parent job remains open.')
    admin('service','restart')
    for attempt in range(20):
        try:
            after=json.loads(call(['show',task,'--json'])['stdout'])[0]
            break
        except AssertionError:time.sleep(.5)
    else:raise AssertionError('Service failed to recover')
    assert after['status']=='closed'
    mark('service restart','Closed task and its state survived restart of the disposable service.')
    admin('backup',a.project)
    admin('restore-new',a.project,a.restore_project)
    restored=json.loads(call(['show',task,'--json'],project=a.restore_project)['stdout'])[0]
    restored_comments=json.loads(call(['comments',task,'--json'],project=a.restore_project)['stdout'])
    assert restored['status']=='closed' and len(restored_comments)==3
    restored_id=json.loads(call(['create','Restore independence '+token,'--json'],project=a.restore_project)['stdout'])['id']
    original=json.loads(call(['list','--all','--limit','0','--json'])['stdout'])
    assert restored_id not in {r['id'] for r in original}
    mark('full backup restore','Restored status and all three comments into a newly named database; a write there did not affect original.')
    call([],action='refresh')
    result={'suite':'beads-team-kit disposable SSH integration','elapsed_seconds':round(time.monotonic()-start,2),'checks':results,'versions':json.loads(Path(__file__).resolve().parents[1].joinpath('versions.json').read_text())}
    Path(a.output).parent.mkdir(parents=True,exist_ok=True);Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
    print('ALL CHECKS PASSED',flush=True)

if __name__=='__main__':main()
