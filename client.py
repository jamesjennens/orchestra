#!/usr/bin/env python3
"""Portable client: SSH by default, explicit local transport, never a silent fallback.

SSH remains the default and unchanged: the endpoint path is the only part of the
remote command line and only trusted config values enter it. Local transport is an
explicit opt-in (config "transport": "local") for a Linux endpoint on this machine;
it runs the same endpoint as an argv list with shell=False. Both transports send the
byte-identical JSON envelope, so attachments and actor semantics do not vary.
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('client.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run the client with Python 3.10 or newer; the beads.cmd and beads.sh wrappers take the interpreter from BEADS_PYTHON.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import re
from pathlib import Path

CLIENT_VERSION = "0.1.0"
SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

def report(root=None, component="client"):
    root = Path(root or Path(__file__).resolve().parent)
    metadata = {"component": component, "version": CLIENT_VERSION,
                "source_commit": "unknown", "build_id": "unknown", "path": str(root)}
    try:
        manifest = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
        if (isinstance(manifest, dict) and manifest.get("schema_version") == 1 and
                manifest.get("component") == "orchestra-kit" and
                manifest.get("version") == CLIENT_VERSION and
                isinstance(manifest.get("source_commit"), str) and
                (manifest["source_commit"] == "unknown" or
                 SOURCE_COMMIT.fullmatch(manifest["source_commit"])) and
                isinstance(manifest.get("build_id"), str) and manifest["build_id"] and
                isinstance(manifest.get("files"), dict) and manifest["files"] and
                all(isinstance(name, str) and Path(name).name == name and
                    name != "provenance.json" and isinstance(digest, str) and
                    re.fullmatch(r"[0-9a-f]{64}", digest) and
                    (root / name).is_file() and
                    _digest(root / name) == digest
                    for name, digest in manifest["files"].items())):
            metadata.update(source_commit=manifest["source_commit"],
                            build_id=manifest["build_id"])
    except (OSError, UnicodeError, ValueError, TypeError):
        pass
    return metadata

def line(metadata):
    return "Orchestra %s: version %s, source %s" % (
        metadata["component"], metadata["version"], metadata["source_commit"],
    )

ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]*')
SERVER_PATH = re.compile(r'/[A-Za-z0-9_./-]+')
FILE_FLAGS = {'--body-file', '--design-file', '--file', '-f'}
TRANSPORTS = {'ssh', 'local'}   # anything else is refused: no fallback to a default
MAX_WIRE = 2_000_000
TIMEOUT = 150

def _endpoint_paths(config):
    endpoint,root = config.get('endpoint'),config.get('root')
    if any(not isinstance(x,str) or not SERVER_PATH.fullmatch(x) for x in (endpoint,root)):
        raise ValueError('Use absolute server paths without spaces')
    return endpoint,root

def _python(config):
    # One executable path only: flags or arguments here would become an unquoted command.
    value = config.get('python', 'python3')
    if not isinstance(value,str) or not value or value.startswith('-') or re.search(r'\s',value):
        raise ValueError('Configure "python" as one Python executable path without flags')
    return value

def _forced_command(config):
    """The optional ``"forced_command": true`` flag: strictly a JSON boolean.

    With it, the server's authorized_keys ``command=`` wrapper (ssh_forced_command.py)
    supplies ``--root`` itself and refuses every flag an ordinary remote command carries,
    so the client sends the endpoint path alone, to select the endpoint. A truthy string
    is refused rather than guessed, so a deployment never gets a command line it did not
    choose.
    """
    value = config.get('forced_command',False)
    if not isinstance(value,bool):
        raise ValueError('Configure "forced_command" as true or false')
    return value

def _ssh_argv(config):
    host = config.get('host')
    if not isinstance(host,str) or not HOST.fullmatch(host):
        raise ValueError('Host must be an SSH alias or user@host')
    # Both paths stay required in forced-command mode too: the wrapper fixes the root, but
    # the client config must still name the deployment it is talking to.
    endpoint,root = _endpoint_paths(config)
    if _forced_command(config):
        command = shlex.quote(endpoint)
    else:
        # The interpreter runs on the SERVER. A bare python3 is platform-python 3.6 on
        # RHEL 8, which cannot run the endpoint, and a host with no python3 on PATH fails
        # outright, so the config may name the deployment's own interpreter (an office
        # install prints its bundled one). No "python" key keeps python3 exactly
        # (kittrial-5bb.182).
        command = ' '.join(shlex.quote(x) for x in [_python(config),endpoint,'--root',root])
    return ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,command],'SSH'

def _local_argv(config):
    # A local endpoint is a Linux Python script, so its paths stay absolute POSIX paths.
    endpoint,root = _endpoint_paths(config)
    return [_python(config),endpoint,'--root',root],'Local endpoint'

def _argv(config):
    if not isinstance(config,dict):raise ValueError('Client config must be a JSON object')
    transport = config.get('transport','ssh')
    if transport == 'ssh':return _ssh_argv(config)
    if transport == 'local':return _local_argv(config)
    raise ValueError(f'Unknown transport {transport!r}; use one of {sorted(TRANSPORTS)}')

def _attachments(args):
    attachments = {};converted = [];i = 0
    while i < len(args):
        token = args[i];flag = token.split('=',1)[0]
        if flag in FILE_FLAGS:
            if '=' in token: value = token.split('=',1)[1]
            else:
                i += 1
                if i >= len(args):raise ValueError('File flag needs a local path')
                value = args[i]
            if value == '-':raise ValueError('Use a local UTF-8 file for attachment input')
            key = str(len(attachments));attachments[key] = {'flag':flag,'text':Path(value).read_text(encoding='utf-8-sig')}
            converted.append('@attachment:'+key)
        else:converted.append(token)
        i += 1
    return converted,attachments

def _wire(project,actor,args,action,path):
    # Same actor rule as the endpoint, enforced here too: a rejected actor must not
    # reach the transport layer, and never reaches the server as a shell word.
    registering=action=='session' and args[:1]==['register']
    resuming=action=='session' and args[:1]==['resume']
    if registering or resuming:
        if not any(a=='--request-id' or a.startswith('--request-id=') for a in args):
            import uuid
            request_id=str(uuid.uuid4())
            print('Session request-id (reuse after an uncertain response): '+request_id,file=sys.stderr,flush=True)
            args=[*args,'--request-id',request_id]
        if registering:actor=actor or ''
    if not registering and (not isinstance(actor,str) or not ACTOR.fullmatch(actor)):
        raise ValueError('Supply a short contributor/session actor')
    converted,attachments = _attachments(args)
    payload = {'project':project,'actor':actor,'action':action,'args':converted,'attachments':attachments}
    if path is not None:payload['path'] = path
    wire = json.dumps(payload,ensure_ascii=False)
    if len(wire) > MAX_WIRE:raise ValueError('Request exceeds 2 MB')
    return wire

#: What endpoint.py answers, and only that, when it was started by an interpreter too old for
#: it: exit 2, nothing on stdout, this one line (kittrial-5bb.191).
TOO_OLD=re.compile(r'(endpoint\.py needs Python 3\.10 or newer and was started with Python \d+\.\d+\.\d+ \([^\n]{0,400}\)\. '
                   r'Nothing was carried out\.)(?: ([^\n]{0,600}))?\n?')

#: The endpoint's own advice tail; under a forced command it contradicts the note below and is left out.
SET_PYTHON = 'Set "python"'

def _interpreter_refusal(returncode,stdout,stderr,config):
    """The endpoint's own sentence when it refused to start under an old interpreter, else None.

    That answer says nothing was carried out, so the client does not put "outcome may be
    uncertain" in front of it. With a confined key the interpreter is the one in the server's
    authorized_keys line, not this configuration's ``python``, and the advice says so.
    """
    found=TOO_OLD.fullmatch(stderr or '') if returncode==2 and not stdout else None
    if not found:return None
    if config.get('transport','ssh')=='ssh' and config.get('forced_command') is True:
        tail=(found.group(2) or '').strip()
        note=(' This key runs a forced command, so the interpreter is the one in its '
              'authorized_keys line on the server, not "python" in this configuration: ask the operator to '
              'print that line again (admin.py authorized-keys) with an interpreter of 3.10 or newer.')
        # The endpoint's own advice says to set "python" here; under a forced command that is
        # wrong (the key's authorized_keys line decides), so the note replaces it (kittrial-5bb.222).
        if tail.startswith(SET_PYTHON):
            return found.group(1)+note
        return found.group(1)+(' '+tail if tail else '')+note
    return stderr.strip()

def request(config,project,actor,args,action='bd',path=None):
    argv,label = _argv(config)
    wire = _wire(project,actor,args,action,path)
    try:
        # A captured console child can still flash a window when the client is
        # launched by a GUI/agent on Windows. Preserve pipes and exit status.
        p = subprocess.run(argv,shell=False,input=wire,text=True,encoding='utf-8',capture_output=True,timeout=TIMEOUT,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{label} timed out; outcome may be uncertain. Inspect state; do not blindly retry mutations.') from None
    if p.returncode:
        too_old=_interpreter_refusal(p.returncode,p.stdout,p.stderr,config)
        if too_old:raise RuntimeError(f'{label}: {too_old}')
        raise RuntimeError(f'{label} failed ({p.returncode}); outcome may be uncertain. {p.stderr[:1000]}')
    try:return json.loads(p.stdout)
    except json.JSONDecodeError:raise RuntimeError('Invalid endpoint response; inspect state before retrying.') from None

# Capability subcommands answered by the coordination endpoint (.60 slices 1a/1b, and the
# lookup-miss log `misses`, kittrial-5bb.77); lookup, resolve and index stay in the client
# and never need a config. `check` runs in the client too, but reads the records (and
# with --record posts `verify`) through the endpoint.
CAPABILITY_ENDPOINT = ('find', 'get', 'list', 'propose', 'revise', 'propose-alias', 'verify', 'misses')

def _capabilities_module():
    """capabilities.py from this client's own directory, loaded by path, or None.

    An unrelated `capabilities` module elsewhere on sys.path is never used, and loading
    leaves no __pycache__ beside the client: the lookup writes nothing.
    """
    import importlib.util
    module_path = Path(__file__).resolve().parent/'capabilities.py'
    if not module_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location('orchestra_capabilities', module_path)
    module = importlib.util.module_from_spec(spec)
    write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = write_bytecode
    return module

def _emit(code,stdout,stderr,out):
    """Same client-owned capture as the endpoint actions: UTF-8, LF, no BOM, and no
    file at all on a nonzero exit."""
    if out:
        if code == 0:
            with open(out,'w',encoding='utf-8',newline='') as capture:
                capture.write(stdout)
        sys.stderr.write(stderr);return code
    sys.stdout.write(stdout);sys.stderr.write(stderr);return code

def _capability(args,out):
    """Client-side, read-only capability lookup over the caller's own checkout.

    It never contacts the endpoint and needs no config, project or actor. The
    module is loaded from this client's own directory by path, so an unrelated
    `capabilities` module elsewhere on sys.path is never used; a standalone client
    copied without it gets a clear refusal instead.
    """
    module = _capabilities_module()
    if module is None:
        sys.stderr.write('ValueError: capability commands need capabilities.py from the same Orchestra kit '
                         'next to this client\n')
        return 2
    code,stdout,stderr = module.run(args)
    if out:
        # Same client-owned capture as the endpoint actions: UTF-8, LF, no BOM,
        # and no file at all on a nonzero exit.
        if code == 0:
            with open(out,'w',encoding='utf-8',newline='') as capture:
                capture.write(stdout)
        sys.stderr.write(stderr);return code
    sys.stdout.write(stdout);sys.stderr.write(stderr);return code

def _capability_lookup(args,config,project,actor,out):
    """`capability lookup` with --config/--project: the local .61 lookup plus the
    endpoint's records (.60 section 7).

    One endpoint `find`; every returned record's pointers are resolved live against the
    caller's own checkout (`live: resolved | missing | unknown`). The local result is
    never lost: if the endpoint cannot answer, the lookup still returns it, with
    `records_warning`. The output stays `capability-lookup-v1` with additive fields.
    """
    module = _capabilities_module()
    if module is None:
        return _capability(['lookup',*args],out)
    code,stdout,stderr = module.run(['lookup',*args])
    if code:
        return _emit(code,stdout,stderr,out)
    payload = json.loads(stdout)
    options = module._parser().parse_args(['lookup',*args])
    try:
        result = request(config,project,actor,['find',options.phrase,'--limit',str(options.limit)],'capability')
        if result['returncode']:
            raise RuntimeError((result.get('stderr') or '').strip().splitlines()[-1:] or ['endpoint refused'])
        found = json.loads(result['stdout'])
    except (RuntimeError,ValueError,OSError) as error:
        payload['records'] = []
        payload['records_warning'] = 'capability records unavailable: %s' % str(error)[:300]
        return _emit(0,json.dumps(payload,ensure_ascii=True,indent=2)+'\n',stderr,out)
    repo = module.open_repo(options.repo)

    def live(pointers):
        rows = []
        for pointer in pointers:
            resolved = module.resolve_pointer(repo,pointer,None).get('resolved')
            rows.append({'pointer':pointer,'live':{True:'resolved',False:'missing'}.get(resolved,'unknown')})
        return rows

    def annotate():
        records = []
        for match,items in (('exact',found.get('records') or []),('candidate',found.get('candidates') or [])):
            for item in items:
                records.append(dict(item,match=match,code=live(item.get('code') or []),
                                    tests=live(item.get('tests') or []),anchors=live(item.get('anchors') or [])))
        return records
    module._reset_parse_state()
    try:
        payload['records'] = module.with_parse_stack(annotate)
    finally:
        module._reset_parse_state()
    payload['records_found'] = bool(found.get('found'))
    payload['records_hint'] = found.get('hint')
    payload['records_coverage'] = found.get('coverage')
    return _emit(0,json.dumps(payload,ensure_ascii=True,indent=2)+'\n',stderr,out)

def _capability_check(args,config,project,actor,out):
    """`capability check --repo PATH [--key KEY]... [--record | --payloads FILE]` (.60 section 5.1).

    Runs where the code is. The recorded capabilities are paged from the endpoint
    (`capability list --pointers`), and every `code`, `tests` and `anchors` pointer of
    each one's current revision is resolved against the caller's checkout with the
    local resolver. Output is `capability-check-v1`; a missing pointer is a result, not
    an error. Nothing is written without `--record`.

    `--record` posts one `capability verify` per capability. It first refuses a checkout
    that is not at a full commit or has uncommitted or untracked changes. That refusal
    is a convenience only: what a reader concludes rests on who wrote the record, and a
    record posted here is always an unverified report. `--payloads FILE` writes the
    same payloads to a file and posts nothing, for `admin.py capability-verify`.
    """
    module = _capabilities_module()
    if module is None:
        return _capability(['lookup'],out)
    if any(token in ('--help','-h') for token in args):
        return _emit(0,json.dumps({'schema_version':1,'contract':module.CONTRACT_VERSION,'command':'capability check',
            'usage':'capability check [--repo PATH] [--key KEY]... [--source auto|ast|graphify] [--graph FILE] '
                    '[--max-graph-mb N] [--record | --payloads FILE]',
            'notes':['Needs --config, --project and --actor: the records are read from the endpoint.',
                     'Nothing is written without --record. A check recorded here is an unverified report; '
                     'an operator or listed verifier records a verified one with admin.py capability-verify '
                     'from a --payloads file.'],
            'output':{'schema':'capability-check-v1'},
            'exit_codes':{'0':'result or help JSON on stdout (a missing pointer is a result)',
                          '2':'validation/transport error on stderr; stdout is not written'}},indent=2)+'\n','',out)
    try:
        parser = module._Parser(prog='capability check',add_help=False)
        parser.add_argument('--repo',default='.');parser.add_argument('--key',action='append',default=[])
        parser.add_argument('--source',default='auto');parser.add_argument('--graph')
        parser.add_argument('--max-graph-mb',type=int,default=module.GRAPH_MB_DEFAULT)
        parser.add_argument('--record',action='store_true');parser.add_argument('--payloads')
        parser.add_argument('--json',action='store_true')
        options = parser.parse_args(args)
        if options.record and options.payloads:
            raise ValueError('use --record or --payloads FILE, not both')
        if options.payloads and (Path(options.payloads).is_symlink() or Path(options.payloads).is_dir()):
            raise ValueError('--payloads: refusing to write through a symbolic link or onto a directory')
        repo = module.open_repo(options.repo)
        writing = options.record or options.payloads
        if writing:
            status = module._git(repo['root'],['status','--porcelain']) if repo['commit'] else None
            if not repo['git'] or not repo['commit'] or status is None:
                raise ValueError('a check is recorded only from a git checkout at a full commit')
            if status.strip():
                raise ValueError('a check is recorded only from a clean checkout: commit or remove the '
                                 'uncommitted and untracked changes first')
        items = [];offset = 0
        while offset is not None:
            result = request(config,project,actor,['list','--state','all','--limit','100','--offset',str(offset),
                                                    '--pointers'],'capability')
            if result['returncode']:
                raise ValueError('capability records unavailable: %s'
                                 % ((result.get('stderr') or '').strip().splitlines() or ['endpoint refused'])[-1][:300])
            page = json.loads(result['stdout'])
            items.extend(page.get('items') or []);offset = page.get('next_offset')
        if any('record_sha256' not in item for item in items):
            raise ValueError('the endpoint kit does not support capability check (it predates capability '
                             'list --pointers)')
        unknown = sorted(set(options.key)-{item['key'] for item in items})
        if unknown:
            raise ValueError('unknown capability key(s): %s' % ', '.join(unknown[:5]))
        chosen = [item for item in items if item['state'] in ('accepted','draft-only')
                  and (not options.key or item['key'] in options.key)]
        source = 'graphify' if options.graph is not None and options.source == 'auto' else options.source

        def resolve_all():
            pointers = [pointer for item in chosen for name in ('code','tests','anchors') for pointer in item[name]]
            needs_graph = any(module.split_pointer(pointer)[1] == '::'
                              and not module.split_pointer(pointer)[0].endswith('.py') for pointer in pointers)
            available = source == 'graphify' or module.contained_file(repo['root'],module.DEFAULT_GRAPH) is not None
            index = module.build_index(repo,source,options.graph,options.max_graph_mb) \
                if needs_graph and source != 'ast' and available else None
            checked = []
            for item in chosen:
                results = []
                for pointer in dict.fromkeys(item['code']+item['tests']+item['anchors']):
                    row = module.resolve_pointer(repo,pointer,index)
                    results.append({'pointer':pointer,'resolved':row['resolved'],'basis':row['basis'],
                                    'reason':row['reason']})
                values = [row['resolved'] for row in results]
                passed = False if False in values else (True if values and all(v is True for v in values) else None)
                checked.append({'key':item['key'],'state':item['state'],'revision':item['revision'],
                                'record_sha256':item['record_sha256'],'passed':passed,'results':results})
            return checked,index
        module._reset_parse_state()
        try:
            checked,index = module.with_parse_stack(resolve_all)
        finally:
            module._reset_parse_state()
    except ValueError as error:
        sys.stderr.write('ValueError: %s\n' % error);return 2
    graphed = index is not None and index.code_source == 'graphify'
    stamp = __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ',__import__('time').gmtime())
    payloads = {}
    if writing:
        for row in checked:
            if row['passed'] is None:
                row['recorded'] = 'not-recordable'
                continue
            built = (index.graph or {}).get('built_at_commit') if graphed else None
            payloads[row['key']] = {
                'schema_version':1,'key':row['key'],'revision':row['revision'],'record_sha256':row['record_sha256'],
                'commit':repo['commit'],'checked_at':stamp,'source':'graphify' if graphed else 'ast',
                'graph_built_at_commit':built if isinstance(built,str) and re.fullmatch(r'[0-9a-f]{7,64}',built) else None,
                'tool':{'name':'orchestra-capability-check','version':str(report(Path(__file__).resolve().parent,'client')['version'])[:40]},
                'results':[{'pointer':r['pointer'],'resolved':r['resolved'],'reason':r['reason']} for r in row['results']],
                'passed':row['passed']}
    warnings = list(index.warnings) if index is not None else []
    if options.payloads:
        # Written to a new private (0600) file beside the target and moved into place, so
        # the write never follows a link and never leaves a half-written or shared file.
        import os,tempfile
        target = Path(options.payloads)
        try:
            if target.is_symlink():
                raise OSError('refusing to write through a symbolic link')
            handle,name = tempfile.mkstemp(prefix='.'+target.name+'.',suffix='.tmp',dir=str(target.parent or '.'))
            try:
                with os.fdopen(handle,'w',encoding='utf-8',newline='') as stream:
                    stream.write(json.dumps({'schema_version':1,'items':list(payloads.values())},ensure_ascii=True,indent=2)+'\n')
                os.replace(name,str(target))
            except BaseException:
                try:os.unlink(name)
                except OSError:pass
                raise
        except OSError as error:
            sys.stderr.write('ValueError: --payloads: %s\n' % error);return 2
        for row in checked:
            row.setdefault('recorded','payload-written')
    elif options.record:
        import os,tempfile
        stopped = None
        for row in checked:
            if row['key'] not in payloads:continue
            if stopped:
                row['recorded'] = 'not-run';continue
            handle,name = tempfile.mkstemp(suffix='.json')
            try:
                with os.fdopen(handle,'w',encoding='utf-8',newline='') as target:
                    target.write(json.dumps(payloads[row['key']]))
                result = request(config,project,actor,['verify','--file',name],'capability')
                if result['returncode']:
                    row['recorded'] = 'refused'
                    row['refusal'] = ((result.get('stderr') or '').strip().splitlines() or ['endpoint refused'])[-1][:300]
                else:
                    answer = json.loads(result['stdout'])
                    row['recorded'] = 'already-recorded' if answer.get('reconciled') else 'recorded'
                    row['identity'] = answer.get('identity')
            except (RuntimeError,ValueError,OSError) as error:
                row['recorded'] = 'uncertain';stopped = str(error)[:300]
            finally:
                try:os.unlink(name)
                except OSError:pass
        if stopped:
            warnings.append('recording stopped: %s; re-run the same command to resume' % stopped)
    flat = [r['resolved'] for row in checked for r in row['results']]
    summary = {'capabilities':len(checked),'passed':sum(1 for row in checked if row['passed'] is True),
               'failed':sum(1 for row in checked if row['passed'] is False),
               'unknown':sum(1 for row in checked if row['passed'] is None),
               'pointers':{'resolved':flat.count(True),'missing':flat.count(False),'unknown':flat.count(None)}}
    if writing:
        summary['recorded'] = {name:sum(1 for row in checked if row.get('recorded') == name)
                               for name in sorted({row.get('recorded') for row in checked if row.get('recorded')})}
    payload = {'schema':'capability-check-v1','contract':module.CONTRACT_VERSION,'trust':module.TRUST,
               'repo':{'git':repo['git'],'commit':repo['commit'],'dirty':repo['dirty']},
               'index':{'code_source':'graphify' if graphed else 'ast','graph':index.graph if index is not None else None},
               'capabilities':checked,'summary':summary,'recording':'record' if options.record else
               ('payloads' if options.payloads else None),'warnings':warnings}
    return _emit(0,json.dumps(payload,ensure_ascii=True,indent=2)+'\n',
                 ''.join('warning: %s\n' % warning for warning in warnings),out)

def main():
    p=argparse.ArgumentParser();p.add_argument('--version',action='store_true')
    p.add_argument('--config');p.add_argument('--project');p.add_argument('--actor')
    p.add_argument('--out',help='write the command result to this path as UTF-8 instead of stdout')
    p.add_argument('args',nargs=argparse.REMAINDER);a=p.parse_args()
    if a.version:
        print(line(report(Path(__file__).resolve().parent, "client")))
        return 0
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    if args[:1]==['capability'] and not (len(args)>1 and args[1] in CAPABILITY_ENDPOINT):
        if len(args)>1 and args[1]=='lookup' and a.config and a.project:
            return _capability_lookup(args[2:],json.loads(Path(a.config).read_text()),a.project,a.actor,a.out)
        if len(args)>1 and args[1]=='check':
            if (not a.config or not a.project) and not any(token in ('--help','-h') for token in args):
                p.error('the following arguments are required: --config, --project')
            return _capability_check(args[2:],json.loads(Path(a.config).read_text()) if a.config else None,
                                     a.project,a.actor,a.out)
        return _capability(args[1:],a.out)
    if not a.config or not a.project:
        p.error('the following arguments are required: --config, --project')
    action='bd';path=None
    if args[:1]==['refresh']:action='refresh';args=[]
    elif args[:1]==['view']:
        action='view';path=args[1] if len(args)>1 else 'CURRENT.md';args=[]
    elif args[:1] in (['brief'],['history'],['checkpoint'],['onboard'],['docs'],['session'],['handoff'],['review'],['work'],['feedback'],['requirement'],['ref'],['capability'],['proposal'],['guidance'],['coordinator']):action=args.pop(0)
    result=request(json.loads(Path(a.config).read_text()),a.project,a.actor,args,action,path)
    output = result['stdout']
    # The server's time of a write that was carried out (kittrial-5bb.97): one line on standard
    # error, so that standard output is exactly what it was. An older endpoint sends none.
    written = result.get('server_time')
    if result['returncode'] == 0 and isinstance(written, str) and re.fullmatch(r'[0-9T:+.Z-]{20,40}', written):
        result['stderr'] = (result.get('stderr') or '') + 'server_time: %s\n' % written
    if action == 'session' and args[:1] == ['resume'] and result['returncode'] == 0:
        client = report(Path(__file__).resolve().parent, "client")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError('Invalid resume response; inspect state before retrying.') from None
        provenance = payload.get('provenance', {})
        kit = provenance.get('kit') if isinstance(provenance, dict) else None
        if not isinstance(kit, dict):
            kit = {"component": "kit", "version": "unknown",
                   "source_commit": "unknown", "path": "unknown"}
        client_source = client.get("source_commit", "unknown")
        kit_source = kit.get("source_commit", "unknown")
        kit_version = kit.get("version", "unknown")
        if kit_version == "unknown" or kit_source == "unknown":
            parity = "unknown"
        elif client["version"] != kit_version or (
                client_source != "unknown" and client_source != kit_source):
            parity = "mismatch"
        elif client_source == kit_source:
            parity = "match"
        else:
            parity = "unknown"
        payload["provenance"] = {"client": client, "kit": kit, "parity": parity}
        output = json.dumps(payload, ensure_ascii=False) + "\n"
    elif action == 'onboard' and result['returncode'] == 0:
        client = report(Path(__file__).resolve().parent, "client")
        output += line(client) + '\n'
        kit_versions = re.findall(r'Orchestra kit: version ([^,]+), source ([^\r\n]+)', output)
        kit_source = kit_versions[-1][1] if kit_versions else "unknown"
        if kit_versions and (kit_versions[-1][0] != client['version'] or
                             kit_source != client['source_commit']):
            output += ('Orchestra version mismatch: client=%s, kit=%s\n'
                       % (client['version'], kit_versions[-1][0]))
        parity = "unknown" if client["source_commit"] == "unknown" or kit_source == "unknown" else (
            "match" if client["source_commit"] == kit_source else "mismatch")
        output += 'Orchestra provenance parity: %s\n' % parity
    if a.out:
        # Client-owned capture: the result is written as UTF-8 without a BOM and
        # with LF line endings, so no shell redirection encoding (PowerShell 5.1
        # `>` writes UTF-16LE, or UTF-8 with a BOM when a profile sets Out-File
        # encoding) can corrupt it. A failed command writes no file and keeps its
        # error on stderr.
        if result['returncode'] == 0:
            with open(a.out,'w',encoding='utf-8',newline='') as capture:
                capture.write(output)
        sys.stderr.write(result['stderr']);return result['returncode']
    sys.stdout.write(output);sys.stderr.write(result['stderr']);return result['returncode']

if __name__=='__main__':
    # Keep redirected document/template output UTF-8 on Windows as well as POSIX.
    # The remote transport already decodes UTF-8; the terminal pipe must not
    # silently re-encode it using the workstation's legacy code page.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:sys.exit(main())
    except (ValueError,RuntimeError,OSError) as e:raise SystemExit(str(e))
