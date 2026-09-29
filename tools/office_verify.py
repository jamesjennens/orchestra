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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args(argv)
    current = Path(args.install_root).expanduser().resolve()/'current'
    release = current.resolve(strict=True)
    manifest = json.loads((release/'manifest.json').read_text(encoding='utf-8'))
    kit = release/'kit'
    provenance = report(kit)
    if (provenance['source_commit'] != manifest.get('source_commit') or
            provenance['build_id'] != manifest.get('build_id')):
        raise SystemExit('Installed release provenance does not match the manifest')
    line, code = office_service.health(office_service.private_root(args.root), args.port)
    record = {'schema_version': 1, 'release': manifest['build_id'],
              'source_commit': manifest['source_commit'],
              'kit_version': provenance['version'],
              'python': platform.python_version(),
              'system': platform.system(), 'machine': platform.machine(),
              'libc': platform.libc_ver(), 'health': line, 'health_exit': code}
    print(json.dumps(record, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
