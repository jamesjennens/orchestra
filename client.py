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
import datetime
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import re
import time
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

def _load_sibling_waiting(module_path):
    """A sibling ``waiting.py`` loaded by path under a name of its own, bytecode off.

    Used only when an unrelated ``waiting`` module shadows the sibling on ``sys.path``:
    the file beside the client is what the client means, never the other module.
    """
    import importlib.util
    if not module_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location('orchestra_waiting', module_path)
    module = importlib.util.module_from_spec(spec)
    write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = write_bytecode
    return module

def _waiting_module():
    """``waiting.py`` from this client's own directory, or None for the inlined copy.

    A worker machine is handed ``client.py`` ON ITS OWN, so the wait logic cannot live
    only in the separate module; a stand-alone copy has no ``waiting.py`` beside it and
    uses :class:`_InlineWait` instead. When the sibling file IS there (the kit's own
    checkout and the kit's tests) that module is used, so inside the kit there is one
    implementation in play. An unrelated ``waiting`` module elsewhere on ``sys.path`` is
    never used: the sibling file must exist, and an import that resolved elsewhere is
    replaced by the sibling loaded by path.
    """
    sibling = Path(__file__).resolve().parent/'waiting.py'
    if not sibling.is_file():
        return None
    try:
        import waiting
    except ImportError:
        return _load_sibling_waiting(sibling)
    module_file = getattr(waiting,'__file__',None)
    if module_file and Path(module_file).resolve() == sibling:
        return waiting
    return _load_sibling_waiting(sibling)

class _InlineWait:
    """The wait view, fingerprint and poll loop, inlined so a stand-alone client can wait.

    A worker machine is handed ``client.py`` ON ITS OWN (docs/CLI_CONTRACT.md), but the
    wait logic of kittrial-5bb.285 lived only in ``waiting.py``, which such a copy does
    not have, so every stand-alone ``client.py wait`` failed with
    ``ModuleNotFoundError: No module named 'waiting'`` (kittrial-5bb.304). This class is
    the client's own copy of the projection, the fingerprint and the loop, used when no
    sibling ``waiting.py`` is present (:func:`_waiting_module`).

    It is a full implementation, not a silent degradation: a stand-alone client answers
    exactly what a kit client answers. ``waiting.py`` stays the module the endpoint and
    the kit's own tests use, and ``tests/test_standalone_client_wait.py`` drives both
    over one corpus of rows and fails if the view, the fingerprint, the bounds or the
    loop's answers differ, so the two copies cannot drift apart.
    """
    WAIT_SCHEMA = 'wait-v1'
    DEFAULT_INTERVAL = 300
    MIN_INTERVAL = 60
    DEFAULT_IDLE_MINUTES = 15
    IDLE_MINUTES_MIN, IDLE_MINUTES_MAX = 1, 10080
    EXIT_WORK = 0
    EXIT_TIMEOUT = 1
    EXIT_USAGE = 2
    EXIT_UNREACHABLE = 3
    MOVE_FIELDS = ('task', 'review_state', 'whose_move', 'next_actor',
                   'contribution_id', 'pending_review_items', 'open_items', 'blocking_items',
                   'checkpoint_at', 'newer_activity', 'newer_activity_marker',
                   'redelivered', 'guidance_version')
    #: The worker fingerprint's fields (kittrial-5bb.369): the caller's own checkpoint
    #: fields are dropped and the checkpoint-independent wait marker takes the marker's
    #: place. Kept identical to ``waiting.WORKER_MOVE_FIELDS``; the coordinator form keeps
    #: ``MOVE_FIELDS`` unchanged.
    # Ownership (including parent/sub-label authors) is resolved by the producer
    # of wait_newer_activity_marker; this projection must not reinterpret actors.
    WORKER_MOVE_FIELDS = ('task', 'review_state', 'whose_move', 'next_actor', 'owner', 'status',
                          'contribution_id', 'pending_review_items', 'redelivered',
                          'guidance_version', 'wait_newer_activity_marker')
    COORDINATOR_TRIGGERS = ('awaiting-review', 'awaiting-integration',
                            'checkpoint-to-coordinator', 'lane-quiet', 'empty-wake')
    EMPTY_WAKE_TRIGGER = 3
    LANE_FLAGS = ('your_move_idle', 'waiting_too_long', 'expect_by_passed')
    AWAITING_REVIEW_STATES = ('awaiting-review', 'legacy-review-ready')
    AWAITING_INTEGRATION_STATES = ('awaiting-integration', 'approved')
    FINGERPRINT = 'wait1:'
    DEFAULT_MAX_TIMEOUT = 3600
    DEFAULT_GIVE_UP_AFTER = 12 * 60 * 60
    DOUBLING_AFTER = 2
    WAIT_STATE_SCHEMA = 'wait-state-v1'
    WAIT_STATE_NAME = '.orchestra-wait-state.json'
    MAX_STATE_ITEMS = 100
    MAX_STATE_TEXT = 200
    MAX_STATE_LIST = 20
    MAX_STATE_BYTES = 64 * 1024
    _INSTANT = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})'
                          r'(?:\.(\d{1,6}))?(?:[Zz]|([+-])(\d{2}):?(\d{2}))$')
    _INSTANT_ADVICE = ('Invalid wait: --idle-since takes an ISO-8601 time with an offset '
                       '(for example 2026-10-17T15:00:00-04:00)')
    _EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)

    class WaitUnreachable(Exception):
        """The service could not be reached, or did not answer a wait read (exit 3)."""

    class WaitRefused(Exception):
        """The endpoint reached but refused the read (exit 2)."""

    @staticmethod
    def _scalar(value):
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        return str(value)

    @staticmethod
    def normalize_item(item):
        if not isinstance(item, dict):
            return {}
        row = {name: _InlineWait._scalar(item.get(name)) for name in _InlineWait.MOVE_FIELDS}
        row['wait_newer_activity_marker'] = _InlineWait._scalar(item.get('wait_newer_activity_marker'))
        row['owner'] = _InlineWait._scalar(item.get('owner') if item.get('owner') is not None
                                           else item.get('assignee'))
        row['status'] = _InlineWait._scalar(item.get('status'))
        attention = item.get('attention')
        if isinstance(attention, dict):
            for name in ('open_items', 'blocking_items', 'checkpoint_at', 'newer_activity',
                         'newer_activity_marker', 'wait_newer_activity_marker',
                         'redelivered', 'pending_review_items'):
                if row.get(name) is None and name in attention:
                    row[name] = _InlineWait._scalar(attention.get(name))
        if not isinstance(row.get('pending_review_items'), int):
            pending = item.get('open_requests')
            if isinstance(pending, int):
                row['pending_review_items'] = pending
            else:
                ids = item.get('pending_request_ids')
                if isinstance(ids, (list, tuple)):
                    row['pending_review_items'] = len(ids)
        if row.get('contribution_id') is None:
            contribution = item.get('contribution')
            if isinstance(contribution, dict) and contribution.get('id') is not None:
                row['contribution_id'] = _InlineWait._scalar(contribution.get('id'))
        for name in ('newer_activity_by_others', 'newer_activity_own',
                     'newer_activity_fields', 'newer_activity_coverage', 'unresolved_directions'):
            if row.get(name) is None:
                value = item.get(name)
                if value is None and isinstance(attention, dict):
                    value = attention.get(name)
                row[name] = _InlineWait._scalar(value)
        return row

    @staticmethod
    def _fingerprinted(row):
        return {name: row.get(name) for name in _InlineWait.MOVE_FIELDS}

    @staticmethod
    def _worker_fingerprinted(row):
        return {name: row.get(name) for name in _InlineWait.WORKER_MOVE_FIELDS}

    @staticmethod
    def _canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)

    @staticmethod
    def _sorted_rows(rows):
        return sorted(rows, key=lambda row: (str(row.get('task') or row.get('lane') or ''),
                                             _InlineWait._canonical(row)))

    @staticmethod
    def digest(value):
        return _InlineWait.FINGERPRINT + hashlib.sha256(
            _InlineWait._canonical(value).encode('utf-8')).hexdigest()

    @staticmethod
    def fingerprint(items):
        return _InlineWait.digest(_InlineWait._sorted_rows(
            [_InlineWait._fingerprinted(_InlineWait.normalize_item(item))
             for item in items if isinstance(item, dict)]))

    @staticmethod
    def is_fingerprint(value):
        return bool(isinstance(value, str) and len(value) == len(_InlineWait.FINGERPRINT) + 64
                    and value.startswith(_InlineWait.FINGERPRINT)
                    and all(character in '0123456789abcdef'
                            for character in value[len(_InlineWait.FINGERPRINT):]))

    @staticmethod
    def state_path(config_path, project=None, actor=None, form='worker'):
        """The client's small wait cache beside its config (Python 3.6 syntax)."""
        if not config_path:
            return None
        directory = os.path.dirname(os.path.abspath(config_path))
        identity = '\x00'.join(str(value or '') for value in (project, actor, form))
        suffix = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
        stem, extension = os.path.splitext(_InlineWait.WAIT_STATE_NAME)
        return os.path.join(directory, '%s-%s%s' % (stem, suffix, extension))

    @staticmethod
    def _state_value(value, depth=0):
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:_InlineWait.MAX_STATE_TEXT]
        if isinstance(value, (list, tuple)):
            if depth >= 4:
                return str(value)[:_InlineWait.MAX_STATE_TEXT]
            return [_InlineWait._state_value(item, depth + 1)
                    for item in list(value)[:_InlineWait.MAX_STATE_LIST]]
        text = str(value)
        return text[:_InlineWait.MAX_STATE_TEXT]

    @staticmethod
    def _state_row(row):
        return dict((name, _InlineWait._state_value(row[name])) for name in sorted(row))

    @staticmethod
    def read_state(path):
        """The state last written at ``path``, or ``None`` (a bad file is absent)."""
        if not path:
            return None
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                text = handle.read(_InlineWait.MAX_STATE_BYTES + 1)
            if len(text) > _InlineWait.MAX_STATE_BYTES:
                return None
            value = json.loads(text)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            return None
        if not isinstance(value, dict) or value.get('schema') != _InlineWait.WAIT_STATE_SCHEMA:
            return None
        remembered = value.get('fingerprint')
        if not _InlineWait.is_fingerprint(remembered):
            remembered = None
        items = value.get('items')
        if not isinstance(items, list):
            items = []
        items = [_InlineWait._state_row(item) for item in items[:_InlineWait.MAX_STATE_ITEMS]
                 if isinstance(item, dict)]
        return {'schema': _InlineWait.WAIT_STATE_SCHEMA,
                'fingerprint': remembered, 'items': items}

    @staticmethod
    def write_state(path, fingerprint, items):
        """Write the cache atomically; a failure is ignored so a wait never fails."""
        if not path or not _InlineWait.is_fingerprint(fingerprint) or not isinstance(items, list):
            return False
        payload = {'schema': _InlineWait.WAIT_STATE_SCHEMA, 'fingerprint': fingerprint,
                   'items': [_InlineWait._state_row(item)
                             for item in items[:_InlineWait.MAX_STATE_ITEMS]
                             if isinstance(item, dict)]}

        def encoded(value):
            return json.dumps(value, ensure_ascii=True, sort_keys=True,
                              separators=(',', ':'))

        try:
            text = encoded(payload)
        except (ValueError, TypeError):
            return False
        if len(text) > _InlineWait.MAX_STATE_BYTES:
            payload['items'] = []
            try:
                text = encoded(payload)
            except (ValueError, TypeError):
                return False
            if len(text) > _InlineWait.MAX_STATE_BYTES:
                return False
        temporary = '%s.%d.tmp' % (path, os.getpid())
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                handle.write(text)
                handle.write('\n')
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError, UnicodeError):
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
            return False
        return True

    @staticmethod
    def _item_key(row):
        for name in ('task', 'lane'):
            if row.get(name) is not None:
                return str(row.get(name))
        return _InlineWait.digest(row)

    @staticmethod
    def _keyed_items(items):
        keyed = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            key = _InlineWait._item_key(item)
            if key in keyed:
                number = 2
                while '%s#%d' % (key, number) in keyed:
                    number += 1
                key = '%s#%d' % (key, number)
            keyed[key] = _InlineWait._state_row(item)
        return keyed

    @staticmethod
    def changed_items(old_items, new_items):
        """Changed fields old -> new, and the items added and removed (item 6)."""
        old = _InlineWait._keyed_items(old_items)
        new = _InlineWait._keyed_items(new_items)
        tasks = []
        for key in sorted(set(old) & set(new)):
            fields = []
            for name in sorted(set(old[key]) | set(new[key])):
                before, after = old[key].get(name), new[key].get(name)
                if before != after:
                    fields.append({'field': name, 'old': before, 'new': after})
            if fields:
                tasks.append({'task': key, 'fields': fields})
        return {'tasks': tasks,
                'added': sorted(set(new) - set(old)),
                'removed': sorted(set(old) - set(new))}

    @staticmethod
    def parse_instant(text):
        """The epoch seconds of an ISO-8601 instant with an offset (Python 3.6 syntax)."""
        if not isinstance(text, str):
            raise ValueError(_InlineWait._INSTANT_ADVICE)
        match = _InlineWait._INSTANT.match(text.strip())
        if match is None:
            raise ValueError(_InlineWait._INSTANT_ADVICE)
        if match.group(8) is not None:
            hours, minutes = int(match.group(9)), int(match.group(10))
            if hours > 23 or minutes > 59:
                raise ValueError(_InlineWait._INSTANT_ADVICE)
            offset = datetime.timedelta(hours=hours, minutes=minutes)
            if match.group(8) == '-':
                offset = -offset
        else:
            offset = datetime.timedelta(0)
        fraction = match.group(7) or ''
        micro = int((fraction + '000000')[:6])
        try:
            moment = datetime.datetime(int(match.group(1)), int(match.group(2)),
                                       int(match.group(3)), int(match.group(4)),
                                       int(match.group(5)), int(match.group(6)), micro,
                                       tzinfo=datetime.timezone(offset))
        except (ValueError, OverflowError):
            raise ValueError(_InlineWait._INSTANT_ADVICE) from None
        return (moment - _InlineWait._EPOCH).total_seconds()

    @staticmethod
    def format_instant(epoch):
        """The ISO-8601 UTC spelling of an epoch; fractional only when it is there."""
        moment = _InlineWait._EPOCH + datetime.timedelta(seconds=float(epoch))
        if moment.microsecond:
            return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        return moment.strftime('%Y-%m-%dT%H:%M:%SZ')

    @staticmethod
    def _seconds(value):
        value = float(value)
        return int(value) if value.is_integer() else value

    @staticmethod
    def timeout_cap(base, max_timeout):
        cap = _InlineWait.DEFAULT_MAX_TIMEOUT if max_timeout is None else max_timeout
        return max(base, cap)

    @staticmethod
    def schedule_timeout(index, base, cap):
        if base <= 0:
            return 0
        if index <= _InlineWait.DOUBLING_AFTER:
            return base
        if index - _InlineWait.DOUBLING_AFTER > 62:
            return cap
        return min(base * float(2 ** (index - _InlineWait.DOUBLING_AFTER)), cap)

    @staticmethod
    def empty_returns(elapsed, base, cap):
        if base <= 0:
            return 0
        remaining = max(0.0, float(elapsed))
        count = 0
        index = 1
        while True:
            step = _InlineWait.schedule_timeout(index, base, cap)
            if step >= cap:
                if remaining >= step:
                    count += int(remaining // step)
                return count
            if remaining < step:
                return count
            remaining -= step
            count += 1
            index += 1

    @staticmethod
    def wait_command(prefix, fingerprint, idle_since, timeout,
                     interval=DEFAULT_INTERVAL, form='worker',
                     idle_minutes=DEFAULT_IDLE_MINUTES, max_timeout=None,
                     give_up_after=DEFAULT_GIVE_UP_AFTER):
        parts = [prefix, 'wait']
        if form == 'coordinator':
            parts += ['--coordinator', '--idle-minutes', str(idle_minutes)]
        parts += ['--interval', str(_InlineWait._seconds(interval))]
        if fingerprint:
            parts += ['--since', fingerprint]
        parts += ['--idle-since', idle_since, '--timeout',
                  str(_InlineWait._seconds(timeout))]
        if max_timeout is not None:
            parts += ['--max-timeout', str(_InlineWait._seconds(max_timeout))]
        if give_up_after != _InlineWait.DEFAULT_GIVE_UP_AFTER:
            parts += ['--give-up-after', str(_InlineWait._seconds(give_up_after))]
        return ' '.join(parts)

    @staticmethod
    def movement(items):
        return _InlineWait._sorted_rows([_InlineWait.normalize_item(item) for item in items or []
                                         if isinstance(item, dict)
                                         and item.get('whose_move') == 'you'])

    @staticmethod
    def worker_view(items):
        rows = _InlineWait.movement(items)
        return {'schema': _InlineWait.WAIT_SCHEMA, 'form': 'worker', 'items': rows,
                'fingerprint': _InlineWait.digest(_InlineWait._sorted_rows(
                    [_InlineWait._worker_fingerprinted(row) for row in rows]))}

    @staticmethod
    def _lane_row(lane, entry=None):
        if entry is None:
            return {'lane': _InlineWait._scalar(lane.get('lane')),
                    'actor': _InlineWait._scalar(lane.get('actor')),
                    'task': None, 'state': _InlineWait._scalar(lane.get('state')),
                    'waiting_on': _InlineWait._scalar(lane.get('waiting_on')),
                    'whose_move': None, 'next_actor': None, 'review_state': None,
                    'flags': sorted(set(lane.get('flag_tasks') or {}))}
        return {'lane': _InlineWait._scalar(lane.get('lane')),
                'actor': _InlineWait._scalar(lane.get('actor')),
                'task': _InlineWait._scalar(entry.get('task')),
                'state': _InlineWait._scalar(entry.get('state')),
                'waiting_on': _InlineWait._scalar(entry.get('waiting_on')),
                'whose_move': _InlineWait._scalar(entry.get('whose_move')),
                'next_actor': _InlineWait._scalar(entry.get('next_actor')),
                'review_state': _InlineWait._scalar(entry.get('review_state')),
                'flags': sorted(set(entry.get('flags') or []))}

    @staticmethod
    def _empty_wake_row(lane):
        return {'lane': _InlineWait._scalar(lane.get('lane')),
                'actor': _InlineWait._scalar(lane.get('actor')),
                'task': _InlineWait._scalar(lane.get('empty_wake_task')),
                'trigger': _InlineWait._scalar(lane.get('empty_wake_trigger')),
                'empty_wakes_24h': lane.get('empty_wakes_24h')
                if isinstance(lane.get('empty_wakes_24h'), int) else 0,
                'at': _InlineWait._scalar(lane.get('empty_wake_at'))}

    @staticmethod
    def coordinator_view(queue_items, lanes_payload):
        lanes = lanes_payload if isinstance(lanes_payload, dict) else {}
        triggers = {name: [] for name in _InlineWait.COORDINATOR_TRIGGERS}
        for item in queue_items or []:
            if not isinstance(item, dict):
                continue
            state = item.get('review_state') or item.get('work_state')
            if state in _InlineWait.AWAITING_REVIEW_STATES and item.get('contribution_id'):
                triggers['awaiting-review'].append(_InlineWait.normalize_item(item))
            elif state in _InlineWait.AWAITING_INTEGRATION_STATES:
                triggers['awaiting-integration'].append(_InlineWait.normalize_item(item))
        for lane in lanes.get('items') or []:
            if not isinstance(lane, dict):
                continue
            lane_flags = set()
            for name in _InlineWait.LANE_FLAGS:
                if lane.get(name):
                    lane_flags.add(name)
            lane_flags.update(name for name in (lane.get('flag_tasks') or {})
                              if name in _InlineWait.LANE_FLAGS)
            if lane_flags:
                row = _InlineWait._lane_row(lane)
                row['flags'] = sorted(lane_flags)
                triggers['lane-quiet'].append(row)
            for entry in lane.get('held') or []:
                if not isinstance(entry, dict):
                    continue
                addressed = (entry.get('state') == 'waiting-on-coordinator'
                             or entry.get('waiting_on') == 'coordinator'
                             or entry.get('next_actor') == 'coordinator')
                if addressed:
                    triggers['checkpoint-to-coordinator'].append(_InlineWait._lane_row(lane, entry))
            count = lane.get('empty_wakes_24h')
            if isinstance(count, int) and count >= _InlineWait.EMPTY_WAKE_TRIGGER:
                triggers['empty-wake'].append(_InlineWait._empty_wake_row(lane))
        grouped = {name: _InlineWait._sorted_rows(rows) for name, rows in triggers.items()}
        flat = [dict(row, trigger=name) for name in _InlineWait.COORDINATOR_TRIGGERS
                for row in grouped[name]]
        fingerprinted = {name: _InlineWait._sorted_rows(
            [_InlineWait._fingerprinted(row) for row in rows])
            for name, rows in triggers.items()
            if rows or name != 'empty-wake'}
        return {'schema': _InlineWait.WAIT_SCHEMA, 'form': 'coordinator', 'triggers': grouped,
                'items': flat, 'fingerprint': _InlineWait.digest(fingerprinted)}

    @staticmethod
    def parse_read_args(args):
        parser = _InlineWait.Parser(prog='wait', add_help=False)
        parser.add_argument('--coordinator', action='store_true')
        parser.add_argument('--idle-minutes', type=int, default=_InlineWait.DEFAULT_IDLE_MINUTES,
                            dest='idle_minutes')
        parser.add_argument('--since')
        parser.add_argument('--json', action='store_true')
        parsed = parser.parse_args(args)
        if not _InlineWait.IDLE_MINUTES_MIN <= parsed.idle_minutes <= _InlineWait.IDLE_MINUTES_MAX:
            raise ValueError('Invalid wait read: --idle-minutes must be %d..%d'
                             % (_InlineWait.IDLE_MINUTES_MIN, _InlineWait.IDLE_MINUTES_MAX))
        if parsed.since is not None and not _InlineWait.is_fingerprint(parsed.since):
            raise ValueError('Invalid wait read: --since takes a fingerprint this kit printed '
                             '(wait1:<64 hex>)')
        return parsed

    @staticmethod
    def parse_client_args(args):
        parsed = _wait_parser().parse_args(args)
        if parsed.interval < _InlineWait.MIN_INTERVAL:
            raise ValueError('Invalid wait: --interval must be at least %d seconds (the default is %d)'
                             % (_InlineWait.MIN_INTERVAL, _InlineWait.DEFAULT_INTERVAL))
        if parsed.timeout is not None and parsed.timeout < 0:
            raise ValueError('Invalid wait: --timeout must be zero or more seconds')
        if parsed.since is not None and not _InlineWait.is_fingerprint(parsed.since):
            raise ValueError('Invalid wait: --since takes a fingerprint this kit printed '
                             '(wait1:<64 hex>)')
        if parsed.idle_since is not None:
            _InlineWait.parse_instant(parsed.idle_since)
        if parsed.max_timeout is not None and parsed.max_timeout <= 0:
            raise ValueError('Invalid wait: --max-timeout must be more than zero seconds')
        if parsed.give_up_after <= 0:
            raise ValueError('Invalid wait: --give-up-after must be more than zero seconds')
        if not _InlineWait.IDLE_MINUTES_MIN <= parsed.idle_minutes <= _InlineWait.IDLE_MINUTES_MAX:
            raise ValueError('Invalid wait: --idle-minutes must be %d..%d'
                             % (_InlineWait.IDLE_MINUTES_MIN, _InlineWait.IDLE_MINUTES_MAX))
        return parsed

    @staticmethod
    def read_args(options, since=None):
        args = []
        if options.coordinator:
            args += ['--coordinator', '--idle-minutes', str(options.idle_minutes)]
        if since is not None:
            args += ['--since', since]
        return args

    @staticmethod
    def run_wait(read, since=None, timeout=None, interval=DEFAULT_INTERVAL,
                 clock=None, sleep=None, form=None, idle_since=None, max_timeout=None,
                 give_up_after=DEFAULT_GIVE_UP_AFTER, command_prefix='client.py',
                 idle_minutes=DEFAULT_IDLE_MINUTES, now=None, state_file=None, fresh=False):
        clock = time.monotonic if clock is None else clock
        sleep = time.sleep if sleep is None else sleep
        form = form or 'worker'
        started = clock()
        polls = 0
        state = _InlineWait.read_state(state_file)
        previous = state.get('items') if state else []
        remembered = None if fresh or state is None else state.get('fingerprint')
        resumed = since if since is not None else remembered
        scheduled = idle_since is not None and timeout is not None
        if scheduled:
            idle_started = _InlineWait.parse_instant(idle_since)
            wall_now = time.time() if now is None else now
            idle_before = max(0.0, wall_now - idle_started)
            cap = _InlineWait.timeout_cap(timeout, max_timeout)

        def with_schedule(view, elapsed, reset):
            if not scheduled:
                return view
            fingerprint = view.get('fingerprint')
            if reset:
                fields = {
                    'next_timeout': _InlineWait._seconds(timeout),
                    'next_command': _InlineWait.wait_command(
                        command_prefix, fingerprint, _InlineWait.format_instant(wall_now + elapsed),
                        timeout, interval, form, idle_minutes, max_timeout, give_up_after),
                    'give_up': False,
                }
                return dict(view, **fields)
            idle_elapsed = idle_before + elapsed
            done = _InlineWait.empty_returns(idle_elapsed, timeout, cap)
            upcoming = _InlineWait.schedule_timeout(done + 1, timeout, cap)
            give_up = idle_elapsed >= give_up_after
            fields = {
                'next_timeout': _InlineWait._seconds(upcoming),
                'next_command': _InlineWait.wait_command(
                    command_prefix, fingerprint, idle_since, upcoming, interval, form,
                    idle_minutes, max_timeout, give_up_after),
                'give_up': give_up,
            }
            if give_up:
                fields['message'] = ('checkpoint and stop: nothing has been your move since %s'
                                     % idle_since)
            return dict(view, **fields)

        def remember(view):
            if state_file is not None:
                _InlineWait.write_state(state_file, view.get('fingerprint'),
                                        view.get('items') or [])

        def with_changes(view):
            if state_file is None:
                return view
            return dict(view, changed=_InlineWait.changed_items(
                previous, view.get('items') or []))

        while True:
            polls += 1
            try:
                view = read()
            except _InlineWait.WaitUnreachable as error:
                return _InlineWait.EXIT_UNREACHABLE, {
                    'schema': _InlineWait.WAIT_SCHEMA, 'form': form,
                    'fingerprint': None, 'items': [],
                    'error': 'the service cannot be reached: %s' % str(error)[:240],
                    'polls': polls}
            except _InlineWait.WaitRefused as error:
                return _InlineWait.EXIT_USAGE, {
                    'schema': _InlineWait.WAIT_SCHEMA, 'form': form,
                    'fingerprint': None, 'items': [],
                    'error': str(error)[:300], 'polls': polls}
            if not isinstance(view, dict):
                return _InlineWait.EXIT_UNREACHABLE, {
                    'schema': _InlineWait.WAIT_SCHEMA, 'form': form,
                    'fingerprint': None, 'items': [],
                    'error': 'the service cannot be reached: it did not answer a wait read',
                    'polls': polls}
            if resumed is None:
                if view.get('items'):
                    answer = with_changes(with_schedule(
                        dict(view, polls=polls), clock() - started, reset=True))
                    remember(answer)
                    return _InlineWait.EXIT_WORK, answer
            elif view.get('fingerprint') != resumed:
                answer = with_changes(with_schedule(
                    dict(view, polls=polls), clock() - started,
                    reset=bool(view.get('items'))))
                remember(answer)
                return _InlineWait.EXIT_WORK, answer
            elapsed = clock() - started
            if timeout is not None and elapsed >= timeout:
                answer = with_schedule(dict(view, polls=polls, timed_out=True), elapsed,
                                       reset=False)
                remember(answer)
                return _InlineWait.EXIT_TIMEOUT, answer
            nap = interval if timeout is None else min(interval, max(0.0, timeout - elapsed))
            if nap <= 0:
                answer = with_schedule(dict(view, polls=polls, timed_out=True), elapsed,
                                       reset=False)
                remember(answer)
                return _InlineWait.EXIT_TIMEOUT, answer
            sleep(nap)

def _emit(code,stdout,stderr,out):
    """Same client-owned capture as the endpoint actions: UTF-8, LF, no BOM, and no
    file at all on a nonzero exit."""
    if out:
        if code == 0:
            with open(out,'w',encoding='utf-8',newline='') as capture:
                capture.write(stdout)
        sys.stderr.write(stderr);return code
    sys.stdout.write(stdout);sys.stderr.write(stderr);return code

def _wait_client_prefix(config_path,project,actor):
    """How this caller invokes the client, as the prefix of the printed ``next_command``.

    ``wait`` runs on the worker's machine, so the command it prints must name the same
    interpreter, client, config, project and actor this invocation used; otherwise an
    agent copying the line would wait on the wrong project. Paths are shell-quoted for
    POSIX, where the wait loop's examples live (a Windows wrapper is a process, not this
    one line, on that platform).
    """
    tokens = [sys.executable or 'python3',str(Path(__file__).resolve())]
    if config_path:
        tokens += ['--config',str(config_path)]
    if project:
        tokens += ['--project',str(project)]
    if actor:
        tokens += ['--actor',str(actor)]
    return ' '.join(shlex.quote(token) for token in tokens)


def _wait_parser(add_help=False):
    parser = _InlineWait.Parser(prog='wait', add_help=add_help)
    parser.add_argument('--coordinator', action='store_true')
    parser.add_argument('--idle-minutes', type=int, default=_InlineWait.DEFAULT_IDLE_MINUTES,
                        dest='idle_minutes')
    parser.add_argument('--interval', type=int, default=_InlineWait.DEFAULT_INTERVAL)
    parser.add_argument('--timeout', type=float, default=None)
    parser.add_argument('--since')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--idle-since')
    parser.add_argument('--max-timeout', type=float, default=None, dest='max_timeout')
    parser.add_argument('--give-up-after', type=float,
                        default=_InlineWait.DEFAULT_GIVE_UP_AFTER, dest='give_up_after')
    parser.add_argument('--json', action='store_true')
    return parser


def _wait_command(args,config,project,actor,out,config_path=None):
    """``client.py wait``: block until something is the caller's move (kittrial-5bb.285).

    The blocking is HERE, in the client, because the harness that wakes a model holds the
    process; the endpoint is asked for one cheap read per poll (the ``wait`` action, one
    ``work`` read, the shared project lock of kittrial-5bb.281) and never for a blocking
    call, so the same read serves an agent over HTTP.

    The loop, the projection and the fingerprint are the sibling :mod:`waiting` when it is
    beside this client (the kit's own checkout and tests); a stand-alone ``client.py``
    has no such module and uses the inlined :class:`_InlineWait` copy instead, which
    ``tests/test_standalone_client_wait.py`` holds to ``waiting.py`` (kittrial-5bb.304).
    This function is only the transport around whichever of the two is in play.

    The exit codes are the task's: 0 something is the caller's move (rows and fingerprint
    on stdout), 1 the ``--timeout`` ran out, 2 the arguments or the endpoint refused, 3 the
    service could not be reached. ``--since FINGERPRINT`` is the state the caller already
    saw, so a restart whose read has not changed blocks instead of returning it again.
    """
    wait = _waiting_module() or _InlineWait
    try:
        options = wait.parse_client_args(args)
    except ValueError as error:
        sys.stderr.write('ValueError: %s\n' % error)
        return wait.EXIT_USAGE
    form = 'coordinator' if options.coordinator else 'worker'
    # The fingerprint the caller ALREADY has (--since, else the state file this client
    # keeps) is carried on the read itself (kittrial-5bb.397), so the endpoint can tell
    # a wake from a poll and record an empty one from the ONE read it already makes.
    # run_wait resolves ``resumed`` the same way; the two must not disagree.
    state_file = wait.state_path(config_path,project,actor,form)
    state = wait.read_state(state_file)
    remembered = None if options.fresh or state is None else state.get('fingerprint')
    resumed = options.since if options.since is not None else remembered
    endpoint_args = wait.read_args(options, resumed)
    # An endpoint older than this kit refuses the unknown ``--since`` on the READ. The
    # empty-wake log is additive, so the wait must still work there: fall back to exactly
    # the old request once, and keep that answer for the rest of this wait.
    plain_endpoint_args = wait.read_args(options, None)
    carries_since = endpoint_args != plain_endpoint_args
    accepts_since = {'value': carries_since}

    def read():
        try:
            result = request(config,project,actor,
                             endpoint_args if accepts_since['value'] else plain_endpoint_args,
                             'wait')
            if (carries_since and accepts_since['value'] and isinstance(result,dict)
                    and result.get('returncode')
                    and '--since' in (result.get('stderr') or '')):
                accepts_since['value'] = False
                result = request(config,project,actor,plain_endpoint_args,'wait')
        except (RuntimeError,ValueError,OSError) as error:
            # The transport said the service could not be reached: not a usage error.
            raise wait.WaitUnreachable(str(error)[:300]) from None
        if not isinstance(result,dict):
            raise wait.WaitUnreachable('the service did not answer a wait read')
        if result.get('returncode'):
            detail = (result.get('stderr') or '').strip().split('\n')[-1:] or ['the endpoint refused the wait read']
            raise wait.WaitRefused(server_text(detail[-1])[:300])
        try:
            return json.loads(result.get('stdout') or '')
        except (ValueError,TypeError) as error:
            raise wait.WaitUnreachable('Invalid wait response: %s' % str(error)[:200]) from None

    code,view = wait.run_wait(read,since=options.since,timeout=options.timeout,
                              interval=options.interval,
                              form=form,
                              idle_since=options.idle_since,
                              max_timeout=options.max_timeout,
                              give_up_after=options.give_up_after,
                              command_prefix=_wait_client_prefix(config_path,project,actor),
                              idle_minutes=options.idle_minutes,
                              state_file=state_file,
                              fresh=options.fresh)
    text = json.dumps(view,ensure_ascii=True,indent=2)+'\n'
    if out:
        # The wait result is a result at every exit code, so --out writes it too; a
        # failure to write it is the only usage error here.
        try:
            with open(out,'w',encoding='utf-8',newline='') as capture:
                capture.write(text)
        except OSError as error:
            sys.stderr.write('ValueError: --out: %s\n' % error)
            return wait.EXIT_USAGE
    else:
        sys.stdout.write(text)
    return code

DELIVERY_SSH_ONLY_HTTP = (
    "Delivered bundles are served over HTTP only; on the host transport, read the "
    "contribution's recorded delivery.path directly."
)


def _delivery_command():
    """``client.py delivery`` (kittrial-5bb.373).

    ``client.py`` is the SSH/local host-transport client; it answers the fixed sentence and
    reads nothing from disk (precision 4, s3-client-config). HTTP bundle metadata and verified
    downloads are served through ``http_client.py delivery``.
    """
    sys.stderr.write('ValueError: %s\n' % DELIVERY_SSH_ONLY_HTTP)
    return 2


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
    ('files list|get|put ...', "a task's files over SSH: list them, get one by its sha256 into a folder, put one"),
    ('session register|resume|end', 'the actor session the endpoint keeps for you'),
    ('review TASK --file FILE', 'contribute a delivery, answer review items, or record a review'),
    ('delivery TASK [--contribution ID] [--out FILE]', 'fetch and verify a delivered bundle over HTTP'),
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
CLIENT_ACTIONS = ('brief','history','checkpoint','onboard','docs','session','handoff','review','delivery','work',
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


# Copy of work.py:502-507 and its constants at 213-218; client.py stays standalone.
HELP_TOKENS = ('-h', '--help')
VALUE_OPTIONS = {'--owner', '--state', '--limit', '--offset', '--handoff-limit',
                 '--handoff-offset', '--file', '-f', '--items-offset', '--items-limit',
                 '--since', '--cursor', '--body-budget', '--ref-limit', '--ref-offset',
                 '--proposal-limit', '--proposal-offset', '--capability-limit', '--capability-offset',
                 '--recent', '--last', '--kind'}


def help_requested(args):
    """True when args ask for help; side-effect free for every command."""
    for index, token in enumerate(args):
        if token in HELP_TOKENS and not (index and args[index - 1] in VALUE_OPTIONS):
            return True
    return False


_COMMAND_HELP_USAGE = {
    'work': 'work [--mine | --owner ACTOR] [--state STATE] [--claimable] [--limit N] [--offset N] '
            '[--handoff-limit N] [--handoff-offset N] [--ref-limit N] [--ref-offset N] [--json]'
            ' | work lanes [--idle-minutes N] [--limit N] [--offset N] [--json]',
    'review': 'review TASK [--file payload.json]',
    'handoff': 'handoff TASK --file payload.json',
    'brief': 'brief TASK [--items-offset N] [--items-limit N] [--full] [--json]',
    'history': 'history TASK [--limit N | --recent N | --last N] [--since TIME] '
               '[--since-checkpoint | --since-my-checkpoint] [--kind comment|event] [--cursor TOKEN] '
               '[--body-budget BYTES]',
    'checkpoint': 'checkpoint TASK --file checkpoint.json [--json] | checkpoint TASK --provenance | '
                  'checkpoint TASK --verify | checkpoint TASK --latest [--provenance] | '
                  'checkpoint TASK --directions [--offset N] [--limit N]',
    'docs': 'docs [NAME] | docs --status',
}


def contributor_command_help(command):
    """Static help for one known command, without configuration or server modules."""
    if command == 'files':
        return _files_parser().format_help()
    if command == 'wait':
        return _wait_parser(add_help=True).format_help()
    listed_usage, description = next(item for item in CONTRIBUTOR_COMMANDS
                                     if item[0].split()[0] == command)
    usage = _COMMAND_HELP_USAGE.get(command, listed_usage)
    return ('usage: %s\n\n%s\n\noptions:\n  -h, --help  show this help message and exit\n'
            % (usage, description))


# -- task files over SSH (kittrial-5bb.384, docs/TASK_FILES_DESIGN.md 3b option B) ------------------------
# One JSON header line, then exactly ``bytes`` raw bytes for a put; the answer is one JSON line, then the
# raw bytes of a get. The limits here are the endpoint's own (task_files.py); this file stays self-contained.
FILES_HEADER_LIMIT = 64 * 1024
FILES_STEP = 64 * 1024
SHA256_TEXT = re.compile(r'[0-9a-f]{64}')

WINDOWS_RESERVED = re.compile(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?')

def _is_link(path):
    """A symbolic link, or on Windows a junction or any other reparse point."""
    if os.path.islink(path):
        return True
    try:
        return bool(getattr(os.lstat(path), 'st_file_attributes', 0) & 0x400)
    except OSError:
        return False

def _clean_file_name(name, for_disk=False):
    """The basename of ``name`` with nothing a path or a terminal obeys left in it; a refusal when none is left."""
    base = re.split(r'[\\/]', name)[-1].strip()
    base = TERMINAL_CONTROL.sub('', base)
    if not base or base in ('.', '..') or len(base) > 255 or ':' in base[:2]:
        raise ValueError('The file has no usable name; nothing was sent.')
    if for_disk and (WINDOWS_RESERVED.fullmatch(base) or base[-1] in '. '):
        raise ValueError('The file name %r cannot be written safely here (a reserved name, or it ends in a dot or space); '
                         'nothing was kept.' % base)
    return base

def _frame_exchange(config, header, body=None, sink=None):
    """Send one framed request; returns ``(answer dict, byte count received)``.

    ``body`` is ``(file object, length)`` sent after the header; ``sink(chunk)`` receives the bytes that follow
    the answer line. The whole exchange has TIMEOUT seconds of silence at most per step and an overall bound
    that grows with the bytes, so a stuck server cannot hold the client for ever.
    """
    argv, label = _argv(config)
    encoded = (json.dumps(header, ensure_ascii=False) + '\n').encode('utf-8')
    if len(encoded) > FILES_HEADER_LIMIT:
        raise ValueError('The request header is over its %d byte limit; nothing was sent.' % FILES_HEADER_LIMIT)
    import select, tempfile, threading
    size = body[1] if body else 0
    deadline = time.monotonic() + TIMEOUT + size / 16384.0 + (header.get('bytes') or 0) / 16384.0
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen(argv, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    timer = threading.Timer(max(1.0, deadline - time.monotonic()), proc.kill)
    timer.daemon = True
    timer.start()
    received = 0
    try:
        try:
            proc.stdin.write(encoded)
            if body:
                sent = 0
                while sent < size:
                    chunk = body[0].read(min(FILES_STEP, size - sent))
                    if not chunk:
                        raise ValueError('The file got shorter while it was sent; the server stored nothing.')
                    proc.stdin.write(chunk)
                    sent += len(chunk)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # the server answered without reading it all: its answer says why
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        first = proc.stdout.readline(FILES_HEADER_LIMIT * 4)
        if not first.endswith(b'\n'):
            proc.wait()
            errors.seek(0)
            tail = server_text(errors.read(1000).decode('utf-8', 'replace'))
            raise RuntimeError('%s failed (%s); outcome may be uncertain. %s' % (label, proc.returncode, tail))
        try:
            answer = json.loads(first.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('Invalid endpoint response; inspect state before retrying.') from None
        if not isinstance(answer, dict) or not isinstance(answer.get('returncode'), int):
            raise RuntimeError('Invalid endpoint response; inspect state before retrying.')
        if sink is not None and answer['returncode'] == 0 and type(answer.get('frame_bytes')) is int:
            want = answer['frame_bytes']
            # The answer says how many bytes follow: the time to receive them is theirs, not the flat TIMEOUT.
            timer.cancel()
            timer = threading.Timer(TIMEOUT + want / 16384.0, proc.kill)
            timer.daemon = True
            timer.start()
            while received < want:
                chunk = proc.stdout.read(min(FILES_STEP, want - received))
                if not chunk:
                    break
                sink(chunk)
                received += len(chunk)
        return answer, received
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for handle in (proc.stdin, proc.stdout, errors):
            try:
                handle.close()
            except OSError:
                pass

def _files_refusal(answer):
    text = server_text((answer.get('stderr') or '').strip()) or 'The server refused the request.'
    sys.stderr.write(text + '\n')
    return 1

def _files_parser():
    parser = argparse.ArgumentParser(prog='files', add_help=True)
    sub = parser.add_subparsers(dest='verb', required=True)
    lister = sub.add_parser('list');lister.add_argument('task')
    getter = sub.add_parser('get');getter.add_argument('task');getter.add_argument('sha256')
    getter.add_argument('--out', required=True, help='an existing folder (not a link) to write the file in')
    getter.add_argument('--force', action='store_true', help='replace a regular file of the same name')
    putter = sub.add_parser('put');putter.add_argument('task');putter.add_argument('path')
    putter.add_argument('--direction', required=True, choices=('to-worker', 'from-worker'))
    putter.add_argument('--note');putter.add_argument('--media-type', dest='media_type')
    return parser


def _files_command(args, config, project, actor):
    a = _files_parser().parse_args(args)
    if not isinstance(actor, str) or not ACTOR.fullmatch(actor):
        raise ValueError('Supply a short contributor/session actor')
    header = {'action': 'file-' + a.verb, 'project': project, 'actor': actor, 'task': a.task}
    if a.verb == 'list':
        answer, _ = _frame_exchange(config, header)
        if answer['returncode'] != 0:return _files_refusal(answer)
        sys.stdout.write(answer['stdout']);sys.stderr.write(answer.get('stderr') or '')
        return 0
    if a.verb == 'put':
        status = os.stat(a.path)
        import stat as stat_module
        if not stat_module.S_ISREG(status.st_mode) or os.path.islink(a.path):
            raise ValueError('%s is not a regular file; nothing was sent.' % a.path)
        digest = hashlib.sha256()
        with open(a.path, 'rb') as source:
            size = 0
            for chunk in iter(lambda: source.read(FILES_STEP), b''):
                digest.update(chunk);size += len(chunk)
            header.update(name=_clean_file_name(os.path.basename(a.path)), direction=a.direction, bytes=size,
                          sha256=digest.hexdigest())
            if a.note is not None:header['note'] = a.note
            if a.media_type is not None:header['media_type'] = a.media_type
            source.seek(0)
            answer, _ = _frame_exchange(config, header, body=(source, size))
        if answer['returncode'] != 0:return _files_refusal(answer)
        sys.stdout.write(answer['stdout']);sys.stderr.write(answer.get('stderr') or '')
        return 0
    return _files_get(a, config, header)

def _files_get(a, config, header):
    if not SHA256_TEXT.fullmatch(a.sha256):
        raise ValueError('The file is named by its 64 hexadecimal sha256; nothing was asked for.')
    folder = a.out
    if _is_link(folder) or not os.path.isdir(folder):
        raise ValueError('--out must be an existing folder and not a link; nothing was asked for.')
    header['sha256'] = a.sha256
    temporary = os.path.join(folder, '.orchestra-get-%d-%s.part' % (os.getpid(), a.sha256[:12]))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(temporary, flags, 0o600)
    digest = hashlib.sha256()
    kept = False
    try:
        with os.fdopen(descriptor, 'wb') as target:
            def sink(chunk):
                digest.update(chunk);target.write(chunk)
            answer, received = _frame_exchange(config, header, sink=sink)
        if answer['returncode'] != 0:return _files_refusal(answer)
        try:
            item = json.loads(answer['stdout'])
        except ValueError:
            raise RuntimeError('Invalid endpoint response; inspect state before retrying.') from None
        name = _clean_file_name(str(item.get('name', '')), for_disk=True)
        if (type(answer.get('frame_bytes')) is not int or received != answer['frame_bytes']
                or item.get('bytes') != received or digest.hexdigest() != a.sha256
                or item.get('sha256') != a.sha256):
            raise RuntimeError('The file did not arrive whole and as named (size or sha256 differs); nothing was kept.')
        final = os.path.join(folder, name)
        if _is_link(final):
            raise ValueError('%s is a link; it is never replaced. Nothing was kept.' % final)
        if a.force and os.path.lexists(final):
            import stat as stat_module
            if not stat_module.S_ISREG(os.lstat(final).st_mode):
                raise ValueError('%s is not a regular file; it is not replaced. Nothing was kept.' % final)
            os.replace(temporary, final)
        else:
            try:
                os.link(temporary, final)
            except FileExistsError:
                raise ValueError('%s exists; use --force to replace it. Nothing was kept.' % final) from None
            except OSError:
                if os.path.lexists(final):
                    raise ValueError('%s exists; use --force to replace it. Nothing was kept.' % final) from None
                os.rename(temporary, final)
        kept = True
        sys.stdout.write(json.dumps({'path': final, 'sha256': a.sha256, 'bytes': received}, ensure_ascii=False) + '\n')
        sys.stderr.write(answer.get('stderr') or '')
        return 0
    finally:
        if not kept or os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass

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
    if ((not a.config or not a.project) and args[0] in contributor_command_names()
            and help_requested(args[1:])):
        sys.stdout.write(contributor_command_help(args[0]))
        return 0
    if not a.config or not a.project:
        p.error('the following arguments are required: --config, --project')
    # A command outside the contributor interface is answered, not refused with a pointer at the
    # operator's tool: the endpoint refuses it too, and this is the same answer without the round
    # trip (kittrial-5bb.283). Every name a contributor may type is in the table, including the
    # native subset the client passes through.
    if args[0] not in contributor_command_names():
        print(server_text(contributor_help_text(args[0])), file=sys.stderr)
        return 1
    if args[0]=='delivery':
        return _delivery_command()
    # wait blocks HERE, in the client, and polls the endpoint's wait read (kittrial-5bb.285);
    # the endpoint is therefore asked per poll, never for one blocking call, so an agent over
    # HTTP can use the same read.
    config_doc = json.loads(Path(a.config).read_text())
    if args[0]=='wait':
        return _wait_command(args[1:],config_doc,a.project,a.actor,a.out,
                             config_path=a.config)
    if args[0]=='files':
        return _files_command(args[1:],json.loads(Path(a.config).read_text()),a.project,a.actor)
    action='bd';path=None
    if args[:1]==['refresh']:action='refresh';args=[]
    elif args[:1]==['view']:
        action='view';path=args[1] if len(args)>1 else 'CURRENT.md';args=[]
    elif args[0] in CLIENT_ACTIONS:action=args.pop(0)
    result=request(config_doc,a.project,a.actor,args,action,path)
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
