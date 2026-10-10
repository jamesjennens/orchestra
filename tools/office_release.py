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
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
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
    # A link whose textual target did not exist yet was only judged lexically when it
    # was inserted; later members can complete that target and change where it points.
    # After the loop every member is materialized, so re-resolve each link and refuse
    # the release if any of them lands outside the extraction root.
    for parent, directories, files in os.walk(str(destination), followlinks=False):
        for name in list(directories) + list(files):
            entry = Path(parent)/name
            if not entry.is_symlink():
                continue
            try:
                contained = inside(entry)
            except (OSError, RuntimeError):
                contained = False
            if not contained:
                raise ValueError('Extracted symlink escapes extraction root: ' + str(entry))


# ---- what a bundled binary needs to start ------------------------------------------------
#
# The pinned bd 1.2.2 was built against glibc 2.34 and cannot start on RHEL 8 (glibc 2.28);
# nothing noticed, because every installation ran on a newer host (kittrial-5bb.161). So a
# release is checked twice: when it is built, against the glibc of the target it is built
# for, and when it is installed, by starting every bundled binary on the target itself.
# What a binary needs is read from the file, so the refusal can name it; no objdump or ldd
# is needed on the host. Like the rest of this file, Python 3.6 standard library only.

PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
DT_STRTAB, DT_VERNEED, DT_VERNEEDNUM = 5, 0x6ffffffe, 0x6fffffff
GLIBC = re.compile(r'^GLIBC_(\d+(?:\.\d+)+)$')
VERSION = re.compile(r'^\d+(?:\.\d+)+$')
#: The vendored binaries, with the arguments that make each say its version and stop.
BINARIES = (('bd', ['--version']), ('dolt', ['version']))
UNREADABLE = 'Not a readable ELF file'


def version_tuple(text):
    return tuple(int(part) for part in text.split('.'))


def elf_needs(data):
    """What an ELF executable needs in order to start, or None when ``data`` is not ELF.

    ``static``: no program interpreter is named, so no shared library is loaded.
    ``interpreter``: the loader a dynamic executable asks for. ``versions``: the symbol
    versions it requires of each shared library (``.gnu.version_r``, found through the
    dynamic segment, so a file without section headers is read too). ``glibc``: the
    highest ``GLIBC_x.y`` among them: the oldest glibc the file can start on.
    """
    if data[:4] != b'\x7fELF':
        return None
    try:
        if data[4] not in (1, 2) or data[5] not in (1, 2):
            raise ValueError(UNREADABLE)
        wide, order = data[4] == 2, '<' if data[5] == 1 else '>'
        if wide:
            phoff, = struct.unpack_from(order + 'Q', data, 32)
            phentsize, phnum = struct.unpack_from(order + 'HH', data, 54)
        else:
            phoff, = struct.unpack_from(order + 'I', data, 28)
            phentsize, phnum = struct.unpack_from(order + 'HH', data, 42)
        loads, interpreter, dynamic = [], None, None
        for index in range(phnum):
            at = phoff + index * phentsize
            if wide:
                kind, _, offset, address, _, size = struct.unpack_from(order + 'IIQQQQ', data, at)
            else:
                kind, offset, address, _, size = struct.unpack_from(order + 'IIIII', data, at)
            if kind == PT_LOAD:
                loads.append((address, offset, size))
            elif kind == PT_INTERP:
                interpreter = data[offset:offset + size].split(b'\0')[0].decode('ascii', 'replace')
            elif kind == PT_DYNAMIC:
                dynamic = (offset, size)

        def located(address):
            for start, offset, size in loads:
                if start <= address < start + size:
                    return offset + address - start
            raise ValueError(UNREADABLE)

        def text(at):
            end = data.index(b'\0', at)
            return data[at:end].decode('ascii', 'replace')
        tags = {}
        if dynamic is not None:
            entry, form = (16, 'qQ') if wide else (8, 'iI')
            for at in range(dynamic[0], dynamic[0] + dynamic[1] - entry + 1, entry):
                tag, value = struct.unpack_from(order + form, data, at)
                if tag == 0:
                    break
                tags.setdefault(tag, value)
        versions = {}
        if DT_VERNEED in tags and DT_STRTAB in tags:
            strings, at = located(tags[DT_STRTAB]), located(tags[DT_VERNEED])
            for _ in range(min(tags.get(DT_VERNEEDNUM, 0), 4096)):
                _, count, name, aux, following = struct.unpack_from(order + 'HHIII', data, at)
                wanted, entry = versions.setdefault(text(strings + name), []), at + aux
                for _ in range(min(count, 4096)):
                    _, _, _, symbol, onward = struct.unpack_from(order + 'IHHII', data, entry)
                    wanted.append(text(strings + symbol))
                    if not onward:
                        break
                    entry += onward
                if not following:
                    break
                at += following
    except (struct.error, IndexError, ValueError):
        raise ValueError(UNREADABLE)
    found = [GLIBC.match(symbol).group(1) for names in versions.values() for symbol in names if GLIBC.match(symbol)]
    return {'static': interpreter is None, 'interpreter': interpreter,
            'versions': {name: sorted(set(names)) for name, names in versions.items()},
            'glibc': max(found, key=version_tuple) if found else None}


def describe_needs(needs):
    """One clause for a refusal or a note: what the file itself says it needs."""
    if needs is None:
        return 'it is not an ELF executable'
    if needs['static']:
        return 'it is statically linked and needs no glibc'
    if needs['glibc']:
        return 'it needs glibc %s or later' % needs['glibc']
    return 'it is dynamically linked and names no glibc version'


def host_glibc():
    """This host's glibc version (``2.28``), or None where the C library does not say."""
    try:
        found = os.confstr('CS_GNU_LIBC_VERSION')
    except (AttributeError, ValueError, OSError):
        return None
    match = re.match(r'^glibc (\d+(?:\.\d+)+)$', found or '')
    return match.group(1) if match else None


def archive_member(data, name):
    """The bytes of the regular file ``name`` in a tar archive, following links inside the archive."""
    wanted = str(_safe_path(name)).replace(os.sep, '/')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        members = {}
        for item in archive:
            try:
                members[str(_safe_path(item.name)).replace(os.sep, '/')] = item
            except ValueError:
                continue
        for _ in range(8):
            item = members.get(wanted)
            if item is None:
                break
            if item.isfile():
                return archive.extractfile(item).read()
            if item.issym():
                wanted = str(_safe_path(os.path.dirname(wanted) + '/' + item.linkname
                                        if os.path.dirname(wanted) else item.linkname)).replace(os.sep, '/')
            elif item.islnk():
                wanted = str(_safe_path(item.linkname)).replace(os.sep, '/')
            else:
                break
    raise ValueError('Archive has no file ' + name)


def pin_of(lock, name, data):
    """Which pinned entry of versions.json the archive ``data`` is: ``name`` or ``name_static``.

    A binary may be pinned twice (kittrial-5bb.161): ``bd`` is upstream's release, which needs
    glibc 2.34; ``bd_static`` is the same version built without cgo, for hosts with an older
    glibc. A release bundles whichever archive it was given, and it must be one of the two.
    """
    for key in (name, name + '_static'):
        if isinstance(lock.get(key), dict) and lock[key].get('sha256') == digest(data):
            return key
    raise ValueError(name + ' archive differs from versions.json pin')


def vendored(lock, name, data):
    """The binary ``name`` out of its pinned archive (``member`` in versions.json, else the name itself)."""
    return archive_member(data, lock[pin_of(lock, name, data)].get('member') or name)


#: How a scratch directory is removed when a bundled binary leaves a detached child behind.
#: bd --version forks a child that writes the usage-metrics event kit just after bd exits, and
#: dolt version re-executes itself as dolt send-metrics (kittrial-5bb.166), so the directory can
#: fill again between the listing and the rmdir.
#: Removal is retried, and each check happens a whole pause after the removal it judges:
#: a writer that recreates the directory a moment later must not leave it in TMPDIR.
SCRATCH_TRIES, SCRATCH_PAUSE, SCRATCH_QUIET = 200, 0.1, 2


def remove_scratch(scratch):
    """Remove a scratch directory, tolerating a binary's detached child still writing into it.

    ``build``'s run check put HOME in a temporary directory and removed it at once, so about one
    build in three died with ``[Errno 39] Directory not empty`` and the folder stayed in TMPDIR.
    What refilled it outlives the command that was started: bd 1.2.2 writes
    ``HOME/.config/bd/config.yaml`` and queues its usage-metrics event kit in
    ``HOME/.beads/eventsData/`` from a detached child, ``dolt version`` re-executes itself as
    a detached ``dolt send-metrics`` and writes its global config, a version-check file and an
    event lock under ``HOME/.dolt/``. ``starts`` passes each program's own switch
    (``BD_DISABLE_METRICS=1``, the same guard the runtime sets in ``admin.environment``, and
    ``DOLT_DISABLE_EVENT_FLUSH=1``), which stops both children; removal is the guard that does
    not depend on either program behaving, so a late writer cannot fail a release check or
    leave its scratch in TMPDIR. Returns whether the directory is gone.
    """
    scratch = Path(scratch)
    quiet = 0
    for attempt in range(SCRATCH_TRIES):
        try:
            shutil.rmtree(str(scratch), ignore_errors=True)
        except OSError:
            pass
        time.sleep(SCRATCH_PAUSE)
        if scratch.exists():                      # a detached writer put it back
            quiet = 0
        else:
            quiet += 1
            if quiet >= SCRATCH_QUIET:            # gone for a whole quiet pause: it stays gone
                return True
    return not scratch.exists()


def starts(label, path, arguments, scratch):
    """Start ``path`` with ``arguments``; return what it said, or raise saying why it cannot start here.

    Both bundled binaries leave a detached child behind unless they are told not to: bd queues its
    usage-metrics event kit, and dolt re-executes itself as ``dolt send-metrics`` after the command
    returns. The guard each one documents is passed here (kittrial-5bb.166): ``BD_DISABLE_METRICS=1``
    for bd and ``DOLT_DISABLE_EVENT_FLUSH=1`` for dolt. Both were measured, not assumed; dolt's
    ``metrics.disabled`` global setting does NOT stop the child.
    """
    path = os.path.abspath(str(path))             # it is started from the scratch directory
    try:
        done = subprocess.run([path] + list(arguments), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=60, cwd=str(scratch),
                              env=dict(os.environ, HOME=str(scratch), BD_DISABLE_METRICS='1',
                                       DOLT_DISABLE_EVENT_FLUSH='1'))
        code, said = done.returncode, done.stdout.decode('utf-8', 'replace')
    except subprocess.TimeoutExpired:
        code, said = None, 'it did not answer within 60 seconds'
    except OSError as error:
        code, said = None, error.strerror or str(error)
    if code == 0:
        return said.strip()
    lines = [line.strip() for line in said.split('\n') if line.strip()]
    # The loader's own sentence, when there is one: "version `GLIBC_2.34' not found (required by ...)".
    reason = next((line for line in lines if 'not found' in line or 'No such file' in line), lines[-1] if lines else 'it said nothing')
    try:
        needs = describe_needs(elf_needs(Path(path).read_bytes()))
    except (OSError, ValueError):
        needs = 'it could not be read as an ELF file'
    here = host_glibc()
    raise ValueError('%s cannot start on this host%s: %s. The file says %s; this host has %s. '
                     'A release must carry a build of it for this host (docs/OFFICE_SERVICE.md)'
                     % (label, '' if code is None else ' (exit code %d)' % code, reason[:300], needs,
                        'glibc ' + here if here else 'a C library that does not state a glibc version'))


def start_vendored(kit, scratch):
    """Start bd and dolt out of an unpacked kit's vendored archives. Returns what each said."""
    kit, scratch = Path(kit), Path(scratch)
    try:
        lock = json.loads((kit/'versions.json').read_text(encoding='utf-8'))
        archives = {name: (kit/'vendor'/(name + '.tar.gz')).read_bytes() for name, _ in BINARIES}
    except (OSError, ValueError):
        raise ValueError('Release lacks versions.json or a vendored bd or dolt archive')
    scratch.mkdir(parents=True, exist_ok=True)
    said = {}
    try:
        for name, arguments in BINARIES:
            target = scratch/name
            target.write_bytes(vendored(lock, name, archives[name]))
            target.chmod(0o755)
            pin = pin_of(lock, name, archives[name])
            said[name] = '%s%s' % ('' if pin == name else '(%s) ' % pin,
                                   starts(name, target, arguments, scratch))
    finally:
        remove_scratch(scratch)
    return said


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
    pins = {}
    for name, path in (('bd', args.bd_archive), ('dolt', args.dolt_archive)):
        if not Path(path).is_file():
            raise ValueError('The --%s-archive file does not exist: %s' % (name, path))
        vendors[name] = Path(path).read_bytes()
        pins[name] = pin_of(lock, name, vendors[name])
        print('office-release: %s archive is the pinned entry %s' % (name, pins[name]), file=sys.stderr)
    # Every bundled binary: what it needs (recorded in the manifest), whether that fits the
    # target's glibc when one is named, and whether bd and dolt start on this build host.
    glibc = args.target_glibc
    if glibc is not None and not VERSION.match(glibc):
        raise ValueError('--target-glibc must be a glibc version such as 2.28')
    payloads = [('python', archive_member(python_archive, args.python_executable))] + \
        [(name, vendored(lock, name, vendors[name])) for name, _ in BINARIES]
    binaries, too_new = {}, []
    for name, payload in payloads:
        needs = elf_needs(payload)
        binaries[name] = None if needs is None else {'static': needs['static'], 'glibc': needs['glibc']}
        print('office-release: %s: %s' % (name, describe_needs(needs)), file=sys.stderr)
        if glibc is not None and needs is not None and needs['glibc'] \
                and version_tuple(needs['glibc']) > version_tuple(glibc):
            too_new.append('%s needs glibc %s' % (name, needs['glibc']))
    if too_new:
        raise ValueError('%s; the target has glibc %s. It could not start there. Bundle a build of it for the '
                         'target: for bd, the archive versions.json pins as bd_static (docs/OFFICE_SERVICE.md)'
                         % ('; '.join(too_new), glibc))
    if not args.no_run_check:
        scratch = Path(tempfile.mkdtemp(prefix='office-release-check-'))
        try:
            for (name, arguments), (_, payload) in zip(BINARIES, payloads[1:]):
                candidate = scratch/name
                candidate.write_bytes(payload)
                candidate.chmod(0o755)
                try:
                    starts(name, candidate, arguments, scratch)
                except ValueError as error:
                    raise ValueError('%s. This was the build host; if it is older than the target and cannot run '
                                     'the target\'s binaries, build with --no-run-check' % error)
        finally:
            if not remove_scratch(scratch):
                print('office-release: could not remove the run-check scratch directory %s' % scratch,
                      file=sys.stderr)
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
                'python_executable': args.python_executable,
                # Additive: what each bundled binary needs, and the glibc the build was checked against.
                'binaries': binaries, 'target_glibc': glibc, 'pins': pins}
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
    try:
        manifest = json.loads(contents['manifest.json'].decode('utf-8'))
    except ValueError:
        raise ValueError('Release manifest is not JSON') from None
    # What the rest reads from it is asked for here: a manifest that is not an object, or that
    # lacks one of these, was a traceback further on (kittrial-5bb.190).
    if not isinstance(manifest, dict) or not all(isinstance(manifest.get(field), str) and manifest[field]
                                                 for field in ('build_id', 'python_executable')):
        raise ValueError('Release manifest is not an object that names its build_id and python_executable')
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


RESTART = 'Restart the supervised service after this release switch; running processes must not mix revisions.'


def _installed_manifest(release):
    """The manifest stored in an installed release, as far as this tool reads it.

    A release folder is the operator's to look into, and its manifest was read as whatever the
    JSON happened to be: `[]` in it, or an object without its interpreter, answered a Python
    traceback from `activate` and `verify` (kittrial-5bb.190). Raises ValueError with a sentence.
    """
    path = release/'manifest.json'
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        raise ValueError('%s is not JSON: the release cannot be read' % path) from None
    if not isinstance(manifest, dict):
        raise ValueError('%s is not a JSON object: the release cannot be read' % path)
    for field in ('build_id', 'python_executable'):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ValueError('%s does not name its %s: the release cannot be read' % (path, field))
    return manifest


def _switch(root, old, target):
    """The two links of a switch: `previous` first, then `current`.

    They are two renames and a run can be killed between them. In this order nothing that is
    served has changed by then (`current` still names the old release, which is whole) and the
    same command again completes the switch; `previous` names the old release too, which
    `rollback` refuses with a sentence. The other order would leave `current` on the new release
    and `previous` on whatever it named before: `rollback` would then go, without a word, to a
    release that was never the one before this (kittrial-5bb.190).
    """
    if old:
        _link(root, 'previous', old)
    _link(root, 'current', target)


def _starts_as_installed(release, manifest):
    """Start an INSTALLED release's interpreter and its bundled bd and dolt, as `install` starts a new
    one before it switches to it. Raises ValueError with the reason when one does not start."""
    executable = release/'python-runtime'/_safe_path(manifest['python_executable'])
    if not executable.is_file():
        raise ValueError('Bundled Python executable missing')
    # Everything is started from a scratch directory of its own (it is HOME and the working
    # directory of what is started): nothing is written into the installed release.
    scratch = Path(tempfile.mkdtemp(prefix='office-release-verify-'))
    try:
        result = starts('The bundled Python', executable, ['--version'], scratch)
        if not re.search(r'Python 3\.(1[0-9]|[2-9][0-9])\.', result):
            raise ValueError('Bundled interpreter must be Python 3.10 or newer')
        start_vendored(release/'kit', scratch/'start-check')
    finally:
        remove_scratch(scratch)


def _switch_to_installed(root, build_id, manifest, compared):
    """Make an already installed release the current one (kittrial-5bb.189).

    After `rollback` the release that was rolled back FROM is still installed, and `install` of
    its archive answered "Release ID already installed": an operator who had gone back could not
    go forward again without building the same thing under a new id. ``compared`` says what the
    release was compared with, for the line that is printed.
    """
    target = 'releases/'+build_id
    old = _current(root, 'current')
    if old == target:
        print('Release %s is already installed and is the current one; nothing was changed.' % build_id)
        return
    _starts_as_installed(root/target, manifest)
    _switch(root, old, target)
    print('Current release is now %s (it was installed already; %s) source=%s previous=%s'
          % (build_id, compared, manifest.get('source_commit'), old or 'none'))
    print(RESTART)


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
        # The same release again (the way forward after a rollback): switched to, not unpacked a
        # second time, and only when it IS the same: the installed manifest is this archive's,
        # byte for byte, so the build id, the source and the interpreter digests are these.
        installed = final/'manifest.json'
        if final.is_symlink() or not final.is_dir() or not installed.is_file() \
                or installed.read_bytes() != contents['manifest.json']:
            raise ValueError('Release ID already installed, and what is installed under it is not this archive '
                             '(the manifests differ, or the installed release is incomplete). A different build '
                             'needs a build id of its own')
        _switch_to_installed(root, manifest['build_id'], manifest, 'its manifest is this archive\'s')
        return
    staged = Path(tempfile.mkdtemp(prefix='.office-release-', dir=str(releases)))
    try:
        safe_extract(contents['source.tar'], staged/'kit')
        safe_extract(contents['python.tar.gz'], staged/'python-runtime')
        executable = staged/'python-runtime'/_safe_path(manifest['python_executable'])
        if not executable.is_file():
            raise ValueError('Bundled Python executable missing')
        result = starts('The bundled Python', executable, ['--version'], staged)
        if not re.search(r'Python 3\.(1[0-9]|[2-9][0-9])\.', result):
            raise ValueError('Bundled interpreter must be Python 3.10 or newer')
        # bd and dolt must start on THIS host before anything is switched (kittrial-5bb.161).
        start_vendored(staged/'kit', staged/'.start-check')
        (staged/'manifest.json').write_bytes(contents['manifest.json'])
        os.rename(str(staged), str(final))
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    old = _current(root, 'current')
    _switch(root, old, 'releases/'+manifest['build_id'])
    print('Installed %s source=%s previous=%s' % (manifest['build_id'],
                                                  manifest.get('source_commit'), old or 'none'))
    print(RESTART)


def activate(args):
    """Switch to a release that is installed, by its build id, when there is no archive at hand."""
    root = Path(args.install_root).expanduser().resolve()
    if not ID.match(args.release or ''):
        raise ValueError('Not a release id: name the folder under releases/')
    final = root/'releases'/args.release
    if final.is_symlink() or not final.is_dir() or not (final/'manifest.json').is_file():
        raise ValueError('No installed release %s under %s' % (args.release, root/'releases'))
    manifest = _installed_manifest(final)
    if manifest.get('build_id') != args.release:
        raise ValueError('The release installed under %s says it is %s' % (args.release, manifest.get('build_id')))
    _switch_to_installed(root, args.release, manifest,
                         'not compared with an archive: `install` of its archive does that')


def rollback(args):
    root = Path(args.install_root).expanduser().resolve()
    prior = _current(root, 'previous')
    current = _current(root, 'current')
    if not prior or not (root/prior).is_dir():
        raise ValueError('No installed previous release')
    if current and (root/prior).resolve() == (root/current).resolve():
        # What a switch (or a rollback) leaves when it is killed between its two links. Going
        # "back" to the current release printed "Current release is now ..." and changed nothing.
        installed = sorted(entry.name for entry in (root/'releases').iterdir()
                           if entry.is_dir() and not entry.is_symlink() and ID.match(entry.name))
        raise ValueError('previous and current both name %s: a switch was interrupted between its two links, so '
                         'the release to go back to is not recorded. Nothing was changed. Installed: %s. Make the '
                         'one you want the current release with `activate --release ID`, or install its archive '
                         'again' % (current, ', '.join(installed)))
    _link(root, 'current', prior)
    if current:
        _link(root, 'previous', current)
    print('Current release is now ' + prior)
    print('Restart the supervised service after rollback; running processes must not mix revisions.')
    if current:
        # The release that was left is still installed (kittrial-5bb.189).
        print('To go forward again to %s: install its archive again, or `activate --release %s`.'
              % (current, current.rpartition('/')[2]))


def verify(args):
    root = Path(args.install_root).expanduser().resolve()
    current = _current(root, 'current')
    if not current:
        raise ValueError('No current release')
    release = root/current
    manifest = _installed_manifest(release)
    python = release/'python-runtime'/_safe_path(manifest['python_executable'])
    subprocess.check_call([str(python), '-c', 'import admin, office_service, http_service'],
                          cwd=str(release/'kit'))
    scratch = Path(tempfile.mkdtemp(prefix='office-release-verify-'))
    try:
        said = start_vendored(release/'kit', scratch/'start-check')
    finally:
        remove_scratch(scratch)
    print('release=%s source=%s python=%s' % (manifest['build_id'],
                                             manifest.get('source_commit'), python))
    for name, _ in BINARIES:
        print('%s starts on this host: %s' % (name, (said[name].split('\n') or [''])[0][:120]))


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
    a.add_argument('--target-glibc', help='the glibc version of the host the release is for (for example 2.28 '
                                          'for RHEL 8); the build refuses a bundled binary that needs a newer one')
    a.add_argument('--no-run-check', action='store_true',
                   help='do not start bd and dolt on the build host (only for a build host that cannot run them)')
    a = sub.add_parser('install')
    a.add_argument('--archive', required=True)
    a.add_argument('--sha256', required=True)
    a.add_argument('--install-root', required=True)
    for action in ('rollback', 'verify'):
        sub.add_parser(action).add_argument('--install-root', required=True)
    a = sub.add_parser('activate', help='switch to a release that is already installed (after a rollback, forward again)')
    a.add_argument('--install-root', required=True)
    a.add_argument('--release', required=True, help='its build id: the folder name under releases/')
    args = parser.parse_args(argv)
    if not args.action:
        parser.error('Choose build, install, activate, rollback or verify')
    try:
        globals()[args.action](args)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print('office-release: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
