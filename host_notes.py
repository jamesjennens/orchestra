"""Private host transport for .447 notes (kittrial-5bb.459).

Only a confined endpoint, with installation-private key access and launcher-bound
identity, signs a request. An unrestricted shell of the service account is inside
the existing kit trust boundary. No network listener or separate note store.
"""
import argparse
from contextlib import contextmanager
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from pathlib import Path

import record_json

DIRECTORY = '.host-notes'
FRAME_MAX = 65536
REPLY_MAX = 1048576
ENROLLMENT_MAX = 1048576
WAIT_MAX = 50
WAIT_INTERVAL = 5
WAITERS_MAX = 16
REQUESTS_MAX = 10000
UNAVAILABLE = 'Host notes unavailable; retry with the same request id. An operator must check the service.'
FORBIDDEN = 'Host coordinator notes are not available to this principal in this project'
OPERATIONS = ('send', 'inbox', 'sent', 'reply', 'noted', 'wait')
PROJECT = re.compile(r'[a-z][a-z0-9]{1,23}\Z')
NOTE = re.compile(r'cnote-[a-f0-9]{24}\Z')
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}\Z')


class Refusal(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def location(root):
    return Path(root) / DIRECTORY


def private_text(path, limit=FRAME_MAX):
    from admin import read_regular_text
    try:
        if path.is_symlink() or (os.name == 'posix' and
                (path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid())):
            raise OSError('private file required')
        return read_regular_text(path, limit=limit)
    except (OSError, ValueError):
        raise Refusal(503, UNAVAILABLE) from None


def read_enrollment(root):
    """Strict, fail-closed metadata; never holds a note or a receipt."""
    from sessions import valid_principal
    directory = location(root)
    if directory.is_symlink() or (directory.exists() and os.name == 'posix' and
            (directory.stat().st_mode & 0o077 or directory.stat().st_uid != os.geteuid())):
        raise Refusal(503, UNAVAILABLE)
    path = directory / 'enrollment.json'
    if not path.exists() and not path.is_symlink():
        return {'schema_version': 1, 'grants': {}, 'audit': []}
    try:
        document = record_json.loads(private_text(path, ENROLLMENT_MAX), object_pairs_hook=unique)
        if not isinstance(document, dict) or set(document) != {'schema_version', 'grants', 'audit'} \
                or type(document['schema_version']) is not int or document['schema_version'] != 1 \
                or not isinstance(document['grants'], dict) or not isinstance(document['audit'], list):
            raise ValueError()
        for project, grants in document['grants'].items():
            if not PROJECT.fullmatch(project) or not isinstance(grants, dict):
                raise ValueError()
            for principal, grant in grants.items():
                valid_principal(principal, 'principal')
                if not isinstance(grant, dict) or set(grant) != {'approved_by', 'approval', 'enrolled_by', 'at'}:
                    raise ValueError()
                if any(not isinstance(v, str) or not v or len(v) > 240 or any(ord(c) < 32 for c in v)
                       for v in grant.values()):
                    raise ValueError()
        if len(document['audit']) > 200 or len(encoded(document)) > ENROLLMENT_MAX:
            raise ValueError()
        for entry in document['audit']:
            if not isinstance(entry, dict) or set(entry) != {'project','principal','actor','operation','approved_by','approval','at'}:
                raise ValueError()
            if entry['operation'] not in ('enroll','revoke') or not isinstance(entry['project'],str) or not PROJECT.fullmatch(entry['project']):
                raise ValueError()
            valid_principal(entry['principal'],'principal')
            for k in ('actor','at'):
                if not isinstance(entry[k],str) or not entry[k] or len(entry[k])>240:
                    raise ValueError()
            for k in ('approved_by','approval'):
                if entry[k] is not None and (not isinstance(entry[k],str) or not entry[k] or len(entry[k])>240):
                    raise ValueError()
        return document
    except (ValueError, TypeError, KeyError):
        raise Refusal(503, UNAVAILABLE) from None


def locked(root):
    from http_authority import file_lock
    directory = location(root)
    if directory.is_symlink() or not directory.is_dir():
        raise Refusal(503, UNAVAILABLE)
    if (directory / 'enrollment.lock').is_symlink():
        raise Refusal(503, UNAVAILABLE)
    return file_lock(directory / 'enrollment.lock', timeout=5)


def enroll(root, project, principal, actor, *, approved_by=None, approval=None, revoke=False):
    """Local operator command only; approval attribution is recorded, not inferred from a name."""
    from admin import operators, atomic_private_write, project_dir
    from keyed_records import require_configured_operator
    from sessions import valid_principal
    from http_authority import server_time
    require_configured_operator(actor, operators(root, strict=True), 'enroll a host coordinator')
    path = project_dir(root, project)
    if not (path / '.beads/metadata.json').is_file():
        raise ValueError('Unknown/uninitialized project')
    if not PROJECT.fullmatch(project):
        raise ValueError('Supply a project id')
    valid_principal(principal, 'principal')
    if not revoke:
        valid_principal(approved_by, 'owner approver')
        if not approved_by.startswith('person:'):
            raise ValueError('Enrollment approval names the person owner')
        for value in (approved_by, approval):
            if not isinstance(value, str) or not value or len(value) > 240 or any(ord(c) < 32 for c in value):
                raise ValueError('Enrollment records the owner approver and approval reference (1..240 plain characters)')
    directory = location(root)
    if directory.is_symlink():
        raise Refusal(503, UNAVAILABLE)
    directory.mkdir(mode=0o700, exist_ok=True)
    if os.name == 'posix' and directory.stat().st_mode & 0o077:
        raise Refusal(503, UNAVAILABLE)
    with locked(root):
        require_configured_operator(actor, operators(root, strict=True), 'enroll a host coordinator')
        document = read_enrollment(root)
        if revoke:
            document['grants'].get(project, {}).pop(principal, None)
        else:
            document['grants'].setdefault(project, {})[principal] = {
                'approved_by': approved_by, 'approval': approval, 'enrolled_by': actor, 'at': server_time()}
            key = directory / 'adapter.key'
            if not key.exists() and not key.is_symlink():
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    handle.write(secrets.token_hex(32))
            read_key(root)
        document['audit'] = (document['audit'] + [{'project': project, 'principal': principal,
                            'actor': actor, 'operation': 'revoke' if revoke else 'enroll',
                            'approved_by': approved_by, 'approval': approval, 'at': server_time()}])[-200:]
        if len(encoded(document)) > ENROLLMENT_MAX:
            raise Refusal(503, UNAVAILABLE)
        atomic_private_write(directory / 'enrollment.json', json.dumps(document))
    return {'project': project, 'principal': principal, 'enrolled': not revoke}


def read_key(root):
    text = private_text(location(root) / 'adapter.key')
    if not re.fullmatch(r'[a-f0-9]{64}', text):
        raise Refusal(503, UNAVAILABLE)
    return bytes.fromhex(text)


def authorize(root, request):
    """The service repeats the launcher's scope/actor and live enrollment checks."""
    from sessions import owners, valid_principal
    from admin import project_dir
    from reserved_comments import refuse_http_actor
    try:
        project, principal, actor, projects = (request[k] for k in ('project', 'principal', 'actor', 'projects'))
        if not isinstance(project, str) or not PROJECT.fullmatch(project) or not isinstance(actor, str) \
                or not IDENTITY.fullmatch(actor) or not isinstance(projects, list) or project not in projects \
                or any(not isinstance(p, str) or not PROJECT.fullmatch(p) for p in projects):
            raise ValueError()
        valid_principal(principal, 'principal')
        refuse_http_actor(actor, False)
        document = read_enrollment(root)
        if principal not in document['grants'].get(project, {}):
            raise ValueError()
        path = project_dir(root, project)
        if not (path / '.beads/metadata.json').is_file() or owners(path).get(actor) != principal:
            raise ValueError()
    except Refusal:
        raise
    except (ValueError, KeyError, TypeError, OSError):
        raise Refusal(403, FORBIDDEN) from None


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal(422, 'Use coordinator notes send|inbox|sent|reply|noted|wait; see docs host-notes')


def parse(args, attachments):
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument('operation', choices=OPERATIONS)
    parser.add_argument('note', nargs='?')
    parser.add_argument('--to')
    parser.add_argument('--request-id')
    parser.add_argument('--since')
    parser.add_argument('--timeout', type=float, default=45)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--json', action='store_true')
    # Local files were already transported by the standalone client. Never open a caller path.
    text = None
    clean = []
    for token in args:
        if token.startswith('@attachment:'):
            item = attachments.get(token.split(':', 1)[1])
            if text is not None or not isinstance(item, dict) or item.get('flag') not in ('--file', '-f') \
                    or not isinstance(item.get('text'), str):
                raise Refusal(422, 'Supply one transported --file with the note text')
            text = item['text']
        else:
            clean.append(token)
    a = vars(parser.parse_args(clean))
    op = a.pop('operation')
    a.pop('json')
    if op in ('send', 'reply', 'noted'):
        if not isinstance(a['request_id'], str) or not re.fullmatch(r'[\x21-\x7e]{1,200}', a['request_id']):
            raise Refusal(422, 'Supply --request-id and reuse it with the identical request after failure')
    if op in ('send', 'reply'):
        if text is None or not text.strip() or len(text) > 4000:
            raise Refusal(422, 'Supply --file with 1..4000 characters of note text')
        if not isinstance(a['to'], str) or not PROJECT.fullmatch(a['to']):
            raise Refusal(422, 'Supply --to PROJECT')
    elif text is not None or a['to'] is not None:
        raise Refusal(422, 'Only send and reply take --to and --file')
    if (op in ('reply', 'noted')) != (a['note'] is not None) or (a['note'] and not NOTE.fullmatch(a['note'])):
        raise Refusal(422, 'Reply and noted name one cnote id')
    if not 0 <= a['timeout'] <= WAIT_MAX or not 0 <= a['offset'] or not 1 <= a['limit'] <= 50:
        raise Refusal(422, 'Wait timeout is 0..50 seconds; offset >=0 and limit 1..50')
    if a['since'] is not None and (not isinstance(a['since'], str) or len(a['since']) > 200):
        raise Refusal(422, 'Supply the returned fingerprint as --since')
    return {'operation': op, 'note': a['note'], 'to': a['to'], 'request_id': a['request_id'],
            'since': a['since'], 'timeout': a['timeout'], 'offset': a['offset'], 'limit': a['limit'], 'text': text}


def unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out: raise ValueError('Duplicate private record field')
        out[k] = v
    return out


def frame(handle, limit=FRAME_MAX):
    line = handle.readline(limit + 1)
    if not line.endswith(b'\n') or len(line) > limit:
        raise Refusal(422, 'Invalid private notes frame')
    try:
        value = record_json.loads(line, object_pairs_hook=unique)
    except ValueError:
        raise Refusal(422, 'Invalid private notes frame') from None
    return value


def peer_frame(handle, limit=FRAME_MAX):
    try:
        return frame(handle, limit)
    except Refusal:
        raise Refusal(503, UNAVAILABLE) from None


def signature(key, challenge, request):
    return hmac.new(key, encoded([challenge, request]), hashlib.sha256).hexdigest()


def call(root, request):
    """Called only after endpoint.launch identity checks, never with JSON-supplied identity."""
    if os.name != 'posix' or not hasattr(socket, 'AF_UNIX'):
        raise Refusal(503, 'Host notes require the Linux coordination service and an updated endpoint')
    try:
        key = read_key(root)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(WAIT_MAX + 10)
            connection.connect(str(location(root) / 'service.sock'))
            with connection.makefile('rwb') as handle:
                greeting = peer_frame(handle)
                if not isinstance(greeting, dict) or set(greeting) != {'challenge', 'proof'} \
                        or not isinstance(greeting['challenge'], str) \
                        or not re.fullmatch(r'[a-f0-9]{64}', greeting['challenge']):
                    raise Refusal(503, UNAVAILABLE)
                if not isinstance(greeting['proof'], str) or not hmac.compare_digest(
                        greeting['proof'], signature(key, 'server-proof', greeting['challenge'])):
                    raise Refusal(503, UNAVAILABLE)
                wire = encoded({'request': request, 'signature': signature(key, greeting['challenge'], request)}) + b'\n'
                if len(wire) > FRAME_MAX:
                    raise Refusal(422, 'Host note request exceeds its limit')
                handle.write(wire); handle.flush()
                answer = peer_frame(handle, REPLY_MAX)
                if not isinstance(answer, dict) or set(answer) not in ({'status', 'result'}, {'status', 'error'}):
                    raise Refusal(503, UNAVAILABLE)
                if answer['status'] != 200:
                    raise Refusal(answer['status'], answer.get('error', UNAVAILABLE))
                return answer['result']
    except Refusal:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal(503, UNAVAILABLE) from None


@contextmanager
def state_change(service):
    """The existing state file is the atomic boundary, including receipt/request metadata."""
    manager = getattr(service, 'host_notes_manager', None)
    if manager is None:
        yield
        return
    depth = getattr(manager.local, 'depth', 0)
    if depth == 0: manager.preflight()
    before = {k: copy.deepcopy(service.state.get(k)) for k in ('coordinator_notes', 'host_note_requests')}
    manager.local.depth = depth + 1
    try:
        yield
    except BaseException:
        from admin import read_regular_text
        try:
            disk = record_json.loads(read_regular_text(service.store.path, limit=32 * 1024 * 1024))
            unchanged = all(disk.get(k) == value for k, value in before.items())
        except Exception:
            unchanged = False
        if unchanged:
            for k, value in before.items():
                if value is None: service.state.pop(k, None)
                else: service.state[k] = value
        raise
    finally:
        manager.local.depth = depth


class HostIdentity:
    """An in-process service object, constructed only after authenticated dispatch."""
    user_id = None
    agent_id = None

    def __init__(self, manager, request):
        self.manager = manager
        self.request = request
        self.actor = request['actor']
        self.principal = request['principal']


class Manager:
    def __init__(self, root, service, pairing):
        self.root, self.service, self.pairing = Path(root), service, pairing
        self.waiters = set()
        self.guard = threading.Lock()
        self.local = threading.local()
        self.stopped = threading.Event()

    def preflight(self):
        from admin import read_regular_text
        try:
            # Cached note text is never a fallback for an unreadable/missing durable store.
            value = record_json.loads(read_regular_text(self.service.store.path, limit=32 * 1024 * 1024))
            if not isinstance(value, dict):
                raise ValueError()
            if any(value.get(k) != self.service.state.get(k) for k in ('coordinator_notes', 'host_note_requests')):
                raise ValueError()
            if self.service.state.get('coordinator_notes') is not None:
                self.service._note_store(write=True)
            requests = self.service.state.get('host_note_requests', {})
            if not isinstance(requests, dict) or len(requests) > REQUESTS_MAX:
                raise ValueError()
            for key, entry in requests.items():
                if not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key) or not isinstance(entry,dict) \
                        or set(entry) != {'digest','note'} or not isinstance(entry['digest'],str) \
                        or not re.fullmatch(r'[0-9a-f]{64}',entry['digest']) or not isinstance(entry['note'],str) \
                        or not NOTE.fullmatch(entry['note']):
                    raise ValueError()
        except (OSError, ValueError, TypeError):
            raise Refusal(503, UNAVAILABLE) from None

    def active(self, project):
        grants = read_enrollment(self.root)['grants'].get(project, {})
        if grants:
            return True
        from http_authority import coordinator_grant, grant_issuer_holds
        state = self.service.state
        return any(project in (agent.get('projects') or []) and agent.get('enabled')
                   and (state.get('memberships') or {}).get(project, {}).get(agent.get('owner')) in ('owner','coordinator')
                   and self.service._may_coordinate(project, agent)
                   and (g := coordinator_grant(state, project, aid)) is not None
                   and grant_issuer_holds(state, project, g)
                   for aid, agent in (state.get('agents') or {}).items() if isinstance(agent, dict))

    def execute(self, request):
        if not isinstance(request, dict) or set(request) != {'project', 'principal', 'actor', 'projects', 'payload'}:
            raise Refusal(422, 'Invalid host notes request')
        payload = request['payload']
        expected = {'operation', 'note', 'to', 'request_id', 'since', 'timeout', 'offset', 'limit', 'text'}
        if not isinstance(payload, dict) or set(payload) != expected or payload.get('operation') not in OPERATIONS:
            raise Refusal(422, 'Invalid host notes operation')
        # Validate again on the authenticated side, not merely in the CLI/parser.
        argv = [payload['operation']]
        if payload['note'] is not None: argv += [payload['note']]
        for field in ('to', 'request_id', 'since', 'timeout', 'offset', 'limit'):
            if payload[field] is not None: argv += ['--' + field.replace('_', '-'), str(payload[field])]
        attachments = {}
        if payload['text'] is not None:
            attachments['0'] = {'flag': '--file', 'text': payload['text']}; argv += ['@attachment:0']
        if parse(argv, attachments) != payload:
            raise Refusal(422, 'Invalid host notes payload')
        if payload['operation'] == 'wait':
            return self.wait(request)
        return self.pass_once(request)

    def pass_once(self, request):
        # Both enrollment and the existing service state are serialized for the whole effect.
        with locked(self.root), self.service.store.lock:
            authorize(self.root, request)
            with state_change(self.service):
                return self.effect(request)

    def effect(self, request):
        op, p, project = request['payload']['operation'], request['payload'], request['project']
        if op == 'wait':
            notes = self.service.unsettled_notes_to([project])
            latest = next((n['id'] for n in reversed(self.service._notes()) if n['to']['project'] == project), None)
            return {'schema': 'host-notes-wait-v1', 'fingerprint': hashlib.sha256(encoded(latest)).hexdigest(),
                    'items': [{'note': n['id'], 'project': project} for n in notes[:50]],
                    'total': len(notes)}
        if op in ('inbox', 'sent'):
            notes, _ = self.service.notes_for(project, 'to' if op == 'inbox' else 'sent')
            if op == 'inbox':
                self.service.mark_notes_fetched(project, notes[p['offset']:p['offset'] + p['limit']])
            return {'project': project, 'box': op, 'items': copy.deepcopy(notes[p['offset']:p['offset'] + p['limit']]),
                    'total': len(notes)}
        key = hashlib.sha256(encoded([request['principal'], project, p['request_id']])).hexdigest()
        digest = hashlib.sha256(encoded(p)).hexdigest()
        journal = self.service.state.get('host_note_requests', {})
        if not isinstance(journal, dict): raise Refusal(503, UNAVAILABLE)
        old = journal.get(key)
        if old is not None:
            if old.get('digest') != digest:
                raise Refusal(409, 'Request id already used with another request')
            note = next((n for n in self.service._notes() if n['id'] == old['note']), None)
            if note is None: raise Refusal(410, 'Request was already handled; its note is no longer retained')
            if op == 'noted':
                matches = (note['id'] == p['note'] and note['to']['project'] == project
                           and note.get('settled') is not None)
            else:
                matches = (note['from']['project'] == project and note['to']['project'] == p['to']
                           and note['text'] == p['text'] and note.get('reply_to') == p['note'])
            if not matches: raise Refusal(503, UNAVAILABLE)
            return {'note': copy.deepcopy(note), 'reconciled': True}
        if len(journal) >= REQUESTS_MAX: raise Refusal(503, UNAVAILABLE)
        identity = HostIdentity(self, request)
        if op == 'noted':
            note = self.service.settle_note(identity, project, p['note'], persist=False)
        else:
            if not self.pairing(project, p['to']):
                raise Refusal(404, 'No permitted coordinator note address')
            unread = self.service.unsettled_notes_to([p['to']])
            if len(unread) >= self.service.NOTES_KEPT:
                raise Refusal(503, 'Coordinator inbox is full; nothing was accepted')
            self.service.note_admit(project, p['to'])
            note, _ = self.service.write_note(identity, project, p['to'], p['text'], p['note'], True,
                                               'No permitted coordinator note address', persist=False)
            receiver = self.active(p['to'])
            note['delivery'] = {'accepted_at': note['created_at'], 'receiver_enrolled': receiver}
            if not receiver: note['delivery']['warning'] = 'No receiver enrolled; accepted does not mean read'
            original = next(n for n in self.service._notes() if n['id'] == note['id'])
            original['delivery'] = dict(note['delivery'])
        journal[key] = {'digest': digest, 'note': note['id']}
        self.service.state['host_note_requests'] = journal
        self.service.store.save()
        return {'note': copy.deepcopy(note), 'reconciled': False}

    def now(self):
        from http_auth import now_iso
        return now_iso(self.service._now())

    def wait(self, request):
        p = request['payload']; identity = (request['principal'], request['project'])
        with locked(self.root): authorize(self.root, request)
        with self.guard:
            if identity in self.waiters or len(self.waiters) >= WAITERS_MAX:
                raise Refusal(429, 'Host notes wait capacity reached; retry later')
            self.waiters.add(identity)
        try:
            deadline = time.monotonic() + p['timeout']
            while True:
                if self.stopped.is_set(): raise Refusal(503, UNAVAILABLE)
                view = self.pass_once(request)
                changed = p['since'] != view['fingerprint']
                if changed or time.monotonic() >= deadline:
                    return dict(view, changed=changed, timed_out=not changed)
                self.stopped.wait(min(WAIT_INTERVAL, max(0, deadline - time.monotonic())))
        finally:
            with self.guard: self.waiters.discard(identity)


def start(root, service, pairing):
    """Opt-in Linux listener. No grant means no listener and no changed worker behavior."""
    if os.name != 'posix' or not hasattr(socket, 'AF_UNIX'):
        raise Refusal(503, 'Host notes listener is Linux-only')
    document = read_enrollment(root)
    if not any(document['grants'].values()): return None
    read_key(root)
    import socketserver
    manager = Manager(root, service, pairing)
    slots = threading.BoundedSemaphore(32)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            from http_auth import HttpError
            self.request.settimeout(5)
            if not slots.acquire(blocking=False): return
            try:
                challenge = secrets.token_hex(32)
                self.wfile.write(encoded({'challenge': challenge, 'proof': signature(read_key(root), 'server-proof', challenge)}) + b'\n'); self.wfile.flush()
                value = frame(self.rfile)
                if not isinstance(value, dict) or set(value) != {'request', 'signature'} \
                        or not isinstance(value['signature'], str) \
                        or not hmac.compare_digest(value['signature'], signature(read_key(root), challenge, value['request'])):
                    raise Refusal(403, FORBIDDEN)
                result = manager.execute(value['request'])
                response = {'status': 200, 'result': result}
            except Refusal as error:
                response = {'status': error.status, 'error': str(error)}
            except HttpError as error:
                said = {403: FORBIDDEN, 404: 'Host note not found', 409: 'Host note conflict; read the current inbox',
                        422: 'Invalid host note; see docs host-notes', 429: 'Host notes rate limit reached; retry later'}
                response = {'status': error.status, 'error': said.get(error.status, UNAVAILABLE)}
            except Exception:
                response = {'status': 503, 'error': UNAVAILABLE}
            finally:
                slots.release()
            try:
                wire = encoded(response) + b'\n'
                if len(wire) > REPLY_MAX:
                    wire = encoded({'status': 503, 'error': 'Host notes page exceeds its limit; request a smaller page'}) + b'\n'
                self.wfile.write(wire); self.wfile.flush()
            except OSError:
                pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        def handle_error(self, *_):
            pass  # Never print a request, note, receipt, traceback or host path.

    import fcntl  # Linux listener only; never imported at module level.
    lease_path = location(root) / 'listener.lock'
    if lease_path.is_symlink(): raise Refusal(503, UNAVAILABLE)
    lease = os.open(lease_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lease)
        raise Refusal(503, 'Host notes listener already running') from None
    path = location(root) / 'service.sock'
    server = None
    try:
        if path.exists() or path.is_symlink():
            st = path.lstat()
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise Refusal(503, UNAVAILABLE)
            path.unlink()  # only the lease holder can replace a crashed listener's private socket
        server = Server(str(path), Handler, bind_and_activate=False)
        server.server_bind(); os.chmod(path, 0o600); server.server_activate()
        service.host_notes_manager = manager
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    except BaseException:
        if server is not None: server.server_close()
        os.close(lease)
        raise

    class Running:
        closed = False
        def close(self):
            if self.closed: return
            self.closed = True
            manager.stopped.set(); server.shutdown(); server.server_close()
            service.host_notes_manager = None
            try: path.unlink()
            except OSError: pass
            os.close(lease)
    return Running()
