#!/usr/bin/env python3
"""Authenticated HTTP adapter for the office transport.

This is the ``.19`` disposable service from ``docs/HTTP_TRANSPORT_DESIGN.md``. It
implements the reviewed route matrix as JSON over HTTP, with every authorization
decision taken here (never in the UI), bounded request bodies and attachments,
uniform clean JSON errors, ``Cache-Control: no-store``, request-id propagation and
idempotent mutations.

The canonical task/checkpoint/review semantics live behind a :class:`CanonicalBackend`
seam. :class:`InProcessBackend` is a disposable implementation used by the local HTTP
contract tests; the Linux deployment binds the same method names to ``endpoint.py``
(see :class:`EndpointBackend`) so that no HTTP route talks to storage directly.

Standard library only. Plaintext binding is refused unless the interface is loopback,
and TLS termination at the service itself is supported with ``--cert``/``--key``.
"""
import argparse
import base64
import binascii
import hashlib
import json
import re
import secrets
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from http_auth import (HttpError, Principal, Service, Store, conflict, forbidden, invalid,
                       not_found, now_iso, request_hash, unauthenticated, uncertain,
                       unsupported)

KIT_VERSION = '0.1.0'
MAX_BODY_BYTES = 262144
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 65536
MAX_ATTACHMENT_TOTAL = 262144
MAX_FILENAME = 128
MAX_PAGE = 100
DEFAULT_PAGE = 50
MAX_CURSOR = 512
IDEMPOTENCY_HEADER = 'Idempotency-Key'
ATTACHMENT_MEDIA_TYPES = ('text/plain', 'text/markdown')
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
IDEMPOTENCY_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$')
CURSOR = re.compile(r'^[A-Za-z0-9_-]{1,%d}$' % MAX_CURSOR)
LOOPBACK = ('127.0.0.1', '::1', 'localhost')


# ------------------------------------------------------------------- attachments
def validate_attachments(raw):
    """Bound and normalize the optional attachment list; reject traversal and overload."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise invalid('attachments must be a list')
    if len(raw) > MAX_ATTACHMENTS:
        raise HttpError(413, 'payload_too_large', 'Too many attachments (max %d)' % MAX_ATTACHMENTS)
    total = 0
    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            raise invalid('Each attachment must be an object')
        name = item.get('name')
        media_type = item.get('media_type', 'text/plain')
        content = item.get('content_base64')
        if not isinstance(name, str) or not name or len(name) > MAX_FILENAME or \
                name != name.replace('\\', '/').split('/')[-1] or name in ('.', '..') or \
                any(ord(c) < 32 for c in name):
            raise invalid('Unsafe attachment filename')
        if media_type not in ATTACHMENT_MEDIA_TYPES:
            raise invalid('Unsupported attachment media type')
        if not isinstance(content, str):
            raise invalid('Attachment content must be base64 text')
        try:
            decoded = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise invalid('Attachment content is not valid base64')
        if len(decoded) > MAX_ATTACHMENT_BYTES:
            raise HttpError(413, 'payload_too_large', 'Attachment exceeds the per-file limit')
        total += len(decoded)
        if total > MAX_ATTACHMENT_TOTAL:
            raise HttpError(413, 'payload_too_large', 'Attachments exceed the total limit')
        normalized.append({'name': name, 'media_type': media_type, 'bytes': len(decoded),
                           'sha256': hashlib.sha256(decoded).hexdigest()})
    return normalized


# ------------------------------------------------------------------ cursors
def cursor_scope(query):
    """The part of the query a cursor is bound to: the page size, not the cursor itself.

    Hashing the whole query would bind the cursor to itself and make every second
    page fail as stale.
    """
    return {key: value for key, value in query.items() if key != 'cursor'}


def make_cursor(principal, project_id, query, offset):
    raw = json.dumps({'u': principal.user_id, 'p': project_id,
                      'q': request_hash(cursor_scope(query))[:16], 'o': offset},
                     separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def read_cursor(principal, project_id, query, cursor):
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
        raise conflict('Invalid cursor')
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')))
    except (ValueError, binascii.Error, UnicodeError):
        raise conflict('Invalid cursor')
    if not isinstance(data, dict) or data.get('u') != principal.user_id or \
            data.get('p') != project_id or \
            data.get('q') != request_hash(cursor_scope(query))[:16] or \
            not isinstance(data.get('o'), int) or data['o'] < 0:
        raise conflict('Cursor is stale or belongs to a different query')
    return data['o']


# ------------------------------------------------------------------ backend seam
class UncertainOutcome(Exception):
    """A canonical mutation may have committed but its result was not observed."""


class InProcessBackend:
    """Disposable canonical operations used for local validation.

    Every ``invoke`` is idempotent for a (principal, project, route, key) tuple: the
    committed result is stored and returned on an exact retry, so an uncertain
    response can be reconciled without duplicating a record.
    """

    ROUTES = ('tasks.create', 'tasks.update', 'tasks.claim', 'checkpoints.add',
              'reviews.add', 'feedback.add', 'projects.create', 'projects.archive',
              'members.set', 'members.remove', 'credentials.issue', 'credentials.revoke')

    def __init__(self, service):
        self.service = service
        self.faults = {}

    def fail_next(self, route, times=1):
        """Test hook: commit, then raise :class:`UncertainOutcome` for the next call(s)."""
        self.faults[route] = times

    @property
    def state(self):
        return self.service.state['canonical']

    def _results(self):
        return self.state.setdefault('results', {})

    def _result_key(self, principal, project_id, route, key):
        return request_hash({'u': principal.user_id, 'p': project_id, 'r': route, 'k': key})

    def invoke(self, route, principal, project_id, payload, key):
        if route not in self.ROUTES:
            raise ValueError('Unknown canonical route %r' % (route,))
        result_key = self._result_key(principal, project_id, route, key) if key else None
        if result_key and result_key in self._results():
            return self._results()[result_key]
        result = self._dispatch(route, principal, project_id, payload)
        if result_key:
            self._results()[result_key] = result
        self.service.store.save()
        if self.faults.get(route, 0) > 0:
            self.faults[route] -= 1
            raise UncertainOutcome()
        return result

    # -- canonical operations --------------------------------------------------
    def _dispatch(self, route, principal, project_id, payload):
        handler = {
            'tasks.create': self._task_create,
            'tasks.update': self._task_update,
            'tasks.claim': self._task_claim,
            'checkpoints.add': self._checkpoint_add,
            'reviews.add': self._review_add,
            'feedback.add': self._feedback_add,
        }.get(route)
        if handler is None:
            raise invalid('Backend route is not implemented for HTTP mapping')
        return handler(principal, project_id, payload)

    def _event(self, project_id, task_id, action, principal, actor=None):
        events = self.state.setdefault('events', [])
        events.append({'time': now_iso(self.service._now()), 'project': project_id,
                       'task': task_id, 'action': action, 'user_id': principal.user_id,
                       'actor': actor or principal.actor})
        if len(events) > 5000:
            del events[:len(events) - 5000]

    def _task(self, project_id, task_id):
        task = self.state['tasks'].get(task_id)
        if task is None or task['project_id'] != project_id:
            raise not_found('Task not found')
        return task

    def _task_create(self, principal, project_id, payload):
        title = payload.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise invalid('Task title must be 1-200 characters')
        description = payload.get('description') or ''
        if not isinstance(description, str) or len(description) > 20000:
            raise invalid('Task description is too long')
        task_id = 'task_' + secrets.token_hex(6)
        task = {'id': task_id, 'project_id': project_id, 'title': title.strip(),
                'description': description, 'status': 'open', 'assignee': None,
                'version': 1, 'created_by': principal.user_id,
                'created_at': now_iso(self.service._now()),
                'attachments': payload.get('attachments') or []}
        self.state['tasks'][task_id] = task
        self.state.setdefault('checkpoints', {})[task_id] = []
        self.state.setdefault('contributions', {})[task_id] = []
        self._event(project_id, task_id, 'task-created', principal)
        return dict(task)

    def _task_update(self, principal, project_id, payload):
        task = self._task(project_id, payload.get('task_id'))
        version = payload.get('version')
        if not isinstance(version, int) or version != task['version']:
            raise conflict('Task was modified; reread it before updating',
                           {'expected_version': task['version']})
        for field in ('title', 'description', 'status'):
            if field in payload and payload[field] is not None:
                value = payload[field]
                if field == 'title' and (not isinstance(value, str) or not value.strip() or len(value) > 200):
                    raise invalid('Task title must be 1-200 characters')
                if field == 'description' and (not isinstance(value, str) or len(value) > 20000):
                    raise invalid('Task description is too long')
                if field == 'status' and value not in ('open', 'closed'):
                    raise invalid('Task status must be open or closed')
                task[field] = value.strip() if isinstance(value, str) else value
        task['version'] += 1
        self._event(project_id, task['id'], 'task-updated', principal)
        return dict(task)

    def _task_claim(self, principal, project_id, payload):
        task = self._task(project_id, payload.get('task_id'))
        if task['status'] != 'open':
            raise conflict('Task is not open')
        if task['assignee'] is not None:
            raise conflict('Task is already claimed')
        task['assignee'] = payload.get('actor') or principal.actor
        task['version'] += 1
        self._event(project_id, task['id'], 'task-claimed', principal, task['assignee'])
        return dict(task)

    def _checkpoint_add(self, principal, project_id, payload):
        task = self._task(project_id, payload.get('task_id'))
        checkpoints = self.state.setdefault('checkpoints', {}).setdefault(task['id'], [])
        latest = checkpoints[-1]['id'] if checkpoints else None
        if payload.get('previous') != latest:
            raise conflict('Checkpoint previous cursor is stale',
                           {'expected_previous': latest})
        summary = payload.get('summary')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1000:
            raise invalid('Checkpoint summary must be 1-1000 characters')
        open_items = payload.get('open_items') or []
        if not isinstance(open_items, list) or len(open_items) > 20 or \
                any(not isinstance(i, dict) or not isinstance(i.get('text'), str) or
                    len(i['text']) > 400 for i in open_items):
            raise invalid('Checkpoint open_items are invalid')
        record = {'id': 'chk_' + secrets.token_hex(6), 'task_id': task['id'],
                  'previous': latest, 'summary': summary.strip(),
                  'activity_cursor': payload.get('activity_cursor'),
                  'actor': payload.get('actor') or principal.actor,
                  'open_items': open_items,
                  'created_at': now_iso(self.service._now())}
        checkpoints.append(record)
        task['version'] += 1
        self._event(project_id, task['id'], 'checkpoint-added', principal, record['actor'])
        return {'checkpoint': record, 'task_version': task['version']}

    def _review_add(self, principal, project_id, payload):
        task = self._task(project_id, payload.get('task_id'))
        operation = payload.get('operation')
        if operation not in ('contribute', 'request-changes', 'approve'):
            raise invalid('Review operation must be contribute, request-changes or approve')
        contributions = self.state.setdefault('contributions', {}).setdefault(task['id'], [])
        if operation == 'contribute':
            commit = payload.get('commit')
            base_commit = payload.get('base_commit')
            digest = payload.get('bundle_sha256')
            summary = payload.get('summary')
            if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise invalid('commit must be a full 40-character lowercase SHA')
            if not isinstance(base_commit, str) or not re.fullmatch(r'[0-9a-f]{40}', base_commit):
                raise invalid('base_commit must be a full 40-character lowercase SHA')
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise invalid('bundle_sha256 must be a 64-character lowercase hex digest')
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
                raise invalid('Contribution summary must be 1-1200 characters')
            record = {'id': 'con_' + secrets.token_hex(6), 'task_id': task['id'],
                      'kind': 'contribution', 'commit': commit, 'base_commit': base_commit,
                      'bundle_sha256': digest, 'summary': summary.strip(),
                      'actor': payload.get('actor') or principal.actor,
                      'created_at': now_iso(self.service._now())}
            contributions.append(record)
            task['review_state'] = 'awaiting-review'
            task['version'] += 1
            self._event(project_id, task['id'], 'contribution', principal, record['actor'])
            return {'contribution': record, 'task_version': task['version']}
        if not contributions:
            raise conflict('There is no contribution to review')
        record = {'id': 'rev_' + secrets.token_hex(6), 'task_id': task['id'], 'kind': operation,
                  'contribution_id': contributions[-1]['id'],
                  'summary': (payload.get('summary') or '')[:1200],
                  'actor': payload.get('actor') or principal.actor,
                  'created_at': now_iso(self.service._now())}
        contributions.append(record)
        task['review_state'] = 'changes-requested' if operation == 'request-changes' else 'approved'
        task['version'] += 1
        self._event(project_id, task['id'], operation, principal, record['actor'])
        return {'review': record, 'task_version': task['version']}

    def _feedback_add(self, principal, project_id, payload):
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise invalid('Feedback text must be 1-2000 characters')
        feedback = self.state.setdefault('feedback', {}).setdefault(project_id, [])
        record = {'id': 'fbk_' + secrets.token_hex(6), 'project_id': project_id,
                  'text': text.strip(), 'source_task': payload.get('source_task'),
                  'evidence': payload.get('evidence'),
                  'actor': payload.get('actor') or principal.actor,
                  'user_id': principal.user_id, 'created_at': now_iso(self.service._now())}
        feedback.append(record)
        self._event(project_id, None, 'feedback-added', principal, record['actor'])
        return {'feedback': record}

    # -- reads (no canonical mutation) ----------------------------------------
    def list_tasks(self, project_id, limit, offset):
        tasks = [t for t in self.state['tasks'].values() if t['project_id'] == project_id]
        tasks.sort(key=lambda t: t['id'])
        return {'items': [dict(t) for t in tasks[offset:offset + limit]],
                'total': len(tasks)}

    def get_task(self, project_id, task_id):
        return dict(self._task(project_id, task_id))

    def task_history(self, project_id, task_id, limit, offset):
        self._task(project_id, task_id)
        events = [e for e in self.state.get('events', [])
                  if e.get('project') == project_id and e.get('task') == task_id]
        return {'items': events[offset:offset + limit], 'total': len(events)}

    def list_feedback(self, project_id, limit, offset):
        items = self.state.get('feedback', {}).get(project_id, [])
        return {'items': items[offset:offset + limit], 'total': len(items)}


class EndpointBackend:
    """Production seam: map authorized HTTP operations onto canonical ``endpoint.py``.

    The Linux service runs ``endpoint.py`` as the same-account canonical process. This
    adapter is intentionally thin: it is constructed with the interpreter, endpoint
    path and runtime root, and the caller supplies the already-authorized project and
    actor. It is not exercised by the portable test suite.
    """

    def __init__(self, python, endpoint, root, runner=None):
        self.python, self.endpoint, self.root = python, endpoint, root
        self.runner = runner

    def call(self, project, actor, action, args, attachments=None):
        import subprocess
        payload = {'project': project, 'actor': actor, 'action': action,
                   'args': args, 'attachments': attachments or {}}
        argv = [self.python, self.endpoint, '--root', self.root]
        completed = subprocess.run(argv, input=json.dumps(payload), text=True,
                                   encoding='utf-8', capture_output=True, timeout=150)
        if completed.returncode:
            raise uncertain('Canonical endpoint failed; outcome may be unknown')
        return json.loads(completed.stdout)


# ------------------------------------------------------------------ HTTP adapter
class RouteContext:
    __slots__ = ('principal', 'payload', 'query', 'params', 'request_id',
                 'idempotency_key', 'body_hash', 'auth_source', 'secure')

    def __init__(self, principal, payload, query, params, request_id, idempotency_key,
                 body_hash, auth_source, secure):
        self.principal = principal
        self.payload = payload
        self.query = query
        self.params = params
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.body_hash = body_hash
        self.auth_source = auth_source
        self.secure = secure


ROUTES = []


def route(method, pattern, *, anonymous=False, csrf=True):
    compiled = re.compile('^' + pattern + '$')

    def register(handler):
        ROUTES.append((method, compiled, handler.__name__, anonymous, csrf))
        return handler
    return register


class ApiHandler(BaseHTTPRequestHandler):
    """One request per call. Class attributes are supplied by :func:`build_handler`."""

    service = None
    backend = None
    trust_proxy = False
    max_body = MAX_BODY_BYTES
    protocol_version = 'HTTP/1.1'
    server_version = 'OrchestraHTTP/' + KIT_VERSION
    sys_version = ''

    # -- plumbing --------------------------------------------------------------
    def log_message(self, fmt, *args):  # never log query strings or headers
        pass

    def _dispatch(self, method):
        request_id = self._request_id()
        self._current_request_id = request_id
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=False).items()}
            match = None
            for verb, pattern, name, anonymous, csrf in ROUTES:
                if verb != method:
                    continue
                candidate = pattern.match(path)
                if candidate:
                    match = (name, anonymous, csrf, candidate.groupdict())
                    break
            if match is None:
                raise not_found('No such operation')
            name, anonymous, csrf, params = match
            payload, auth_source = self._read_request(method, anonymous, params, csrf,
                                                      request_id)
            ctx = RouteContext(self._principal, payload, query, params, request_id,
                               self._idempotency_key(), self._body_hash, auth_source,
                               self._is_secure())
            status, response = getattr(self, name)(ctx)
            self._send_json(status, response)
        except HttpError as error:
            self._note_denied(error, request_id)
            self._send_json(error.status, error.body(request_id))
        except Exception:
            # No traceback, no internal detail: a clean, generic JSON error.
            self._send_json(500, {'error': {'code': 'internal_error',
                                            'message': 'Internal error'},
                                  'request_id': request_id})

    do_GET = lambda self: self._dispatch('GET')
    do_POST = lambda self: self._dispatch('POST')
    do_PUT = lambda self: self._dispatch('PUT')
    do_PATCH = lambda self: self._dispatch('PATCH')
    do_DELETE = lambda self: self._dispatch('DELETE')
    do_HEAD = lambda self: self._dispatch('HEAD')

    def _request_id(self):
        supplied = self.headers.get('X-Request-Id')
        if isinstance(supplied, str) and REQUEST_ID.fullmatch(supplied):
            return supplied
        return 'req_' + secrets.token_hex(8)

    def _is_secure(self):
        return getattr(self.connection, 'cipher', None) is not None or not self._peer_is_loopback()

    def _peer_is_loopback(self):
        try:
            return self.client_address[0] in LOOPBACK
        except (IndexError, TypeError):
            return False

    def _source(self):
        if self.trust_proxy:
            forwarded = self.headers.get('X-Forwarded-For')
            if isinstance(forwarded, str) and forwarded:
                candidate = forwarded.split(',')[-1].strip()
                if REQUEST_ID.fullmatch(candidate):
                    return candidate
        return self.client_address[0] if self.client_address else 'unknown'

    def _idempotency_key(self):
        key = self.headers.get(IDEMPOTENCY_HEADER)
        if key is None:
            return None
        if not IDEMPOTENCY_KEY.fullmatch(key):
            raise invalid('Idempotency-Key must be 8-128 URL-safe characters')
        return key

    def _read_request(self, method, anonymous, params, csrf, request_id):
        body = self._read_body()
        payload = None
        if body:
            content_type = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if content_type != 'application/json':
                raise unsupported('Content-Type must be application/json')
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise invalid('Request body is not valid JSON')
            if not isinstance(payload, dict):
                raise invalid('Request body must be a JSON object')
        self._body_hash = request_hash(payload) if payload is not None else request_hash(None)
        self._principal = None
        self._set_cookie_token = None
        auth_source = None
        if not anonymous:
            self._principal, auth_source = self._authenticate()
            if csrf and method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                if auth_source == 'cookie':
                    supplied = self.headers.get('X-CSRF-Token')
                    if not isinstance(supplied, str) or not secrets.compare_digest(
                            supplied, self._principal.csrf or ''):
                        raise forbidden('CSRF token missing or invalid')
        return payload, auth_source

    def _read_body(self):
        length_header = self.headers.get('Content-Length')
        if length_header is None:
            return b''
        try:
            length = int(length_header)
        except ValueError:
            raise invalid('Invalid Content-Length')
        if length < 0:
            raise invalid('Invalid Content-Length')
        if length > self.max_body:
            raise HttpError(413, 'payload_too_large', 'Request body exceeds the configured limit')
        if length == 0:
            return b''
        return self.rfile.read(length)

    def _authenticate(self):
        header = self.headers.get('Authorization')
        token = None
        source = None
        if isinstance(header, str) and header.startswith('Bearer '):
            token, source = header[7:].strip(), 'bearer'
        if not token:
            cookie = self.headers.get('Cookie') or ''
            for part in cookie.split(';'):
                name, _, value = part.strip().partition('=')
                if name == 'orchestra_session' and value:
                    token, source = value, 'cookie'
                    break
        if not token:
            raise unauthenticated()
        return self.service.authenticate(token, source=self._source()), source

    def _note_denied(self, error, request_id):
        if error.status not in (401, 403):
            return
        principal = getattr(self, '_principal', None)
        try:
            with self.service.store.lock:
                self.service.audit(request_id, principal, 'authorization', 'denied',
                                   reason=error.code)
                self.service.store.save()
        except Exception:
            pass

    def _send_json(self, status, body):
        # 204/304 are bodyless by definition; sending a body would desynchronize an
        # HTTP/1.1 keep-alive connection.
        payload = b'' if status in (204, 304) else \
            (json.dumps(body, ensure_ascii=False) + '\n').encode('utf-8')
        self.send_response(status)
        if payload:
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Request-Id', getattr(self, '_current_request_id', '') or '')
        if getattr(self, '_set_cookie_token', None):
            self._send_cookie(self._set_cookie_token)
        self.end_headers()
        if payload and self.command != 'HEAD':
            self.wfile.write(payload)

    def _send_cookie(self, token):
        attributes = 'orchestra_session=%s; Path=/; HttpOnly; SameSite=Strict' % token
        if self._is_secure():
            attributes += '; Secure'
        self.send_header('Set-Cookie', attributes)

    # -- mutation helper -------------------------------------------------------
    def _mutate(self, ctx, route_name, project_id, fn, *, status=200, idempotent=True,
                replay_status=None):
        key = ctx.idempotency_key if idempotent else None
        digest = None
        if key is not None:
            outcome = self.service.idempotency_check(ctx.principal, project_id, route_name,
                                                     key, ctx.body_hash)
            if outcome is not None:
                kind, stored_status, response = outcome
                if kind == 'replay':
                    # An exact retry must not re-deliver a one-time secret; the caller
                    # marks that with replay_status (200 metadata-only for issue routes).
                    return (replay_status or stored_status), response
                # 'unknown': reconcile by re-invoking the idempotent backend.
            digest = self.service.idempotency_begin(ctx.principal, project_id, route_name,
                                                    key, ctx.body_hash)
        try:
            public, stored = fn()
        except UncertainOutcome:
            self.service.idempotency_unknown(digest)
            self.service.audit(ctx.request_id, ctx.principal, route_name, 'unknown',
                               project_id=project_id, reason='uncertain')
            self.service.store.save()
            raise uncertain('The operation may have committed; reconcile with the same '
                            'idempotency key')
        except HttpError as error:
            self.service.idempotency_release(digest)
            self.service.audit(ctx.request_id, ctx.principal, route_name,
                               'denied' if error.status in (401, 403) else 'rejected',
                               project_id=project_id, reason=error.code)
            self.service.store.save()
            raise
        except Exception:
            self.service.idempotency_release(digest)
            raise
        self.service.idempotency_commit(digest, status, stored)
        self.service.audit(ctx.request_id, ctx.principal, route_name, 'committed',
                           project_id=project_id)
        self.service.store.save()
        return status, public

    def _project(self, ctx, minimum='viewer'):
        return self.service.require_project(ctx.principal, ctx.params['pid'], minimum)

    def _task_payload(self, ctx):
        payload = dict(ctx.payload or {})
        payload['task_id'] = ctx.params['tid']
        return payload

    # -- session and account routes -------------------------------------------
    @route('POST', r'/v1/sessions', anonymous=True, csrf=False)
    def sessions_create(self, ctx):
        payload = ctx.payload or {}
        result = self.service.login(payload.get('username'), payload.get('password'),
                                    source=self._source(), request_id=ctx.request_id)
        self._set_cookie_token = result['session_token']
        self._current_request_id = ctx.request_id
        return 201, {'session': {'token': result['session_token'],
                                 'csrf_token': result['csrf_token'],
                                 'expires_at': result['expires_at'],
                                 'idle_expires_at': result['idle_expires_at']},
                     'user': result['user']}

    @route('GET', r'/v1/sessions/current')
    def sessions_whoami(self, ctx):
        principal = ctx.principal
        return 200, {'user': {'id': principal.user_id, 'display_name': principal.display_name,
                              'superuser': principal.superuser},
                     'via': principal.via,
                     'credential': principal.credential_id,
                     'project': principal.credential_project}

    @route('DELETE', r'/v1/sessions/current')
    def sessions_delete(self, ctx):
        return 204, self.service.logout(ctx.principal, request_id=ctx.request_id)

    @route('POST', r'/v1/accounts')
    def accounts_create(self, ctx):
        payload = ctx.payload or {}
        return self._mutate(ctx, 'accounts.create', None,
                            lambda: self._account_created(ctx, payload), status=201,
                            idempotent=bool(ctx.idempotency_key))

    def _account_created(self, ctx, payload):
        user = self.service.create_user(ctx.principal, payload.get('username'),
                                        payload.get('display_name'))
        return user, user

    @route('GET', r'/v1/accounts')
    def accounts_list(self, ctx):
        return 200, {'items': self.service.list_users(ctx.principal)}

    @route('POST', r'/v1/accounts/(?P<uid>[A-Za-z0-9_]+)/password')
    def account_password(self, ctx):
        payload = ctx.payload or {}

        def change():
            result = self.service.change_password(ctx.principal, ctx.params['uid'],
                                                  payload.get('current_password'),
                                                  payload.get('new_password'))
            return result, result
        return self._mutate(ctx, 'accounts.password', None, change,
                            idempotent=bool(ctx.idempotency_key))

    @route('POST', r'/v1/accounts/(?P<uid>[A-Za-z0-9_]+)/reset')
    def account_reset(self, ctx):
        def issue():
            result = self.service.issue_reset(ctx.principal, ctx.params['uid'],
                                              request_id=ctx.request_id)
            # The reset value is delivered exactly once; an idempotent replay must
            # return metadata only, never the value again.
            stored = {key: value for key, value in result.items() if key != 'reset_value'}
            stored['reset_value_available'] = False
            return result, stored
        return self._mutate(ctx, 'accounts.reset', None, issue, status=201,
                            replay_status=200)

    @route('POST', r'/v1/accounts/(?P<uid>[A-Za-z0-9_]+)/reset/redeem', anonymous=True,
           csrf=False)
    def account_reset_redeem(self, ctx):
        payload = ctx.payload or {}
        result = self.service.redeem_reset(ctx.params['uid'], payload.get('reset_value'),
                                           payload.get('new_password'))
        # Redemption is anonymous but token-authorized: record the account, never the
        # reset value or the new password.
        self.service.audit(ctx.request_id, None, 'accounts.reset.redeem', 'committed',
                           reason='account=%s' % ctx.params['uid'])
        self.service.store.save()
        return 200, result

    @route('POST', r'/v1/accounts/(?P<uid>[A-Za-z0-9_]+)/disable')
    def account_disable(self, ctx):
        return self._mutate(ctx, 'accounts.disable', None,
                            lambda: self._account_disabled(ctx), idempotent=bool(ctx.idempotency_key))

    def _account_disabled(self, ctx):
        result = self.service.disable_user(ctx.principal, ctx.params['uid'])
        return result, result

    # -- project and membership routes ----------------------------------------
    @route('POST', r'/v1/projects')
    def projects_create(self, ctx):
        payload = ctx.payload or {}

        def create():
            project_id = payload.get('project_id')
            result = self.service.create_project(ctx.principal, payload.get('name'), project_id)
            return result, result
        return self._mutate(ctx, 'projects.create', payload.get('project_id'), create,
                            status=201)

    @route('GET', r'/v1/projects')
    def projects_list(self, ctx):
        return 200, {'items': self.service.list_projects(ctx.principal)}

    @route('GET', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)')
    def project_get(self, ctx):
        return 200, self.service.project_view(ctx.principal, ctx.params['pid'])

    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/archive')
    def project_archive(self, ctx):
        def archive():
            result = self.service.archive_project(ctx.principal, ctx.params['pid'])
            return result, result
        return self._mutate(ctx, 'projects.archive', ctx.params['pid'], archive)

    @route('PUT', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/members/(?P<uid>[A-Za-z0-9_]+)')
    def member_set(self, ctx):
        payload = ctx.payload or {}

        def set_member():
            result = self.service.set_member(ctx.principal, ctx.params['pid'],
                                             ctx.params['uid'], payload.get('role'),
                                             request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'members.set:' + ctx.params['uid'], ctx.params['pid'],
                            set_member)

    @route('DELETE', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/members/(?P<uid>[A-Za-z0-9_]+)')
    def member_remove(self, ctx):
        def remove():
            result = self.service.remove_member(ctx.principal, ctx.params['pid'],
                                                ctx.params['uid'],
                                                request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'members.remove:' + ctx.params['uid'], ctx.params['pid'],
                            remove)

    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/worker-credentials')
    def credential_issue(self, ctx):
        payload = ctx.payload or {}

        def issue():
            result = self.service.issue_credential(ctx.principal, ctx.params['pid'],
                                                   label=payload.get('label'),
                                                   scopes=payload.get('scopes'),
                                                   actor=payload.get('actor'),
                                                   request_id=ctx.request_id)
            public = {'operation': 'credentials.issue', 'credential': result}
            stored = {'operation': 'credentials.issue',
                      'credential': {k: v for k, v in result.items() if k != 'secret'},
                      'secret_available': False}
            return public, stored
        return self._mutate(ctx, 'credentials.issue', ctx.params['pid'], issue, status=201,
                            replay_status=200)

    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/worker-credentials/'
                  r'(?P<cid>[A-Za-z0-9_]+)/revoke')
    def credential_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_credential(ctx.principal, ctx.params['pid'],
                                                    ctx.params['cid'],
                                                    request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'credentials.revoke:' + ctx.params['cid'],
                            ctx.params['pid'], revoke, status=204)

    # -- task routes -----------------------------------------------------------
    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks')
    def tasks_create(self, ctx):
        self._project(ctx, 'contributor')
        payload = dict(ctx.payload or {})
        payload['attachments'] = validate_attachments(payload.get('attachments'))

        def create():
            result = self.backend.invoke('tasks.create', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key)
            return result, result
        return self._mutate(ctx, 'tasks.create', ctx.params['pid'], create, status=201)

    @route('PATCH', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks/(?P<tid>[A-Za-z0-9_]+)')
    def tasks_update(self, ctx):
        self._project(ctx, 'contributor')
        payload = self._task_payload(ctx)

        def update():
            result = self.backend.invoke('tasks.update', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key)
            return result, result
        return self._mutate(ctx, 'tasks.update:' + ctx.params['tid'], ctx.params['pid'],
                            update)

    @route('GET', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks')
    def tasks_list(self, ctx):
        self._project(ctx, 'viewer')
        limit, offset = self._page(ctx, ctx.query)
        result = self.backend.list_tasks(ctx.params['pid'], limit, offset)
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             offset + limit)
                                 if offset + limit < result['total'] else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks/(?P<tid>[A-Za-z0-9_]+)')
    def tasks_get(self, ctx):
        self._project(ctx, 'viewer')
        return 200, self.backend.get_task(ctx.params['pid'], ctx.params['tid'])

    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks/(?P<tid>[A-Za-z0-9_]+)/claim')
    def tasks_claim(self, ctx):
        self._project(ctx, 'contributor')
        payload = self._task_payload(ctx)
        actor = self.service.bind_actor(ctx.principal, payload.pop('actor', None))
        payload['actor'] = actor

        def claim():
            result = self.backend.invoke('tasks.claim', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key)
            return result, result
        return self._mutate(ctx, 'tasks.claim:' + ctx.params['tid'], ctx.params['pid'],
                            claim)

    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks/(?P<tid>[A-Za-z0-9_]+)/checkpoints')
    def checkpoints_add(self, ctx):
        self._project(ctx, 'contributor')
        payload = self._task_payload(ctx)
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            result = self.backend.invoke('checkpoints.add', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key)
            return result, result
        return self._mutate(ctx, 'checkpoints.add:' + ctx.params['tid'], ctx.params['pid'],
                            add, status=201)

    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks/(?P<tid>[A-Za-z0-9_]+)/reviews')
    def reviews_add(self, ctx):
        self._project(ctx, 'contributor')
        payload = self._task_payload(ctx)
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            result = self.backend.invoke('reviews.add', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key)
            return result, result
        return self._mutate(ctx, 'reviews.add:' + ctx.params['tid'], ctx.params['pid'],
                            add, status=201)

    @route('GET', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/tasks/(?P<tid>[A-Za-z0-9_]+)/history')
    def tasks_history(self, ctx):
        self._project(ctx, 'viewer')
        limit, offset = self._page(ctx, ctx.query)
        result = self.backend.task_history(ctx.params['pid'], ctx.params['tid'], limit, offset)
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             offset + limit)
                                 if offset + limit < result['total'] else None)
        return 200, result

    # -- feedback and audit ----------------------------------------------------
    @route('POST', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/feedback')
    def feedback_add(self, ctx):
        self._project(ctx, 'contributor')
        payload = dict(ctx.payload or {})
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            result = self.backend.invoke('feedback.add', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key)
            return result, result
        return self._mutate(ctx, 'feedback.add', ctx.params['pid'], add, status=201)

    @route('GET', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/feedback')
    def feedback_list(self, ctx):
        self._project(ctx, 'viewer')
        limit, offset = self._page(ctx, ctx.query)
        result = self.backend.list_feedback(ctx.params['pid'], limit, offset)
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             offset + limit)
                                 if offset + limit < result['total'] else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>[A-Za-z0-9_]+)/audit')
    def audit_list(self, ctx):
        self._project(ctx, 'owner')
        limit, offset = self._page(ctx, ctx.query)
        events = [e for e in self.service.state['audit'] if e.get('project_id') == ctx.params['pid']]
        body = {'items': events[offset:offset + limit], 'total': len(events)}
        body['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                           offset + limit)
                               if offset + limit < len(events) else None)
        return 200, body

    @route('GET', r'/healthz', anonymous=True, csrf=False)
    def healthz(self, ctx):
        return 200, {'status': 'ok'}

    def _page(self, ctx, query):
        limit_raw = query.get('limit', str(DEFAULT_PAGE))
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise invalid('limit must be an integer')
        if not 1 <= limit <= MAX_PAGE:
            raise invalid('limit must be between 1 and %d' % MAX_PAGE)
        offset = read_cursor(ctx.principal, ctx.params.get('pid'), query, query.get('cursor'))
        return limit, offset


def build_handler(service, backend, *, trust_proxy=False, max_body=MAX_BODY_BYTES):
    return type('ConfiguredApiHandler', (ApiHandler,), {
        'service': service, 'backend': backend, 'trust_proxy': trust_proxy,
        'max_body': max_body,
    })


def create_server(service, backend, *, host='127.0.0.1', port=0, trust_proxy=False,
                  max_body=MAX_BODY_BYTES, certfile=None, keyfile=None,
                  allow_plaintext_non_loopback=False):
    """Bind the service. Refuse a non-loopback plaintext listener unless explicitly allowed."""
    loopback = host in LOOPBACK
    if not loopback and certfile is None and not allow_plaintext_non_loopback:
        raise ValueError('Refusing plaintext on a non-loopback interface; supply TLS or '
                         'explicitly allow disposable plaintext')
    httpd = ThreadingHTTPServer((host, port), build_handler(service, backend,
                                                            trust_proxy=trust_proxy,
                                                            max_body=max_body))
    httpd.daemon_threads = True
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(description='Orchestra authenticated HTTP service')
    parser.add_argument('--state', required=True, help='private service state path (outside source)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', help='TLS certificate (PEM)')
    parser.add_argument('--key', help='TLS private key (PEM)')
    parser.add_argument('--trust-proxy', action='store_true',
                        help='trust X-Forwarded-For from the configured reverse proxy')
    parser.add_argument('--max-body', type=int, default=MAX_BODY_BYTES)
    parser.add_argument('--bootstrap-user', help='one-time operator bootstrap superuser')
    args = parser.parse_args(argv)

    store = Store(args.state)
    if args.bootstrap_user:
        import getpass
        password = getpass.getpass('New superuser password: ')
        Service.bootstrap_superuser(store, args.bootstrap_user, password)
        print('Bootstrapped %s' % args.bootstrap_user)
        return 0
    service = Service(store)
    backend = InProcessBackend(service)
    httpd = create_server(service, backend, host=args.host, port=args.port,
                          trust_proxy=args.trust_proxy, max_body=args.max_body,
                          certfile=args.cert, keyfile=args.key)
    print('orchestra-http listening on %s:%d' % (args.host, httpd.server_address[1]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
