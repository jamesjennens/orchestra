#!/usr/bin/env python3
"""Read-only UAT verification record for an installed office service."""
import argparse
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import office_service
from version import report


def bundled_bd(root, kit):
    """Which pinned bd the runtime carries, and whether it starts on this host (kittrial-5bb.161)."""
    import subprocess
    found = {'pin': None, 'starts': False, 'says': None}
    try:
        receipt = json.loads((Path(root)/'bin'/'bd.receipt.json').read_text(encoding='utf-8'))
        lock = json.loads((Path(kit)/'versions.json').read_text(encoding='utf-8'))
        found['pin'] = next((key for key in ('bd', 'bd_static') if isinstance(lock.get(key), dict)
                             and lock[key].get('sha256') == receipt.get('archive_sha256')), None)
    except (OSError, ValueError):
        pass
    try:
        done = subprocess.run([str(Path(root)/'bin'/'bd'), '--version'], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=60)
        lines = [line.strip() for line in done.stdout.decode('utf-8', 'replace').splitlines() if line.strip()]
        found['starts'] = done.returncode == 0
        found['says'] = (lines[0] if done.returncode == 0 else lines[-1])[:200] if lines else None
    except (OSError, subprocess.TimeoutExpired) as error:
        found['says'] = str(error)[:200]
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--config', help='the private office service JSON, to ask the listener it configures '
                                         '(without it: plain HTTP on 127.0.0.1)')
    args = parser.parse_args(argv)
    current = Path(args.install_root).expanduser().resolve()/'current'
    release = current.resolve(strict=True)
    manifest = json.loads((release/'manifest.json').read_text(encoding='utf-8'))
    kit = release/'kit'
    provenance = report(kit)
    if (provenance['source_commit'] != manifest.get('source_commit') or
            provenance['build_id'] != manifest.get('build_id')):
        raise SystemExit('Installed release provenance does not match the manifest')
    settings = office_service.service_config(args.config) if args.config else None
    served = office_service.listener(settings, args.port)
    line, code = office_service.health(office_service.private_root(args.root), args.port, settings)
    record = {'schema_version': 1, 'release': manifest['build_id'],
              'source_commit': manifest['source_commit'],
              'kit_version': provenance['version'],
              'python': platform.python_version(),
              'system': platform.system(), 'machine': platform.machine(),
              'libc': platform.libc_ver(), 'health': line, 'health_exit': code,
              'listener': {'shape': served['shape'], 'host': served['host'], 'port': served['port'],
                           'public_url': served['public_url']},
              'bd': bundled_bd(office_service.private_root(args.root), kit)}
    print(json.dumps(record, sort_keys=True))
    return code or (0 if record['bd']['starts'] else 1)


if __name__ == '__main__':
    sys.exit(main())
