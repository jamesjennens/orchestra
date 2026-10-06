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

def install(root, asset_dir=None):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('This tested lockfile supports Linux x86_64 only.')
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = json.loads(Path(__file__).with_name('versions.json').read_text())
    for name in ('bd', 'dolt'):
        # A binary may have a second pinned archive, NAME_static (kittrial-5bb.161): "bd" is
        # upstream's release, which needs glibc 2.34, and "bd_static" the same version built
        # without cgo for older hosts. A release bundles one of them as vendor/NAME.tar.gz and
        # that one is installed; a download is always the first, the only one with a url. An
        # existing binary is kept as long as it is either pin: prepare never swaps one for the
        # other in a runtime that is in use.
        pins={key:lock[key] for key in (name,name+'_static') if key in lock}
        dest=root/'bin'/name
        receipt=root/'bin'/(name+'.receipt.json')
        if dest.exists():
            if not receipt.exists(): raise SystemExit(f'Refusing to replace unmanaged binary: {dest}')
            old=json.loads(receipt.read_text())
            kept=[key for key,pin in pins.items() if pin['sha256']==old['archive_sha256']]
            if not kept or hashlib.sha256(dest.read_bytes()).hexdigest()!=old['binary_sha256']:
                raise SystemExit(f'Binary/pin mismatch; use a new deployment root for upgrade: {dest}')
            print(f'{name}: verified existing {pins[kept[0]]["version"]}'+('' if kept[0]==name else f' ({kept[0]})'),flush=True)
            continue
        local = Path(asset_dir) / (name + '.tar.gz') if asset_dir else None
        if local is not None:
            data=local.read_bytes()
        else:
            with urllib.request.urlopen(lock[name]['url'], timeout=60) as response:
                data=response.read()
        found=[key for key,pin in pins.items() if pin['sha256']==hashlib.sha256(data).hexdigest() and (local is not None or key==name)]
        if not found: raise SystemExit(f'{name}: archive checksum mismatch')
        key=found[0];item=pins[key]
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
            member=archive.getmember(item['member'])
            if not member.isfile(): raise SystemExit('Expected a regular binary member')
            binary=archive.extractfile(member).read()
        dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=dest.with_suffix('.tmp')
        tmp.write_bytes(binary); tmp.chmod(0o755); os.replace(tmp,dest)
        receipt.write_text(json.dumps({'version':item['version'],'archive_sha256':item['sha256'],'binary_sha256':hashlib.sha256(binary).hexdigest(),'pin':key},indent=2)+'\n')
        print(f'{name}: installed verified {item["version"]}'+('' if key==name else f' ({key})'),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    install(p.parse_args().root)
