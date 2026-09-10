#!/usr/bin/env python3
"""Install only the two digest-pinned binaries into an explicit deployment root."""
import argparse
import hashlib
import io
import json
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

def install(root):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('This tested lockfile supports Linux x86_64 only.')
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = json.loads(Path(__file__).with_name('versions.json').read_text())
    for name in ('bd', 'dolt'):
        item=lock[name]
        dest=root/'bin'/name
        receipt=root/'bin'/(name+'.receipt.json')
        if dest.exists():
            if not receipt.exists(): raise SystemExit(f'Refusing to replace unmanaged binary: {dest}')
            old=json.loads(receipt.read_text())
            if old['archive_sha256']!=item['sha256'] or hashlib.sha256(dest.read_bytes()).hexdigest()!=old['binary_sha256']:
                raise SystemExit(f'Binary/pin mismatch; use a new deployment root for upgrade: {dest}')
            print(f'{name}: verified existing {item["version"]}',flush=True)
            continue
        with urllib.request.urlopen(item['url'], timeout=60) as response:
            data=response.read()
        if hashlib.sha256(data).hexdigest()!=item['sha256']: raise SystemExit(f'{name}: archive checksum mismatch')
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
            member=archive.getmember(item['member'])
            if not member.isfile(): raise SystemExit('Expected a regular binary member')
            binary=archive.extractfile(member).read()
        dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=dest.with_suffix('.tmp')
        tmp.write_bytes(binary); tmp.chmod(0o755); os.replace(tmp,dest)
        receipt.write_text(json.dumps({'version':item['version'],'archive_sha256':item['sha256'],'binary_sha256':hashlib.sha256(binary).hexdigest()},indent=2)+'\n')
        print(f'{name}: installed verified {item["version"]}',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    install(p.parse_args().root)
