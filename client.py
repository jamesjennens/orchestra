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
import difflib
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

#: The characters a terminal obeys and a server must not be able to send through the client's own
#: messages: the C0 controls except newline and tab, DEL and the C1 controls, the Unicode
#: bidirectional controls, and the line and paragraph separators (kittrial-5bb.241). Server text
#: that reached the terminal raw could rewrite the line it was shown on or reorder it; the client
#: is what every lane and office user runs, so it escapes them before printing.
TERMINAL_CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f'
                              '\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029]')

def server_text(text):
    """``text`` from a server, with the characters a terminal obeys shown escaped.

    The ONE place server-supplied text passes through before the client prints it
    (kittrial-5bb.241): ``\\x1b`` for a C0 or C1 control other than newline and tab, which stay as
    they are, and ``\\u202e`` for a bidirectional control or a line/paragraph separator. Printable
    text comes back untouched, and the caller's own bound is applied BEFORE this, so the bounds
    (600 and 1000 characters) are unchanged.
    """
    if not text:
        return text or ''
    return TERMINAL_CONTROL.sub(lambda match: '\\x%02x'%ord(match.group())
                                if ord(match.group())<256 else '\\u%04x'%ord(match.group()),text)

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
        raw_tail=(found.group(2) or '').strip()
        tail=server_text(raw_tail)
        note=(' This key runs a forced command, so the interpreter is the one in its '
              'authorized_keys line on the server, not "python" in this configuration: ask the operator to '
              'print that line again (admin.py authorized-keys) with an interpreter of 3.10 or newer.')
        # The endpoint's own advice says to set "python" here; under a forced command that is
        # wrong (the key's authorized_keys line decides), so the note replaces it (kittrial-5bb.222).
        # The recogniser reads the RAW tail; the text printed is escaped (kittrial-5bb.241).
        if raw_tail.startswith(SET_PYTHON):
            return server_text(found.group(1))+note
        return server_text(found.group(1))+(' '+tail if tail else '')+note
    return server_text(stderr.strip())

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
        raise RuntimeError(f'{label} failed ({p.returncode}); outcome may be uncertain. {server_text(p.stderr[:1000])}')
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

def _wait_command(args,config,project,actor,out):
    """``client.py wait``: block until something is the caller's move (kittrial-5bb.285).

    The blocking is HERE, in the client, because the harness that wakes a model holds the
    process; the endpoint is asked for one cheap read per poll (the ``wait`` action, one
    ``work`` read, the shared project lock of kittrial-5bb.281) and never for a blocking
    call, so the same read serves an agent over HTTP. The loop, the projection and the
    fingerprint are in :mod:`waiting`; this function is only the transport around them.

    The exit codes are the task's: 0 something is the caller's move (rows and fingerprint
    on stdout), 1 the ``--timeout`` ran out, 2 the arguments or the endpoint refused, 3 the
    service could not be reached. ``--since FINGERPRINT`` is the state the caller already
    saw, so a restart whose read has not changed blocks instead of returning it again.
    """
    import waiting
    try:
        options = waiting.parse_client_args(args)
    except ValueError as error:
        sys.stderr.write('ValueError: %s\n' % error)
        return waiting.EXIT_USAGE
    endpoint_args = waiting.read_args(options)

    def read():
        try:
            result = request(config,project,actor,endpoint_args,'wait')
        except (RuntimeError,ValueError,OSError) as error:
            # The transport said the service could not be reached: not a usage error.
            raise waiting.WaitUnreachable(str(error)[:300]) from None
        if not isinstance(result,dict):
            raise waiting.WaitUnreachable('the service did not answer a wait read')
        if result.get('returncode'):
            detail = (result.get('stderr') or '').strip().split('\n')[-1:] or ['the endpoint refused the wait read']
            raise waiting.WaitRefused(server_text(detail[-1])[:300])
        try:
            return json.loads(result.get('stdout') or '')
        except (ValueError,TypeError) as error:
            raise waiting.WaitUnreachable('Invalid wait response: %s' % str(error)[:200]) from None

    code,view = waiting.run_wait(read,since=options.since,timeout=options.timeout,
                                 interval=options.interval,
                                 form='coordinator' if options.coordinator else 'worker')
    text = json.dumps(view,ensure_ascii=True,indent=2)+'\n'
    if out:
        # The wait result is a result at every exit code, so --out writes it too; a
        # failure to write it is the only usage error here.
        try:
            with open(out,'w',encoding='utf-8',newline='') as capture:
                capture.write(text)
        except OSError as error:
            sys.stderr.write('ValueError: --out: %s\n' % error)
            return waiting.EXIT_USAGE
    else:
        sys.stdout.write(text)
    return code

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
            raise RuntimeError((result.get('stderr') or '').strip().split('\n')[-1:] or ['endpoint refused'])
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
                                 % ((result.get('stderr') or '').strip().split('\n') or ['endpoint refused'])[-1][:300])
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
                    row['refusal'] = ((result.get('stderr') or '').strip().split('\n') or ['endpoint refused'])[-1][:300]
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

#: The commands a contributor may run, one line each (kittrial-5bb.283): ``client.py help`` prints
#: this, an unknown command is answered with it, and the endpoint's refusal repeats it. The same
#: table lives in endpoint.py - a worker is handed this file on its own, so it must not import a
#: server-side module - and tests/test_endpoint_help.py keeps the two copies equal.
CONTRIBUTOR_COMMANDS = (
    ('brief TASK', 'the compact read of one task: its state, its newer activity, its checkpoint'),
    ('history TASK', "the task's comments and review records, newest first"),
    ('checkpoint TASK --file FILE', 'record where you are on a task, for the next run'),
    ('work', 'the tasks whose move is yours, and the ones you may claim'),
    ('wait', 'block until something is your move, then print it and its fingerprint'),
    ('ready', 'the tasks nobody holds that you may claim'),
    ('session register|resume|end', 'the actor session the endpoint keeps for you'),
    ('review TASK --file FILE', 'contribute a delivery, answer review items, or record a review'),
    ('handoff ...', 'hand a task to another actor, and read what the endpoint reconciled'),
    ('guidance get|ack', 'the worker guidance in force, and the acknowledgement of a version'),
    ('onboard', 'what a new worker is told about this project'),
    ('docs NAME', 'a kit document; read `docs start` and `docs worker-guide` first'),
    ('feedback add|list|correct', 'Orchestra feedback - about Orchestra itself, not about the project'),
    ('requirement ...', 'requirement records (the project owns these)'),
    ('requirements ...', 'requirement reads and owner answers'),
    ('ref ...', 'reference records'),
    ('capability ...', 'capability records: find, get, list, propose, revise, verify'),
    ('proposal ...', 'proposal records and their review'),
    ('coordinator ...', 'the coordinator reads for this project'),
    ('refresh', 'refresh the cached export the reads are answered from'),
    ('view [NAME]', 'render one of the project views'),
    ('list', 'the tasks of the project'),
    ('show TASK', 'one task as the tracker holds it'),
    ('search TEXT', 'the tasks matching a search'),
    ('count', 'how many tasks match'),
    ('create ...', 'create a task'),
    ('update TASK ...', 'change a task'),
    ('close TASK', 'close a task'),
    ('reopen TASK', 'reopen a closed task'),
    ('comments TASK', 'the comments of a task'),
    ('comments add TASK --file FILE', 'add a comment'),
    ('dep ...', 'the dependencies of a task'),
    ('state ...', 'the state events of a task'),
    ('lint', 'check the tracker for problems'),
)

#: The two documents a new worker should read, named in every answer this module gives (the task
#: says both by name: kittrial-5bb.283).
DOCS_TO_READ_FIRST = ('docs start', 'docs worker-guide')

#: The sentence every answer about a command outside the contributor interface carries: the refusal
#: used to point a worker at the installation operator's own host tool (kittrial-5bb.283).
ADMIN_TOOL_NOTE = 'admin.py is for the installation operator on the server, not for workers'

#: What someone typing a name that is nearly a command probably meant. The two the task names, and
#: the spellings seen in the field; anything else falls back to difflib.
NEAREST_COMMANDS = {'briefing': 'brief TASK', 'workerguide': 'docs worker-guide',
                    'worker-guide': 'docs worker-guide', 'workersguide': 'docs worker-guide'}

#: The actions the client dispatches itself; every other name in the table above is passed to the
#: endpoint. The dispatch in main() is written in terms of this tuple, and a test holds every one of
#: its names to a line of the table, so the help cannot fall behind the commands (kittrial-5bb.283).
CLIENT_ACTIONS = ('brief','history','checkpoint','onboard','docs','session','handoff','review','work',
                  'wait','feedback','requirement','requirements','ref','capability','proposal','guidance',
                  'coordinator')


def contributor_command_names():
    """The first word of every command in the table: what a contributor may type."""
    return tuple(dict.fromkeys(usage.split()[0] for usage, _text in CONTRIBUTOR_COMMANDS))


def nearest_contributor_command(name):
    """The command ``name`` probably meant, or ``None`` (kittrial-5bb.283).

    The names people actually mistype are answered from the table above; anything else is matched
    against the command names, so a near miss still gets an answer rather than silence.
    """
    if name in NEAREST_COMMANDS:
        return NEAREST_COMMANDS[name]
    close = difflib.get_close_matches(name, sorted(contributor_command_names()), n=1, cutoff=0.6)
    return close[0] if close else None


def contributor_help_text(unknown=None):
    """The answer a contributor gets: the commands it may run, and the two documents to read.

    ``unknown`` is the command someone typed that is not one of them; the answer then names the
    nearest command when there is one, and always says where admin.py belongs (kittrial-5bb.283).
    """
    lines = []
    if unknown is None:
        lines.append('The commands a contributor may run, one line each:')
    else:
        lines.append('%r is outside the contributor interface. The commands a contributor may run, '
                     'one line each:' % unknown)
    lines += ['  %s - %s' % (usage, text) for usage, text in CONTRIBUTOR_COMMANDS]
    lines.append('')
    lines.append('Read %s and %s first.' % DOCS_TO_READ_FIRST)
    if unknown is not None:
        nearest = nearest_contributor_command(unknown)
        if nearest is not None:
            lines.append('Did you mean `%s`?' % nearest)
    lines.append(ADMIN_TOOL_NOTE + '.')
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser(epilog=contributor_help_text(),
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version',action='store_true')
    p.add_argument('--config');p.add_argument('--project');p.add_argument('--actor')
    p.add_argument('--out',help='write the command result to this path as UTF-8 instead of stdout')
    p.add_argument('args',nargs=argparse.REMAINDER);a=p.parse_args()
    if a.version:
        print(line(report(Path(__file__).resolve().parent, "client")))
        return 0
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    # help is a contributor command, answered HERE and never sent to the endpoint
    # (kittrial-5bb.283): a worker asking for help must not need a server, and the endpoint's own
    # refusal is what this replaces. No command at all answers the same way.
    if not args or args[:1]==['help']:
        print(contributor_help_text())
        return 0
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
    # A command outside the contributor interface is answered, not refused with a pointer at the
    # operator's tool: the endpoint refuses it too, and this is the same answer without the round
    # trip (kittrial-5bb.283). Every name a contributor may type is in the table, including the
    # native subset the client passes through.
    if args[0] not in contributor_command_names():
        print(server_text(contributor_help_text(args[0])), file=sys.stderr)
        return 1
    # wait blocks HERE, in the client, and polls the endpoint's wait read (kittrial-5bb.285);
    # the endpoint is therefore asked per poll, never for one blocking call, so an agent over
    # HTTP can use the same read.
    if args[0]=='wait':
        return _wait_command(args[1:],json.loads(Path(a.config).read_text()),a.project,a.actor,a.out)
    action='bd';path=None
    if args[:1]==['refresh']:action='refresh';args=[]
    elif args[:1]==['view']:
        action='view';path=args[1] if len(args)>1 else 'CURRENT.md';args=[]
    elif args[0] in CLIENT_ACTIONS:action=args.pop(0)
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
