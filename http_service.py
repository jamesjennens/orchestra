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
import ipaddress
import json
import re
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from http_auth import (AGENT_SECRET_ENV, CAP_ACCOUNTS_ADMIN, CAP_AGENTS, CAP_APPROVE,
                       CAP_CHECKPOINTS, CAP_FEEDBACK,
                       CAP_PROJECT_ADMIN, CAP_PROJECT_CREATE, CAP_READ, CAP_REVIEWS,
                       CAP_TASKS, RESULT_RETENTION_SECONDS, HttpError, Service, Store,
                       authority_request, conflict, forbidden, invalid, not_found,
                       not_implemented, now_iso, request_hash, unauthenticated,
                       uncertain, unsupported)

KIT_VERSION = '0.1.0'
MAX_BODY_BYTES = 262144
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 65536
MAX_ATTACHMENT_TOTAL = 262144
MAX_FILENAME = 128
MAX_PAGE = 100
DEFAULT_PAGE = 50
MAX_CURSOR = 512
#: Bound on the read-time agent next-action list. Attention is computed from bounded,
#: cached, per-request task reads (one full snapshot per granted project); nothing is
#: scheduled or polled.
AGENT_ACTION_LIMIT = 50
#: Bound on agent attention, expressed as pages of :data:`MAX_PAGE` each, i.e. 20,000
#: tasks for one project. The bound is applied in memory to the single full snapshot
#: (:meth:`EndpointBackend.read_tasks`), not by re-reading the canonical store per
#: page, so a bound that is reached reports ``truncated`` without extra reads.
AGENT_MAX_PAGES = 200
#: Cap on the *claimable* suggestions collected per read. Claimable work is a suggestion
#: list, not the agent's own work, so it is the only list the page-sized cap applies to:
#: an agent's own changes-requested, blocked or awaiting-review task is always collected
#: however many claimable tasks sit beside it.
AGENT_CLAIMABLE_LIMIT = MAX_PAGE
#: Bound on the projects one ``GET /v1/me/work`` read walks (the caller's own
#: memberships, each read once). Reaching it reports ``truncated``.
ME_WORK_MAX_PROJECTS = 50
#: Size bound on the per-server short-lived read cache used by ``GET /v1/me/work``
#: (entries are keyed per principal and project; see ``READ_CACHE_SECONDS``).
READ_CACHE_MAX_ENTRIES = 2048
#: Task-list filters the browser sends. Filtering reads the project's one full
#: snapshot and pages the filtered rows, so the cursor stays exact.
TASK_STATUS_FILTERS = ('active', 'open', 'in_progress', 'blocked', 'closed')
TASK_FILTER_TEXT_MAX = 200
IDEMPOTENCY_HEADER = 'Idempotency-Key'
ATTACHMENT_MEDIA_TYPES = ('text/plain', 'text/markdown')
# One identifier pattern for every route parameter. Canonical Orchestra ids contain
# hyphens and dots (``kittrial-5bb.19``), which the original ``[A-Za-z0-9_]+``
# rejected. The first character must be alphanumeric, so ``.``/``..`` never match.
ID = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}'
SAFE_ID = re.compile(r'^' + ID + r'$')
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
IDEMPOTENCY_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$')
CURSOR = re.compile(r'^[A-Za-z0-9_-]{1,%d}$' % MAX_CURSOR)
LOOPBACK = ('127.0.0.1', '::1', 'localhost')

# ------------------------------------------------------------------ web interface
#: The browser interface shipped beside this module. ``--web-root`` relocates it and
#: ``--no-web`` turns static serving off; ``/v1`` is unaffected either way.
DEFAULT_WEB_ROOT = Path(__file__).resolve().parent / 'web'
#: The only media types the static route ever serves. Anything else is a 404.
STATIC_MEDIA_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
}
#: Directories (relative to the web root) whose files may be served. The web root
#: itself only serves the entry page and an optional favicon.
STATIC_DIRECTORIES = ('css', 'js', 'js/views', 'img')
STATIC_TOP_LEVEL = ('index.html', 'favicon.ico')
#: Development-only files that must never be served by the production service: the
#: clickable prototype, its in-memory mock and any private sample data.
STATIC_EXCLUDED = ('prototype.html', 'js/prototype.js', 'js/mock.js')
STATIC_EXCLUDED_DIRECTORIES = ('data',)
#: One strict shape for a static path: plain segments, no ``%`` escapes, no dot
#: segments, no backslashes and no empty segments. It is checked on the raw request
#: path, before any decoding, so an encoded traversal can never be reinterpreted.
STATIC_PATH = re.compile(r'^/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}/){0,3}'
                         r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$')
STATIC_MAX_BYTES = 4 * 1024 * 1024
CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                           "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                           "base-uri 'none'; form-action 'self'")


def _static_allowed(relative):
    directory, _, name = relative.rpartition('/')
    # Exclusions compare case-insensitively so a case-insensitive filesystem cannot
    # hand out ``js/Mock.js``; the allowlist itself stays exact.
    folded = relative.lower()
    if folded in STATIC_EXCLUDED or folded.split('/')[0] in STATIC_EXCLUDED_DIRECTORIES:
        return False
    if directory:
        return directory in STATIC_DIRECTORIES
    return name in STATIC_TOP_LEVEL


def static_file(web_root, raw_path):
    """Resolve one request path to ``(file, media type)`` under ``web_root``, or ``None``.

    Fail closed: a path outside the fixed allowlist, a development-only file, an
    unknown extension, a dot/encoded segment, a directory, or a symlink that
    resolves outside the web root (or onto an excluded file) is simply not found.
    The caller cannot tell which rule refused it, so the route discloses nothing
    about the tree.
    """
    if web_root is None or not isinstance(raw_path, str):
        return None
    if raw_path == '/':
        raw_path = '/index.html'
    if not STATIC_PATH.fullmatch(raw_path):
        return None
    relative = raw_path[1:]
    if not _static_allowed(relative):
        return None
    suffix = Path(relative).suffix.lower()
    if suffix not in STATIC_MEDIA_TYPES:
        return None
    try:
        root = Path(web_root).resolve(strict=True)
        candidate = (root / relative).resolve(strict=True)
        resolved = candidate.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError):
        return None
    # Re-apply the allowlist to the *resolved* location, so a symlink inside web/
    # cannot alias an excluded file (for example ``js/app.js -> mock.js``).
    if not _static_allowed(resolved) or Path(resolved).suffix.lower() != suffix:
        return None
    if not candidate.is_file():
        return None
    return candidate, STATIC_MEDIA_TYPES[suffix]


def address_matches(peer, network):
    """True when ``peer`` is inside the trusted ``network`` (address or CIDR)."""
    if not isinstance(peer, str) or not peer:
        return False
    if network == 'localhost':
        return peer in ('127.0.0.1', '::1')
    try:
        return ipaddress.ip_address(peer) in ipaddress.ip_network(network, strict=False)
    except ValueError:
        return False


# ------------------------------------------------------------------- attachments
def validate_attachments(raw):
    """Bound and normalize the optional attachment list; reject traversal and overload.

    Attachment *content* is still not bound to durable canonical storage (that is the
    deferred artifact route in ``kittrial-5bb.13``/the artifact binding), so a
    syntactically valid attachment is rejected with a clean 501 rather than accepted
    and silently discarded. Bounds, traversal and media-type errors keep their
    existing 413/422 responses so a malformed request is still described precisely.
    """
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
    if normalized:
        # No durable attachment binding exists yet, so acknowledging the task with a
        # 201 would discard submitted evidence. Fail closed and say exactly that.
        raise not_implemented('Durable attachment binding is not implemented yet; '
                              'resend without attachments or wait for the artifact route')
    return normalized


# ------------------------------------------------------------------ cursors
def cursor_scope(query):
    """The part of the query a cursor is bound to: the page size, not the cursor itself.

    Hashing the whole query would bind the cursor to itself and make every second
    page fail as stale.
    """
    return {key: value for key, value in query.items() if key != 'cursor'}


def make_cursor(principal, project_id, query, offset, extra=None):
    payload = {'u': principal.user_id, 'p': project_id,
               'q': request_hash(cursor_scope(query))[:16], 'o': offset}
    if extra is not None:
        payload['x'] = extra
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def read_cursor(principal, project_id, query, cursor):
    """Return ``{'o': offset, 'x': route-specific continuation}`` for one request."""
    if cursor is None:
        return {'o': 0, 'x': None}
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
    return {'o': data['o'], 'x': data.get('x')}


def result_is_stored(service, key):
    """True when the service's record store already holds a canonical result.

    The result replay path used to be a bounded JSON map inside ``http.json`` with a
    2048-entry / 2 MiB count+byte eviction; it now lives in the SQLite record store
    (``Store.records``) with time-only retention, so the JSON document neither grows
    with the number of keyed operations nor is rewritten by one.
    """
    return service.has_result(key)


def _redact_agent_secret(result):
    """The stored/replayable copy of an agent create/issue result: never the secret.

    A one-time agent secret is returned exactly once. An exact idempotent retry replays
    this redacted copy with ``200`` and ``secret_available: false``, so a retry can
    never re-deliver a credential.
    """
    stored = dict(result)
    credential = dict(stored.get('credential') or {})
    credential.pop('secret', None)
    credential['secret_available'] = False
    stored['credential'] = credential
    stored['secret_available'] = False
    return stored


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

    def _result_key(self, principal, project_id, route, key, target):
        return request_hash({'u': principal.user_id, 'c': principal.credential_id or '-',
                             'p': project_id or '-', 'r': route, 't': target or '-',
                             'k': key})

    def invoke(self, route, principal, project_id, payload, key, target=None, authorize=None,
               capability=None):
        if route not in self.ROUTES:
            raise ValueError('Unknown canonical route %r' % (route,))
        result_key = self._result_key(principal, project_id, route, key, target) if key else None
        # The authority re-check, the canonical write and the durable result share one
        # critical section. A concurrent revocation therefore either commits before
        # this section (and is observed by ``authorize``) or after it (and is
        # serialized after the write); there is no window in which a caller that has
        # already lost authority lands a write.
        with self.service.store.lock:
            if authorize is not None:
                authorize()
            if result_key is not None and self.service.has_result(result_key):
                return self.service.result_get(result_key)
            result = self._dispatch(route, principal, project_id, payload)
            if result_key is not None:
                self.service.result_put(result_key, result)
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
        moment = now_iso(self.service._now())
        events.append({'time': moment, 'project': project_id,
                       'task': task_id, 'action': action, 'user_id': principal.user_id,
                       'actor': actor or principal.actor})
        task = self.state['tasks'].get(task_id) if task_id else None
        if isinstance(task, dict):
            task['updated_at'] = moment
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
        priority = payload.get('priority', 2)
        if type(priority) is not int or not 0 <= priority <= 4:
            raise invalid('Task priority must be an integer 0-4')
        task_id = 'task_' + secrets.token_hex(6)
        task = {'id': task_id, 'project_id': project_id, 'title': title.strip(),
                'priority': priority,
                'description': description, 'status': 'open', 'assignee': None,
                'review_state': 'none',
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
        if operation not in ('contribute', 'request-changes', 'respond', 'approve'):
            raise invalid('Review operation must be contribute, request-changes, respond '
                          'or approve')
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
            delivery = payload.get('delivery')
            branch = None
            if digest is None and isinstance(delivery, dict):
                # The canonical review protocol's delivery object (review_workflow):
                # a remote branch or a bundle with its digest.
                if delivery.get('kind') == 'remote':
                    branch = delivery.get('branch')
                    if not isinstance(branch, str) or not branch.strip() or len(branch) > 300 or \
                            not isinstance(delivery.get('remote'), str) or \
                            not delivery['remote'].strip() or len(delivery['remote']) > 1000:
                        raise invalid('A remote delivery needs a remote and a branch')
                elif delivery.get('kind') == 'bundle':
                    digest = delivery.get('sha256')
                else:
                    raise invalid('Delivery must be remote or bundle')
            if branch is None and (not isinstance(digest, str) or
                                   not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise invalid('bundle_sha256 must be a 64-character lowercase hex digest')
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
                raise invalid('Contribution summary must be 1-1200 characters')
            record = {'id': 'con_' + secrets.token_hex(6), 'task_id': task['id'],
                      'kind': 'contribution', 'commit': commit, 'base_commit': base_commit,
                      'bundle_sha256': digest, 'branch': branch, 'summary': summary.strip(),
                      'actor': payload.get('actor') or principal.actor,
                      'created_at': now_iso(self.service._now())}
            contributions.append(record)
            task['review_state'] = 'awaiting-review'
            task['version'] += 1
            self._event(project_id, task['id'], 'contribution', principal, record['actor'])
            return {'contribution': record, 'task_version': task['version']}
        current = [r for r in contributions if r['kind'] == 'contribution']
        if not current:
            raise conflict('There is no contribution to review')
        # A review names the revision it judges (canonical ``contribution``); a stale
        # reviewer who read an older revision is refused instead of judging new work.
        named = payload.get('contribution')
        if named is not None and named != current[-1]['id']:
            raise conflict('A newer contribution arrived; reread the task before reviewing',
                           {'current_contribution': current[-1]['id']})
        items = self._review_items(payload.get('items')) if operation == 'request-changes' \
            else []
        record = {'id': 'rev_' + secrets.token_hex(6), 'task_id': task['id'], 'kind': operation,
                  'contribution_id': current[-1]['id'],
                  'summary': (payload.get('summary') or '')[:1200],
                  'items': items,
                  'actor': payload.get('actor') or principal.actor,
                  'created_at': now_iso(self.service._now())}
        contributions.append(record)
        task['review_state'] = 'changes-requested' if operation == 'request-changes' else 'approved'
        task['version'] += 1
        self._event(project_id, task['id'], operation, principal, record['actor'])
        return {'review': record, 'task_version': task['version']}

    @staticmethod
    def _review_items(raw):
        """Normalize requested-change items: plain strings or canonical ``{id, text}``."""
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 20:
            raise invalid('Review items must be a list of at most 20 entries')
        items = []
        for index, item in enumerate(raw):
            if isinstance(item, str):
                item = {'id': 'item-%d' % (index + 1), 'text': item}
            if not isinstance(item, dict) or not isinstance(item.get('text'), str) or \
                    not item['text'].strip() or len(item['text']) > 1000 or \
                    not isinstance(item.get('id'), str) or not SAFE_ID.fullmatch(item['id']):
                raise invalid('Each review item needs an id and 1-1000 characters of text')
            items.append({'id': item['id'], 'text': item['text'].strip()})
        return items

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
    def read_tasks(self, project_id):
        """One full canonical snapshot of every in-project row.

        This is the single-read seam (:data:`AGENT_MAX_PAGES` walks chunk it in
        memory instead of re-reading the canonical store per page). ``list_tasks``
        stays the paginated view over the *same* snapshot so the HTTP page route is
        unchanged.
        """
        tasks = [t for t in self.state['tasks'].values() if t['project_id'] == project_id]
        tasks.sort(key=lambda t: t['id'])
        return {'items': [dict(t) for t in tasks], 'total': len(tasks)}

    def list_tasks(self, project_id, limit, offset):
        snapshot = self.read_tasks(project_id)
        return {'items': snapshot['items'][offset:offset + limit],
                'total': snapshot['total']}

    def get_task(self, project_id, task_id):
        return dict(self._task(project_id, task_id))

    def task_history(self, project_id, task_id, limit, offset, canonical=None):
        self._task(project_id, task_id)
        events = [e for e in self.state.get('events', [])
                  if e.get('project') == project_id and e.get('task') == task_id]
        return {'items': events[offset:offset + limit], 'total': len(events),
                'canonical_cursor': None, 'activity_cursor': None}

    def list_feedback(self, project_id, limit, offset):
        items = self.state.get('feedback', {}).get(project_id, [])
        return {'items': items[offset:offset + limit], 'total': len(items)}

    def _review_view(self, task):
        """The task's review chain, projected like ``review_workflow.projection``.

        A requested change is open until a later revision arrives (the disposable
        backend has no separate ``respond`` record).
        """
        records = self.state.get('contributions', {}).get(task['id'], [])
        contributions = [r for r in records if r['kind'] == 'contribution']
        requests = []
        for position, record in enumerate(records):
            if record['kind'] != 'request-changes':
                continue
            later = [r for r in records[position + 1:] if r['kind'] == 'contribution']
            items = record.get('items') or (
                [{'id': 'item-1', 'text': record['summary']}] if record.get('summary') else [])
            for item in items:
                resolved = later[0] if later else None
                requests.append({
                    'id': item['id'], 'request': record['id'], 'text': item['text'],
                    'contribution': record['contribution_id'], 'author': record['actor'],
                    'at': record['created_at'], 'status': 'resolved' if resolved else 'open',
                    'resolution': ('Addressed in revision %d' % (contributions.index(resolved) + 1))
                    if resolved else None})
        contribution = None
        if contributions:
            current = contributions[-1]
            contribution = {'id': current['id'], 'revision': len(contributions),
                            'commit': current['commit'], 'base_commit': current['base_commit'],
                            'branch': current.get('branch'), 'summary': current['summary'],
                            'author': current['actor'], 'at': current['created_at']}
        return {'state': task.get('review_state') or 'none', 'contribution': contribution,
                'requests': requests,
                'open_requests': sum(1 for r in requests if r['status'] == 'open'),
                'latest_id': records[-1]['id'] if records else None,
                'revisions': len(contributions)}

    def task_brief(self, project_id, task_id):
        """Everything the task page shows, from one read of the in-process state."""
        task = self._task(project_id, task_id)
        checkpoints = self.state.get('checkpoints', {}).get(task_id) or []
        checkpoint = None
        if checkpoints:
            last = checkpoints[-1]
            checkpoint = {'id': last['id'], 'at': last['created_at'], 'author': last['actor'],
                          'summary': last['summary'], 'next_action': last.get('next_action'),
                          'open_items': [{'id': item.get('id'),
                                          'kind': item.get('kind') or 'item',
                                          'text': item.get('text')}
                                         for item in last.get('open_items') or []]}
        return {'task': dict(task), 'checkpoint': checkpoint, 'review': self._review_view(task),
                # The disposable backend records no lifecycle evidence, so every fact is
                # honestly unknown rather than inferred from the review state.
                'lifecycle': {}, 'depends_on': []}

    #: The disposable backend is cheap to read and tests expect fresh reads.
    READ_CACHE_SECONDS = 0

    def review_states(self, project_id, queue=None):
        """Review state of every task: in-process rows carry it themselves."""
        return {'states': {t['id']: t.get('review_state') or 'none'
                           for t in self.read_tasks(project_id)['items']},
                'complete': True}

    def review_queue(self, project_id):
        """Every task with current work, highest-attention review states first.

        Mirrors ``work.queue``: a closed task stays listed only while its review is
        still active. The project is read once; the route pages the result.
        """
        items = []
        for task in self.read_tasks(project_id)['items']:
            review = self._review_view(task)
            if task.get('status') == 'closed' and review['state'] not in ACTIVE_REVIEW_STATES:
                continue
            items.append(queue_item(project_id, task, review['state'],
                                    review['contribution'], review['open_requests']))
        items.sort(key=queue_order)
        return {'items': items, 'complete': True}


#: Review states that still need someone to act (``work.queue`` keeps these even on a
#: closed task). ``approved`` is the in-process name for ``awaiting-integration``.
ACTIVE_REVIEW_STATES = ('changes-requested', 'error', 'awaiting-review', 'legacy-review-ready',
                        'awaiting-integration', 'approved')
QUEUE_PRIORITY = {'changes-requested': 0, 'error': 1, 'awaiting-review': 2,
                  'legacy-review-ready': 2, 'awaiting-integration': 3, 'approved': 3}


def queue_order(item):
    return (QUEUE_PRIORITY.get(item['review_state'], 4), str(item['id']))


def queue_item(project_id, task, review_state, contribution, open_requests):
    """One review-queue row, in the shape both backends return."""
    return {'id': task.get('id'), 'project_id': project_id, 'title': task.get('title'),
            'status': task.get('status'), 'assignee': task.get('assignee'),
            'priority': task.get('priority'), 'created_at': task.get('created_at'),
            'updated_at': task.get('updated_at'),
            'review_state': review_state or 'none', 'contribution': contribution,
            'open_requests': open_requests}


class EndpointBackend:
    """Production seam: map authorized HTTP operations onto canonical ``endpoint.py``.

    This is the runnable Linux binding, not deployment glue. Every method the routes
    need is implemented:

    * ``invoke`` maps task/checkpoint/review mutations onto the canonical client
      protocol (``bd create/update`` and the structured ``checkpoint``/``review``
      actions that already own claim, review and checkpoint semantics);
    * ``read_tasks`` performs the one full canonical read of a project;
      ``list_tasks`` slices that snapshot into the paginated HTTP view, and
      ``get_task``/``task_history`` map onto ``bd show`` and the canonical
      ``history`` action;
    * ``list_feedback`` fails closed with a clean 501 until the dedicated feedback
      stream (``kittrial-5bb.13``) is integrated into the canonical command set;
      the service never substitutes its own in-memory feedback for canonical data.

    Uncertain-write reconciliation is durable: the committed canonical result is
    stored in the service state under the full semantic target and key, so an exact
    retry after a lost response returns the stored result rather than repeating the
    canonical write. The authority half of the boundary is re-checked when the
    receipt is written, so a revocation that commits while the canonical call is in
    flight is surfaced as an uncertain outcome, never as success.
    """

    ROUTES = InProcessBackend.ROUTES
    #: Routes with no canonical command yet, and why. Fails closed rather than
    #: pretending the service store is canonical.
    UNRESOLVED = {
        'feedback.add': 'the canonical feedback command ships with kittrial-5bb.13',
    }
    READ_UNRESOLVED = {
        'list_feedback': 'the canonical feedback command ships with kittrial-5bb.13',
    }

    def __init__(self, python, endpoint, root, *, service, actor_namespace='http',
                 timeout=150, runner=None):
        self.python = python
        self.endpoint = endpoint
        self.root = root
        self.service = service
        self.actor_namespace = actor_namespace
        self.timeout = timeout
        self.runner = runner
        self.faults = {}
        # Server-side authority locations. The endpoint is launched with these paths;
        # they never travel in a request body, so a request cannot redirect the
        # live-authority read or make the endpoint create a lock at a chosen path.
        self.authority_store = str(service.store.path)
        self.authority_lock = str(service.store.path) + '.lock'

    def fail_next(self, route, times=1):
        """Test hook: commit canonically, then report an uncertain outcome."""
        self.faults[route] = times

    @property
    def state(self):
        return self.service.state['canonical']

    def _result_key(self, principal, project_id, route, key, target):
        return request_hash({'u': principal.user_id, 'c': principal.credential_id or '-',
                             'p': project_id or '-', 'r': route, 't': target or '-',
                             'k': key})

    def _actor(self, principal):
        """Canonical actor label for the authenticated principal.

        A session acts as its stable user id; a credential acts as its registered
        attribution namespace. This is the same value ``Service.bind_actor`` accepts,
        so a claim and a later review by the same principal carry the same actor and
        the canonical workflow's assignee check holds.
        """
        return principal.actor or principal.user_id

    # -- transport -------------------------------------------------------------
    def _endpoint(self, action, project, actor, args, attachments=None, operation_id=None,
                  authority=None, require_authority=False, route=None):
        if self.runner is not None:
            return self.runner(action=action, project=project, actor=actor, args=args,
                               attachments=attachments or {}, operation_id=operation_id,
                               authority=authority)
        import subprocess
        payload = {'project': project, 'actor': actor, 'action': action, 'args': args,
                   'attachments': attachments or {}}
        # The operation identity and the authority descriptor travel with the
        # mutation: the endpoint reserves the identity and re-validates live
        # authority immediately before the canonical effect. The descriptor carries
        # the principal only; the *store and lock locations* are launch configuration
        # below, never request data.
        if operation_id:
            payload['operation_id'] = operation_id
        if authority:
            payload['authority'] = authority
        # The HTTP route is recorded on the journal row (``operations.route``) and is
        # part of the operation hash, so the identity is bound to the route as well.
        if route and operation_id:
            payload['route'] = route
        argv = [self.python, self.endpoint, '--root', self.root,
                '--authority-store', self.authority_store,
                '--authority-lock', self.authority_lock]
        if require_authority:
            argv.append('--require-authority')
        try:
            completed = subprocess.run(argv,
                                       input=json.dumps(payload), text=True, encoding='utf-8',
                                       capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise uncertain('Canonical endpoint timed out; outcome may be unknown')
        if completed.returncode:
            raise uncertain('Canonical endpoint failed; outcome may be unknown')
        try:
            return json.loads(completed.stdout)
        except ValueError:
            raise uncertain('Canonical endpoint returned an invalid response')

    def _run(self, action, project, actor, args, attachments=None, operation_id=None,
             authority=None, require_authority=False, route=None):
        reply = self._endpoint(action, project, actor, args, attachments,
                               operation_id=operation_id, authority=authority,
                               require_authority=require_authority, route=route)
        code = reply.get('returncode') if isinstance(reply, dict) else None
        stderr = (reply.get('stderr') or '') if isinstance(reply, dict) else ''
        stdout = (reply.get('stdout') or '') if isinstance(reply, dict) else ''
        if code == 126:
            # The endpoint re-validated live authority immediately before the effect
            # and refused it. Nothing was written.
            detail = stderr.strip().splitlines()[-1][:200] if stderr.strip() else None
            if (reply.get('authority_status') or 403) == 401:
                raise unauthenticated(detail or 'Authentication is no longer valid')
            raise forbidden(detail or 'Authority was revoked before the canonical write')
        if code == 124:
            raise uncertain('Canonical command timed out; outcome may be unknown')
        if code:
            detail = stderr.strip().splitlines()[-1][:200] if stderr.strip() else None
            if code == 2:
                raise invalid('Canonical command rejected the request', detail)
            raise uncertain('Canonical command failed; outcome may be unknown')
        return _canonical_payload(stdout)

    # -- mutations -------------------------------------------------------------
    def invoke(self, route, principal, project_id, payload, key, target=None, authorize=None,
               capability=None):
        if route not in self.ROUTES:
            raise ValueError('Unknown canonical route %r' % (route,))
        if route in self.UNRESOLVED:
            raise not_implemented('Canonical backend cannot perform %s: %s'
                                  % (route, self.UNRESOLVED[route]))
        result_key = self._result_key(principal, project_id, route, key, target) if key else None
        with self.service.store.lock:
            if authorize is not None:
                authorize()
            if result_key is not None and self.service.has_result(result_key):
                return self.service.result_get(result_key)
        # The durable canonical operation identity is the same deterministic digest as
        # the local result key, so an exact retry after a lost response carries the
        # identity the endpoint journaled with the effect.
        operation_id = result_key
        action, project, args, attachments = self._command(route, principal, project_id,
                                                           payload, operation_id)
        authority = None
        if capability is not None:
            # Principal descriptor only: the store/lock locations are endpoint launch
            # configuration (``self.authority_store``), not request data. The decision
            # time is the service's monotone expiry clock, so the canonical endpoint
            # re-check cannot revive an item expired by a backward step.
            authority = authority_request(principal, project_id, capability,
                                          now=self.service._expiry_now())
        try:
            result = self._run(action, project, payload.get('actor') or self._actor(principal),
                               args, attachments, operation_id=operation_id,
                               authority=authority, require_authority=capability is not None,
                               route=route)
        except HttpError as failure:
            # The endpoint reported an uncertain outcome (124 or an unclassified
            # failure) for a mutation whose identity it still reserves. Preserve that
            # uncertainty in the HTTP receipt too, so an exact retry reconciles
            # through the durable operation identity instead of releasing the key.
            if failure.status == 503:
                raise UncertainOutcome() from None
            raise
        # The endpoint's guarded section linearized live authority with the effect, so
        # no further authority re-check is needed here: a revocation that committed
        # before the effect refused it, and one that commits after the effect has not
        # changed the authority the effect ran under.
        if result_key is not None:
            with self.service.store.lock:
                self.service.result_put(result_key, result)
                self.service.store.save()
        if self.faults.get(route, 0) > 0:
            self.faults[route] -= 1
            raise UncertainOutcome()
        return result

    #: Exact canonical review field sets (``review_workflow.validate``). The HTTP route
    #: payload may carry extra presentation fields; only these are forwarded.
    REVIEW_COMMON = ('schema_version', 'operation_id', 'previous')
    REVIEW_FIELDS = {
        'contribute': ('repository', 'commit', 'base_commit', 'delivery', 'summary',
                       'supersedes'),
        'request-changes': ('contribution', 'items'),
        'respond': ('contribution', 'resolutions'),
        'approve': ('contribution', 'summary'),
    }
    #: Optional canonical review fields: forwarded only when the caller supplied them,
    #: so old payloads keep the exact legacy field set (no operation-inappropriate
    #: nulls) and a follow-on's additive ``follows`` relation is not silently dropped
    #: into a first contribution or a supersede.
    REVIEW_OPTIONAL_FIELDS = {
        'contribute': ('follows',),
    }
    CHECKPOINT_FIELDS = ('schema_version', 'previous', 'activity_cursor', 'source_commit',
                         'branch', 'intent', 'acceptance', 'summary', 'next_action',
                         'open_items', 'resolved')

    def _command(self, route, principal, project_id, payload, operation_id=None):
        """Build the canonical action and argv for one authorized mutation.

        The argv uses the endpoint's ``@attachment:`` transport (never a raw
        ``--body-file``/``--file`` flag, which ``endpoint.execute`` rejects) and the
        review/checkpoint bodies carry exactly the fields the canonical validators
        require — including ``schema_version`` and ``operation_id`` — and no
        operation-inappropriate nulls.
        """
        task_id = payload.get('task_id')
        if route == 'tasks.create':
            title = str(payload.get('title') or '')
            description = payload.get('description')
            if description:
                return ('bd', project_id, ['create', title, '@attachment:0', '--json'],
                        {'0': {'flag': '--body-file', 'text': description}})
            return 'bd', project_id, ['create', title, '--json'], {}
        if route == 'tasks.update':
            args = ['update', str(task_id), '--json']
            if payload.get('title') is not None:
                args[2:2] = ['--title', str(payload['title'])]
            if payload.get('status') in ('open', 'closed'):
                args[2:2] = ['--status', payload['status']]
            if payload.get('description') is not None:
                args[2:2] = ['--description', str(payload['description'])]
            return 'bd', project_id, args, {}
        if route == 'tasks.claim':
            actor = payload.get('actor') or self._actor(principal)
            return ('bd', project_id,
                    ['update', str(task_id), '--status', 'in_progress', '--assignee',
                     str(actor), '--json'], {})
        if route == 'checkpoints.add':
            missing = [field for field in self.CHECKPOINT_FIELDS if field not in payload]
            if missing:
                raise invalid('Canonical checkpoint payload is missing fields: %s'
                              % ', '.join(missing))
            body = {field: payload.get(field) for field in self.CHECKPOINT_FIELDS}
            body['schema_version'] = payload.get('schema_version', 1)
            body['task'] = task_id
            return ('checkpoint', project_id, [str(task_id), '@attachment:0'],
                    {'0': {'flag': '--file', 'text': json.dumps(body)}})
        if route == 'reviews.add':
            operation = payload.get('operation')
            if operation not in self.REVIEW_FIELDS:
                raise invalid('Review operation must be contribute, request-changes, '
                              'respond or approve')
            fields = self.REVIEW_COMMON + self.REVIEW_FIELDS[operation]
            missing = [field for field in fields
                       if field not in payload and not
                       (field == 'operation_id' and operation_id)]
            if missing:
                raise invalid('Canonical review payload is missing fields: %s'
                              % ', '.join(missing))
            body = {field: payload.get(field) for field in fields}
            for field in self.REVIEW_OPTIONAL_FIELDS.get(operation, ()):
                if field in payload:
                    body[field] = payload.get(field)
            body['schema_version'] = payload.get('schema_version', 1)
            body['operation_id'] = payload.get('operation_id') or operation_id
            if not body['operation_id']:
                raise invalid('Canonical review payload needs an operation_id')
            body['operation'] = operation
            body['task'] = task_id
            return ('review', project_id, [str(task_id), '@attachment:0'],
                    {'0': {'flag': '--file', 'text': json.dumps(body)}})
        raise invalid('Backend route is not implemented for the canonical endpoint: %s' % route)

    # -- reads (no canonical mutation) ----------------------------------------
    @staticmethod
    def _in_project(rows, project_id):
        """Keep only rows the canonical read proves belong to the authorized project.

        The endpoint reads as the privileged ``http/read`` actor; a task identifier
        from another project must never be disclosed through this seam.
        """
        return [row for row in rows
                if not isinstance(row, dict) or row.get('project_id') in (None, project_id)]

    def read_tasks(self, project_id):
        """One full canonical read of a project: the single-read seam.

        ``bd list --all --limit 0 --json`` is the kit's own full-read form (see
        coordination.py) and costs exactly one ``endpoint.py`` subprocess. Attention
        chunks this in-memory snapshot instead of paying one full read per page, and
        the paginated HTTP route slices the same snapshot so both stay exact.
        """
        rows = self._run('bd', project_id, self.actor_namespace + '/read',
                         ['list', '--all', '--limit', '0', '--json'])
        rows = rows if isinstance(rows, list) else rows.get('items', [])
        rows = self._in_project(rows, project_id)
        return {'items': rows, 'total': len(rows)}

    def list_tasks(self, project_id, limit, offset):
        # The HTTP page is sliced from the one full snapshot so the offset cursor
        # stays exact; the canonical read happens once per call, not once per page.
        snapshot = self.read_tasks(project_id)
        return {'items': snapshot['items'][offset:offset + limit],
                'total': snapshot['total']}

    def get_task(self, project_id, task_id):
        row = self._run('bd', project_id, self.actor_namespace + '/read',
                        ['show', str(task_id), '--json'])
        if isinstance(row, list):
            row = row[0] if row else {}
        if isinstance(row, dict) and row.get('project_id') not in (None, project_id):
            raise not_found('Task not found')
        return row

    def task_history(self, project_id, task_id, limit, offset, canonical=None):
        """Page the canonical snapshot cursor, never asking for more than its limit.

        The canonical ``history`` action accepts 1..20 entries per call and binds its
        continuation cursor to a snapshot digest. Accumulating exact-size canonical
        pages keeps the HTTP offset slice correct for any page size (the previous
        ``--limit offset+limit`` form was rejected for every page above 20).
        """
        needed = offset + limit
        collected = []
        cursor = canonical
        total = 0
        activity = None
        pages = 0
        while len(collected) < needed and pages < 100:
            pages += 1
            argv = [str(task_id), '--limit', str(min(20, needed - len(collected))), '--json']
            if cursor:
                argv += ['--cursor', cursor]
            reply = self._run('history', project_id, self.actor_namespace + '/read', argv)
            if isinstance(reply, list):
                collected.extend(reply)
                total = len(reply)
                cursor = None
                break
            entries = reply.get('entries') or []
            if reply.get('activity_cursor'):
                activity = reply['activity_cursor']
            collected.extend(entries)
            total = reply.get('total_entries', len(collected))
            cursor = reply.get('next_cursor')
            if cursor is None:
                break
        return {'items': collected[offset:offset + limit], 'total': total,
                'canonical_cursor': cursor, 'activity_cursor': activity}

    def list_feedback(self, project_id, limit, offset):
        raise not_implemented('Canonical backend has no feedback read yet: %s'
                              % self.READ_UNRESOLVED['list_feedback'])

    #: Unresolved checkpoint items and pending review requests shown per brief. The
    #: canonical ``brief`` bounds these itself (``--items-limit`` 1..10, five pending
    #: requests with a count); the page links to ``history`` for the rest.
    BRIEF_ITEMS = 10

    def task_brief(self, project_id, task_id):
        """Map the canonical ``brief --json`` read onto the task page's shape.

        Two canonical reads: ``bd show`` for the editable task row (title,
        description, version) and ``brief`` for checkpoint, review projection,
        lifecycle facts and dependencies. Nothing is inferred beyond what they say.
        """
        task = self.get_task(project_id, task_id)
        data = self._run('brief', project_id, self.actor_namespace + '/read',
                         [str(task_id), '--json', '--items-limit', str(self.BRIEF_ITEMS)])
        if not isinstance(data, dict):
            raise uncertain('Canonical brief returned an unexpected shape')
        checkpoint = None
        if data.get('checkpoint'):
            point = data['checkpoint']
            unresolved = data.get('unresolved') or {}
            checkpoint = {'id': point.get('comment_id'), 'at': point.get('timestamp'),
                          'author': point.get('author'), 'summary': data.get('current_position'),
                          'next_action': data.get('next_action'),
                          'branch': point.get('branch'), 'source_commit': point.get('source_commit'),
                          'newer_activity': point.get('newer_activity'),
                          'open_items': [{'id': item.get('id'), 'kind': item.get('kind'),
                                          'text': item.get('text')}
                                         for item in unresolved.get('items') or []
                                         if isinstance(item, dict)],
                          'open_items_total': unresolved.get('total')}
        review = data.get('review') or {}
        current = review.get('contribution')
        priors = review.get('prior_contributions_total') or 0
        contribution = None
        if isinstance(current, dict):
            delivery = current.get('delivery') or {}
            contribution = {'id': current.get('comment_id'), 'revision': priors + 1,
                            'commit': current.get('commit'),
                            'base_commit': current.get('base_commit'),
                            'branch': delivery.get('branch') if isinstance(delivery, dict) else None,
                            'repository': current.get('repository'),
                            'summary': current.get('summary'), 'author': current.get('author'),
                            'at': current.get('timestamp')}
        requests = [{'id': item.get('item'), 'request': item.get('request'),
                     'text': item.get('text'), 'contribution': item.get('contribution'),
                     'author': item.get('author'), 'at': item.get('timestamp'),
                     'status': 'open', 'resolution': None}
                    for item in review.get('pending_requests') or [] if isinstance(item, dict)]
        lifecycle = {dimension: {'value': fact.get('value'), 'note': None}
                     for dimension, fact in (data.get('lifecycle') or {}).items()
                     if isinstance(fact, dict)}
        dependencies = (data.get('dependencies') or {}).get('items') or []
        return {'task': task, 'checkpoint': checkpoint,
                'review': {'state': review.get('review_state') or 'none',
                           'contribution': contribution, 'requests': requests,
                           'open_requests': review.get('pending_total', len(requests)),
                           'latest_id': review.get('latest_comment_id'),
                           'revisions': priors + 1 if contribution else 0,
                           'warnings': review.get('warnings') or []},
                'lifecycle': lifecycle,
                'depends_on': [{'id': d.get('depends_on_id'), 'title': d.get('depends_on_id'),
                                'status': 'unknown', 'type': d.get('type')}
                               for d in dependencies if isinstance(d, dict)],
                'warnings': data.get('warnings') or []}

    #: ``GET /v1/me/work`` may reuse one principal's queue read of a project for this
    #: long, so a burst of page loads does not re-export every project each time.
    READ_CACHE_SECONDS = 20
    #: ``review_states`` is derived from ``review_queue`` (see the handler's reuse).
    REVIEW_STATES_FROM_QUEUE = True

    def review_states(self, project_id, queue=None):
        """Review state per task from the canonical ``work`` projection.

        ``bd list`` rows carry no review state, so the task list merges this in. The
        ``work`` queue lists every open task and every closed task whose review is
        still active; a closed task it omits has a finished (or no) review, which is
        reported as unknown rather than guessed. ``complete`` is false when the
        bounded walk stopped early; tasks past the bound are unknown too.
        """
        queue = queue if queue is not None else self.review_queue(project_id)
        return {'states': {item['id']: item['review_state'] for item in queue['items']},
                'complete': bool(queue.get('complete')), 'closed_unknown': True}

    #: Bound on the canonical ``work`` pages one queue read walks (``work`` allows at
    #: most 100 rows per call and re-exports the project each call). Reaching it
    #: reports ``complete: false`` rather than reading on.
    QUEUE_MAX_PAGES = 10

    def review_queue(self, project_id):
        """The canonical ``work`` queue (review projection per task), fully paged.

        ``work`` already applies the kit's own rules: structured review wins over
        legacy labels, a closed task stays listed only while its review is active,
        and rows come highest-attention first.
        """
        items = []
        offset = 0
        complete = False
        for _ in range(self.QUEUE_MAX_PAGES):
            page = self._run('work', project_id, self.actor_namespace + '/read',
                             ['--limit', str(MAX_PAGE), '--offset', str(offset), '--json'])
            if not isinstance(page, dict):
                raise uncertain('Canonical work queue returned an unexpected shape')
            for row in page.get('items') or []:
                if not isinstance(row, dict):
                    continue
                contribution = ({'id': row.get('contribution_id'), 'commit': row.get('commit'),
                                 'revision': None, 'at': None}
                                if row.get('contribution_id') else None)
                task = {'id': row.get('task'), 'title': row.get('title'),
                        'status': row.get('status'), 'assignee': row.get('owner')}
                items.append(queue_item(project_id, task, row.get('review_state'),
                                        contribution, row.get('pending_review_items') or 0))
            offset = page.get('next_offset')
            if offset is None:
                complete = True
                break
        items.sort(key=queue_order)
        return {'items': items, 'complete': complete}


def _canonical_payload(stdout):
    """Parse the JSON body a canonical command returned, tolerating NDJSON."""
    text = (stdout or '').strip()
    if not text:
        raise uncertain('Canonical command returned no data; outcome may be unknown')
    try:
        return json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line[:1] in ('{', '['):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
    raise uncertain('Canonical command returned unparsable data; outcome may be unknown')


def next_action(task):
    """Who acts next on a task, derived only from its status, assignee and review state."""
    state = task.get('review_state')
    if task.get('status') == 'closed' and state not in ACTIVE_REVIEW_STATES:
        return None
    if state is None:
        # Unknown (the canonical projection did not cover this row): say nothing
        # rather than invite a claim or delivery on work that may be under review.
        return None
    if state == 'integrated':
        return {'who': 'owner', 'text': 'Follow the release workflow for the integrated work'}
    if state in ('awaiting-review', 'legacy-review-ready'):
        return {'who': 'owner', 'text': 'Review the delivered contribution'}
    if state == 'changes-requested':
        return {'who': 'assignee', 'text': 'Address the requested changes'}
    if state in ('approved', 'awaiting-integration'):
        return {'who': 'owner', 'text': 'Integrate the approved contribution'}
    if state == 'error':
        return {'who': 'owner', 'text': 'Needs operator attention'}
    if task.get('assignee'):
        return {'who': 'assignee', 'text': 'Deliver a contribution'}
    return {'who': 'anyone', 'text': 'Claim this task'}


def task_matches(task, filters):
    """Apply the browser's task-list filters to one canonical row."""
    status = filters.get('status')
    if status == 'active' and task.get('status') == 'closed':
        return False
    if status and status != 'active' and task.get('status') != status:
        return False
    review = filters.get('review_state')
    if review:
        # An unknown state (``None``: the canonical projection did not cover the row)
        # matches no review filter, not even ``none``.
        if task.get('review_state') is None:
            return False
        # ``approved`` (disposable backend) and ``awaiting-integration`` (canonical
        # projection) are the same state; either filter finds both.
        approved = ('approved', 'awaiting-integration')
        wanted = approved if review in approved else (review,)
        if (task.get('review_state') or 'none') not in wanted:
            return False
    assignee = filters.get('assignee')
    if assignee and task.get('assignee') != assignee:
        return False
    text = filters.get('q')
    if text:
        haystack = ' '.join(str(task.get(key) or '') for key in ('id', 'title', 'description'))
        if text.lower() not in haystack.lower():
            return False
    return True


# ------------------------------------------------------------------ HTTP adapter
class RouteContext:
    __slots__ = ('principal', 'payload', 'query', 'params', 'request_id',
                 'idempotency_key', 'body_hash', 'auth_source', 'secure',
                 'route_target', 'authorize')

    def __init__(self, principal, payload, query, params, request_id, idempotency_key,
                 body_hash, auth_source, secure, route_target):
        self.principal = principal
        self.payload = payload
        self.query = query
        self.params = params
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.body_hash = body_hash
        self.auth_source = auth_source
        self.secure = secure
        self.route_target = route_target
        self.authorize = None


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
    web_root = None
    read_cache = None
    read_cache_lock = None
    trusted_proxies = ()
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
        # Per-request agent read cache. An HTTP/1.1 keep-alive connection reuses this
        # handler instance, so the cache is reset for every request and never outlives it.
        self._agent_task_cache = {}
        self._queue_cache = {}
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            if method in ('GET', 'HEAD') and self.web_root is not None and \
                    not path.startswith('/v1/') and path != '/healthz':
                return self._serve_static(path)
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
            # The semantic route target is the method plus the concrete path (with the
            # real account/project/task id substituted). It is the idempotency
            # namespace, so two different targets never share a receipt.
            route_target = '%s %s' % (method, path)
            ctx = RouteContext(self._principal, payload, query, params, request_id,
                               self._idempotency_key(), self._body_hash, auth_source,
                               self._is_secure(), route_target)
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

    def _peer_address(self):
        try:
            return self.client_address[0]
        except (IndexError, TypeError):
            return ''

    def _is_secure(self):
        """Whether this request reached the service over a trusted secure channel.

        TLS terminated at the service is obviously secure. Behind the documented
        reverse proxy the loopback peer is NOT secure by itself, so a plaintext
        loopback connection must not be treated as HTTPS. Forwarded scheme headers
        are honored only when the immediate peer is a configured trusted proxy, so
        an arbitrary client cannot claim ``https`` to win a Secure cookie.
        """
        if getattr(self.connection, 'cipher', None) is not None:
            return True
        if self._forwarded_proto() == 'https':
            return True
        return False

    def _peer_is_trusted_proxy(self):
        peer = self._peer_address()
        return any(address_matches(peer, network) for network in self.trusted_proxies)

    def _forwarded_proto(self):
        if not self._peer_is_trusted_proxy():
            return None
        value = self.headers.get('X-Forwarded-Proto')
        if isinstance(value, str) and value:
            return value.split(',')[0].strip().lower()
        return None

    def _source(self):
        if self._peer_is_trusted_proxy():
            forwarded = self.headers.get('X-Forwarded-For')
            if isinstance(forwarded, str) and forwarded:
                candidate = forwarded.split(',')[-1].strip()
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    return self._peer_address()
                return candidate
        return self._peer_address() or 'unknown'

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

    def _serve_static(self, path):
        """Anonymous GET/HEAD of one allowlisted web-interface file (see :func:`static_file`).

        The body is read once and bounded; the response carries the page's strict
        Content-Security-Policy and revalidates on every load (``no-cache`` plus a
        content ETag), so a redeploy is picked up without stale scripts.
        """
        self._read_body()  # bounded; a stray GET body must not desynchronize keep-alive
        found = static_file(self.web_root, path)
        body = None
        if found is not None:
            candidate, media_type = found
            try:
                with open(candidate, 'rb') as handle:
                    body = handle.read(STATIC_MAX_BYTES + 1)
            except OSError:
                body = None
            if body is not None and len(body) > STATIC_MAX_BYTES:
                body = None
        if body is None:
            raise not_found('Not found')
        etag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
        status = 304 if self.headers.get('If-None-Match') == etag else 200
        self.send_response(status)
        if status == 200:
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(len(body)))
        self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Security-Policy', CONTENT_SECURITY_POLICY)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('X-Request-Id', getattr(self, '_current_request_id', '') or '')
        self.end_headers()
        if status == 200 and self.command != 'HEAD':
            self.wfile.write(body)

    def _send_cookie(self, token):
        attributes = 'orchestra_session=%s; Path=/; HttpOnly; SameSite=Strict' % token
        if self._is_secure():
            attributes += '; Secure'
        self.send_header('Set-Cookie', attributes)

    # -- mutation helper -------------------------------------------------------
    def _mutate(self, ctx, route_name, project_id, fn, *, capability, allow_self_user=None,
                status=200, idempotent=True, replay_status=None, serialize=True,
                canonical=False, reason=None):
        """Run one authorized, idempotent mutation.

        ``capability`` names the authority the route needs. The idempotency key is
        *reserved atomically* (:meth:`Service.idempotency_reserve`) before the effect,
        so two concurrent identical requests cannot both create the effect: the
        second either replays a committed result or receives a 409. ``serialize=True``
        holds the store lock across the authority check and the write for in-process
        mutations. Canonical mutations pass ``serialize=False`` and ``canonical=True``:
        their durable operation identity lets a retry after an uncertain outcome
        reconcile the canonical result through the endpoint journal instead of
        repeating the effect. ``reason`` is an optional short redacted clarification
        (for example the target's identity) added to the route's audit events; it must
        never carry a secret or request body.
        """
        ctx.authorize = (lambda: self.service.check_authority(
            ctx.principal, project_id, capability, allow_self_user=allow_self_user))
        idem_route = '%s %s' % (route_name, ctx.route_target)
        key = ctx.idempotency_key if idempotent else None
        digest = None
        if key is not None:
            kind, value, response = self.service.idempotency_reserve(
                ctx.principal, project_id, idem_route, key, ctx.body_hash,
                canonical=canonical and serialize is False)
            if kind == 'replay':
                # An exact retry must not re-deliver a one-time secret; the caller
                # marks that with replay_status (200 metadata-only for issue routes).
                return (replay_status or value), response
            digest = value
            # 'new' or 'reconcile': both proceed with the reserved digest; a canonical
            # reconcile re-invokes with the durable operation identity.
        try:
            if serialize:
                with self.service.store.lock:
                    ctx.authorize()
                    public, stored = fn()
            else:
                ctx.authorize()
                public, stored = fn()
        except UncertainOutcome:
            self.service.idempotency_unknown(digest)
            self.service.audit(ctx.request_id, ctx.principal, route_name, 'unknown',
                               project_id=project_id, reason=reason or 'uncertain')
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
        self._forget_cached_reads(ctx.principal, project_id)
        self.service.audit(ctx.request_id, ctx.principal, route_name, 'committed',
                           project_id=project_id, reason=reason)
        self.service.store.save()
        return status, public

    def require(self, ctx, capability):
        """Authorize one read/route capability against live authority."""
        return self.service.check_authority(ctx.principal, ctx.params.get('pid'), capability)

    def _project(self, ctx, capability=CAP_READ):
        return self.service.check_authority(ctx.principal, ctx.params['pid'], capability)

    def _task_payload(self, ctx):
        payload = dict(ctx.payload or {})
        payload['task_id'] = ctx.params['tid']
        return payload

    def _paged(self, ctx, items, limit, state, **extra):
        """One bounded page of an in-memory list, with a cursor bound to this query."""
        offset = state['o']
        body = {'items': items[offset:offset + limit], 'total': len(items)}
        body['next_cursor'] = (make_cursor(ctx.principal, ctx.params.get('pid'), ctx.query,
                                           offset + limit)
                               if offset + limit < len(items) else None)
        body.update(extra)
        return body

    def _task_view(self, task, names):
        """A task row plus presentation fields (assignee name, next action). Additive."""
        view = dict(task)
        assignee = view.get('assignee')
        view['assignee_name'] = names.get(assignee, assignee) if assignee else None
        view['next_action'] = next_action(view)
        return view

    def _task_views(self, tasks):
        names = self.service.actor_names([t.get('assignee') for t in tasks])
        return [self._task_view(t, names) for t in tasks]

    def _review_queue(self, project_id, shared=False):
        """One review-queue read of a project per request (and, when ``shared`` and the
        backend allows it, reused for ``READ_CACHE_SECONDS`` by the same principal).

        Authorization is never cached: callers re-check live authority first.
        """
        cache = getattr(self, '_queue_cache', None)
        if cache is None:
            cache = self._queue_cache = {}
        if project_id in cache:
            return cache[project_id]
        ttl = getattr(self.backend, 'READ_CACHE_SECONDS', 0) if shared else 0
        key = (getattr(self._principal, 'user_id', None), project_id)
        now = time.monotonic()
        if ttl:
            with self.read_cache_lock:
                hit = self.read_cache.get(key)
            if hit is not None and hit[0] > now:
                cache[project_id] = hit[1]
                return hit[1]
        result = self.backend.review_queue(project_id)
        cache[project_id] = result
        if ttl:
            with self.read_cache_lock:
                if len(self.read_cache) >= READ_CACHE_MAX_ENTRIES:
                    for stale in [k for k, v in self.read_cache.items() if v[0] <= now] or \
                            list(self.read_cache)[:READ_CACHE_MAX_ENTRIES // 2]:
                        self.read_cache.pop(stale, None)
                self.read_cache[key] = (now + ttl, result)
        return result

    def _forget_cached_reads(self, principal, project_id):
        """Drop the principal's cached ``/v1/me/work`` reads after its own write.

        The project's entry goes (all of them when the write had no project), so the
        author sees their own claim, delivery or review at once; other principals'
        entries still expire on their short TTL.
        """
        cache = getattr(self, 'read_cache', None)
        if cache is None or principal is None:
            return
        with self.read_cache_lock:
            for key in [k for k in cache if k[0] == principal.user_id and
                        (project_id is None or k[1] == project_id)]:
                cache.pop(key, None)

    def _with_review_states(self, project_id, rows):
        """Give every row its review state (``None`` when unknown). Returns completeness."""
        if all(isinstance(r, dict) and 'review_state' in r for r in rows):
            return True
        # A backend whose states come from its queue projection reuses this request's
        # queue read instead of paying for a second one.
        queue = self._review_queue(project_id) \
            if getattr(self.backend, 'REVIEW_STATES_FROM_QUEUE', False) else None
        read = self.backend.review_states(project_id, queue=queue)
        states = read['states']
        for row in rows:
            if isinstance(row, dict) and 'review_state' not in row:
                row['review_state'] = states.get(row.get('id'))
        return bool(read.get('complete'))

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
        body = {'user': {'id': principal.user_id,
                         'username': self.service.username_of(principal.user_id),
                         'display_name': principal.display_name,
                         'superuser': principal.superuser},
                'via': principal.via,
                'credential': principal.credential_id,
                'project': principal.credential_project}
        # A browser keeps its CSRF token in memory only, so a reload re-reads it here.
        # It is returned only to the cookie session it belongs to; a cross-origin page
        # cannot read this same-origin response.
        if ctx.auth_source == 'cookie' and principal.csrf:
            body['csrf_token'] = principal.csrf
        return 200, body

    @route('DELETE', r'/v1/sessions/current')
    def sessions_delete(self, ctx):
        return 204, self.service.logout(ctx.principal, request_id=ctx.request_id)

    @route('POST', r'/v1/accounts')
    def accounts_create(self, ctx):
        payload = ctx.payload or {}
        return self._mutate(ctx, 'accounts.create', None,
                            lambda: self._account_created(ctx, payload), status=201,
                            capability=CAP_ACCOUNTS_ADMIN,
                            idempotent=bool(ctx.idempotency_key))

    def _account_created(self, ctx, payload):
        user = self.service.create_user(ctx.principal, payload.get('username'),
                                        payload.get('display_name'))
        return user, user

    @route('GET', r'/v1/accounts')
    def accounts_list(self, ctx):
        self.require(ctx, CAP_ACCOUNTS_ADMIN)
        return 200, {'items': self.service.list_users(ctx.principal)}

    @route('GET', r'/v1/accounts/lookup')
    def accounts_lookup(self, ctx):
        """Exact username -> account, for a project administrator adding a member."""
        project = ctx.query.get('project')
        if not isinstance(project, str) or not SAFE_ID.fullmatch(project):
            raise invalid('project is required: the project the member is being added to')
        return 200, self.service.lookup_account(ctx.principal, project,
                                                ctx.query.get('username'),
                                                request_id=ctx.request_id)

    @route('POST', r'/v1/accounts/(?P<uid>' + ID + r')/password')
    def account_password(self, ctx):
        payload = ctx.payload or {}

        def change():
            result = self.service.change_password(ctx.principal, ctx.params['uid'],
                                                  payload.get('current_password'),
                                                  payload.get('new_password'))
            return result, result
        return self._mutate(ctx, 'accounts.password', None, change,
                            capability=CAP_ACCOUNTS_ADMIN,
                            allow_self_user=ctx.params['uid'],
                            idempotent=bool(ctx.idempotency_key))

    @route('POST', r'/v1/accounts/(?P<uid>' + ID + r')/reset')
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
                            capability=CAP_ACCOUNTS_ADMIN, replay_status=200)

    @route('POST', r'/v1/accounts/(?P<uid>' + ID + r')/reset/redeem', anonymous=True,
           csrf=False)
    def account_reset_redeem(self, ctx):
        payload = ctx.payload or {}
        # Anonymous but reset-value-authorized. The token lookup is uniform, so a
        # missing account and a wrong value are indistinguishable to the caller.
        result = self.service.redeem_reset(ctx.params['uid'], payload.get('reset_value'),
                                           payload.get('new_password'))
        # Redemption is anonymous but token-authorized: record the account, never the
        # reset value or the new password.
        self.service.audit(ctx.request_id, None, 'accounts.reset.redeem', 'committed',
                           reason='account=%s' % ctx.params['uid'])
        self.service.store.save()
        return 200, result

    @route('POST', r'/v1/accounts/(?P<uid>' + ID + r')/disable')
    def account_disable(self, ctx):
        return self._mutate(ctx, 'accounts.disable', None,
                            lambda: self._account_disabled(ctx),
                            capability=CAP_ACCOUNTS_ADMIN,
                            allow_self_user=ctx.params['uid'],
                            idempotent=bool(ctx.idempotency_key))

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
        # The idempotency namespace for creation is the principal + route, never a
        # body-derived project id: the same key with changed content must conflict
        # (409) instead of creating a second project.
        return self._mutate(ctx, 'projects.create', None, create,
                            status=201, capability=CAP_PROJECT_CREATE)

    @route('GET', r'/v1/projects')
    def projects_list(self, ctx):
        return 200, {'items': self.service.list_projects(ctx.principal)}

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')')
    def project_get(self, ctx):
        self._project(ctx, CAP_READ)
        return 200, self.service.project_view(ctx.principal, ctx.params['pid'])

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/archive')
    def project_archive(self, ctx):
        def archive():
            result = self.service.archive_project(ctx.principal, ctx.params['pid'])
            return result, result
        return self._mutate(ctx, 'projects.archive', ctx.params['pid'], archive,
                            capability=CAP_PROJECT_ADMIN)

    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/members/(?P<uid>' + ID + r')')
    def member_set(self, ctx):
        payload = ctx.payload or {}

        def set_member():
            result = self.service.set_member(ctx.principal, ctx.params['pid'],
                                             ctx.params['uid'], payload.get('role'),
                                             request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'members.set', ctx.params['pid'], set_member,
                            capability=CAP_PROJECT_ADMIN)

    @route('DELETE', r'/v1/projects/(?P<pid>' + ID + r')/members/(?P<uid>' + ID + r')')
    def member_remove(self, ctx):
        def remove():
            result = self.service.remove_member(ctx.principal, ctx.params['pid'],
                                                ctx.params['uid'],
                                                request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'members.remove', ctx.params['pid'], remove,
                            capability=CAP_PROJECT_ADMIN)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/members')
    def members_list(self, ctx):
        self._project(ctx, CAP_READ)
        limit, state = self._page(ctx, ctx.query)
        items = self.service.list_members(ctx.principal, ctx.params['pid'])
        return 200, self._paged(ctx, items, limit, state)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/worker-credentials')
    def credentials_list(self, ctx):
        self._project(ctx, CAP_PROJECT_ADMIN)
        limit, state = self._page(ctx, ctx.query)
        items = self.service.list_worker_credentials(ctx.principal, ctx.params['pid'])
        return 200, self._paged(ctx, items, limit, state)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/worker-credentials')
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
                            capability=CAP_PROJECT_ADMIN, replay_status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/worker-credentials/'
                  r'(?P<cid>' + ID + r')/revoke')
    def credential_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_credential(ctx.principal, ctx.params['pid'],
                                                    ctx.params['cid'],
                                                    request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'credentials.revoke', ctx.params['pid'], revoke, status=204,
                            capability=CAP_PROJECT_ADMIN)

    # -- personal agent routes -------------------------------------------------
    #
    # The agent's own API is registered before ``/v1/agents/{aid}`` so the literal
    # ``me`` path always wins. Every route here returns stable JSON; the browser
    # screens are the separate task kittrial-5bb.20 and are not built here.
    @route('GET', r'/v1/agents/me')
    def agents_me(self, ctx):
        agent = self.service.agent_for_credential(ctx.principal)
        return 200, {'agent': agent, 'attention': self._agent_attention_view(agent),
                     'generated_at': now_iso(self.service._now())}

    @route('GET', r'/v1/agents/me/next')
    def agents_me_next(self, ctx):
        agent = self.service.agent_for_credential(ctx.principal)
        attention = self._agent_attention(ctx.principal, agent)
        projects = [{'id': p['id'], 'name': p['name']}
                    for p in self.service.list_projects(ctx.principal)]
        return 200, {
            'agent': agent,
            'attention': self._agent_attention_view(agent, attention),
            'next_action': attention['actions'][0] if attention['actions'] else None,
            'next_actions': attention['actions'],
            'projects': projects,
            'links': {'self': '/v1/agents/me', 'next': '/v1/agents/me/next'},
            'manual_cadence': 'Computed at read time; no polling or scheduled work. '
                              'Re-read this route when the owner resumes the agent.',
            'generated_at': now_iso(self.service._now()),
        }

    @route('POST', r'/v1/agents')
    def agents_create(self, ctx):
        payload = dict(ctx.payload or {})

        def create():
            result = self.service.create_agent(
                ctx.principal, name=payload.get('name'), tool=payload.get('tool'),
                working_directory=payload.get('working_directory'),
                machine=payload.get('machine'), notes=payload.get('notes'),
                projects=payload.get('projects'), scopes=payload.get('scopes'),
                request_id=ctx.request_id)
            return result, _redact_agent_secret(result)
        return self._mutate(ctx, 'agents.create', None, create, status=201,
                            capability=CAP_AGENTS, replay_status=200)

    def _agent_items(self, principal):
        items = []
        for agent in self.service.list_agents(principal):
            attention = self._agent_attention(principal, agent)
            items.append(dict(agent, attention=self._agent_attention_view(agent, attention),
                              resume_prompt=self._agent_resume_prompt(agent, attention)))
        return items

    @route('GET', r'/v1/agents')
    def agents_list(self, ctx):
        self.require(ctx, CAP_AGENTS)
        items = self._agent_items(ctx.principal)
        return 200, {'items': items, 'total': len(items),
                     'generated_at': now_iso(self.service._now())}

    @route('GET', r'/v1/agents/(?P<aid>' + ID + r')')
    def agents_get(self, ctx):
        self.require(ctx, CAP_AGENTS)
        agent = self.service.get_agent(ctx.principal, ctx.params['aid'])
        attention = self._agent_attention(ctx.principal, agent)
        return 200, dict(agent, attention=self._agent_attention_view(agent, attention),
                         resume_prompt=self._agent_resume_prompt(agent, attention),
                         generated_at=now_iso(self.service._now()))

    @route('PATCH', r'/v1/agents/(?P<aid>' + ID + r')')
    def agents_update(self, ctx):
        payload = dict(ctx.payload or {})

        def update():
            result = self.service.update_agent(ctx.principal, ctx.params['aid'], payload)
            return result, result
        return self._mutate(ctx, 'agents.update', None, update, capability=CAP_AGENTS)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/disable')
    def agents_disable(self, ctx):
        def disable():
            result = self.service.disable_agent(ctx.principal, ctx.params['aid'])
            return result, result
        return self._mutate(ctx, 'agents.disable', None, disable, capability=CAP_AGENTS)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/enable')
    def agents_enable(self, ctx):
        def enable():
            result = self.service.enable_agent(ctx.principal, ctx.params['aid'])
            return result, result
        return self._mutate(ctx, 'agents.enable', None, enable, capability=CAP_AGENTS)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/credentials')
    def agents_credential_issue(self, ctx):
        payload = dict(ctx.payload or {})

        def issue():
            result = self.service.issue_agent_credential(
                ctx.principal, ctx.params['aid'], scopes=payload.get('scopes'),
                label=payload.get('label'), request_id=ctx.request_id)
            return result, _redact_agent_secret(result)
        return self._mutate(ctx, 'agents.credentials.issue', None, issue, status=201,
                            capability=CAP_AGENTS, replay_status=200)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/credentials/'
                  r'(?P<cid>' + ID + r')/revoke')
    def agents_credential_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_agent_credential(
                ctx.principal, ctx.params['aid'], ctx.params['cid'],
                request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.credentials.revoke', None, revoke, status=204,
                            capability=CAP_AGENTS)

    # -- project-scoped agent routes (owner decision 4) ------------------------
    #
    # The boundary is the project's own administration capability, NOT
    # ``CAP_AGENTS``: a project owner/admin governs which agents may work in their
    # project without gaining any control over an agent it does not own. Both routes
    # return the deliberately narrow :meth:`Service.project_agent_view`, which never
    # carries ``working_directory``.
    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/agents')
    def project_agents_list(self, ctx):
        self._project(ctx, CAP_PROJECT_ADMIN)
        items = self.service.list_project_agents(ctx.principal, ctx.params['pid'])
        return 200, {'project': ctx.params['pid'], 'items': items, 'total': len(items),
                     'generated_at': now_iso(self.service._now())}

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')')
    def project_agent_get(self, ctx):
        self._project(ctx, CAP_PROJECT_ADMIN)
        agent = self.service.get_project_agent(ctx.principal, ctx.params['pid'],
                                               ctx.params['aid'])
        return 200, {'project': ctx.params['pid'], 'agent': agent,
                     'generated_at': now_iso(self.service._now())}

    @route('DELETE', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')')
    def project_agent_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_agent_project(
                ctx.principal, ctx.params['pid'], ctx.params['aid'],
                request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.project.revoke', ctx.params['pid'], revoke,
                            capability=CAP_PROJECT_ADMIN,
                            reason='agent %s' % ctx.params['aid'])

    # -- read-time agent attention --------------------------------------------
    def _agent_blocked_tasks(self):
        """Task ids whose latest checkpoint still lists open items.

        This reads the in-process canonical view when it is present; the canonical
        endpoint binding does not mirror checkpoints into it, so a missing view simply
        yields no ``blocked`` signal rather than a wrong one.
        """
        state = getattr(self.backend, 'state', None)
        if not isinstance(state, dict):
            return set()
        checkpoints = state.get('checkpoints')
        if not isinstance(checkpoints, dict):
            return set()
        blocked = set()
        for task_id, items in checkpoints.items():
            if isinstance(items, list) and items:
                last = items[-1]
                if isinstance(last, dict) and last.get('open_items'):
                    blocked.add(task_id)
        return blocked

    def _agent_action(self, priority, kind, project_id, task, reason):
        task_id = task.get('id')
        base = '/v1/projects/%s/tasks/%s' % (project_id, task_id)
        return {'priority': priority, 'kind': kind, 'project': project_id,
                'task': task_id, 'title': task.get('title'),
                'status': task.get('status'), 'review_state': task.get('review_state'),
                'assignee': task.get('assignee'), 'reason': reason,
                'links': {'task': base, 'brief': base, 'history': base + '/history',
                          'project': '/v1/projects/%s' % project_id}}

    def _agent_may_read(self, principal, project_id):
        """The task-route authority check for one project, reused by attention.

        ``Service.check_authority`` is the single boundary every task route uses and it
        delegates to ``http_authority.decide``, so attention can never read a project
        that the same principal would be refused on ``GET /v1/projects/{id}/tasks``:
        for an agent credential that is the live credential, the live enable state and
        the owner's *current* role; for the owner view it is the owner's current
        membership. Anything else (including a stored grant the owner has since lost)
        is skipped rather than read.
        """
        try:
            self.service.check_authority(principal, project_id, CAP_READ)
        except HttpError:
            return False
        return True

    def _agent_project_tasks(self, project_id):
        """Every task row of one project from ONE canonical read per HTTP request.

        The backend's :meth:`read_tasks` snapshot is fetched at most once per project
        per request (the endpoint binding spawns one ``endpoint.py`` / ``bd list
        --all`` for it), so attention over a project of any size costs one read, not
        one read per :data:`MAX_PAGE` page. The snapshot is chunked in memory to the
        same :data:`AGENT_MAX_PAGES` bound the page walk used, so the ``complete``
        flag - and therefore ``truncated`` - keeps exactly its rev2 meaning: a bound
        that is not reached means every page was seen. Nothing is cached across
        requests; the next request re-reads canonical state.
        """
        cache = getattr(self, '_agent_task_cache', None)
        if cache is None:
            cache = self._agent_task_cache = {}
        if project_id in cache:
            return cache[project_id]
        snapshot = self.backend.read_tasks(project_id)
        rows = [task for task in (snapshot.get('items') or []) if isinstance(task, dict)]
        bound = AGENT_MAX_PAGES * MAX_PAGE
        result = {'tasks': rows[:bound], 'complete': len(rows) <= bound}
        cache[project_id] = result
        return result

    def _agent_attention(self, principal, agent):
        """Owner/agent-visible attention, computed at read time from bounded reads.

        Each granted project is re-authorized with the same live-authority check the
        task routes apply (:meth:`_agent_may_read`), so a project the principal can no
        longer read drops out of attention instead of leaking its tasks. Each project
        is read once per request (:meth:`_agent_project_tasks`) and walked to its last
        page, so an agent's own task is counted however deep it sorts; the page-sized
        cap applies only to the claimable suggestions.
        """
        actor = agent.get('actor') or agent.get('id')
        blocked = self._agent_blocked_tasks()
        counts = {'claimable': 0, 'claimed': 0, 'changes_requested': 0,
                  'awaiting_review': 0, 'blocked': 0}
        own_actions = []
        claimable_actions = []
        truncated = False
        for project_id in agent.get('projects') or []:
            if not self._agent_may_read(principal, project_id):
                continue
            try:
                read = self._agent_project_tasks(project_id)
            except HttpError:
                # A project the principal can no longer open simply drops out.
                continue
            if not read['complete']:
                truncated = True
            for task in read['tasks']:
                assigned = task.get('assignee') == actor
                review = task.get('review_state')
                if assigned and review == 'changes-requested':
                    counts['changes_requested'] += 1
                    own_actions.append(self._agent_action(
                        1, 'changes-requested', project_id, task,
                        'A reviewer requested changes on this contribution.'))
                elif assigned and review == 'awaiting-review':
                    counts['awaiting_review'] += 1
                    own_actions.append(self._agent_action(
                        4, 'awaiting-review', project_id, task,
                        'Waiting for a human review decision.'))
                elif assigned and task.get('status') != 'closed' and \
                        task.get('id') in blocked:
                    counts['blocked'] += 1
                    own_actions.append(self._agent_action(
                        2, 'blocked', project_id, task,
                        'The latest checkpoint left unresolved items.'))
                if assigned:
                    counts['claimed'] += 1
                elif task.get('status') == 'open' and task.get('assignee') is None:
                    counts['claimable'] += 1
                    if len(claimable_actions) < AGENT_CLAIMABLE_LIMIT:
                        claimable_actions.append(self._agent_action(
                            3, 'claimable-task', project_id, task,
                            'Open, unclaimed work the agent may take.'))
        # The count is exact over every page; only the collected suggestions are capped,
        # so a long claimable list can never hide the agent's own feedback.
        if counts['claimable'] > len(claimable_actions):
            truncated = True
        actions = own_actions + claimable_actions
        actions.sort(key=lambda a: (a['priority'], a['project'], a['task']))
        if len(actions) > AGENT_ACTION_LIMIT:
            actions = actions[:AGENT_ACTION_LIMIT]
            truncated = True
        if counts['changes_requested']:
            state = 'changes-requested'
        elif counts['blocked']:
            state = 'blocked'
        elif counts['awaiting_review']:
            state = 'waiting-review'
        elif counts['claimed']:
            state = 'working'
        else:
            state = 'idle'
        return {'state': state, 'summary': self._agent_summary(state, counts),
                'counts': counts, 'actions': actions, 'truncated': truncated,
                'computed_at': now_iso(self.service._now())}

    @staticmethod
    def _agent_summary(state, counts):
        if state == 'changes-requested':
            return ('%d contribution(s) have changes requested; act on them first.'
                    % counts['changes_requested'])
        if state == 'blocked':
            return ('%d task(s) have unresolved checkpoint items.'
                    % counts['blocked'])
        if state == 'working':
            return ('%d claimed task(s) in flight; %d claimable.'
                    % (counts['claimed'], counts['claimable']))
        if state == 'waiting-review':
            return ('%d contribution(s) waiting for a human review decision.'
                    % counts['awaiting_review'])
        return ('Idle: %d claimable task(s), nothing in flight.' % counts['claimable'])

    @staticmethod
    def _agent_attention_view(agent, attention=None):
        if attention is None:
            attention = {'state': 'unknown', 'summary': 'Not computed.',
                         'counts': {}, 'actions': [], 'truncated': False,
                         'computed_at': None}
        return {key: attention[key] for key in
                ('state', 'summary', 'counts', 'truncated', 'computed_at')}

    def _agent_resume_prompt(self, agent, attention):
        """A copyable prompt for the owner's local assistant. No secret is included."""
        server = self.service.public_url or '<ORCHESTRA_SERVER_URL>'
        directory = agent.get('working_directory')
        where = (' in %s' % directory) if directory else ''
        first = attention['actions'][0] if attention['actions'] else None
        if first:
            what = ('Next: %s on %s in project %s.'
                    % (first['kind'], first['task'], first['project']))
        else:
            what = 'There is nothing queued right now.'
        return ("Open your agent folder%s for agent '%s' (%s). %s\n"
                "Read %s from VS Code secret storage or your OS credential store (an\n"
                "environment variable is only a fallback), then run:\n"
                "  curl -fsS -H \"Authorization: Bearer $%s\" %s/v1/agents/me/next"
                % (where, agent['name'], agent['id'], what, AGENT_SECRET_ENV,
                   AGENT_SECRET_ENV, server))

    # -- task routes -----------------------------------------------------------
    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks')
    def tasks_create(self, ctx):
        self._project(ctx, CAP_TASKS)
        payload = dict(ctx.payload or {})
        payload['attachments'] = validate_attachments(payload.get('attachments'))

        def create():
            result = self.backend.invoke('tasks.create', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_TASKS)
            return result, result
        return self._mutate(ctx, 'tasks.create', ctx.params['pid'], create, status=201,
                            capability=CAP_TASKS, serialize=False, canonical=True)

    @route('PATCH', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')')
    def tasks_update(self, ctx):
        self._project(ctx, CAP_TASKS)
        payload = self._task_payload(ctx)

        def update():
            result = self.backend.invoke('tasks.update', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_TASKS)
            return result, result
        return self._mutate(ctx, 'tasks.update', ctx.params['pid'], update,
                            capability=CAP_TASKS, serialize=False, canonical=True)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks')
    def tasks_list(self, ctx):
        self._project(ctx, CAP_READ)
        limit, state = self._page(ctx, ctx.query)
        filters = self._task_filters(ctx.query)
        if filters:
            rows = [t for t in self.backend.read_tasks(ctx.params['pid']).get('items') or []
                    if isinstance(t, dict)]
            complete = self._with_review_states(ctx.params['pid'], rows)
            rows = [t for t in rows if task_matches(t, filters)]
            result = {'items': rows[state['o']:state['o'] + limit], 'total': len(rows)}
        else:
            result = self.backend.list_tasks(ctx.params['pid'], limit, state['o'])
            complete = self._with_review_states(ctx.params['pid'], result['items'])
        result['items'] = self._task_views(result['items'])
        # False when some rows' review state is unknown (``review_state: null``): the
        # bounded canonical projection did not cover them, or they are closed tasks
        # whose finished review the queue no longer lists.
        result['review_states_complete'] = complete and all(
            t.get('review_state') is not None for t in result['items'])
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             state['o'] + limit)
                                 if state['o'] + limit < result['total'] else None)
        return 200, result

    @staticmethod
    def _task_filters(query):
        filters = {key: query[key] for key in ('status', 'review_state', 'assignee', 'q')
                   if query.get(key)}
        if 'status' in filters and filters['status'] not in TASK_STATUS_FILTERS:
            raise invalid('status must be one of %s' % ', '.join(TASK_STATUS_FILTERS))
        if 'review_state' in filters and not re.fullmatch(r'[a-z][a-z-]{0,39}',
                                                          filters['review_state']):
            raise invalid('review_state is not a review state')
        if 'assignee' in filters and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}',
                                                      filters['assignee']):
            raise invalid('assignee is not an actor label')
        if len(filters.get('q', '')) > TASK_FILTER_TEXT_MAX:
            raise invalid('q must be at most %d characters' % TASK_FILTER_TEXT_MAX)
        return filters

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')')
    def tasks_get(self, ctx):
        self._project(ctx, CAP_READ)
        return 200, self._task_views([self.backend.get_task(ctx.params['pid'],
                                                             ctx.params['tid'])])[0]

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/brief')
    def tasks_brief(self, ctx):
        """The task page in one read: row, current checkpoint, review chain, facts.

        Readable by any project member (``CAP_READ``), exactly like the task and its
        history. Reading never acknowledges anything.
        """
        self._project(ctx, CAP_READ)
        pid, tid = ctx.params['pid'], ctx.params['tid']
        brief = self.backend.task_brief(pid, tid)
        review = brief['review']
        checkpoint = brief.get('checkpoint')
        contribution = review.get('contribution')
        actors = [brief['task'].get('assignee')]
        actors += [checkpoint.get('author')] if checkpoint else []
        actors += [contribution.get('author')] if contribution else []
        actors += [r.get('author') for r in review.get('requests') or []]
        names = self.service.actor_names(actors)
        task = dict(brief['task'], review_state=review.get('state') or 'none')
        brief['task'] = self._task_view(task, names)
        if checkpoint:
            checkpoint['author_name'] = names.get(checkpoint.get('author'))
        if contribution:
            contribution['author_name'] = names.get(contribution.get('author'))
        for request in review.get('requests') or []:
            request['author_name'] = names.get(request.get('author'))
        base = '/v1/projects/%s/tasks/%s' % (pid, tid)
        brief['links'] = {'task': base, 'history': base + '/history',
                          'reviews': base + '/reviews', 'checkpoints': base + '/checkpoints'}
        brief['generated_at'] = now_iso(self.service._now())
        return 200, brief

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/claim')
    def tasks_claim(self, ctx):
        self._project(ctx, CAP_TASKS)
        payload = self._task_payload(ctx)
        actor = self.service.bind_actor(ctx.principal, payload.pop('actor', None))
        payload['actor'] = actor

        def claim():
            result = self.backend.invoke('tasks.claim', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_TASKS)
            return result, result
        return self._mutate(ctx, 'tasks.claim', ctx.params['pid'], claim,
                            capability=CAP_TASKS, serialize=False, canonical=True)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/checkpoints')
    def checkpoints_add(self, ctx):
        self._project(ctx, CAP_CHECKPOINTS)
        payload = self._task_payload(ctx)
        payload.setdefault('schema_version', 1)
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            result = self.backend.invoke('checkpoints.add', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_CHECKPOINTS)
            return result, result
        return self._mutate(ctx, 'checkpoints.add', ctx.params['pid'], add, status=201,
                            capability=CAP_CHECKPOINTS, serialize=False, canonical=True)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/reviews')
    def reviews_add(self, ctx):
        payload = self._task_payload(ctx)
        payload.setdefault('schema_version', 1)
        # Approval is an owner action; the contributor who delivered the work can
        # contribute or request changes, never approve. A worker credential can never
        # approve at all.
        capability = CAP_APPROVE if payload.get('operation') == 'approve' else CAP_REVIEWS
        self._project(ctx, capability)
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            result = self.backend.invoke('reviews.add', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=capability)
            return result, result
        return self._mutate(ctx, 'reviews.add', ctx.params['pid'], add, status=201,
                            capability=capability, serialize=False, canonical=True)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/history')
    def tasks_history(self, ctx):
        self._project(ctx, CAP_READ)
        limit, state = self._page(ctx, ctx.query)
        result = self.backend.task_history(ctx.params['pid'], ctx.params['tid'], limit,
                                           state['o'], canonical=state['x'])
        who = lambda e: (e.get('user_id') or e.get('author')) if isinstance(e, dict) else None
        # (A canonical entry's author may be structured; ActorNames.get ignores it.)
        names = self.service.actor_names([who(e) for e in result['items']])
        body = {'items': [dict(e, user_name=names.get(who(e))) if isinstance(e, dict) else e
                          for e in result['items']], 'total': result['total']}
        if result.get('activity_cursor'):
            body['activity_cursor'] = result['activity_cursor']
        continuation = result.get('canonical_cursor')
        body['next_cursor'] = (
            make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                        state['o'] + len(result['items']), continuation)
            if continuation else None)
        return 200, body

    # -- review queue and personal work ---------------------------------------
    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/queue')
    def project_queue(self, ctx):
        """Contributions in flight, grouped by who acts next (the Reviews page).

        Readable by any project member (``CAP_READ``): every row is already visible
        in the task list; the queue only orders and projects it. ``state`` narrows to
        one review state.
        """
        self._project(ctx, CAP_READ)
        limit, state = self._page(ctx, ctx.query)
        wanted = ctx.query.get('state')
        if wanted is not None and wanted not in ACTIVE_REVIEW_STATES:
            raise invalid('state must be one of %s' % ', '.join(ACTIVE_REVIEW_STATES))
        read = self._review_queue(ctx.params['pid'])
        items = [item for item in read['items']
                 if (item['review_state'] == wanted if wanted else
                     item['review_state'] in ACTIVE_REVIEW_STATES)]
        body = self._paged(ctx, items, limit, state, complete=bool(read.get('complete')),
                           generated_at=now_iso(self.service._now()))
        body['items'] = self._task_views(body['items'])
        return 200, body

    @route('GET', r'/v1/me/work')
    def me_work(self, ctx):
        """The signed-in person's own work and review attention across their projects.

        Session only (an agent reads ``/v1/agents/me/next``). Walks the caller's own
        memberships, re-authorizing each project with the live ``CAP_READ`` check,
        one queue read per project and at most :data:`ME_WORK_MAX_PROJECTS` projects.
        ``to_review`` lists contributions only in projects where the caller holds the
        approval capability. Computed at read time; nothing is scheduled or marked.
        """
        principal = ctx.principal
        if principal.via == 'credential':
            raise forbidden('Session authority required; an agent reads /v1/agents/me/next')
        actor = principal.actor or principal.user_id
        projects = [p for p in self.service.list_projects(principal)
                    if not p.get('archived') and principal.user_id in (p.get('members') or [])]
        truncated = len(projects) > ME_WORK_MAX_PROJECTS
        assigned, to_review, unavailable = [], [], []
        for project in projects[:ME_WORK_MAX_PROJECTS]:
            capabilities = self.service.capabilities_for(principal, project['id'])
            if CAP_READ not in capabilities:
                continue
            try:
                read = self._review_queue(project['id'], shared=True)
            except HttpError as error:
                unavailable.append({'project': project['id'], 'reason': error.code})
                continue
            truncated = truncated or not read.get('complete')
            for item in read['items']:
                row = dict(item, project_name=project['name'])
                if item.get('assignee') == actor and item.get('status') != 'closed':
                    assigned.append(row)
                if CAP_APPROVE in capabilities and item['review_state'] in (
                        'awaiting-review', 'legacy-review-ready', 'awaiting-integration',
                        'approved'):
                    to_review.append(row)
        if len(assigned) > MAX_PAGE or len(to_review) > MAX_PAGE:
            truncated = True
        try:
            agents = self._agent_items(principal)
        except HttpError:
            agents = []
        return 200, {'assigned': self._task_views(assigned[:MAX_PAGE]),
                     'to_review': self._task_views(to_review[:MAX_PAGE]),
                     'agents': agents, 'truncated': truncated, 'unavailable': unavailable,
                     'generated_at': now_iso(self.service._now())}

    # -- feedback and audit ----------------------------------------------------
    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/feedback')
    def feedback_add(self, ctx):
        self._project(ctx, CAP_FEEDBACK)
        payload = dict(ctx.payload or {})
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            result = self.backend.invoke('feedback.add', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_FEEDBACK)
            return result, result
        return self._mutate(ctx, 'feedback.add', ctx.params['pid'], add, status=201,
                            capability=CAP_FEEDBACK, serialize=False, canonical=True)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/feedback')
    def feedback_list(self, ctx):
        self._project(ctx, CAP_READ)
        limit, state = self._page(ctx, ctx.query)
        result = self.backend.list_feedback(ctx.params['pid'], limit, state['o'])
        names = self.service.actor_names([f.get('user_id') for f in result['items']])
        result['items'] = [dict(f, author_name=names.get(f.get('user_id'), f.get('actor')),
                                at=f.get('created_at')) for f in result['items']]
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             state['o'] + limit)
                                 if state['o'] + limit < result['total'] else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/audit')
    def audit_list(self, ctx):
        self._project(ctx, CAP_PROJECT_ADMIN)
        limit, state = self._page(ctx, ctx.query)
        events = [e for e in self.service.state['audit'] if e.get('project_id') == ctx.params['pid']]
        page = events[state['o']:state['o'] + limit]
        names = self.service.actor_names([e.get('user_id') for e in page])
        body = {'items': [dict(e, user_name=names.get(e.get('user_id')), detail=e.get('reason'))
                          for e in page], 'total': len(events)}
        body['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                           state['o'] + limit)
                               if state['o'] + limit < len(events) else None)
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
        state = read_cursor(ctx.principal, ctx.params.get('pid'), query, query.get('cursor'))
        return limit, state


def build_handler(service, backend, *, trusted_proxies=(), max_body=MAX_BODY_BYTES,
                  web_root=DEFAULT_WEB_ROOT):
    return type('ConfiguredApiHandler', (ApiHandler,), {
        'service': service, 'backend': backend,
        'web_root': str(web_root) if web_root is not None else None,
        'read_cache': {}, 'read_cache_lock': threading.Lock(),
        'trusted_proxies': tuple(trusted_proxies or ()),
        'max_body': max_body,
    })


def create_server(service, backend, *, host='127.0.0.1', port=0, trusted_proxies=(),
                  max_body=MAX_BODY_BYTES, certfile=None, keyfile=None,
                  allow_plaintext_non_loopback=False, web_root=DEFAULT_WEB_ROOT):
    """Bind the service. Refuse a non-loopback plaintext listener unless explicitly allowed."""
    loopback = host in LOOPBACK
    if not loopback and certfile is None and not allow_plaintext_non_loopback:
        raise ValueError('Refusing plaintext on a non-loopback interface; supply TLS or '
                         'explicitly allow disposable plaintext')
    httpd = ThreadingHTTPServer((host, port), build_handler(service, backend,
                                                            trusted_proxies=trusted_proxies,
                                                            max_body=max_body,
                                                            web_root=web_root))
    httpd.daemon_threads = True
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def build_backend(service, args):
    """Select the canonical backend. ``endpoint`` is the documented Linux service."""
    if args.backend == 'endpoint':
        return EndpointBackend(args.endpoint_python, args.endpoint, args.root,
                               service=service, actor_namespace=args.actor_namespace,
                               timeout=args.endpoint_timeout)
    return InProcessBackend(service)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Orchestra authenticated HTTP service')
    parser.add_argument('--state', required=True, help='private service state path (outside source)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', help='TLS certificate (PEM)')
    parser.add_argument('--key', help='TLS private key (PEM)')
    parser.add_argument('--trusted-proxy', action='append', default=[], metavar='ADDR',
                        help='honor forwarded headers only from this address/CIDR (repeatable)')
    parser.add_argument('--trust-proxy', action='store_true',
                        help='legacy alias: trust forwarded headers from loopback only')
    parser.add_argument('--backend', choices=('endpoint', 'inprocess'), default='endpoint',
                        help='canonical binding (default: endpoint)')
    parser.add_argument('--endpoint-python', default='python3',
                        help='interpreter that runs the canonical endpoint')
    parser.add_argument('--endpoint', help='path to canonical endpoint.py')
    parser.add_argument('--root', help='canonical runtime root passed to the endpoint')
    parser.add_argument('--actor-namespace', default='http',
                        help='actor namespace attributed to session principals')
    parser.add_argument('--endpoint-timeout', type=int, default=150)
    parser.add_argument('--max-body', type=int, default=MAX_BODY_BYTES)
    parser.add_argument('--public-url',
                        help='canonical base URL of this service, used only to render '
                             'copyable agent setup/resume snippets (e.g. https://host)')
    parser.add_argument('--bootstrap-user', help='one-time operator bootstrap superuser')
    web = parser.add_mutually_exclusive_group()
    web.add_argument('--web-root', default=str(DEFAULT_WEB_ROOT),
                     help='directory holding the browser interface served at / '
                          '(default: the kit web/ directory)')
    web.add_argument('--no-web', action='store_true',
                     help='serve only the JSON API; do not serve the browser interface')
    args = parser.parse_args(argv)

    store = Store(args.state)
    if args.bootstrap_user:
        import getpass
        password = getpass.getpass('New superuser password: ')
        Service.bootstrap_superuser(store, args.bootstrap_user, password)
        print('Bootstrapped %s' % args.bootstrap_user)
        return 0
    if args.backend == 'endpoint' and (not args.endpoint or not args.root):
        parser.error('--backend endpoint requires --endpoint and --root '
                     '(use --backend inprocess only for a disposable local check)')
    trusted = list(args.trusted_proxy)
    if args.trust_proxy and 'localhost' not in trusted:
        trusted.append('localhost')
    service = Service(store, public_url=args.public_url)
    backend = build_backend(service, args)
    httpd = create_server(service, backend, host=args.host, port=args.port,
                          trusted_proxies=trusted, max_body=args.max_body,
                          certfile=args.cert, keyfile=args.key,
                          web_root=None if args.no_web else args.web_root)
    print('orchestra-http listening on %s:%d (backend=%s, web=%s)'
          % (args.host, httpd.server_address[1], args.backend,
             'off' if args.no_web else args.web_root))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
