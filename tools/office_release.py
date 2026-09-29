#!/usr/bin/env python3
"""Build, install and roll back an offline, unprivileged Orchestra release.

The installer uses only Python 3.6 standard library on the target host. The
installed service uses the separately bundled Python 3.10+ interpreter.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def member(name, payload):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    return info


def _safe_path(raw):
    if not raw or raw.startswith('/') or '\\' in raw:
        raise ValueError('Unsafe archive path: ' + raw)
    parts = []
    for part in raw.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise ValueError('Archive path escapes extraction root: ' + raw)
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise ValueError('Empty archive path')
    return Path(*parts)


def safe_extract(data, destination):
    """Extract a digest-verified tar; refuse special files and escaping links."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    root = destination
    def inside(path):
        return os.path.commonpath((str(root), str(path.resolve()))) == str(root)
    def safe_parent(path):
        parent = path.parent
        if not inside(parent):
            raise ValueError('Archive parent escapes extraction root: ' + str(path))
        while parent != destination:
            if parent.is_symlink():
                raise ValueError('Archive member traverses a symlink: ' + str(path))
            parent = parent.parent
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        for item in archive:
            relative = _safe_path(item.name)
            target = destination/relative
            safe_parent(target)
            if target.is_symlink() or (target.exists() and not item.isdir()):
                raise ValueError('Duplicate or linked archive member: ' + item.name)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(item.mode & 0o755)
            elif item.issym():
                if item.linkname.startswith('/'):
                    raise ValueError('Absolute archive symlink: ' + item.name)
                link = target.parent/item.linkname
                if not inside(link):
                    raise ValueError('Archive symlink escapes extraction root: ' + item.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(item.linkname)
            elif item.islnk():
                source = destination/_safe_path(item.linkname)
                if not inside(source) or source.is_symlink() or not source.is_file():
                    raise ValueError('Archive hard link target missing: ' + item.linkname)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(str(source), str(target))
            else:
                raise ValueError('Unsupported archive member: ' + item.name)


def _git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def build(args):
    repo = Path(args.repo).resolve()
    commit = _git(repo, 'rev-parse', '--verify', args.commit+'^{commit}').decode().strip()
    if not re.match(r'^[0-9a-f]{40}$', commit):
        raise ValueError('Source commit must resolve to a full SHA-1 commit')
    if not ID.match(args.build_id):
        raise ValueError('Build ID must be a safe release directory name')
    python_archive = Path(args.python_archive).read_bytes()
    if digest(python_archive) != args.python_sha256:
        raise ValueError('Bundled Python archive digest mismatch')
    if _safe_path(args.python_executable) != Path(args.python_executable):
        raise ValueError('Python executable must be a normal relative archive path')
    archived = _git(repo, 'archive', '--format=tar', commit)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archived), mode='r:') as source:
        for item in source:
            if item.name in ('VERSION', 'client.py', 'version.py'):
                files[item.name] = source.extractfile(item).read()
            if item.name == 'versions.json':
                lock = json.load(source.extractfile(item))
    if set(files) != {'VERSION', 'client.py', 'version.py'}:
        raise ValueError('Source archive lacks version/provenance inputs')
    if 'lock' not in locals():
        raise ValueError('Source archive lacks versions.json')
    vendors = {}
    for name, path in (('bd', args.bd_archive), ('dolt', args.dolt_archive)):
        vendors[name] = Path(path).read_bytes()
        if digest(vendors[name]) != lock[name]['sha256']:
            raise ValueError(name + ' archive differs from versions.json pin')
    provenance = {'schema_version': 1, 'component': 'orchestra-kit',
                  'version': files['VERSION'].decode('utf-8').strip(),
                  'source_commit': commit, 'build_id': args.build_id,
                  'files': {name: digest(value.replace(b'\r\n', b'\n'))
                            for name, value in files.items()}}
    source_bytes = io.BytesIO()
    with tarfile.open(fileobj=source_bytes, mode='w') as target:
        with tarfile.open(fileobj=io.BytesIO(archived), mode='r:') as source:
            for item in source:
                if item.name == 'provenance.json':
                    continue
                target.addfile(item, source.extractfile(item) if item.isfile() else None)
        for name, data in (('provenance.json', canonical(provenance)+b'\n'),
                           ('vendor/bd.tar.gz', vendors['bd']),
                           ('vendor/dolt.tar.gz', vendors['dolt'])):
            target.addfile(member(name, data), io.BytesIO(data))
    manifest = {'schema_version': 1, 'build_id': args.build_id, 'source_commit': commit,
                'version': provenance['version'], 'source_sha256': digest(source_bytes.getvalue()),
                'python_sha256': digest(python_archive),
                'python_executable': args.python_executable}
    output = Path(args.output)
    if output.exists():
        raise ValueError('Refusing to replace existing artifact')
    with tarfile.open(str(output), mode='w:gz') as package:
        for name, data in (('manifest.json', canonical(manifest)+b'\n'),
                           ('source.tar', source_bytes.getvalue()),
                           ('python.tar.gz', python_archive)):
            package.addfile(member(name, data), io.BytesIO(data))
    print('%s  %s' % (digest(output.read_bytes()), output))


def _read_package(path):
    contents = {}
    with tarfile.open(str(path), mode='r:gz') as package:
        for item in package:
            if item.name not in ('manifest.json', 'source.tar', 'python.tar.gz') or not item.isfile():
                raise ValueError('Unexpected release package member: ' + item.name)
            if item.name in contents:
                raise ValueError('Duplicate release package member: ' + item.name)
            contents[item.name] = package.extractfile(item).read()
    if set(contents) != {'manifest.json', 'source.tar', 'python.tar.gz'}:
        raise ValueError('Incomplete release package')
    manifest = json.loads(contents['manifest.json'].decode('utf-8'))
    if (manifest.get('schema_version') != 1 or not ID.match(manifest.get('build_id', '')) or
            digest(contents['source.tar']) != manifest.get('source_sha256') or
            digest(contents['python.tar.gz']) != manifest.get('python_sha256')):
        raise ValueError('Release manifest or inner archive digest mismatch')
    return manifest, contents


def _link(root, name, target):
    temporary = root/('.'+name+'.new')
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(str(temporary), str(root/name))


def _current(root, name):
    link = root/name
    if not link.is_symlink():
        return None
    target = os.readlink(str(link))
    resolved = (root/target).resolve()
    if not str(resolved).startswith(str((root/'releases').resolve())+os.sep):
        raise ValueError(name + ' points outside releases')
    return target


def install(args):
    archive = Path(args.archive)
    if digest(archive.read_bytes()) != args.sha256:
        raise ValueError('Release artifact SHA-256 mismatch')
    manifest, contents = _read_package(archive)
    root = Path(args.install_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    releases = root/'releases'
    releases.mkdir(exist_ok=True)
    final = releases/manifest['build_id']
    if final.exists():
        raise ValueError('Release ID already installed')
    staged = Path(tempfile.mkdtemp(prefix='.office-release-', dir=str(releases)))
    try:
        safe_extract(contents['source.tar'], staged/'kit')
        safe_extract(contents['python.tar.gz'], staged/'python-runtime')
        executable = staged/'python-runtime'/_safe_path(manifest['python_executable'])
        if not executable.is_file():
            raise ValueError('Bundled Python executable missing')
        result = subprocess.check_output([str(executable), '--version'], stderr=subprocess.STDOUT)
        if not re.search(br'Python 3\.(1[0-9]|[2-9][0-9])\.', result):
            raise ValueError('Bundled interpreter must be Python 3.10 or newer')
        (staged/'manifest.json').write_bytes(contents['manifest.json'])
        os.rename(str(staged), str(final))
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    old = _current(root, 'current')
    if old:
        _link(root, 'previous', old)
    _link(root, 'current', 'releases/'+manifest['build_id'])
    print('Installed %s source=%s previous=%s' % (manifest['build_id'],
                                                  manifest['source_commit'], old or 'none'))
    print('Restart the supervised service after this release switch; running processes must not mix revisions.')


def rollback(args):
    root = Path(args.install_root).expanduser().resolve()
    prior = _current(root, 'previous')
    current = _current(root, 'current')
    if not prior or not (root/prior).is_dir():
        raise ValueError('No installed previous release')
    _link(root, 'current', prior)
    if current:
        _link(root, 'previous', current)
    print('Current release is now ' + prior)
    print('Restart the supervised service after rollback; running processes must not mix revisions.')


def verify(args):
    root = Path(args.install_root).expanduser().resolve()
    current = _current(root, 'current')
    if not current:
        raise ValueError('No current release')
    release = root/current
    manifest = json.loads((release/'manifest.json').read_text(encoding='utf-8'))
    python = release/'python-runtime'/_safe_path(manifest['python_executable'])
    subprocess.check_call([str(python), '-c', 'import admin, office_service, http_service'],
                          cwd=str(release/'kit'))
    print('release=%s source=%s python=%s' % (manifest['build_id'],
                                             manifest['source_commit'], python))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action')
    a = sub.add_parser('build')
    a.add_argument('--repo', required=True)
    a.add_argument('--commit', required=True)
    a.add_argument('--build-id', required=True)
    a.add_argument('--python-archive', required=True)
    a.add_argument('--python-sha256', required=True)
    a.add_argument('--python-executable', required=True)
    a.add_argument('--bd-archive', required=True)
    a.add_argument('--dolt-archive', required=True)
    a.add_argument('--output', required=True)
    a = sub.add_parser('install')
    a.add_argument('--archive', required=True)
    a.add_argument('--sha256', required=True)
    a.add_argument('--install-root', required=True)
    for action in ('rollback', 'verify'):
        sub.add_parser(action).add_argument('--install-root', required=True)
    args = parser.parse_args(argv)
    if not args.action:
        parser.error('Choose build, install, rollback or verify')
    try:
        globals()[args.action](args)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print('office-release: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
