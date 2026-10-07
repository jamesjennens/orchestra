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
import errno
import hashlib
import ipaddress
import json
import os
import record_json
import re
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import agent_prompts
import project_setup
from coordination import MERGE_SLOT_SUFFIX, is_merge_slot, merge_slot_sentence
from reserved_comments import (ANCHOR_READ_IDS_MAX, carries_record_label, hide_records,
                               is_record_anchor)
from http_auth import (AGENT_SECRET_ENV, agent_secret_file, CAP_ACCOUNTS_ADMIN, CAP_AGENTS, CAP_APPROVE,
                       CAP_CHECKPOINTS, CAP_FEEDBACK, CAP_PROPOSALS,
                       CAP_PROJECT_ADMIN, CAP_PROJECT_CREATE, CAP_PROJECT_HOST_CREATE, CAP_READ, CAP_REVIEWS,
                       CAP_TASKS, RESULT_RETENTION_SECONDS, HttpError, Service, Store,
                       address_group, authority_request, busy, conflict, forbidden, invalid, not_found,
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
#: How far back `/v1/me/contributions` pages: the endpoint's own list ceiling per read.
ME_CONTRIBUTIONS_MAX = 100
#: Bound on the per-task detail reads one ``GET /v1/me/work`` adds on a backend whose
#: queue lacks request ids, review times and checkpoint state (the canonical binding:
#: one ``brief`` subprocess per task, cached per principal like the queue).
ME_WORK_DETAIL_MAX = 10
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
# A reference catalog key (reference_records.KEY): lowercase and dotted.
REFERENCE_KEY = r'[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+'
PROPOSAL_KEY = r'p-[0-9a-f]{12}'
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
        data = record_json.loads(base64.urlsafe_b64decode(padded.encode('ascii')))
    except (ValueError, binascii.Error, UnicodeError):
        raise conflict('Invalid cursor')
    if not isinstance(data, dict) or data.get('u') != principal.user_id or \
            data.get('p') != project_id or \
            data.get('q') != request_hash(cursor_scope(query))[:16] or \
            type(data.get('o')) is not int or data['o'] < 0:      # a bool is not an offset
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


# The canonical Beads project name rule, as admin.validate_name enforces it on the host
# and endpoint.py on every request: 2-24 lowercase letters/digits, beginning with a letter.
CANONICAL_PROJECT = re.compile(r'[a-z][a-z0-9]{1,23}')
#: A calendar day as the server's own records write it.
DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
REGISTER_HINT = ('A project is created on the coordination host by an operator (admin.py add-project NAME); '
                 'a superuser then registers it here with that name.')
NO_CANONICAL = ('This project has no canonical Beads project behind it (it was created by an older kit), so its '
                'tasks cannot be used. Archive it; ' + REGISTER_HINT)
UNCONFIRMED = ('This project was created before registering a project was limited to superusers, and no superuser '
               'has confirmed it, so it cannot be used. A superuser confirms it (Projects page, or POST '
               '/v1/projects/ID/confirm) or archives it.')

#: Said instead of the record's own sentence when the record is ARCHIVED and the backend
#: will not serve it (kittrial-5bb.90 review item 2.1): the confirm route refuses an
#: archived record and the record is already archived, so "confirm it or archive it" is
#: two dead ends. Only removing the access being added works, and this says which call
#: does it, and who may make it (kittrial-5bb.140): the refusal goes to whoever tried to
#: add access, usually the agent's owner, but the project-scoped DELETE needs the project
#: role, so it is offered beside the PATCH that the owner can use.
ARCHIVED_UNUSABLE = ('This project is archived and this server will not serve its record; an archived record '
                     'cannot be confirmed, so nothing that grants access is accepted on it. Remove the access '
                     'being added instead, which does work on an archived record: an agent grant (PATCH the '
                     'agent with its projects without this one, or DELETE /v1/projects/ID/agents/AGENT, which a '
                     'project owner or superuser may use), a membership, or a worker credential.')

#: Canonical names that cannot be registered: the same segment is a literal route under
#: ``/v1/projects/...`` (``GET /v1/projects/unconfirmed`` is the upgrade check), so a
#: project with that name could never be read back (kittrial-5bb.90).
RESERVED_PROJECTS = frozenset({'unconfirmed'})


def project_unusable(service, project_id):
    """Why the endpoint backend will not serve a project record: (kind, reason), or None.

    The one predicate behind `usable`, the route refusals and the backend guard
    (kittrial-5bb.80 and .84), read at request time:

    - `no-canonical`: the id is not a canonical project name (a `proj_...` record from an
      older kit). No canonical project can ever be behind it.
    - `unconfirmed`: the id is a canonical name, but nothing shows a superuser stood
      behind the mapping. Before kittrial-5bb.80 any account could create a record with
      such an id and so become owner of that canonical project. A record counts when the
      register route wrote it (`registered_by`), a superuser confirmed it
      (`confirmed_by`), or its creator is a superuser today.

    An id with no record is not unusable here: that is the register route's own check.
    """
    if not isinstance(project_id, str) or not CANONICAL_PROJECT.fullmatch(project_id):
        return 'no-canonical', NO_CANONICAL
    state = getattr(service, 'state', None)
    record = (state or {}).get('projects', {}).get(project_id)
    if not isinstance(record, dict) or record.get('registered_by') or record.get('confirmed_by'):
        return None
    creator = (state.get('users') or {}).get(record.get('created_by'))
    if isinstance(creator, dict) and creator.get('superuser'):
        return None
    return 'unconfirmed', UNCONFIRMED


def unusable_projects(service):
    """Every project record the endpoint backend will not serve and that is not archived,
    for a superuser's review: who created it and who its members are."""
    state = service.state
    users = state.get('users') or {}

    def person(user_id):
        return {'id': user_id, 'username': (users.get(user_id) or {}).get('username')}
    items = []
    for project_id, record in sorted(state.get('projects', {}).items()):
        verdict = project_unusable(service, project_id)
        if verdict is None or record.get('archived'):
            continue
        members = state.get('memberships', {}).get(project_id, {})
        items.append({'id': project_id, 'name': record.get('name'), 'kind': verdict[0], 'reason': verdict[1],
                      'created_by': person(record.get('created_by')), 'created_at': record.get('created_at'),
                      'members': [dict(person(user_id), role=role) for user_id, role in sorted(members.items())]})
    return items


class InProcessBackend:
    """Disposable canonical operations used for local validation.

    Every ``invoke`` is idempotent for a (principal, project, route, key) tuple: the
    committed result is stored and returned on an exact retry, so an uncertain
    response can be reconciled without duplicating a record.
    """

    ROUTES = ('tasks.create', 'tasks.update', 'tasks.claim', 'checkpoints.add',
              'reviews.add', 'feedback.add', 'projects.create', 'projects.archive',
              'members.set', 'members.remove', 'credentials.issue', 'credentials.revoke',
              'proposals.submit', 'proposals.dispose')
    # Contributed requirement proposals are native records behind the canonical endpoint;
    # this service-local backend has no store for them (the routes answer 501).
    PROPOSALS = False
    # Everything here is service-local, so a project is created by the HTTP route itself.
    PROJECT_CREATE = 'create'

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
        if operation not in ('contribute', 'request-changes', 'respond', 'approve', 'recommend'):
            raise invalid('Review operation must be contribute, request-changes, respond, '
                          'approve or recommend')
        contributions = self.state.setdefault('contributions', {}).setdefault(task['id'], [])
        if operation == 'recommend':
            return self._recommend(principal, project_id, task, contributions, payload)
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
            # Like the canonical projection, a new revision does not resolve requests
            # that need a respond record: they stay open until the contributor responds.
            task['review_state'] = ('changes-requested' if self._requests(contributions, True)
                                    else 'awaiting-review')
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
        actor = payload.get('actor') or principal.actor
        items, resolutions = [], []
        if operation == 'request-changes':
            items = self._review_items(payload.get('items'))
        elif operation == 'respond':
            # Canonical rule (review_workflow.execute): only the current assignee may
            # respond, and every resolution must name a still-open request item.
            if not task.get('assignee') or actor != task['assignee']:
                raise forbidden('Only the task assignee may respond to requested changes')
            resolutions = self._resolutions(payload.get('resolutions'),
                                            self._requests(contributions, True))
        elif operation == 'approve' and self._requests(contributions, True):
            raise conflict('Cannot approve while requested changes remain unresolved')
        record = {'id': 'rev_' + secrets.token_hex(6), 'task_id': task['id'], 'kind': operation,
                  'contribution_id': current[-1]['id'],
                  'summary': (payload.get('summary') or '')[:1200],
                  'items': items,
                  'actor': actor,
                  'created_at': now_iso(self.service._now())}
        if operation == 'request-changes':
            # Marks the request as following the canonical respond rule. Requests
            # recorded before the respond step existed carry no flag and keep the old
            # rule (a later revision resolves them), so older disposable state reads
            # the same as before.
            record['needs_respond'] = True
        if operation == 'respond':
            record['resolutions'] = resolutions
        contributions.append(record)
        if operation == 'approve':
            task['review_state'] = 'approved'
        else:
            task['review_state'] = ('changes-requested' if self._requests(contributions, True)
                                    else 'awaiting-review')
        task['version'] += 1
        self._event(project_id, task['id'], operation, principal, record['actor'])
        return {'review': record, 'task_version': task['version']}

    def _recommend(self, principal, project_id, task, records, payload):
        """A reviewer's recommendation (kittrial-5bb.115), with the canonical rules.

        Kept in its own list, beside the review records, so it never becomes the
        chain's latest record and never changes the review state. The same rules as
        ``review_recommendations.execute``: the current contribution and its commit,
        only while it awaits review, never by its author or the task's assignee.
        """
        import review_recommendations as rec
        from review_workflow import author_key, plain_text
        current = [r for r in records if r['kind'] == 'contribution']
        if not current:
            raise conflict('There is no contribution to recommend')
        contribution = current[-1]
        if payload.get('contribution') != contribution['id']:
            raise conflict('That is not the task\'s current contribution; reread the task before recommending',
                           {'current_contribution': contribution['id']})
        if payload.get('commit') != contribution['commit']:
            raise conflict('That is not the current contribution\'s commit; reread the task before recommending')
        if (task.get('review_state') or 'none') != 'awaiting-review' or task.get('status') == 'closed':
            raise conflict('A recommendation is accepted only while the contribution awaits review')
        if payload.get('verdict') not in rec.VERDICTS:
            raise invalid('verdict must be approve; a reviewer who wants changes requests changes')
        summary = payload.get('summary')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > rec.SUMMARY_MAX:
            raise invalid('A recommendation summary must be 1-%d characters' % rec.SUMMARY_MAX)
        raw = payload.get('items')
        if raw is not None and not isinstance(raw, list):
            raise invalid('Recommendation items must be a list')
        items = self._review_items(raw)
        try:
            plain_text(summary, 'summary')
            for item in items:
                plain_text(item['text'], 'note text')
        except ValueError as error:
            raise invalid(str(error))
        actor = payload.get('actor') or principal.actor
        if author_key(actor) in (author_key(contribution['actor']), author_key(task.get('assignee'))):
            raise forbidden('Nobody recommends their own contribution')
        if author_key(actor) in {author_key(view['author']) for view in self._standing_recommendations(task, records)}:
            raise conflict(rec.ALREADY_RECOMMENDED % actor)
        record = {'id': 'rec_' + secrets.token_hex(6), 'task_id': task['id'], 'kind': 'recommendation',
                  'contribution_id': contribution['id'], 'commit': contribution['commit'],
                  'verdict': payload['verdict'], 'summary': summary.strip(), 'items': items, 'actor': actor,
                  'created_at': now_iso(self.service._now()),
                  # Its place among the review records, so a later decision makes it lapse.
                  'after': len(records)}
        self.state.setdefault('recommendations', {}).setdefault(task['id'], []).append(record)
        self._event(project_id, task['id'], 'recommend', principal, actor)
        return {'recommendation': self._recommendation_view(record), 'task_version': task['version']}

    @staticmethod
    def _recommendation_view(record):
        return {'id': record['id'], 'author': record['actor'], 'at': record['created_at'],
                'contribution': record['contribution_id'], 'commit': record['commit'],
                'verdict': record['verdict'], 'summary': record['summary'],
                'items': [dict(item) for item in record['items']]}

    def _standing_recommendations(self, task, records):
        """The recommendations that stand for the task's current contribution, newest first."""
        from review_workflow import author_key
        current = [r for r in records if r['kind'] == 'contribution']
        if not current or (task.get('review_state') or 'none') != 'awaiting-review' \
                or task.get('status') == 'closed':
            return []
        contribution = current[-1]
        decided = max([position for position, record in enumerate(records)
                       if record['kind'] in ('request-changes', 'approve')
                       and record.get('contribution_id') == contribution['id']] or [-1])
        # The author is excluded on every read; the assignee only when a recommendation is
        # written, so a later reassignment does not hide an honest one (as canonically).
        excluded = {author_key(contribution['actor'])}
        newest = {}
        for record in self.state.get('recommendations', {}).get(task['id']) or []:
            if record['contribution_id'] != contribution['id'] or record['after'] <= decided:
                continue
            if author_key(record['actor']) in excluded:
                continue
            newest[author_key(record['actor'])] = record
        ordered = sorted(newest.values(), key=lambda record: (record['after'], record['created_at']), reverse=True)
        return [self._recommendation_view(record) for record in ordered[:20]]

    @staticmethod
    def _requests(records, open_only=False):
        """Every requested-change item with its status, in record order.

        A request recorded with ``needs_respond`` stays open until a ``respond``
        record resolves it (the canonical rule), whatever revisions arrive meanwhile.
        A legacy request without the flag is resolved by the next revision.
        """
        contributions = [r for r in records if r['kind'] == 'contribution']
        resolved = {}
        for record in records:
            if record['kind'] == 'respond':
                for item in record.get('resolutions') or []:
                    resolved[(item['request'], item['item'])] = dict(item, at=record['created_at'],
                                                                     author=record['actor'])
        found = []
        for position, record in enumerate(records):
            if record['kind'] != 'request-changes':
                continue
            later = [r for r in records[position + 1:] if r['kind'] == 'contribution']
            items = record.get('items') or (
                [{'id': 'item-1', 'text': record['summary']}] if record.get('summary') else [])
            for item in items:
                entry = {'id': item['id'], 'request': record['id'], 'text': item['text'],
                         'contribution': record['contribution_id'], 'author': record['actor'],
                         'at': record['created_at'], 'status': 'open', 'resolution': None}
                answer = resolved.get((record['id'], item['id']))
                if answer is not None:
                    entry.update(status='resolved', resolution=answer['reason'],
                                 evidence=answer['evidence'], resolved_at=answer['at'],
                                 resolved_by=answer['author'])
                elif not record.get('needs_respond') and later:
                    entry.update(status='resolved', resolution='Addressed in revision %d'
                                 % (contributions.index(later[0]) + 1))
                found.append(entry)
        return [r for r in found if r['status'] == 'open'] if open_only else found

    @staticmethod
    def _resolutions(raw, open_requests):
        """Validate a respond's resolutions the way ``review_workflow.validate`` does."""
        if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
            raise invalid('A response needs 1-20 resolutions')
        pending = {(r['request'], r['id']) for r in open_requests}
        seen, clean = set(), []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {'request', 'item', 'reason',
                                                           'evidence'}:
                raise invalid('Each resolution needs exactly request, item, reason and evidence')
            for field in ('request', 'item'):
                if not isinstance(item[field], str) or not SAFE_ID.fullmatch(item[field]):
                    raise invalid('Resolution %s must be an id' % field)
            for field in ('reason', 'evidence'):
                if not isinstance(item[field], str) or not item[field].strip() or \
                        len(item[field]) > 1000 or '\x00' in item[field]:
                    raise invalid('Resolution %s must be 1-1000 characters' % field)
            key = (item['request'], item['item'])
            if key in seen:
                raise invalid('Duplicate resolution')
            if key not in pending:
                raise conflict('A resolution must name a still-open requested change; reread '
                               'the task')
            seen.add(key)
            clean.append({'request': item['request'], 'item': item['item'],
                          'reason': item['reason'].strip(), 'evidence': item['evidence'].strip()})
        return clean

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
    def references(self, project_id, options):
        """The reference catalog (.41 slice 1): the in-process backend holds no native
        records, so its catalog is honestly empty."""
        return {'schema_version': 1, 'total': 0, 'items': [], 'next_offset': None,
                'coverage': 'the in-process backend holds no native reference records'}

    def reference(self, project_id, key):
        raise not_found('Reference not found')

    def read_tasks(self, project_id):
        """One full canonical snapshot of every in-project row.

        This is the single-read seam (:data:`AGENT_MAX_PAGES` walks chunk it in
        memory instead of re-reading the canonical store per page). ``list_tasks``
        stays the paginated view over the *same* snapshot so the HTTP page route is
        unchanged.
        """
        tasks = self._project_rows(project_id)
        return {'items': tasks, 'total': len(tasks)}

    def _project_rows(self, project_id):
        tasks = [t for t in self.state['tasks'].values() if t['project_id'] == project_id]
        tasks.sort(key=lambda t: t['id'])
        # Record anchors never reach a task surface (kittrial-5bb.64).
        return hide_records([dict(t) for t in tasks])

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

        A requested change is open until the contributor's ``respond`` record resolves
        it, as canonically; a newer revision alone does not (``_requests`` keeps the
        old revision-resolves rule only for requests recorded before the respond step).
        """
        records = self.state.get('contributions', {}).get(task['id'], [])
        contributions = [r for r in records if r['kind'] == 'contribution']
        requests = self._requests(records)
        contribution = None
        if contributions:
            current = contributions[-1]
            contribution = {'id': current['id'], 'revision': len(contributions),
                            'commit': current['commit'], 'base_commit': current['base_commit'],
                            'branch': current.get('branch'), 'summary': current['summary'],
                            'author': current['actor'], 'at': current['created_at']}
        standing = self._standing_recommendations(task, records)
        return {'state': task.get('review_state') or 'none', 'contribution': contribution,
                # Additive (kittrial-5bb.115): advice to approve the current contribution.
                'recommendation': standing[0] if standing else None,
                # Each in full: the brief route leaves out the author's own person and shows the next.
                'recommendations': [dict(r) for r in standing],
                'requests': requests,
                'open_requests': sum(1 for r in requests if r['status'] == 'open'),
                'latest_id': records[-1]['id'] if records else None,
                'latest_at': records[-1]['created_at'] if records else None,
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
                                          'text': item.get('text'), 'source': item.get('source')}
                                         for item in last.get('open_items') or []]}
        return {'task': dict(task), 'checkpoint': checkpoint, 'review': self._review_view(task),
                'carry_open_items': [dict(item) for item in checkpoints[-1].get('open_items') or []
                                     if isinstance(item, dict)] if checkpoints else [],
                # The disposable backend records no lifecycle evidence, so every fact is
                # honestly unknown rather than inferred from the review state.
                'lifecycle': {}, 'depends_on': [],
                # This backend keeps no activity cursor; its checkpoints need none.
                'activity_cursor': None}

    #: The disposable backend is cheap to read and tests expect fresh reads.
    READ_CACHE_SECONDS = 0

    def review_states(self, project_id, queue=None):
        """Review state of every task: in-process rows carry it themselves."""
        return {'states': {t['id']: t.get('review_state') or 'none'
                           for t in self.read_tasks(project_id)['items']},
                'complete': True}

    def agent_tasks(self, project_id, actor=None, queue=None):
        """The tasks one actor holds (every held task when ``actor`` is None), each with
        what agent attention needs. ``queue`` is accepted for the endpoint backend's
        signature and is not needed here.

        The same shape as :meth:`EndpointBackend.agent_tasks` (which reads the canonical
        ``work`` view): review state, the open request ids, whether a contribution
        exists, how many open items the latest checkpoint lists, when it was written
        and whether a review record is newer than it. A closed task stays listed only
        while its review is still active, as in ``work``.
        """
        if queue is not None:
            return own_queue_tasks(queue, actor)
        rows = []
        for task in self._project_rows(project_id):
            if not task.get('assignee') or (actor is not None and task.get('assignee') != actor):
                continue
            review = self._review_view(task)
            if task.get('status') == 'closed' and review['state'] not in ('changes-requested', 'error',
                                                                       'awaiting-integration', 'approved'):
                continue
            rows.append({'id': task['id'], 'title': task.get('title'), 'status': task.get('status'),
                         'assignee': task.get('assignee'), 'review_state': review['state'],
                         'contribution_id': (review['contribution'] or {}).get('id'),
                         **self._agent_fields(task, review)})
        return {'tasks': rows, 'complete': True}

    def _agent_fields(self, task, review):
        saved=(self.state.get('checkpoints') or {}).get(task['id'])
        last=saved[-1] if isinstance(saved,list) and saved and isinstance(saved[-1],dict) else None
        unreadable=saved is not None and (not isinstance(saved,list) or
                   (bool(saved) and (last is None or not isinstance(last.get('open_items'),list))))
        at=last.get('created_at') if last and not unreadable else None
        return {'pending_change_requests':[r['id'] for r in review['requests'] if r['status']=='open'][:20],
                'open_items':None if unreadable else (len(last.get('open_items') or []) if last else 0),
                'checkpoint_at':at,
                'newer_activity':self._other_agent_activity(task,last) if last and not unreadable else None}

    def _other_agent_activity(self, task, checkpoint):
        events=[e for e in self.state.get('events') or [] if e.get('task')==task['id']]
        # Real writes have an ordered checkpoint event. Do not count that event
        # itself, even when an owner wrote the checkpoint for the assignee.
        anchors=[i for i,e in enumerate(events) if e.get('action')=='checkpoint-added'
                 and e.get('actor')==checkpoint.get('actor')
                 and e.get('time','')>=checkpoint.get('created_at','')]
        if anchors:
            return any(e.get('actor')!=task.get('assignee') for e in events[anchors[-1]+1:])
        # Older or directly imported in-process checkpoints have no event anchor.
        # Only attributed review records can establish external activity there.
        at=checkpoint.get('created_at')
        return bool(at and any(r.get('created_at','')>at and r.get('actor')!=task.get('assignee')
                               for r in self.state.get('contributions',{}).get(task['id'],[])))

    def review_queue(self, project_id):
        """Every task with current work, highest-attention review states first.

        Mirrors ``work.queue``: a closed task stays listed only while its review is
        still active. The project is read once; the route pages the result.
        """
        items = []
        for task in self.read_tasks(project_id)['items']:
            review = self._review_view(task)
            if task.get('status') == 'closed' and review['state'] not in ('changes-requested', 'error',
                                                                       'awaiting-integration', 'approved'):
                continue
            items.append(queue_item(
                project_id, task, review['state'], review['contribution'],
                review['open_requests'],
                pending_request_ids=[r['id'] for r in review['requests'] if r['status'] == 'open'],
                waiting_since=review['latest_at'],
                attention=self._agent_fields(task, review),
                recommended_by=[r['author'] for r in review['recommendations']],
                contribution_author=(review['contribution'] or {}).get('author')))
        items.sort(key=queue_order)
        return {'items': items, 'complete': True, 'warnings': []}


#: Review states that still need someone to act (``work.queue`` keeps these even on a
#: closed task). ``approved`` is the in-process name for ``awaiting-integration``.
#: ``withdrawn``/``superseded`` are additive (kittrial-5bb.94): the canonical ``work``
#: queue lists such a task while a blocking item is still open, so the HTTP queue and
#: its ``state=`` filter accept them too or the two transports disagree
#: (kittrial-5bb.110 item 9).
ACTIVE_REVIEW_STATES = ('changes-requested', 'error', 'awaiting-review', 'legacy-review-ready',
                        'awaiting-integration', 'approved', 'withdrawn', 'superseded')
QUEUE_PRIORITY = {'changes-requested': 0, 'error': 1, 'awaiting-review': 2,
                  'legacy-review-ready': 2, 'awaiting-integration': 3, 'approved': 3}

#: Review-payload fields the HTTP route accepts beyond the canonical per-operation
#: set. ``task_id`` is filled from the path and ``actor`` is bound to the
#: authenticated principal; ``contribution_revision``/``contribution_commit`` are the
#: web page's presentation copies of the revision the reviewer saw. A contribution may
#: also carry the two legacy in-process delivery fields ``bundle_sha256``/``branch``.
#: Anything else -- an unknown key, or a canonical field on the WRONG operation (a
#: top-level ``severity``, a ``disposition`` on a request-changes) -- is refused
#: instead of silently dropped (kittrial-5bb.110 item 4: those used to be dropped and
#: answered 201).
REVIEW_HTTP_FIELDS = frozenset({'task_id', 'actor', 'contribution_revision',
                                'contribution_commit'})
REVIEW_HTTP_OPERATION_FIELDS = {'contribute': frozenset({'bundle_sha256', 'branch'})}
#: How many unsupported field names one refusal reports, and how long each may be. The
#: refusal used to echo every name in full: a 300-character key came back whole, and 400
#: unknown keys produced a 2,429-character error (kittrial-5bb.110 item 4).
UNSUPPORTED_FIELDS_SHOWN = 5
#: A field name is echoed only when it is a plain identifier. Anything else (control
#: characters, punctuation, a name built to read as a sentence in the error) is not
#: repeated to the caller.
UNSUPPORTED_FIELD_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,39}\Z')


def unsupported_fields_text(keys):
    """A bounded, non-echoing list of the caller's unsupported review field names.

    At most ``UNSUPPORTED_FIELDS_SHOWN`` names, each echoed only when it is a plain
    identifier, with ``(+N more)`` for the rest (kittrial-5bb.110 item 4 P3).
    """
    names = sorted(str(key) for key in keys)
    shown = [name if UNSUPPORTED_FIELD_NAME.match(name) else '<non-identifier name>'
             for name in names[:UNSUPPORTED_FIELDS_SHOWN]]
    if len(names) > UNSUPPORTED_FIELDS_SHOWN:
        shown.append('(+%d more)' % (len(names) - UNSUPPORTED_FIELDS_SHOWN))
    return ', '.join(shown)


def queue_order(item):
    # Among contributions that await review, one a reviewer recommends approving comes
    # first: it is the one an owner can act on at once (kittrial-5bb.115).
    return (QUEUE_PRIORITY.get(item['review_state'], 4), 0 if item.get('recommended') else 1, str(item['id']))


def queue_item(project_id, task, review_state, contribution, open_requests,
               pending_request_ids=None, waiting_since=None, integration=None,
               integration_warnings=None, attention=None, recommended_by=None, contribution_author=None):
    """One review-queue row, in the shape both backends return.

    ``pending_request_ids`` and ``waiting_since`` (time of the latest review record)
    are filled where the backend knows them; the canonical ``work`` projection
    reports only a count of pending items, so there they stay empty/``None`` here and
    ``GET /v1/me/work`` fills a bounded number of rows from ``task_detail``.

    ``integration`` and ``integration_warnings`` are additive (kittrial-5bb.52):
    the canonical backend passes the ``work`` row's integration block and its
    any-pass-wins disagreement warnings through; the disposable backend records no
    lifecycle evidence and leaves them ``None``/empty.
    """
    return {'id': task.get('id'), 'project_id': project_id, 'title': task.get('title'),
            'status': task.get('status'), 'assignee': task.get('assignee'),
            'priority': task.get('priority'), 'created_at': task.get('created_at'),
            'updated_at': task.get('updated_at'),
            'review_state': review_state or 'none', 'contribution': contribution,
            'open_requests': open_requests,
            'pending_request_ids': list(pending_request_ids or []),
            'waiting_since': waiting_since,
            'integration': integration,
            'integration_warnings': list(integration_warnings or []),
            # What agent attention needs from the canonical ``work`` row (kittrial-5bb.114):
            # the request-changes record ids, and the latest checkpoint's open items,
            # time and whether anything is newer. None where the backend does not say.
            'attention': attention,
            # Additive (kittrial-5bb.115): reviewers who recommend approving the current
            # contribution while nobody has decided. Never an approval.
            'recommended': bool(recommended_by),
            'recommended_by': list(recommended_by or []),
            # Additive (kittrial-5bb.115 review): who delivered the current contribution, so
            # the person rule can be applied to a row as it is to a brief.
            'contribution_author': contribution_author}


def own_queue_tasks(queue, actor=None):
    """Split one current review snapshot by assignee, retaining unknown checkpoint state."""
    tasks=[]
    for row in queue.get('items') or []:
        if not isinstance(row,dict) or not row.get('assignee') or (actor is not None and row['assignee']!=actor):
            continue
        fields=row.get('attention') if isinstance(row.get('attention'),dict) else {}
        tasks.append({'id':row.get('id'),'title':row.get('title'),'status':row.get('status'),
                      'assignee':row.get('assignee'),'review_state':row.get('review_state'),
                      'contribution_id':(row.get('contribution') or {}).get('id'),
                      'pending_change_requests':list(fields.get('pending_change_requests') or [])[:20],
                      'open_items':fields.get('open_items',0),'checkpoint_at':fields.get('checkpoint_at'),
                      'newer_activity':fields.get('newer_activity')})
    return {'tasks':tasks,'complete':bool(queue.get('complete'))}


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
    PROPOSALS = True

    def __init__(self, python, endpoint, root, *, service, actor_namespace='http',
                 timeout=150, runner=None, create_timeout=900):
        self.python = python
        self.endpoint = endpoint
        self.root = root
        self.service = service
        self.actor_namespace = actor_namespace
        self.timeout = timeout
        #: How long one project creation may take (kittrial-5bb.118 part 2): it grows with the
        #: number of project databases on the server. See OPERATIONS, the cost of many projects.
        self.create_timeout = max(int(create_timeout), int(timeout))
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
    # A project's tasks live in a canonical Beads project that only an operator creates on
    # the host (admin.py add-project), so the HTTP route only REGISTERS an existing one
    # (kittrial-5bb.80). The HTTP project id is the canonical project name.
    PROJECT_CREATE = 'register'

    def project_exists(self, project):
        """Whether the canonical project `project` exists and is initialized on the host.

        One cheap read through the endpoint: endpoint.py refuses an unknown or
        uninitialized project with "Unknown/uninitialized project" before any native
        call. Any other failure is raised as the read's own error, never read as absent.
        """
        if not isinstance(project, str) or not CANONICAL_PROJECT.fullmatch(project):
            return False
        reply = self._endpoint('bd', project, self.actor_namespace + '/read',
                               ['list', '--limit', '1', '--json'], check_usable=False)
        if isinstance(reply, dict) and reply.get('returncode') == 2 \
                and 'Unknown/uninitialized project' in (reply.get('stderr') or ''):
            said = (reply.get('stderr') or '').strip().splitlines()[-1]
            marker = 'Unknown/uninitialized project: '
            if marker in said:
                # It is on the host and is a creation that has not finished (or whose record is
                # damaged): say that, not "no such project; run add-project" (review 01a109cc).
                raise conflict(said.split(marker, 1)[1][:300])
            return False
        self._checked(reply)
        # The endpoint serves a project whose creation record is damaged (a registered project
        # does not depend on that record), so registration asks the host explicitly whether
        # this name may be registered (kittrial-5bb.149). An endpoint without the action or
        # without the answer says nothing against it, as before.
        asked = self._endpoint('setup-status', project, self.actor_namespace + '/read', [], check_usable=False)
        if isinstance(asked, dict) and asked.get('returncode') == 0:
            try:
                status = _canonical_payload(asked.get('stdout') or '')
            except HttpError:
                status = None
            reason = status.get('creation_record') if isinstance(status, dict) else None
            if isinstance(reason, str) and reason:
                raise conflict(reason[:300])
        return True

    def _endpoint(self, action, project, actor, args, attachments=None, operation_id=None,
                  authority=None, require_authority=False, route=None, check_usable=True, timeout=None):
        """One answer of the canonical endpoint; its mark of a configuration fault is raised here.

        Here and not where a route reads the answer, so that no route can hand the
        endpoint's line (the path of the file, the parser's or the system's words) to the
        person: some routes read a return code of 2 their own way (kittrial-5bb.156 review).
        """
        reply = self._ask(action, project, actor, args, attachments, operation_id, authority,
                          require_authority, route, check_usable, timeout)
        if isinstance(reply, dict) and reply.get('returncode') == 2 and reply.get('fault') == 'configuration':
            # The server's own configuration file cannot be read: the endpoint's line names the
            # file and the parser's words. Those are for the operator, in this service's log;
            # the person is told who to ask. Nothing was carried out.
            said = (reply.get('stderr') or '').strip().splitlines()
            print('configuration: the endpoint could not read the deployment configuration for %s: %s'
                  % (action or 'a request', ascii(said[-1][:600]) if said else '(nothing)'), file=sys.stderr, flush=True)
            refused = HttpError(503, 'server_configuration', self.CONFIGURATION_UNREADABLE)
            refused.nothing_done = True
            raise refused
        return reply

    def _ask(self, action, project, actor, args, attachments, operation_id, authority,
             require_authority, route, check_usable, timeout):
        if isinstance(project, str):
            # A record the backend will not serve is refused here, before any endpoint
            # process starts: a `proj_...` id (no canonical project can be behind it;
            # this replaces the endpoint's 422 for a malformed name) or a canonical id
            # no superuser stands behind (kittrial-5bb.84). `project_exists` passes
            # check_usable=False: it is the read that registration and confirmation rest on.
            verdict = project_unusable(self.service, project) if check_usable else (
                None if CANONICAL_PROJECT.fullmatch(project) else ('no-canonical', NO_CANONICAL))
            if verdict is not None:
                raise conflict(verdict[1])
        store = self.service.store
        if authority and store.unsaved_since is not None:
            # The endpoint reads the session's idle deadline from the state file. A last-use
            # stamp that is only in memory (Store.save_soon) is written before the endpoint is
            # asked, so a live session is never refused as idle for it; when the lock still
            # cannot be had the request is answered busy, and nothing was sent.
            try:
                store.save()
            except TimeoutError as waited:
                print('busy: the state could not be saved before %s was sent to the endpoint, so it was not sent: %s'
                      % (action, ascii(str(waited)[:400])), file=sys.stderr, flush=True)
                not_sent = busy()
                not_sent.nothing_done = True
                raise not_sent from None
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
                                       capture_output=True, timeout=timeout or self.timeout)
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
        return self._checked(reply, action)

    #: How much of a canonical refusal's last line is handed on. A checkpoint refusal lists
    #: every problem with the record (kittrial-5bb.113), so it gets room for all of them.
    DETAIL_LIMIT = 200
    DETAIL_LIMITS = {'checkpoint': 6000}
    #: The follow-on base refusal is handed on whole (kittrial-5bb.158): cut at 200 it lost the
    #: reason, the recorder's name and what an operator must do.
    BASE_REFUSAL_LIMIT = 1500

    @classmethod
    def _detail_limit(cls, action, said):
        """How much of a canonical refusal's last line is handed on.

        The larger limit is for the kit's own sentence only, never for a caller's text echoed
        back at length: the line must BE the follow-on base refusal, whole, as
        ``review_workflow.BASE_REFUSAL`` describes it (the kit's words, hexadecimal commit
        ids, a recorder name of a constrained shape). Anything else keeps the limit it had.
        """
        if action == 'review' and cls.base_refusal(said) is not None:
            return cls.BASE_REFUSAL_LIMIT            # the longest form of the sentence is well below it
        return cls.DETAIL_LIMITS.get(action, cls.DETAIL_LIMIT)

    @staticmethod
    def base_refusal(said):
        """The follow-on base refusal in ``said`` (a canonical refusal line), or None when it is not one, whole."""
        from review_workflow import BASE_REFUSAL
        text = said[len('ValueError: '):] if isinstance(said, str) and said.startswith('ValueError: ') else None
        return text if text is not None and BASE_REFUSAL.fullmatch(text) else None

    @classmethod
    def _checked(cls, reply, action=None):
        """The payload of one canonical reply, or the HttpError its return code means."""
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
        if code == 75:
            # The endpoint was occupied (a wait for a lock ran out): the request may simply be
            # sent again. Its line names the lock file it waited for, a path of the host, so it
            # goes to this service's log for the operator and the person gets the service's own
            # sentence (kittrial-5bb.149).
            cls._log_busy(action, stderr)
            raise busy()
        if code:
            said = stderr.strip().splitlines()[-1] if stderr.strip() else None
            detail = said[:cls._detail_limit(action, said)] if said else None
            if code == 2:
                raise invalid('Canonical command rejected the request', detail)
            raise uncertain('Canonical command failed; outcome may be unknown')
        return _canonical_payload(stdout)

    #: Said when the endpoint reports that the server's configuration file cannot be read.
    CONFIGURATION_UNREADABLE = ("The server's configuration cannot be read, so this request was not carried out. "
                                'Ask an operator of the server to look.')

    @staticmethod
    def _log_busy(action, stderr):
        """Which lock, and how long: the endpoint's own line, for the operator, in the service log."""
        said = (stderr or '').strip().splitlines()
        print('busy: the endpoint answered return code 75 for %s: %s'
              % (action or 'a request', ascii(said[-1][:400]) if said else '(nothing)'), file=sys.stderr, flush=True)

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
            # Not when the answer itself says that nothing was carried out (kittrial-5bb.156):
            # the request was never sent to the endpoint, or the endpoint could not read the
            # server's configuration and refused it.
            if failure.status == 503 and not getattr(failure, 'nothing_done', False):
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
        # A reviewer's recommendation (kittrial-5bb.115): a record beside the chain, so
        # it carries no `previous` (see REVIEW_NO_PREVIOUS).
        'recommend': ('contribution', 'commit', 'verdict', 'summary', 'items'),
        # The additive review-workflow operations (kittrial-5bb.94, checked over HTTP by
        # kittrial-5bb.110 item 9). Without these the canonical backend refused every
        # one of them as an unknown operation, so the new states could not be reached
        # over HTTP at all.
        'withdraw': ('contribution', 'reason'),
        'request-review': ('contribution', 'reviewer'),
        'resolve-item': ('contribution', 'request', 'item', 'reason'),
        'decline-review': ('contribution', 'request', 'reason'),
    }
    #: Operations whose canonical record takes no ``previous``.
    REVIEW_NO_PREVIOUS = ('recommend',)
    #: Optional canonical review fields: forwarded only when the caller supplied them,
    #: so old payloads keep the exact legacy field set (no operation-inappropriate
    #: nulls) and a follow-on's additive ``follows`` relation is not silently dropped
    #: into a first contribution or a supersede. The request-changes summary, a
    #: request-review summary and the withdrawn/resolve dispositions are the same kind
    #: of additive field (kittrial-5bb.94 items 3 and 5).
    REVIEW_OPTIONAL_FIELDS = {
        'contribute': ('follows',),
        'request-changes': ('summary',),
        'request-review': ('summary',),
        'withdraw': ('disposition',),
        'resolve-item': ('disposition',),
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
            if title.startswith(('-', '@')):
                # The title is a positional argument of the native create: a leading dash
                # would be read as a flag, a leading @ as the attachment transport.
                raise invalid('A task title cannot start with "-" or "@"')
            description = payload.get('description')
            if description and str(description).strip():
                # A blank description is no description (as PATCH treats it as "clear"):
                # the endpoint refuses an empty body file.
                return ('bd', project_id, ['create', title, '@attachment:0', '--json'],
                        {'0': {'flag': '--body-file', 'text': description}})
            return 'bd', project_id, ['create', title, '--json'], {}
        if route == 'tasks.update':
            args = ['update', str(task_id), '--json']
            if str(payload.get('title') or '').startswith(('-', '@')):
                # Same rule as create (review 01a10308).
                raise invalid('A task title cannot start with "-" or "@"')
            if payload.get('title') is not None:
                args[2:2] = ['--title', str(payload['title'])]
            if payload.get('status') in ('open', 'closed'):
                args[2:2] = ['--status', payload['status']]
            attachments = {}
            if payload.get('description') is not None and not str(payload['description']).strip():
                # Clearing the description: bd refuses an empty body file, and an empty
                # value cannot be mistaken for a flag, so it goes inline (review 01a10352).
                args[2:2] = ['--description', '']
            elif payload.get('description') is not None:
                # Free text never travels as a free-standing argument: like create, the
                # description goes through the attachment transport, so a text that looks
                # like a flag ("--help"), a list ("- item") or the transport itself
                # ("@attachment:0") is stored as written whatever the native parser does.
                args[2:2] = ['@attachment:0']
                attachments = {'0': {'flag': '--body-file', 'text': str(payload['description'])}}
            return 'bd', project_id, args, attachments
        if route == 'tasks.claim':
            actor = payload.get('actor') or self._actor(principal)
            return ('bd', project_id,
                    ['update', str(task_id), '--status', 'in_progress', '--assignee',
                     str(actor), '--json'], {})
        if route == 'checkpoints.add':
            # A field left out is sent as left out: the canonical validator names it with
            # every other problem of the record, where refusing it here hid the rest
            # (kittrial-5bb.113 review).
            body = {field: payload[field] for field in self.CHECKPOINT_FIELDS if field in payload}
            body['schema_version'] = payload.get('schema_version', 1)
            body['task'] = task_id
            return ('checkpoint', project_id, [str(task_id), '@attachment:0'],
                    {'0': {'flag': '--file', 'text': json.dumps(body)}})
        if route == 'reviews.add':
            operation = payload.get('operation')
            # A fixed sentence, never the caller's value: `%r` of a 5,000-character
            # operation came back in a 5,204-character error (kittrial-5bb.110 item 4).
            # An operation sent as a list or object used to raise TypeError (an
            # unhashable dict key) and answer 500; the str check makes it this 422.
            if not isinstance(operation, str) or operation not in self.REVIEW_FIELDS:
                raise invalid('Unsupported review operation; expected one of: %s'
                              % ', '.join(sorted(self.REVIEW_FIELDS)))
            common = tuple(field for field in self.REVIEW_COMMON
                           if not (field == 'previous' and operation in self.REVIEW_NO_PREVIOUS))
            if operation == 'recommend' and payload.get('items') is None:
                # A NULL items is absent, exactly like every other null optional field the
                # released clients send (kittrial-5bb.110 items 1 and 2): a recommendation
                # with no notes is the empty list the canonical record requires.
                payload = dict(payload, items=[])
            fields = common + self.REVIEW_FIELDS[operation]
            missing = [field for field in fields
                       if field not in payload and not
                       (field == 'operation_id' and operation_id)]
            if missing:
                raise invalid('Canonical review payload is missing fields: %s'
                              % ', '.join(missing))
            body = {field: payload.get(field) for field in fields}
            for field in self.REVIEW_OPTIONAL_FIELDS.get(operation, ()):
                # A null optional field is ABSENT, not a value: the kit's own client used
                # to send every optional field as null, and forwarding `"summary": null`
                # made the canonical writer refuse a legacy request-changes
                # (kittrial-5bb.110 item 1). The canonical required fields (built above)
                # keep their null, because their ABSENCE is an invalid field set.
                if payload.get(field) is not None:
                    body[field] = payload.get(field)
            body['schema_version'] = payload.get('schema_version', 1)
            body['operation_id'] = payload.get('operation_id') or operation_id
            if not body['operation_id']:
                raise invalid('Canonical review payload needs an operation_id')
            body['operation'] = operation
            body['task'] = task_id
            return ('review', project_id, [str(task_id), '@attachment:0'],
                    {'0': {'flag': '--file', 'text': json.dumps(body)}})
        if route in ('proposals.submit', 'proposals.dispose'):
            # Contributed requirement proposals (.58 slice 1b). The body was composed by
            # the route from server-bound values; the endpoint re-verifies them against
            # the live authority it re-validates (proposal_records.HttpContext).
            body = dict(payload['body'])
            body['operation_id'] = body.get('operation_id') or operation_id
            if not body['operation_id']:
                raise invalid('Send an Idempotency-Key header or an operation_id')
            return ('proposal', project_id, [payload['command'], '@attachment:0'],
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

    def references(self, project_id, options):
        """One page of the reference catalog through the endpoint's read-only `ref list`."""
        args = ['list', '--limit', str(options['limit']), '--offset', str(options['offset'])]
        for tag in options.get('tags') or []:
            args += ['--tag', caller_arg(tag, 'tag')]
        for name in ('owner', 'state', 'due', 'authority'):
            if options.get(name):
                args += ['--' + name, caller_arg(options[name], name)]
        return self._ref_read(project_id, args)

    def reference(self, project_id, key):
        """One entry through `ref get`; an unknown or unfinished key is a 404."""
        return self._ref_read(project_id, ['get', caller_arg(key, 'key')], missing=True)

    def proposal_read(self, project_id, args, missing=False):
        """One read-only `proposal get|list|mine` through the endpoint (.58 slice 1b).

        Launched by this service, the endpoint returns the unfiltered view (coordinator
        text included); the ROUTE withholds it per caller. An unknown key is a 404.
        """
        reply = self._endpoint('proposal', project_id, self.actor_namespace + '/read', args)
        code = reply.get('returncode') if isinstance(reply, dict) else None
        stderr = (reply.get('stderr') or '') if isinstance(reply, dict) else ''
        if code == 2 and 'Unknown action' in stderr:
            raise not_implemented('The canonical endpoint is older than this HTTP service and has '
                                  'no requirement proposals; install the same kit for both')
        if code == 2 and missing and ('Unknown proposal key' in stderr or 'no revision record yet' in stderr):
            raise not_found('Proposal not found')
        payload = self._checked(reply)
        if not isinstance(payload, dict):
            raise uncertain('Canonical proposal read returned an unexpected shape')
        return payload

    def _ref_read(self, project_id, args, missing=False):
        reply = self._endpoint('ref', project_id, self.actor_namespace + '/read', args)
        code = reply.get('returncode') if isinstance(reply, dict) else None
        stderr = (reply.get('stderr') or '') if isinstance(reply, dict) else ''
        if code == 2 and 'Unknown action' in stderr:
            raise not_implemented('The canonical endpoint is older than this HTTP service and has '
                                  'no reference catalog; install the same kit for both')
        if code == 2 and missing and ('Unknown reference key' in stderr
                                      or 'no revision record yet' in stderr):
            raise not_found('Reference not found')
        payload = self._checked(reply)
        if not isinstance(payload, dict):
            raise uncertain('Canonical reference read returned an unexpected shape')
        return payload

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
        # Record anchors (references, proposals, settings, capabilities) are filtered
        # at this one seam, so the task list with its Closed/All tabs and `q` search,
        # agent attention and every other snapshot reader never shows them
        # (kittrial-5bb.64; the shared hidden-surface list).
        rows = hide_records(self._without_record_anchors(project_id,
                                                         self._in_project(rows, project_id)))
        # The project's merge slot is an internal record too (kittrial-5bb.113): on real
        # bd it is a row of type task, and was offered to agents as claimable work.
        rows = [row for row in rows if not is_merge_slot(row)]
        return {'items': rows, 'total': len(rows)}

    def _without_record_anchors(self, project_id, rows):
        """Drop the record anchors from a listed snapshot with ONE canonical read.

        ``bd list`` returns no comments, and a row is an anchor only when it also
        holds a v1 record comment, so the endpoint's read-only ``anchors`` action
        answers the snapshot in one native read (kittrial-5bb.71): just the labelled
        rows' labels and comments when there are at most ``ANCHOR_READ_IDS_MAX`` of
        them, otherwise one export. The read takes no coordination lock. A snapshot
        with no row carrying a record type label needs no extra read at all. A failed
        or malformed answer fails the list closed rather than showing anchors.
        """
        labelled = [str(row.get('id')) for row in rows
                    if isinstance(row, dict) and carries_record_label(row)]
        if not labelled:
            return rows
        args = labelled if len(labelled) <= ANCHOR_READ_IDS_MAX else []
        reply = self._endpoint('anchors', project_id, self.actor_namespace + '/read', args)
        if isinstance(reply, dict) and reply.get('returncode') == 2 and \
                'Unknown action' in (reply.get('stderr') or ''):
            # An endpoint from an older kit than this HTTP service.
            raise not_implemented('The canonical endpoint is older than this HTTP service '
                                  'and has no anchors read; install the same kit for both')
        reply = self._checked(reply)
        anchors = reply.get('anchors') if isinstance(reply, dict) else None
        if not isinstance(anchors, list) or not all(isinstance(item, str) for item in anchors):
            raise uncertain('Canonical anchors read returned an unexpected shape')
        hidden = set(anchors)
        return [row for row in rows if not (isinstance(row, dict) and str(row.get('id')) in hidden)]

    def _with_record_comments(self, project_id, rows):
        """Give rows carrying a record type label their comments.

        Used for the single row of :meth:`get_task` (``bd show`` returns no comments,
        and a row is a record anchor only when it also holds a v1 record comment):
        at most one extra ``comments`` read per request. Lists use
        :meth:`_without_record_anchors` instead, one read per snapshot.
        """
        out = []
        for row in rows:
            if isinstance(row, dict) and carries_record_label(row) and \
                    not isinstance(row.get('comments'), list):
                comments = self._run('bd', project_id, self.actor_namespace + '/read',
                                     ['comments', str(row.get('id')), '--json'])
                row = dict(row, comments=comments if isinstance(comments, list) else [])
            out.append(row)
        return out

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
        return self._with_record_comments(project_id, [row])[0]

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

    def task_detail(self, project_id, task_id):
        """What the ``work`` queue row lacks, from ONE canonical ``brief`` read.

        Used by ``GET /v1/me/work`` for at most ``ME_WORK_DETAIL_MAX`` rows per
        request. Returns the pending request item ids (the brief lists at most five;
        ``pending_request_ids_complete`` says whether that is all of them), the time
        the current wait began (newest pending request for changes-requested, the
        current revision's delivery for awaiting-review; ``None`` otherwise), the
        current revision number and ``blocked`` (the latest checkpoint lists
        unresolved items).
        """
        data = self._run('brief', project_id, self.actor_namespace + '/read',
                         [str(task_id), '--json', '--items-limit', '1'])
        if not isinstance(data, dict):
            raise uncertain('Canonical brief returned an unexpected shape')
        review = data.get('review') or {}
        pending = [p for p in review.get('pending_requests') or [] if isinstance(p, dict)]
        ids = [p['item'] for p in pending if isinstance(p.get('item'), str)]
        state = review.get('review_state')
        current = review.get('contribution') if isinstance(review.get('contribution'), dict) \
            else None
        since = None
        if state == 'changes-requested':
            stamps = [p.get('timestamp') for p in pending
                      if agent_prompts.parse_time(p.get('timestamp'))]
            since = max(stamps, key=agent_prompts.parse_time) if stamps else None
        elif state in ('awaiting-review', 'legacy-review-ready') and current:
            since = current.get('timestamp')
        unresolved = (data.get('unresolved') or {}).get('total')
        detail = {'pending_request_ids': ids,
                  'pending_request_ids_complete': len(ids) >= (review.get('pending_total')
                                                               or len(ids)),
                  'waiting_since': since,
                  'blocked': bool(data.get('checkpoint')) and bool(unresolved)}
        if current:
            detail['contribution'] = {
                'id': current.get('comment_id'), 'commit': current.get('commit'),
                'revision': (review.get('prior_contributions_total') or 0) + 1,
                'at': current.get('timestamp')}
        return detail

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
        carry = []
        if data.get('checkpoint'):
            point = data['checkpoint']
            unresolved = data.get('unresolved') or {}
            carry = self._open_items_in_full(project_id, task_id, point.get('comment_id'), unresolved)
            checkpoint = {'id': point.get('comment_id'), 'at': point.get('timestamp'),
                          'author': point.get('author'), 'summary': data.get('current_position'),
                          'next_action': data.get('next_action'),
                          'branch': point.get('branch'), 'source_commit': point.get('source_commit'),
                          'newer_activity': point.get('newer_activity'),
                          'open_items': [{'id': item.get('id'), 'kind': item.get('kind'),
                                          'text': item.get('text'), 'source': item.get('source')}
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
                     'status': 'open', 'resolution': None,
                     # Additive (kittrial-5bb.94, exposed over HTTP by kittrial-5bb.110
                     # item 9): the item's severity and the request-changes summary that
                     # asked for it. Absent severity reads as blocking.
                     'severity': item.get('severity') or 'blocking',
                     'summary': item.get('summary')}
                    for item in review.get('pending_requests') or [] if isinstance(item, dict)]

        def note_view(item):
            return {'id': item.get('item'), 'request': item.get('request'),
                    'text': item.get('text'), 'contribution': item.get('contribution'),
                    'author': item.get('author'), 'at': item.get('timestamp'),
                    'status': 'open', 'severity': item.get('severity') or 'note',
                    'summary': item.get('summary')}

        def request_view(entry):
            return {'request': entry.get('request'), 'reviewer': entry.get('reviewer'),
                    'summary': entry.get('summary'), 'contribution': entry.get('contribution'),
                    'author': entry.get('author'), 'at': entry.get('timestamp')}

        # The additive review-workflow states (kittrial-5bb.94): non-blocking items,
        # open first-class review requests, the requests their named reviewer declined
        # and the withdraw/supersede record for the current contribution.
        note_requests = [note_view(item) for item in review.get('note_requests') or []
                         if isinstance(item, dict)]
        pending_review_requests = [request_view(entry)
                                   for entry in review.get('pending_review_requests') or []
                                   if isinstance(entry, dict)]
        declined_review_requests = [dict(request_view(entry), reason=entry.get('reason'))
                                    for entry in review.get('declined_review_requests') or []
                                    if isinstance(entry, dict)]
        raw_withdrawal = review.get('withdrawal')
        withdrawal = ({'disposition': raw_withdrawal.get('disposition'),
                       'reason': raw_withdrawal.get('reason'),
                       'author': raw_withdrawal.get('author'),
                       'at': raw_withdrawal.get('timestamp')}
                      if isinstance(raw_withdrawal, dict) else None)
        lifecycle = {dimension: {'value': fact.get('value'), 'note': None}
                     for dimension, fact in (data.get('lifecycle') or {}).items()
                     if isinstance(fact, dict)}
        dependencies = (data.get('dependencies') or {}).get('items') or []
        return {'task': task, 'checkpoint': checkpoint, 'carry_open_items': carry,
                'review': {'state': review.get('review_state') or 'none',
                           'contribution': contribution, 'requests': requests,
                           'open_requests': review.get('pending_total', len(requests)),
                           'latest_id': review.get('latest_comment_id'),
                           # Additive (kittrial-5bb.94): the new states travel with every
                           # brief read, exactly as `review TASK` reports them.
                           'note_requests': note_requests,
                           'pending_review_requests': pending_review_requests,
                           'declined_review_requests': declined_review_requests,
                           'withdrawal': withdrawal,
                           # Additive (kittrial-5bb.115): the standing recommendation.
                           'recommendation': self._recommendation(review.get('recommendation')),
                           # The newest few carry their whole content (a kit before that
                           # change gives a name and a time only, and is read the same way).
                           'recommendations': [self._recommendation(entry) if 'summary' in entry else
                                               {'id': entry.get('comment_id'), 'author': entry.get('author'),
                                                'at': entry.get('timestamp')}
                                               for entry in review.get('recommendations') or []
                                               if isinstance(entry, dict)],
                           'revisions': priors + 1 if contribution else 0,
                           'warnings': review.get('warnings') or [],
                           # Additive (kittrial-5bb.52): the machine-readable
                           # any-pass-wins disagreement entries naming both facts
                           # and both scopes.
                           'integration_disagreements': review.get('integration_disagreements') or []},
                'lifecycle': lifecycle,
                'depends_on': [{'id': d.get('depends_on_id'), 'title': d.get('depends_on_id'),
                                'status': 'unknown', 'type': d.get('type')}
                               for d in dependencies if isinstance(d, dict)],
                # What a checkpoint must carry to say which activity it has seen. It is in
                # the canonical brief; without it here an agent could not write a first
                # checkpoint from the brief alone (kittrial-5bb.113).
                'activity_cursor': data.get('activity_cursor'),
                'warnings': data.get('warnings') or []}

    def _open_items_in_full(self, project_id, task_id, checkpoint_id, unresolved):
        """Every open item of the current checkpoint, as recorded, or None when they could not all be read.

        The next checkpoint must carry each one forward unchanged, ``source`` included, so
        the checkpoint template needs all of them (kittrial-5bb.113 review). The task page
        shows the first ten. A task with more costs ONE further ``brief`` read that asks for
        all of them (a checkpoint holds at most 100). An endpoint older than that page size
        refuses it, and the items are then read ten at a time, at most ``OPEN_ITEM_PAGES``
        reads. A checkpoint written between two reads makes the list unusable, and the
        template then says to read again.
        """
        items = [dict(item) for item in unresolved.get('items') or [] if isinstance(item, dict)]
        offset = unresolved.get('next_offset')
        if offset is None:
            return items
        try:
            whole = self._run('brief', project_id, self.actor_namespace + '/read',
                              [str(task_id), '--json', '--items-limit', str(self.OPEN_ITEMS_MAX), '--items-offset', '0'])
        except HttpError as refusal:
            if refusal.status != 422:
                raise
            whole = None
        if whole is not None:
            if not isinstance(whole, dict) or (whole.get('checkpoint') or {}).get('comment_id') != checkpoint_id:
                return None
            every = whole.get('unresolved') or {}
            if every.get('next_offset') is not None:
                return None
            return [dict(item) for item in every.get('items') or [] if isinstance(item, dict)]
        for _ in range(self.OPEN_ITEM_PAGES):
            if offset is None:
                return items
            page = self._run('brief', project_id, self.actor_namespace + '/read',
                             [str(task_id), '--json', '--items-limit', str(self.BRIEF_ITEMS),
                              '--items-offset', str(offset)])
            if not isinstance(page, dict) or (page.get('checkpoint') or {}).get('comment_id') != checkpoint_id:
                return None
            more = page.get('unresolved') or {}
            items += [dict(item) for item in more.get('items') or [] if isinstance(item, dict)]
            offset = more.get('next_offset')
        return items if offset is None else None

    #: The page size that holds every open item a checkpoint may have (briefing.CHECKPOINT_ITEMS_MAX).
    OPEN_ITEMS_MAX = 100
    #: Reads of ten that an older endpoint may cost instead (100 items, ten a page).
    OPEN_ITEM_PAGES = 9

    @staticmethod
    def _recommendation(value):
        """The canonical recommendation view in the shape the in-process backend returns."""
        if not isinstance(value, dict):
            return None
        return {'id': value.get('comment_id'), 'author': value.get('author'), 'at': value.get('timestamp'),
                'contribution': value.get('contribution'), 'commit': value.get('commit'),
                'verdict': value.get('verdict'), 'summary': value.get('summary'),
                'items': [dict(item) for item in value.get('items') or [] if isinstance(item, dict)]}

    #: ``GET /v1/me/work`` may reuse one principal's queue read of a project for this
    #: long, so a burst of page loads does not re-export every project each time.
    READ_CACHE_SECONDS = 20
    #: ``review_states`` is derived from ``review_queue`` (see the handler's reuse).
    REVIEW_STATES_FROM_QUEUE = True

    def creation_identity(self, principal, name, key, body_hash):
        """The digest kept with a host-created project for the request that registered it.

        Of the request as it was authenticated: the operation identity the endpoint journals
        the creation under (the account and its credential, the route, the project name and
        the idempotency key) and the hash of the request body. So "the same request again"
        is the same account sending the same body with the same key, and nothing else.
        Only the digest is kept: the web record holds neither the key nor the body.
        """
        operation = self._result_key(principal, None, 'projects.host-create', key, name)
        return hashlib.sha256(('%s %s' % (operation, body_hash or '-')).encode('utf-8')).hexdigest()

    def retired_on_host(self, project):
        """Whether an operator retired ``project`` on the host, so that nothing is served under the name.

        Read from the root itself: no endpoint process and no lock, so the answer to a
        creator's repeat never waits for either (kittrial-5bb.156 review). What cannot be
        read says nothing against the project.
        """
        try:
            import admin
            root = Path(self.root)
            if (root / 'projects' / project / '.beads' / 'metadata.json').is_file():
                return False
            return any(name == project for name, _ in admin.retired_entries(root))
        except (OSError, ValueError):
            return False

    def create_host_project(self, principal, name, key):
        """Create project ``name`` on the host for ``principal`` (kittrial-5bb.118 part 2).

        One ``create-project`` endpoint action. The endpoint re-checks the grant and the
        limit against the live authority store under its lock, reserves the operation
        identity before the first write and records the result with it, so the same
        request sent again returns what happened instead of creating twice. Returns the
        host's result (``status`` ``created`` or ``incomplete``). A refusal (name not
        available, limit reached, nothing made) is a 409 with the host's sentence.
        """
        operation_id = self._result_key(principal, None, 'projects.host-create', key, name)
        authority = authority_request(principal, None, CAP_PROJECT_HOST_CREATE,
                                      now=self.service._expiry_now())
        # A creation takes longer the more project databases the server holds, so it has its
        # own, longer timeout (--create-timeout). No other request waits for it.
        reply = self._endpoint('create-project', name, principal.user_id, [], operation_id=operation_id,
                               authority=authority, require_authority=True, route='projects.host-create',
                               check_usable=False, timeout=self.create_timeout)
        if isinstance(reply, dict) and reply.get('returncode') == 75:
            import project_creation
            said = (reply.get('stderr') or '').strip().splitlines()
            sentence = project_creation.busy_sentence(said[-1]) if said else None
            if sentence is None:
                # A wait for a lock ran out before the creation's own code answered: the
                # endpoint's line names the lock file. The general sentence, and the log.
                self._log_busy('create-project %s' % name, reply.get('stderr'))
                raise busy(retry_after=60)
            raise busy(sentence, retry_after=60)
        if isinstance(reply, dict) and reply.get('returncode') == 2:
            import project_creation
            said = (reply.get('stderr') or '').strip().splitlines()
            sentence = project_creation.creation_sentence(said[-1]) if said else None
            if sentence is None:
                # Not one of the creation's own sentences: an exception's text or a host path
                # (kittrial-5bb.143). The person is told only that it failed; the line is for
                # the operator, in this service's log, bounded and with control characters shown.
                print('create-project %s answered a line that is not a creation sentence: %s'
                      % (name, ascii(said[-1][:400]) if said else '(nothing)'), file=sys.stderr, flush=True)
                raise conflict(self.CREATION_FAILED)
            raise conflict(sentence)
        return self._checked(reply)

    ONBOARDING_FAILED = 'The onboarding text could not be saved on the server. Ask an operator of the server to look.'
    #: Said when the host's answer to a creation is not one of its own sentences. It does not
    #: say that nothing was made, because the service cannot know.
    CREATION_FAILED = ('The project could not be created, or was only partly made. Ask an operator of the server '
                       'to look before you try again.')

    def set_onboarding(self, principal, project_id, text, key):
        """An owner sets (``text``) or clears (``None``) the project's onboarding text.

        One service-only ``set-onboarding`` endpoint action. The endpoint re-checks the
        project-administration capability under the authority lock and applies the same
        size limit as ``admin.py set-onboarding`` plus the plain-text rule. The text
        travels as an attachment, never on a command line.
        """
        operation_id = self._result_key(principal, project_id, 'projects.onboarding', key, None)
        authority = authority_request(principal, project_id, CAP_PROJECT_ADMIN, now=self.service._expiry_now())
        attachments = {} if text is None else {'text': {'flag': '--file', 'text': text}}
        reply = self._endpoint('set-onboarding', project_id, principal.user_id,
                               ['clear'] if text is None else ['set'], attachments, operation_id=operation_id,
                               authority=authority, require_authority=True, route='projects.onboarding')
        if isinstance(reply, dict) and reply.get('returncode') == 2:
            said = (reply.get('stderr') or '').strip().splitlines()
            sentence = said[-1][:400] if said else ''
            if not sentence.startswith('ValueError: '):
                # Not a refusal of the text by the kit's own rules but a failure on the host (a
                # file that cannot be written, say): its line may name host paths, so it goes
                # to this service's log and not to the person (kittrial-5bb.143).
                print('set-onboarding %s answered a line that is not a refusal of the text: %s'
                      % (project_id, ascii(sentence) if sentence else '(nothing)'), file=sys.stderr, flush=True)
                raise conflict(self.ONBOARDING_FAILED)
            raise invalid(sentence[len('ValueError: '):])
        return self._checked(reply)

    def read_onboarding(self, project_id):
        """The project's onboarding document as stored, or None when none is set."""
        reply = self._endpoint('docs', project_id, self.actor_namespace + '/read', ['project'])
        if isinstance(reply, dict) and reply.get('returncode') == 2 and \
                'Onboarding document missing' in (reply.get('stderr') or ''):
            return None
        code = reply.get('returncode') if isinstance(reply, dict) else None
        if code:
            self._checked(reply)
        return reply.get('stdout') if isinstance(reply, dict) else None

    def host_creations(self, principal):
        """What the host says about project creations (superusers; read-only).

        ``{'items': the ones that run or need an operator, 'created': the finished ones,
        'server': {'used', 'limit'}}``. An endpoint older than this revision answers
        ``items`` only.
        """
        authority = authority_request(principal, None, CAP_ACCOUNTS_ADMIN, now=self.service._expiry_now())
        reply = self._endpoint('project-creations', None, principal.user_id, [], authority=authority,
                               require_authority=True)
        if isinstance(reply, dict) and reply.get('returncode') == 2:
            # A failure on the host, not a refusal of the request: its line may name a host path
            # and an exception, and this list is shown on a page (kittrial-5bb.149).
            said = (reply.get('stderr') or '').strip().splitlines()
            print('project-creations answered a failure: %s' % (ascii(said[-1][:400]) if said else '(nothing)'),
                  file=sys.stderr, flush=True)
            raise conflict(self.CREATIONS_UNREADABLE)
        result = self._checked(reply, 'project-creations')
        result = result if isinstance(result, dict) else {}
        return {'items': [item for item in result.get('items') or [] if isinstance(item, dict)],
                'created': [item for item in result.get('created') or [] if isinstance(item, dict)],
                'server': result.get('server') if isinstance(result.get('server'), dict) else None,
                'server_readable': result.get('server_readable') is not False}

    CREATIONS_UNREADABLE = ('The project creations on the server could not be read. Ask an operator of the server '
                            'to look.')

    #: An account's own standing on the host is asked for at most this often (seconds).
    STANDING_CACHE_SECONDS = 20

    def creation_standing(self, principal):
        """The names ``principal`` holds on the host and whether the server is full, or None.

        One read-only endpoint action, kept for a few seconds per account: the session
        read asks on every page load. None when the host cannot say (an older endpoint,
        a failure): the page then shows the web service's own count, as before.
        """
        cache = getattr(self, '_standing_cache', None)
        if cache is None:
            cache = self._standing_cache = {}
        now = time.monotonic()
        kept = cache.get(principal.user_id)
        if kept and now - kept[0] < self.STANDING_CACHE_SECONDS:
            return kept[1]
        authority = authority_request(principal, None, CAP_PROJECT_HOST_CREATE, now=self.service._expiry_now())
        try:
            result = self._checked(self._endpoint('creation-standing', None, principal.user_id, [], authority=authority,
                                                  require_authority=True))
        except HttpError:
            result = None
        if not isinstance(result, dict) or not isinstance(result.get('held'), list):
            result = None
        else:
            result = {'held': [name for name in result['held'] if isinstance(name, str)],
                      'server_full': result.get('server_full') is True}
        cache[principal.user_id] = (now, result)
        if len(cache) > 500:
            cache.pop(next(iter(cache)))
        return result

    def forget_standing(self, user_id):
        getattr(self, '_standing_cache', {}).pop(user_id, None)

    def setup_status(self, project_id):
        """What the host knows about this project's setup (kittrial-5bb.118).

        One read-only ``setup-status`` endpoint action: guidance, onboarding and backup
        as states with a version or a time, never their text. An endpoint that
        predates the action refuses it as an unknown action; the caller shows those
        steps as not available on this server.
        """
        return self._run('setup-status', project_id, self.actor_namespace + '/read', [])

    def review_states(self, project_id, queue=None):
        """Review state per task from the canonical ``work`` projection.

        ``bd list`` rows carry no review state, so the task list merges this in. The
        ``work`` queue lists every open task and every closed task whose review is
        still active; a closed task it omits has a finished (or no) review, which is
        reported as unknown rather than guessed. ``complete`` is false when the
        bounded walk stopped early; tasks past the bound are unknown too.
        """
        queue = queue if queue is not None else self.review_queue(project_id)
        # The integration disagreement warnings (kittrial-5bb.52) travel with the
        # states read as the same additive top-level list ``review_queue`` returns.
        return {'states': {item['id']: item['review_state'] for item in queue['items']},
                'complete': bool(queue.get('complete')), 'closed_unknown': True,
                'warnings': list(queue.get('warnings') or [])}

    #: Bound on the canonical ``work`` pages one queue read walks (``work`` allows at
    #: most 100 rows per call and re-exports the project each call). Reaching it
    #: reports ``complete: false`` rather than reading on.
    QUEUE_MAX_PAGES = 10

    @staticmethod
    def _attention_fields(row):
        """The four attention fields of one canonical ``work`` row, type-checked."""
        open_items = row.get('open_items', 0)
        return {'pending_change_requests': [value for value in row.get('pending_change_requests') or []
                                            if isinstance(value, str)][:20],
                'open_items': open_items if type(open_items) is int and open_items>=0 else None,
                'checkpoint_at': row.get('checkpoint_at') if isinstance(row.get('checkpoint_at'), str) else None,
                'newer_activity': row.get('newer_activity') if type(row.get('newer_activity')) is bool else None}

    def agent_tasks(self, project_id, actor=None, queue=None):
        """The tasks one actor holds, from the canonical ``work --owner ACTOR`` view.

        With no ``actor`` it answers for every held task from the unfiltered view: the
        owners' agent list and My work ask once per project for all their agents, not
        once per agent, and pass the review ``queue`` they have already read so that it
        costs no further ``work`` read at all.

        ``bd list`` rows carry no review state and no checkpoint, so agent attention
        read from them alone never reported changes requested, a contribution awaiting
        review or a blocker on this backend (kittrial-5bb.114). ``work`` computes the
        review projection per task and, since that change, the open items of the
        latest checkpoint and the pending request ids, so one owner-filtered read per
        project carries everything. It is paged to the same bound as the review queue;
        an actor holding fewer than :data:`MAX_PAGE` tasks costs one read. ``work
        --owner`` also lists tasks the actor was only NAMED to review; those are not
        the actor's own and are left out here.
        """
        if actor is not None and (not isinstance(actor,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}',actor)):
            return {'tasks':[], 'complete':True}
        if queue is not None:
            return own_queue_tasks(queue,actor)
        if actor is None:
            queue = queue if queue is not None else self.review_queue(project_id)
            rows = []
            for item in queue.get('items') or []:
                if not isinstance(item, dict) or not item.get('assignee'):
                    continue
                attention = item.get('attention') if isinstance(item.get('attention'), dict) else {}
                rows.append({'id': item.get('id'), 'title': item.get('title'), 'status': item.get('status'),
                             'assignee': item.get('assignee'), 'review_state': item.get('review_state'),
                             'contribution_id': (item.get('contribution') or {}).get('id'),
                             'pending_change_requests': list(attention.get('pending_change_requests') or []),
                             'open_items': attention.get('open_items',0),
                             'checkpoint_at': attention.get('checkpoint_at'),
                             'newer_activity': attention.get('newer_activity')})
            return {'tasks': rows, 'complete': bool(queue.get('complete'))}
        if not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}', actor):
            return {'tasks': [], 'complete': True}
        owner = ['--owner', caller_arg(actor, 'actor')]
        rows = []
        offset = 0
        complete = False
        for _ in range(self.QUEUE_MAX_PAGES):
            page = self._run('work', project_id, self.actor_namespace + '/read',
                             owner + ['--limit', str(MAX_PAGE), '--offset', str(offset), '--json'])
            if not isinstance(page, dict):
                raise uncertain('Canonical work queue returned an unexpected shape')
            for row in page.get('items') or []:
                if not isinstance(row, dict) or row.get('owner') != actor:
                    continue
                rows.append(dict({'id': row.get('task'), 'title': row.get('title'), 'status': row.get('status'),
                                  'assignee': row.get('owner'), 'review_state': row.get('review_state'),
                                  'contribution_id': row.get('contribution_id')},
                                 **self._attention_fields(row)))
            offset = page.get('next_offset')
            if offset is None:
                complete = True
                break
        return {'tasks': rows, 'complete': complete}

    def review_queue(self, project_id):
        """The canonical ``work`` queue (review projection per task), fully paged.

        ``work`` already applies the kit's own rules: structured review wins over
        legacy labels, a closed task stays listed only while its review is active,
        and rows come highest-attention first.
        """
        items = []
        warnings = []
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
                if row.get('task') == '%s%s' % (project_id, MERGE_SLOT_SUFFIX):
                    # The row bd keeps as the project's merge slot. This kit's `work` never
                    # lists it; an older endpoint did, and a slot that lost its label would
                    # be listed again. It is never offered here (kittrial-5bb.113 review).
                    continue
                contribution = ({'id': row.get('contribution_id'), 'commit': row.get('commit'),
                                 'revision': None, 'at': None}
                                if row.get('contribution_id') else None)
                task = {'id': row.get('task'), 'title': row.get('title'),
                        'status': row.get('status'), 'assignee': row.get('owner')}
                row_warnings = [w for w in row.get('integration_warnings') or []
                                if isinstance(w, str)]
                for warning in row_warnings:
                    if warning not in warnings:
                        warnings.append(warning)
                items.append(queue_item(project_id, task, row.get('review_state'),
                                        contribution, row.get('pending_review_items') or 0,
                                        integration=row.get('integration'),
                                        integration_warnings=row_warnings,
                                        attention=self._attention_fields(row),
                                        recommended_by=[name for name in row.get('recommended_by') or []
                                                        if isinstance(name, str)],
                                        contribution_author=row.get('contribution_author')
                                        if isinstance(row.get('contribution_author'), str) else None))
            offset = page.get('next_offset')
            if offset is None:
                complete = True
                break
        items.sort(key=queue_order)
        return {'items': items, 'complete': complete, 'warnings': sorted(warnings)}


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


def caller_arg(value, name):
    """A caller-supplied value on its way into an endpoint argument list.

    Never a flag and never the attachment transport: a query value such as `--help`
    used to reach the endpoint's own option parser and came back as its help payload
    (kittrial-5bb.70 review 01a10262). Each route validates its values against their
    closed set or pattern first; this is the backstop every such value passes through.
    """
    if not isinstance(value, str) or not value or value.startswith(('-', '@')) or '\0' in value:
        raise invalid('%s is not a valid value' % name)
    return value


def operator_allowlist_warnings(root):
    """What to say at service start when the runtime's operator allowlist holds a name
    with the shape of an HTTP account or agent id: that id would be an operator for every
    host-command check, and this service acts under such ids. Never raises."""
    if not root:
        return []
    try:
        document = json.loads((Path(root) / 'deployment.private.json').read_text(encoding='utf-8'))
        names = document.get('operators')
        names = [names] if isinstance(names, str) else names
    except (OSError, ValueError, AttributeError):
        return []
    from http_authority import http_shaped_names
    shaped = http_shaped_names(names if isinstance(names, list) else [])
    return ['Warning: the operator allowlist of %s holds %s, which has the shape of an HTTP account or agent id. '
            'Remove it with admin.py operators remove NAME --confirm-revoke.' % (root, ', '.join(shaped))] \
        if shaped else []


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

    def setup(self):
        """The TLS handshake, in this connection's own thread and under the client deadline.

        The listening socket is plain (kittrial-5bb.163 review): wrapped itself, it did the
        handshake inside ``accept``, in the one accepting thread and without a limit, so a
        single client that connected and said nothing stopped every other client.
        """
        server = self.server
        context = getattr(server, 'tls_context', None)
        self._guarded = hasattr(server, 'watch')
        try:
            if context is not None:
                self.request = context.wrap_socket(self.request, server_side=True, do_handshake_on_connect=False)
            if self._guarded:
                # The second half of the bound (see GuardedServer): no single wait on the client is
                # longer than this, on every platform. A little longer than the deadline, so that
                # it is the reaper that ends a wait wherever it can.
                self.request.settimeout(server.client_seconds + server.TIMEOUT_MARGIN)
        except OSError as gone:
            # The socket is closed already (the client reset it, or the server gave the
            # connection up): there is nobody to serve, and it is not an error of the service.
            # Never a traceback out of the thread (kittrial-5bb.175).
            raise ConnectionAbortedError('the connection was closed before it was served: %s'
                                         % (str(gone)[:120] or type(gone).__name__)) from None
        if context is not None:
            if self._guarded:
                server.watch(self.request, server.client_seconds)
            try:
                self.request.do_handshake()
            except Exception as failed:
                self.request.close()
                raise ConnectionAbortedError('TLS handshake not completed: %s'
                                             % (type(failed).__name__ if not str(failed) else str(failed)[:120])) from None
            finally:
                if self._guarded:
                    server.unwatch(self.request)
        super().setup()
        if self._guarded:
            self.wfile = _WatchedWriter(self.wfile, server, self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            if getattr(self.server, 'tls_context', None) is not None:
                # The server closes the socket it accepted; the TLS socket made from it is this one.
                try:
                    self.request.close()
                except OSError:
                    pass

    def handle_one_request(self):
        """One request, with a bound on how long the client may take to send it.

        The watch runs from here (waiting for the request line: a connection that has just
        been made, or an idle keep-alive one) until the request has been read, and not while
        the service works on it. ``_dispatch`` stops it; a body is read under its own.
        """
        if not getattr(self, '_guarded', False):
            return super().handle_one_request()
        self.server.watch(self.connection, self.server.client_seconds)
        try:
            return super().handle_one_request()
        except ConnectionError:
            self.close_connection = True          # the client went away, or was cut off for being too slow
        finally:
            self.server.unwatch(self.connection)

    #: Said for a write when the service's own lock could not be had in time: it may have been
    #: carried out already, so "not completed" would not always be true.
    BUSY_UNCERTAIN = ('The server was busy and cannot say whether this request was carried out. Look before you '
                      'repeat it, or send it again with the same idempotency key.')
    #: The same for a write that carried no idempotency key (kittrial-5bb.156): there is no key
    #: to send again, and a repeat is a second request.
    BUSY_UNCERTAIN_NO_KEY = ('The server was busy and cannot say whether this request was carried out. Look before '
                             'you repeat it: sent again without an idempotency key, it may be carried out twice.')
    #: `_mutate`'s own two, for an outcome the endpoint left unknown.
    UNCERTAIN = 'The operation may have committed; reconcile with the same idempotency key'
    UNCERTAIN_NO_KEY = ('The operation may have committed. Look before you repeat it: sent again without an '
                        'idempotency key, it may be carried out twice.')

    def _dispatch(self, method):
        if getattr(self, '_guarded', False):
            self.server.unwatch(self.connection)        # the request line and headers are here: the service's time now
        request_id = self._request_id()
        self._current_request_id = request_id
        # Per-request agent read cache. An HTTP/1.1 keep-alive connection reuses this
        # handler instance, so the cache is reset for every request and never outlives it.
        self._agent_task_cache = {}
        self._request_reads = {}
        # What the answer to a lock wait that runs out depends on: whether the route had begun
        # (before it, nothing can have been carried out), which route, and whether a key came.
        begun = None
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
            begun = (name, ctx.idempotency_key is not None)
            status, response = getattr(self, name)(ctx)
            self._send_json(status, response)
        except HttpError as error:
            self._note_denied(error, request_id)
            self._retry_after = getattr(error, 'retry_after', None)
            self._send_json(error.status, error.body(request_id))
        except TimeoutError as waited:
            # Which lock, for the operator: the message of the wait names the lock file.
            print('busy: a wait for a lock ran out in the service for %s: %s' % (method, ascii(str(waited)[:400])),
                  file=sys.stderr, flush=True)
            # A wait for the state lock ran out (file_lock raises it while acquiring). The server
            # is occupied; this is not an internal error. For a read nothing can have happened.
            # For a write the wait may have run out AFTER the effect: a creation was made and
            # registered, and the lock for the receipt could not be had (seen on real bd,
            # kittrial-5bb.149). The service does not know which, and says so.
            # Not every write: before the route began nothing was carried out, and a log-in that
            # was made and not answered is a session nobody holds (kittrial-5bb.156). Otherwise
            # the sentence names the idempotency key only when the request carried one.
            if method in ('GET', 'HEAD') or begun is None or begun[0] == 'sessions_create':
                error = busy()
                self._retry_after = error.retry_after
            else:
                error = uncertain(self.BUSY_UNCERTAIN if begun[1] else self.BUSY_UNCERTAIN_NO_KEY)
            self._send_json(error.status, error.body(request_id))
        except ConnectionError:
            raise                                   # the client is gone: nothing to answer, and not an internal error
        except Exception:
            # No traceback, no internal detail: a clean, generic JSON error.
            self._send_json(500, {'error': {'code': 'internal_error',
                                            'message': 'Internal error'},
                                  'request_id': request_id})

    def _admit(self, method):
        """Behind a trusted proxy, one forwarded address has a share of the requests being served.

        The connection is the proxy's, so the limit per address (``GuardedServer``) cannot be
        taken at accept; it is taken here, for the time this request is served, under the
        address the service already takes as the request's source. A request over it is
        answered 503 ``busy`` before its route begins: nothing was carried out.
        """
        server = self.server
        group = self._forwarded_group() if hasattr(server, 'request_begins') else None
        if group is None:
            return self._dispatch(method)
        if not server.request_begins(group):
            server.unwatch(self.connection)
            error = busy(ADDRESS_BUSY, retry_after=1)
            self._retry_after = error.retry_after
            self._say_close = True                # its body, if it has one, was not read
            self._current_request_id = self._request_id()
            return self._send_json(error.status, error.body(self._current_request_id))
        try:
            return self._dispatch(method)
        finally:
            server.request_ends(group)

    do_GET = lambda self: self._admit('GET')
    do_POST = lambda self: self._admit('POST')
    do_PUT = lambda self: self._admit('PUT')
    do_PATCH = lambda self: self._admit('PATCH')
    do_DELETE = lambda self: self._admit('DELETE')
    do_HEAD = lambda self: self._admit('HEAD')

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

    def _source_is_shared(self):
        """Whether this request's source is an address everybody arrives from, not one client's.

        True where no client address was forwarded and the peer is the service's own host
        (loopback: the SSH tunnel of a first install, or a proxy on the host that was not
        named) or a trusted proxy. In both the people behind it cannot be told apart, so a
        share per address would be one share for all of them: ten people logging in at nine
        in the morning would be turned away for each other. Such a log-in is held to the
        places in all only. With a forwarded address from a trusted proxy the source is that
        client's, and it has its share.
        """
        if self._forwarded_group() is not None:
            return False
        if self._peer_is_trusted_proxy():
            return True
        try:
            return ipaddress.ip_address(self._peer_address().split('%', 1)[0]).is_loopback
        except ValueError:
            return False

    def _forwarded_group(self):
        """The address group a trusted proxy forwarded this request for; None for any other request."""
        if not self._peer_is_trusted_proxy():
            return None
        forwarded = self.headers.get('X-Forwarded-For')
        if not isinstance(forwarded, str) or not forwarded:
            return None
        candidate = forwarded.split(',')[-1].strip()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return None
        return address_group(candidate)

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
                # Bounded nesting (kittrial-5bb.108): a body nested deeper than the kit ever
                # writes is refused here, as bad JSON, and never reaches a route or the
                # operation journal. It used to be an HTTP 500.
                payload = record_json.loads(body)
            except (ValueError, UnicodeError):
                raise invalid('Request body is not valid JSON')
            if not isinstance(payload, dict):
                raise invalid('Request body must be a JSON object')
        try:
            self._body_hash = request_hash(payload) if payload is not None else request_hash(None)
        except UnicodeEncodeError:
            # Half of a surrogate pair is valid JSON text and not valid Unicode: it cannot be hashed,
            # stored or sent on. It answered 500 on every route (kittrial-5bb.118 part 2 review).
            raise invalid('The request holds text that is not valid Unicode (half of a surrogate pair)') from None
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
        if not getattr(self, '_guarded', False):
            return self.rfile.read(length)
        self.server.watch(self.connection, self.server.client_seconds)
        try:
            body = self.rfile.read(length)
        except OSError:
            # The socket's own timeout. Not the TimeoutError of a lock wait, which _dispatch
            # answers as "busy": this is the client, and there is nobody to answer.
            body = b''
        finally:
            self.server.unwatch(self.connection)
        if len(body) < length:
            # Cut off for being too slow, or gone: there is nobody to answer.
            raise ConnectionAbortedError('the request body did not arrive')
        return body

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
        if getattr(self, '_audited_refusal', False):
            self._audited_refusal = False
            return
        if error.status not in (401, 403):
            return
        principal = getattr(self, '_principal', None)
        try:
            with self.service.store.lock:
                self.service.audit(request_id, principal, 'authorization', 'denied',
                                   reason=error.code)
                self.service.store.save_soon('the audit entry of a refusal')
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
        if getattr(self, '_retry_after', None):
            self.send_header('Retry-After', str(int(self._retry_after)))
            self._retry_after = None
        if getattr(self, '_say_close', False):
            self.send_header('Connection', 'close')         # which also ends the connection after this answer
            self._say_close = False
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
                canonical=False, reason=None, refused_reason=None):
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
            raise uncertain(self.UNCERTAIN if ctx.idempotency_key is not None else self.UNCERTAIN_NO_KEY)
        except HttpError as error:
            self.service.idempotency_release(digest)
            # ``refused_reason`` lets a route whose target is not a registered project (a
            # creation) say what was refused; the error code stays first.
            self.service.audit(ctx.request_id, ctx.principal, route_name,
                               'denied' if error.status in (401, 403) else 'rejected',
                               project_id=project_id,
                               reason=error.code if refused_reason is None else '%s: %s' % (error.code, refused_reason))
            # The refusal is the answer whether or not its audit entry can be saved now
            # (kittrial-5bb.156): nothing was carried out, so a lock wait that runs out here
            # must not turn it into "cannot say". The entry is written with the next save.
            self.service.store.save_soon('the audit entry of a refusal')
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

    #: Said when a recommendation is refused because it is not independent of the author.
    NOT_INDEPENDENT = ('A recommendation must be independent of the author: the contribution\'s author, the '
                       'task\'s assignee, the person who owns the agent that delivered it, and that person\'s '
                       'other agents cannot recommend it')

    def _independent(self, actors, parties):
        """The ``actors`` who are a different PERSON from every one of ``parties``.

        The canonical rule compares actor names, and a person and their agents are
        different names. The web service knows who owns each agent, so here a person,
        their agent, and two agents of one person are all the same party
        (kittrial-5bb.115).
        """
        persons = {self.service.actor_person(party) for party in parties if isinstance(party, str) and party}
        return [actor for actor in actors
                if isinstance(actor, str) and self.service.actor_person(actor) not in persons]

    @staticmethod
    def _read_parties(assignee, author):
        """Whom a standing recommendation in a BRIEF is compared with when it is read.

        The contribution's author, and nobody else: the assignee rule is applied when a
        recommendation is written, so a reassignment hides nothing. A brief whose
        contribution does not say who delivered is compared with the assignee (review
        01a10c80). With neither, the list is empty, and the caller shows no
        recommendation: "nobody to compare with" is never read as "independent of
        everybody" (kittrial-5bb.137).
        """
        if isinstance(author, str) and author:
            return [author]
        return [assignee] if isinstance(assignee, str) and assignee else []

    def _independent_queue(self, read):
        """Drop from each row's ``recommended_by`` anyone who is the contribution author's person.

        The same party as the brief compares with (kittrial-5bb.115 review): a
        recommendation by another agent of the contribution's author is left out of the
        queue and My work as it is left out of the brief. The assignee is not compared
        on a read, here or in the canonical reader: that rule is applied when a
        recommendation is written, so a later reassignment hides nothing.

        A row that does not say who delivered counts NO recommendation (kittrial-5bb.137).
        Such a row comes from an endpoint older than this service, in a staged upgrade or a
        rollback: the service cannot tell whose recommendation is independent, and it does
        not guess from the assignee, who may have changed since the delivery. The queue
        row, My work and the agent actions then show no recommendation until the endpoint
        is updated; the brief, which names the author, still shows an independent one.

        The names that were not counted are kept on such a row as ``recommended_unchecked``
        (kittrial-5bb.147). They say nothing about whether the delivery is recommended.
        They answer one question the service CAN answer whoever delivered: has this
        reviewer recommended it already. Without them a reviewer on a mixed installation
        was offered the same delivery again on every read.
        """
        changed = False
        for item in read.get('items') or []:
            names = item.get('recommended_by') or []
            author = item.get('contribution_author')
            if names and not (isinstance(author, str) and author):
                kept, item['recommended_unchecked'] = [], list(names)
            elif names:
                kept = self._independent(names, [author])
            else:
                kept = names
            if len(kept) != len(names):
                item['recommended_by'], item['recommended'], changed = kept, bool(kept), True
        if changed:
            read['items'].sort(key=queue_order)
        return read

    @staticmethod
    def _every_recommender(row):
        """Everyone who recommended the row's delivery, whether or not the service counts them."""
        return [name for name in (row.get('recommended_by') or []) + (row.get('recommended_unchecked') or [])
                if isinstance(name, str)]

    def _own_party_recommended(self, row, actor):
        """Whether ``actor``'s own PERSON has already recommended the row's delivery (kittrial-5bb.154).

        The one answer to "already recommended?" for an agent's actions and for My work: the
        actor itself, the person who owns it, or another agent of that person. Independence
        is by person everywhere else in the review rules, and a second agent of the same
        person adds no independent reading. Read from every name on the row, counted or not.
        """
        names = self._every_recommender(row)
        return len(self._independent(names, [actor])) != len(names)

    def _review_queue(self, project_id, shared=False):
        """One review-queue read of a project per request (and, when ``shared`` and the
        backend allows it, reused for ``READ_CACHE_SECONDS`` by the same principal).

        Authorization is never cached: callers re-check live authority first.
        """
        return self._cached_read('queue', project_id,
                                 lambda: self._independent_queue(self.backend.review_queue(project_id)), shared)

    def _cached_read(self, kind, project_id, load, shared=False):
        """One canonical read per request, optionally reused across requests.

        Within a request a read of ``(kind, project_id)`` happens once. With ``shared``
        and a backend ``READ_CACHE_SECONDS`` above zero, the result is also kept in the
        server's small read cache for that long, keyed by the *principal* (user id and
        credential id, so an agent never sees its owner's entry or the reverse), the
        project and the read kind. Callers re-check live authority before every read;
        authorization is never cached, and neither is usability (kittrial-5bb.90): each
        entry carries the verdict it was stored under, and a record that became usable
        or unusable is a miss, so the loader's own refusal or fresh read wins at once.
        The principal's own successful write drops its entries for that project
        (:meth:`_forget_cached_reads`). The cache is bounded to
        :data:`READ_CACHE_MAX_ENTRIES` entries.
        """
        memo = getattr(self, '_request_reads', None)
        if memo is None:
            memo = self._request_reads = {}
        if (kind, project_id) in memo:
            return memo[(kind, project_id)]
        ttl = getattr(self.backend, 'READ_CACHE_SECONDS', 0) if shared else 0
        principal = self._principal
        key = (getattr(principal, 'user_id', None), getattr(principal, 'credential_id', None)
               or '-', project_id, kind)
        now = time.monotonic()
        usable = (self._unusable(project_id) is None) if ttl else None
        if ttl and self.read_cache is not None:
            with self.read_cache_lock:
                hit = self.read_cache.get(key)
            # A two-tuple is an entry written before this rule (or aged by a test): it
            # carries no verdict, so it is served as before.
            if hit is not None and hit[0] > now and (len(hit) < 3 or hit[2] == usable):
                memo[(kind, project_id)] = hit[1]
                return hit[1]
        result = load()
        memo[(kind, project_id)] = result
        if ttl and self.read_cache is not None:
            with self.read_cache_lock:
                if len(self.read_cache) >= READ_CACHE_MAX_ENTRIES:
                    for stale in [k for k, v in self.read_cache.items() if v[0] <= now] or \
                            list(self.read_cache)[:READ_CACHE_MAX_ENTRIES // 2]:
                        self.read_cache.pop(stale, None)
                self.read_cache[key] = (now + ttl, result, usable)
        return result

    def _forget_cached_reads(self, principal, project_id):
        """Drop the principal's cached reads after its own successful write.

        The project's entries go (all of them when the write had no project), for
        every credential of the same user, so the author sees their own claim,
        delivery or review at once; other users' entries still expire on their short
        TTL. The current request's own memo is dropped too.
        """
        memo = getattr(self, '_request_reads', None)
        if memo:
            for key in [k for k in memo if project_id is None or k[1] == project_id]:
                memo.pop(key, None)
        cache = getattr(self, 'read_cache', None)
        if cache is None or principal is None:
            return
        with self.read_cache_lock:
            for key in [k for k in cache if k[0] == principal.user_id and
                        (project_id is None or k[2] == project_id)]:
                cache.pop(key, None)

    def _with_review_states(self, project_id, rows, shared=False):
        """Give every row its review state (``None`` when unknown). Returns completeness."""
        if all(isinstance(r, dict) and 'review_state' in r for r in rows):
            return True
        # A backend whose states come from its queue projection reuses this request's
        # queue read (and, with ``shared``, the principal's short-lived cached one)
        # instead of paying for a second one.
        queue = self._review_queue(project_id, shared=shared) \
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
                                    source=self._source(), request_id=ctx.request_id,
                                    shared_source=self._source_is_shared())
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
                'project': principal.credential_project,
                # How "New project" works here (kittrial-5bb.80): `create` (service-local
                # backend), `register` (a superuser registers an existing canonical
                # project) or `operator-only` (an operator creates it on the host).
                'project_create': (
                    'create' if getattr(self.backend, 'PROJECT_CREATE', 'create') == 'create' else
                    'register' if principal.superuser and principal.via != 'credential' else 'operator-only')}
        # Additive (kittrial-5bb.118 part 2): whether this account may create a project
        # on the host from here, with its limit and how many it has. Only where the
        # backend has a host.
        if getattr(self.backend, 'PROJECT_CREATE', 'create') == 'register':
            body['project_host_create'] = self._host_creation(principal)
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
        items = self.service.list_users(ctx.principal)
        # Additive (kittrial-5bb.118 part 2 revision): the names each account holds on the host
        # that are not registered here count toward its limit too. One host read, for a
        # superuser's session only; left out when the host cannot say.
        if getattr(self.backend, 'PROJECT_CREATE', 'create') == 'register' and hasattr(self.backend, 'host_creations')                 and ctx.principal.superuser and ctx.principal.via != 'credential':
            try:
                found = self.backend.host_creations(ctx.principal)
            except HttpError:
                found = None
            if found is not None:
                registered = self.service.state['projects']
                held = {}
                for item in found['items'] + found['created']:
                    if (isinstance(item.get('project'), str) and item['project'] not in registered
                            and item.get('state') != 'damaged' and isinstance(item.get('by'), str)):
                        held.setdefault(item['by'], []).append(item['project'])
                for view in items:
                    view['projects_held'] = sorted(held.get(view['id'], []))
        return 200, {'items': items}

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
        if getattr(self.backend, 'PROJECT_CREATE', 'create') == 'register':
            if payload.get('create') is True:
                return self._project_host_create(ctx, payload)
            return self._project_register(ctx, payload)

        def create():
            project_id = payload.get('project_id')
            result = self.service.create_project(ctx.principal, payload.get('name'), project_id)
            return result, result
        # The idempotency namespace for creation is the principal + route, never a
        # body-derived project id: the same key with changed content must conflict
        # (409) instead of creating a second project.
        return self._mutate(ctx, 'projects.create', None, create,
                            status=201, capability=CAP_PROJECT_CREATE)

    #: Said to everyone who may not create or register, whatever the payload names.
    NOT_ALLOWED_TO_CREATE = 'Only a superuser registers a project on this server. ' + REGISTER_HINT

    def _host_creation(self, principal):
        """``Service.host_creation`` with what the host holds for the account counted in.

        A creation that is running, stopped or stalled holds a place the web service has
        no record of; the endpoint counts it and refuses at the limit. Counted here too,
        so ``allowed`` and ``used`` say what the endpoint will answer (review 01a109cc).
        ``reason`` gains ``server-limit``: the server holds as many project databases as
        its operator allows, whatever this account's own numbers are.
        """
        creation = self.service.host_creation(principal)
        if creation['reason'] in ('credential', 'no-grant') or not hasattr(self.backend, 'creation_standing'):
            return creation
        standing = self.backend.creation_standing(principal)
        if standing is None:
            return dict(creation, held=None)          # the host could not say
        registered = set(self.service.state['projects'])
        held = [name for name in standing['held'] if name not in registered]
        used = creation['used'] + len(held)
        creation = dict(creation, used=used, held=held)
        if creation['limit'] is not None and used >= creation['limit']:
            creation.update(allowed=False, reason='limit')
        elif standing['server_full']:
            creation.update(allowed=False, reason='server-limit')
        return creation

    #: Said to the account a project is registered to by its own creation, and to nobody else.
    ALREADY_YOURS = 'You already have project %s: you created it on this server%s. Nothing was made again.'
    #: The same, when an operator has since retired the project on the host: the page links nothing.
    RETIRED_YOURS = ('You created project %s on this server%s, and it has since been retired there, so it is no '
                     'longer served. The name is not available: choose another name.')

    def _created_here_by(self, record, principal):
        """Whether ``record`` is a project this account created on the host and still belongs to."""
        made = record.get('host_created')
        return principal.via != 'credential' and isinstance(made, dict) and made.get('by') == principal.user_id \
            and record.get('created_by') == principal.user_id \
            and principal.user_id in (self.service.state['memberships'].get(record.get('id')) or {})

    def _project_host_create(self, ctx, payload):
        """`POST /v1/projects` with `"create": true`: create the project on the host and register it.

        For a superuser, or an account a superuser granted "may create projects", within
        the grant's limit (kittrial-5bb.118 part 2). Never a credential. Everyone else
        gets the same 403 as the register route, before anything about the name is
        looked at, so the answer cannot be used to find out which names exist.

        The project is registered here only after the host reports it created, first
        backup included. Until then nothing about it is visible in the web interface. A
        creation that stopped half way answers 409 with the host's sentence, which names
        the project and says an operator must finish or remove it.
        """
        principal = ctx.principal
        creation = self.service.host_creation(principal)
        asked = payload.get('project_id')
        # What the audit says was asked for: the name when it is one, never caller text otherwise.
        shown = asked if isinstance(asked, str) and CANONICAL_PROJECT.fullmatch(asked) else '(not a project name)'

        def refuse(error):
            """A refusal before the mutation starts is audited with the route and the name (review 01a109cc)."""
            with self.service.store.lock:
                self.service.audit(ctx.request_id, principal, 'projects.host-create',
                                   'denied' if error.status in (401, 403) else 'rejected',
                                   reason='%s: create %s' % (error.code, shown))
                self.service.store.save_soon('the audit entry of a refusal')
            # The generic authorization entry would only repeat it.
            self._audited_refusal = True
            raise error
        if creation['reason'] in ('credential', 'no-grant'):
            refuse(forbidden(self.NOT_ALLOWED_TO_CREATE))
        if hasattr(self.backend, 'forget_standing'):
            self.backend.forget_standing(principal.user_id)
        if set(payload) - {'create', 'project_id', 'name'}:
            refuse(invalid('Send project_id, name and create only'))
        project_id = payload.get('project_id')
        if not isinstance(project_id, str) or not CANONICAL_PROJECT.fullmatch(project_id):
            refuse(invalid('project_id must be 2-24 lowercase letters or digits, beginning with a letter'))
        name = payload.get('name') or project_id
        try:
            self.service._validate_project_name(name)
        except HttpError as error:
            refuse(error)
        key = ctx.idempotency_key or ctx.request_id
        identity = self.backend.creation_identity(principal, project_id, key, ctx.body_hash) \
            if hasattr(self.backend, 'creation_identity') else None
        existing = self.service.state['projects'].get(project_id)
        again = False
        if existing is not None:
            refusal = conflict('Project name %s is not available: choose another name' % project_id)
            if self._created_here_by(existing, principal):
                # The creator's own repeat (kittrial-5bb.156). The exact request that registered
                # it, sent again with its idempotency key: the endpoint's journal answers what
                # happened and the project is returned, 201. Anything else from the creator is
                # told that they have it. Nobody else learns who has the name, or since when.
                made = existing.get('host_created') or {}
                again = ctx.idempotency_key is not None and identity is not None and made.get('operation') == identity
                day = str(existing.get('created_at') or '')[:10]
                since = ' on ' + day if DAY.fullmatch(day) else ''
                if hasattr(self.backend, 'retired_on_host') and self.backend.retired_on_host(project_id):
                    # The web record outlives a retirement on the host. "You already have it", with
                    # a link, would point at a project that answers "unknown" (review of .156).
                    again = False
                    refusal = conflict(self.RETIRED_YOURS % (project_id, since), {'project': project_id, 'state': 'retired'})
                else:
                    refusal = conflict(self.ALREADY_YOURS % (project_id, since), {'project': project_id, 'state': 'yours'})
            if not again:
                return self._refuse_unless_replay(ctx, 'projects.host-create', refusal,
                                                  capability=CAP_PROJECT_HOST_CREATE)

        def create():
            try:
                result = self.backend.create_host_project(principal, project_id, key)
            except HttpError as failure:
                # Busy means nothing was done (another creation is running): it is answered as
                # busy, to be sent again, and is not an outcome to reconcile.
                if failure.status == 503 and failure.code != 'busy' and not getattr(failure, 'nothing_done', False):
                    raise UncertainOutcome() from None
                if again and failure.status == 409:
                    # The same request for a project that is registered to this account, and the
                    # endpoint will not answer it from its journal: sent from another session
                    # (logged in again; the page keeps its key), the operation identity belongs
                    # to the first one. Nothing was made, and the project is theirs: say that,
                    # not "could not be created" (review of kittrial-5bb.156).
                    raise refusal from None
                raise
            if not isinstance(result, dict) or result.get('status') not in ('created', 'incomplete'):
                raise UncertainOutcome()
            if result['status'] == 'incomplete':
                raise conflict(result.get('message') or 'Project %s did not finish; an operator must finish or '
                               'remove it' % project_id, {'project': project_id, 'state': 'incomplete',
                                                          'stage': result.get('stage')})
            view = self._usable(self.service.register_host_created(principal, project_id, name, result,
                                                                   operation=identity))
            view['backup'] = result.get('backup')
            return view, view
        limit = 'none (superuser)' if creation['limit'] is None else str(creation['limit'])
        reason = 'create %s account=%s limit=%s count=%d' % (project_id, principal.user_id, limit,
                                                              creation['used'] + (0 if again else 1))
        if again:
            reason += ' (the same request again; it is registered already)'
        return self._mutate(ctx, 'projects.host-create', None, create, status=201,
                            capability=CAP_PROJECT_HOST_CREATE, serialize=False, canonical=True, reason=reason,
                            refused_reason='create %s account=%s' % (project_id, principal.user_id))

    @route('GET', r'/v1/project-creations')
    def project_creations(self, ctx):
        """Superusers: creations that stopped half way, so that none is forgotten.

        Each names the project, the account that started it, the stage it stopped at and
        the two operator commands. Empty where the backend has no host.
        """
        principal = self._superuser_session(ctx, 'Only a superuser sees the project creations')
        self.require(ctx, CAP_ACCOUNTS_ADMIN)
        if getattr(self.backend, 'PROJECT_CREATE', 'create') != 'register' or \
                not hasattr(self.backend, 'host_creations'):
            return 200, {'items': [], 'host': 'not-applicable'}
        found = self.backend.host_creations(principal)
        registered = self.service.state['projects']
        waiting = [item for item in found['created'] if isinstance(item.get('project'), str)
                   and item['project'] not in registered]
        names = self.service.actor_names([item.get('by') for item in found['items'] + waiting])

        def view(item, **more):
            project = item['project']
            return dict({'project': project, 'state': item.get('state'), 'by': item.get('by'),
                         'by_name': names.get(item.get('by'), item.get('by')), 'stage': item.get('stage'),
                         'started_at': item.get('started_at'), 'stopped_at': item.get('stopped_at')}, **more)
        items = []
        for item in found['items']:
            if not isinstance(item.get('project'), str):
                continue
            project, state = item['project'], item.get('state')
            # The commands are tied to what the record reads as (review 01a109cc): a running
            # creation has none, and no reading is given `retire-project --force`.
            remove = 'admin.py remove-creation %s --actor OPERATOR --reason REASON' % project
            items.append(view(item, what=item.get('command'),
                              finish='admin.py finish-project %s' % project if state == 'incomplete' else None,
                              remove=remove if state in ('incomplete', 'stalled', 'damaged') else None))
        # Finished on the server and not registered here: the creator's grant was revoked, the
        # account was disabled or the server was over its limit when the work ended.
        unregistered = [view(item, state='created-unregistered', completed_at=item.get('completed_at'),
                             what='It is complete on the server and not registered here. Register it as a superuser '
                                  '(New project, with this name), or retire it on the server.',
                             finish=None, remove=None) for item in waiting]
        server = found['server']
        if server is None and not found.get('server_readable', True):
            server = {'used': None, 'limit': None,
                      'note': 'The server\'s limit of project databases could not be read, so no project can be '
                              'created from the web interface. An operator must look at the server\'s configuration.'}
        elif server is not None:
            server = {'used': server.get('used'), 'limit': server.get('limit'),
                      'note': 'Counts every project database on the server: archived and retired projects and '
                              'unfinished creations too, because their databases stay on it. An operator changes '
                              'the limit (admin.py project-creations --set-server-limit N --actor OPERATOR).'}
        return 200, {'items': items, 'unregistered': unregistered, 'server': server, 'host': 'available'}

    @route('PUT', r'/v1/accounts/(?P<uid>' + ID + r')/project-grant')
    def account_project_grant(self, ctx):
        """A superuser lets an account create projects: `{"limit": N}` (default 5)."""
        payload = ctx.payload or {}
        if set(payload) - {'limit'}:
            raise invalid('Send limit only')
        self.require(ctx, CAP_ACCOUNTS_ADMIN)
        return 200, self.service.set_project_grant(ctx.principal, ctx.params['uid'], payload.get('limit'),
                                                   request_id=ctx.request_id)

    @route('DELETE', r'/v1/accounts/(?P<uid>' + ID + r')/project-grant')
    def account_project_grant_clear(self, ctx):
        self.require(ctx, CAP_ACCOUNTS_ADMIN)
        return 200, self.service.clear_project_grant(ctx.principal, ctx.params['uid'], request_id=ctx.request_id)

    def _project_register(self, ctx, payload):
        """`POST /v1/projects` on the endpoint backend: register an existing canonical project.

        Superuser only, and the refusal for anyone else comes first and is the same
        whatever the payload names, so the existence check below can never be used by a
        non-superuser to probe for canonical project names. The id must be a canonical
        project name; a second registration of the same name is refused with 409 (two
        HTTP projects never map to one canonical project); the canonical project must
        exist and be initialized on the host, checked with one endpoint read before any
        store write. Only the registering superuser becomes a member (owner).
        """
        principal = ctx.principal
        if principal is None or principal.via == 'credential' or not principal.superuser:
            raise forbidden('Only a superuser registers a project on this server. ' + REGISTER_HINT)
        project_id = payload.get('project_id')
        if not isinstance(project_id, str) or not CANONICAL_PROJECT.fullmatch(project_id):
            raise invalid('project_id must be the canonical project name: 2-24 lowercase letters or digits, '
                          'beginning with a letter (the NAME given to admin.py add-project)')
        if project_id in RESERVED_PROJECTS:
            # A literal route of the same name is registered before `/v1/projects/{pid}`
            # (kittrial-5bb.90): the record could never be read back, so refuse it here
            # rather than on every later read.
            raise invalid('project_id %s is reserved for this server\'s own routes; choose another canonical '
                          'project name' % project_id)
        name = payload.get('name') or project_id
        existing = self.service.state['projects'].get(project_id)
        if existing is not None:
            # An exact retry of the registration that made this record replays its 201
            # (kittrial-5bb.84); anything else is the conflict.
            return self._refuse_unless_replay(ctx, 'projects.create', conflict(
                'Canonical project %s is already registered as project %r%s; one canonical project '
                'is registered once' % (project_id, existing.get('name'),
                                        ' (archived)' if existing.get('archived') else '')))
        if not self.backend.project_exists(project_id):
            raise invalid('No canonical project %s exists on the coordination host. An operator creates it there '
                          'first (admin.py add-project %s), then you register it here.' % (project_id, project_id))

        def register():
            self.service.create_project(principal, name, project_id)
            # The mark that a superuser registered this mapping; `project_unusable` reads it.
            self.service.state['projects'][project_id]['registered_by'] = principal.user_id
            result = self._usable(self.service.project_view(principal, project_id))
            return result, result
        return self._mutate(ctx, 'projects.create', None, register, status=201, capability=CAP_ACCOUNTS_ADMIN,
                            reason='register ' + project_id)

    def _refuse_unless_replay(self, ctx, route_name, error, capability=CAP_ACCOUNTS_ADMIN):
        """Raise `error`, unless this request is an exact idempotent retry of one that
        already committed on this route: then its stored response is replayed.

        For a superuser route whose own pre-checks would otherwise refuse the retry of
        its success ("already registered", "needs no confirmation"). The reservation
        is released again when the refusal is raised.
        """
        if ctx.idempotency_key is None:
            raise error

        def refuse():
            raise error
        return self._mutate(ctx, route_name, None, refuse, capability=capability)

    def _superuser_session(self, ctx, message):
        principal = ctx.principal
        if principal is None or principal.via == 'credential' or not principal.superuser:
            raise forbidden(message)
        return principal

    def _unusable(self, project_id):
        """`project_unusable` for this backend: only the endpoint backend has the rule."""
        if getattr(self.backend, 'PROJECT_CREATE', 'create') != 'register':
            return None
        return project_unusable(self.service, project_id)

    def _require_usable(self, project_id):
        """Refuse granting or extending access on a record the backend will not serve
        (kittrial-5bb.84): adding or changing a member, issuing a worker credential, a
        new agent grant. Whatever REMOVES access stays open on such a record (removing
        a member, revoking a credential or an agent grant, archiving), so cleaning one
        up never requires confirming it first. Called after the route's authority
        check, so it never tells a caller without access that the record exists.

        An ARCHIVED record is answered with :data:`ARCHIVED_UNUSABLE` instead of the
        record's own sentence (kittrial-5bb.90 review item 2.1): an archived record
        cannot be confirmed, so advising "confirm it or archive it" points at two dead
        ends, while removing the access does work.
        """
        verdict = self._unusable(project_id)
        if verdict is None:
            return
        record = self.service.state.get('projects', {}).get(project_id)
        if isinstance(record, dict) and record.get('archived'):
            raise conflict(ARCHIVED_UNUSABLE)
        raise conflict(verdict[1] + ' Until then nothing that grants access is accepted on it; removing a '
                       'member, revoking a credential or an agent grant, and archiving still work.')

    def _usable(self, view):
        """Mark a project the backend cannot serve (kittrial-5bb.80, .84)."""
        view = dict(view)
        verdict = self._unusable(view.get('id'))
        view['usable'] = verdict is None
        if verdict is not None:
            view['unusable_reason'] = ('No canonical Beads project is behind this project (it was created by an '
                                       'older kit); archive it. ' + REGISTER_HINT) \
                if verdict[0] == 'no-canonical' else verdict[1]
            view['needs_confirmation'] = verdict[0] == 'unconfirmed'
        return view

    @route('GET', r'/v1/projects')
    def projects_list(self, ctx):
        return 200, {'items': [self._usable(view) for view in self.service.list_projects(ctx.principal)]}

    # Registered before `/v1/projects/{pid}` so the literal path always wins.
    @route('GET', r'/v1/projects/unconfirmed')
    def projects_unconfirmed(self, ctx):
        """The upgrade check (kittrial-5bb.84): every record this backend will not serve,
        for a superuser to confirm or archive, with who created it and who its members
        are (the ordinary list shows a superuser every project, but not those)."""
        self._superuser_session(ctx, 'Only a superuser reviews unconfirmed projects on this server.')
        self.require(ctx, CAP_ACCOUNTS_ADMIN)
        register = getattr(self.backend, 'PROJECT_CREATE', 'create') == 'register'
        items = unusable_projects(self.service) if register else []
        return 200, {'items': items, 'total': len(items)}

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/confirm')
    def project_confirm(self, ctx):
        """A superuser confirms a legacy record with a canonical id (kittrial-5bb.84).

        Superuser session only; the refusal for anyone else comes first and is the same
        whatever id is named. The canonical project must exist on the host (one endpoint
        read). Memberships are not changed: the response lists them for review.
        """
        principal = self._superuser_session(ctx, 'Only a superuser confirms a project on this server.')
        project_id = ctx.params['pid']
        record = self.service.state['projects'].get(project_id)
        if record is None:
            raise not_found('Project not found')
        verdict = self._unusable(project_id)
        if verdict is None:
            return self._refuse_unless_replay(ctx, 'projects.confirm',
                                              conflict('This project needs no confirmation'))
        if verdict[0] != 'unconfirmed':
            raise conflict(verdict[1])
        if record.get('archived'):
            raise conflict('This project is archived')
        if not self.backend.project_exists(project_id):
            raise invalid('No canonical project %s exists on the coordination host, so this record cannot be '
                          'confirmed. Archive it.' % project_id)

        def confirm():
            item = next(item for item in unusable_projects(self.service) if item['id'] == project_id)
            record['confirmed_by'] = principal.user_id
            record['confirmed_at'] = now_iso(self.service._now())
            # Backfill the mark the register route writes: a record a superuser stands
            # behind stays usable even if that superuser is later demoted (kittrial-5bb.90).
            # A record that already names its registrant keeps it.
            record.setdefault('registered_by', principal.user_id)
            result = {'id': project_id, 'name': record.get('name'), 'usable': True,
                      'confirmed_by': principal.user_id, 'confirmed_at': record['confirmed_at'],
                      'created_by': item['created_by'], 'members': item['members']}
            return result, result
        return self._mutate(ctx, 'projects.confirm', None, confirm, capability=CAP_ACCOUNTS_ADMIN,
                            reason='confirm ' + project_id)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')')
    def project_get(self, ctx):
        self._project(ctx, CAP_READ)
        return 200, self._usable(self.service.project_view(ctx.principal, ctx.params['pid']))

    @route('PATCH', r'/v1/projects/(?P<pid>' + ID + r')')
    def project_update(self, ctx):
        """Change a project's own fields. Today: ``repository`` (kittrial-5bb.118)."""
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) != {'repository'}:
            raise invalid('Send exactly the field to change: repository')

        def update():
            result = self.service.set_project_repository(ctx.principal, ctx.params['pid'],
                                                         payload['repository'], request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'projects.update', ctx.params['pid'], update,
                            capability=CAP_PROJECT_ADMIN)

    def _onboarding_owner(self, ctx):
        # Project administration is refused for every credential by the authority rule itself
        # (CREDENTIAL_FORBIDDEN_CAPABILITIES), so no second check for a credential stands here:
        # one stood, could not be reached, and no test could pin it (review 01a109cc).
        self._project(ctx, CAP_PROJECT_ADMIN)
        if not hasattr(self.backend, 'set_onboarding'):
            raise not_implemented('This server has no host project behind it, so there is no onboarding text to set')

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/onboarding')
    def project_onboarding(self, ctx):
        """The onboarding text, for an owner to edit (kittrial-5bb.118 part 2).

        ``source`` says who set it: ``web`` (an owner, here) or ``operator`` (on the
        server). Only owner-written text is returned for editing; the operator's own
        document is reported as set, with its size, and is not returned here.
        """
        self._onboarding_owner(ctx)
        from onboarding import PROJECT_LIMIT, WEB_HEADER, split_web
        document = self.backend.read_onboarding(ctx.params['pid'])
        if document is None:
            return 200, {'state': 'not-set', 'source': None, 'text': None, 'limit': PROJECT_LIMIT}
        by_owner, text = split_web(document)
        return 200, {'state': 'set', 'source': 'web' if by_owner else 'operator',
                     'text': text if by_owner else None, 'bytes': len(document.encode('utf-8')),
                     'limit': PROJECT_LIMIT, 'header': WEB_HEADER if by_owner else None}

    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/onboarding')
    def project_onboarding_set(self, ctx):
        """An owner sets the project's onboarding text: `{"text": "..."}`.

        Stored with a first line that says an owner wrote it in the web interface, so
        every reader sees it as information from the project, not as an instruction from
        the server's operator. Guidance is not settable here. Audited with the size,
        never the text.
        """
        self._onboarding_owner(ctx)
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) != {'text'} or not isinstance(payload['text'], str):
            raise invalid('Send exactly: text')
        try:
            payload['text'].encode('utf-8')
        except UnicodeEncodeError:
            # Half of a surrogate pair cannot be stored as UTF-8; it used to answer 500 (review 01a109cc).
            raise invalid('Project onboarding must be valid Unicode text (it holds half of a surrogate pair)') from None
        return self._onboarding_write(ctx, payload['text'])

    @route('DELETE', r'/v1/projects/(?P<pid>' + ID + r')/onboarding')
    def project_onboarding_clear(self, ctx):
        """Remove onboarding text an owner set here. The operator's own text is not removable here."""
        self._onboarding_owner(ctx)
        return self._onboarding_write(ctx, None)

    def _onboarding_write(self, ctx, text):
        key = ctx.idempotency_key or ctx.request_id

        def write():
            try:
                result = self.backend.set_onboarding(ctx.principal, ctx.params['pid'], text, key)
            except HttpError as failure:
                # Not when the answer says that nothing was carried out (the server's configuration
                # could not be read; kittrial-5bb.156 review).
                if failure.status == 503 and not getattr(failure, 'nothing_done', False):
                    raise UncertainOutcome() from None
                raise
            kept = result.get('operator_text_kept_as') if isinstance(result, dict) else None
            if kept:
                # The owner's text replaced the operator's: say so in the audit, with where the copy is.
                with self.service.store.lock:
                    self.service.audit(ctx.request_id, ctx.principal, 'projects.onboarding', 'committed',
                                       project_id=ctx.params['pid'],
                                       reason='account=%s replaced the text an operator set; a copy is kept beside it as %s'
                                              % (ctx.principal.user_id, str(kept)[:60]))
                    self.service.store.save()
            return result, result
        size = 'cleared' if text is None else 'set %d bytes' % len(text.encode('utf-8'))
        return self._mutate(ctx, 'projects.onboarding', ctx.params['pid'], write, capability=CAP_PROJECT_ADMIN,
                            serialize=False, canonical=True, reason='account=%s %s' % (ctx.principal.user_id, size))

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/setup')
    def project_setup(self, ctx):
        """What is done and what is left to set a project up (kittrial-5bb.118).

        For owners and superusers. Every state is read at request time: from this
        service (members, repository, agents), from the task list, and from the host
        through the read-only ``setup-status`` endpoint action (guidance, onboarding,
        backup). Nothing here writes anywhere, and no guidance or onboarding text and
        no other member's agent setup is returned.
        """
        self._project(ctx, CAP_PROJECT_ADMIN)
        if ctx.principal.via == 'credential':
            raise forbidden('Session authority required')
        body = project_setup.steps(self, ctx.principal, ctx.params['pid'])
        body['generated_at'] = now_iso(self.service._now())
        return 200, body

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
            self._require_usable(ctx.params['pid'])
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
            self._require_usable(ctx.params['pid'])
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
        # `repository` is where the project's owner says its code lives (kittrial-5bb.118):
        # a label, null when not recorded. Data for the agent, never an instruction.
        projects = [{'id': p['id'], 'name': p['name'], 'repository': p.get('repository')}
                    for p in self.service.list_projects(ctx.principal)]
        return 200, {
            'agent': agent,
            'attention': self._agent_attention_view(agent, attention),
            'next_action': attention['actions'][0] if attention['actions'] else None,
            'next_actions': attention['actions'],
            'projects': projects,
            'repositories_note': self.service.REPOSITORY_NOTE
            if any(project['repository'] for project in projects) else None,
            'links': {'self': '/v1/agents/me', 'next': '/v1/agents/me/next'},
            'manual_cadence': 'Computed at read time; no polling or scheduled work. '
                              'Re-read this route when the owner resumes the agent.',
            'generated_at': now_iso(self.service._now()),
        }

    @route('POST', r'/v1/agents')
    def agents_create(self, ctx):
        payload = dict(ctx.payload or {})

        def create():
            self._require_grantable(ctx.principal, payload.get('projects'), ())
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
            # One read per project for all the agents listed, not one per agent.
            attention = self._agent_attention(principal, agent, many=True)
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
            record = self.service.state['agents'].get(ctx.params['aid'])
            if isinstance(record, dict):
                # An id with no agent record is the service's own 404: judging a grant
                # here would answer the usability refusal for an agent that is not there
                # (kittrial-5bb.90 item 1).
                self._require_grantable(ctx.principal, payload.get('projects'),
                                        record.get('projects') or (),
                                        owner_id=record.get('owner'))
            result = self.service.update_agent(ctx.principal, ctx.params['aid'], payload)
            return result, result
        return self._mutate(ctx, 'agents.update', None, update, capability=CAP_AGENTS)

    def _require_grantable(self, principal, projects, held, owner_id=None):
        """Refuse a NEW agent grant on a record the backend will not serve (kittrial-5bb.84, .90).

        Judged against the *agent owner's* live membership, not the caller's: a superuser
        editing somebody else's agent is not a member of the owner's projects, and a
        record's usability is the same for everyone. A project the owner cannot see is
        left to the service's own not-found refusal, so no record is revealed to either
        account; a grant the agent already holds is left alone, so its other fields stay
        editable.

        Nothing is judged for a caller who may not administer the agent (kittrial-5bb.90
        item 1): the owner's membership is not the caller's business, and answering the
        usability refusal to an outsider would tell a caller with no rights that the agent
        id exists, that the record exists and is unusable, and that the owner is a member.
        The service's own ``_agent_owned`` then answers its unchanged 404, exactly as the
        credential route in the same commit leaves an unauthorized caller to it.
        """
        if not isinstance(projects, (list, tuple)):
            return
        owner_id = owner_id or principal.user_id
        caller = self.service.state.get('users', {}).get(principal.user_id) or {}
        if not (caller.get('superuser') or owner_id == principal.user_id):
            return
        owner = self.service.state.get('users', {}).get(owner_id) or {}
        owner_is_superuser = bool(owner.get('superuser'))
        for project_id in projects:
            if not isinstance(project_id, str) or project_id in held:
                continue
            # An id with no record is the service's own not-found refusal; this only judges
            # records that exist and that the owner could otherwise be granted.
            if project_id not in self.service.state['projects']:
                continue
            if owner_is_superuser or owner_id in self.service.state['memberships'].get(project_id, {}):
                self._require_usable(project_id)

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
            agent = self.service.state['agents'].get(ctx.params['aid'])
            user = self.service.state.get('users', {}).get(ctx.principal.user_id) or {}
            if isinstance(agent, dict) and agent.get('enabled') and (
                    user.get('superuser') or agent.get('owner') == ctx.principal.user_id):
                # A new credential extends the agent's access to the projects it already
                # holds, so on a record the backend will not serve it is refused like any
                # other grant (kittrial-5bb.90). Checked only for an agent this principal
                # may administer, so an unauthorized caller still gets the service's 404.
                # Never checked for a DISABLED agent: the service's own earlier "A disabled
                # agent cannot receive a credential" must answer first, not this record's
                # sentence (kittrial-5bb.90 review item 3.2).
                for project_id in agent.get('projects') or ():
                    self._require_usable(project_id)
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
        """Task ids whose latest checkpoint still lists open items (``GET /v1/me/work``).

        This reads the in-process canonical view when it is present; the canonical
        endpoint binding does not mirror checkpoints into it, so a missing view simply
        yields no ``blocked`` signal rather than a wrong one. Agent attention no longer
        uses it (kittrial-5bb.114): see :meth:`_agent_own_tasks`.
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

    def _agent_own_tasks(self, project_id, actor, many=False):
        """Split this request's current review snapshot by the actual assignee.

        Both own attention and claimable suggestions use the same paged ``work``
        snapshot. No additional native task-list or owner-filtered read is needed,
        whether one agent or all the owner's agents are displayed. Authority is
        still checked per caller/project; nothing is cached across requests here.
        """
        cache = getattr(self, '_agent_own_cache', None)
        if cache is None:
            cache = self._agent_own_cache = {}
        key = (project_id, None if many else actor)
        if key not in cache:
            read = self.backend.agent_tasks(project_id, None if many else actor,
                                            queue=self._review_queue(project_id))
            cache[key] = {'tasks': [task for task in read.get('tasks') or [] if isinstance(task, dict)],
                          'complete': bool(read.get('complete'))}
        read = cache[key]
        if not many:
            return read
        return {'tasks': [task for task in read['tasks'] if task.get('assignee') == actor],
                'complete': read['complete']}

    #: At most this many reviewer actions are listed per read; the counts stay exact.
    AGENT_REVIEW_LIMIT = 20

    def _agent_review_actions(self, principal, agent, project_id, rows, counts):
        """The review work one agent is shown in one project: the actions to list.

        Two kinds, both at the priority of claimable work and listed before it
        (kittrial-5bb.115). Everything comes from ``rows``, the review-queue snapshot the
        caller already holds: no further read.

        ``review-recommended``: a contribution that awaits review and has a standing
        recommendation, shown to an agent whose OWNER can approve it. The agent cannot
        approve; the action says to tell the owner it is ready.

        ``to-review``: a contribution that awaits review and that this agent has not
        recommended yet, shown to any agent that may review.

        An agent is shown a delivery only if it could itself recommend it: it holds the
        reviews capability in the project, and it is a different PERSON from the task's
        assignee and from the contribution's author (not its own, not its owner's, not
        another agent of its owner; see ``_independent``). ``counts`` gains the exact
        numbers; the caller caps the whole list over every project (``_capped_review_actions``)
        and can tell from the counts that some were left out.
        """
        agent_id = agent.get('id')
        actor = agent.get('actor') or agent_id
        may_review, owner_approves = self.service.agent_review_standing(agent_id, project_id)
        if not may_review:
            return []
        actions = []
        for row in rows:
            if row.get('review_state') != 'awaiting-review' or row.get('status') == 'closed':
                continue
            if not self._independent([actor], [row.get('assignee'), row.get('contribution_author')]):
                continue
            recommended_by = [name for name in row.get('recommended_by') or [] if isinstance(name, str)]
            if recommended_by and owner_approves:
                kind, count, who = 'review-recommended', 'review_recommended', 'owner'
                reason = ('A reviewer recommends approving this contribution. Tell your owner it is ready to '
                          'approve; an agent cannot approve.')
            elif not self._own_party_recommended(row, actor):
                kind, count, who = 'to-review', 'to_review', 'agent'
                reason = ('A contribution by someone else awaits review. Review it, then record a recommendation '
                          'or request changes.')
            else:
                continue                      # this agent's person has recommended it already
            counts[count] += 1
            contribution = row.get('contribution') if isinstance(row.get('contribution'), dict) else {}
            actions.append(self._agent_action(
                4, kind, project_id, row, reason, who=who, recommended_by=recommended_by[:20],
                contribution=contribution.get('id'), commit=contribution.get('commit')))
        return actions                        # ordered by the caller, over every project

    def _capped_review_actions(self, actions):
        """At most AGENT_REVIEW_LIMIT review actions in ALL, over every project (review 01a10c80).

        A cap on each project's share let three full projects list three times the limit
        and push claimable work off the list.
        Recommended ones are kept first, then by project and task.
        """
        ordered = sorted(actions, key=lambda action: (0 if action['kind'] == 'review-recommended' else 1,
                                                       action['project'], action['task']))
        return ordered[:self.AGENT_REVIEW_LIMIT]

    @staticmethod
    def _agent_review_summary(counts):
        """What to add to an agent's summary when there is review work, whatever its state.

        The state values do not change for review work, so without this an agent with
        nothing of its own would read "idle, nothing in flight" while contributions wait
        for it or for its owner.
        """
        parts = []
        if counts.get('review_recommended'):
            parts.append('%d contribution(s) recommended for approval: tell the owner' % counts['review_recommended'])
        if counts.get('to_review'):
            parts.append('%d contribution(s) to review' % counts['to_review'])
        return (' ' + '; '.join(parts) + '.') if parts else ''

    #: The order of the kinds that share a priority. The ORDER of ``next_actions`` is the
    #: contract; the priority numbers are not renumbered when a kind is added.
    AGENT_KIND_ORDER = {'review-recommended': 0, 'to-review': 1, 'claimable-task': 2}

    def _agent_action(self, priority, kind, project_id, task, reason, **extra):
        task_id = task.get('id')
        base = '/v1/projects/%s/tasks/%s' % (project_id, task_id)
        action = {'priority': priority, 'kind': kind, 'project': project_id,
                  'task': task_id, 'title': task.get('title'),
                  'status': task.get('status'), 'review_state': task.get('review_state'),
                  'assignee': task.get('assignee'), 'reason': reason,
                  'links': {'task': base, 'brief': base, 'history': base + '/history',
                            'project': '/v1/projects/%s' % project_id}}
        action.update(extra)
        return action

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
        """Reuse the bounded current-work snapshot, including unclaimed open tasks.

        The endpoint costs one work command per page; the in-process backend reads
        its project once. There is no separate task-list read. Both the backend's
        page bound and the attention bound are preserved in ``complete``.
        """
        cache = getattr(self, '_agent_task_cache', None)
        if cache is None:
            cache = self._agent_task_cache = {}
        if project_id in cache:
            return cache[project_id]
        snapshot = self._review_queue(project_id)
        rows = [task for task in (snapshot.get('items') or []) if isinstance(task, dict)]
        bound = AGENT_MAX_PAGES * MAX_PAGE
        result = {'tasks': rows[:bound], 'complete': len(rows) <= bound and bool(snapshot.get('complete'))}
        cache[project_id] = result
        return result

    def _agent_attention(self, principal, agent, many=False):
        """Owner/agent-visible attention, computed at read time from bounded reads.

        Each granted project is re-authorized with the same live-authority check the
        task routes apply (:meth:`_agent_may_read`), so a project the principal can no
        longer read drops out of attention instead of leaking its tasks.

        One current-work snapshot per project and request supplies both the agent's
        own states and the unclaimed open tasks. The endpoint pays one work command
        per page, with no extra task-list read; every agent in an owner's list reuses
        those same rows. Nothing is cached across requests here.

        One action per own task, the most pressing reason first, then in this order:
        changes requested (1), blocked by its latest checkpoint (2), in progress with no
        contribution yet (3), then claimable work (4), then a contribution that waits
        for a reviewer or, once approved, for integration (5): the agent can do nothing
        about those, so they never sit ahead of work the agent can do. ``in_progress`` counts own open tasks with no
        contribution that are NOT blocked. A blocked action
        carries ``blocked_since`` (when the checkpoint was written) and
        ``newer_activity`` (whether another actor wrote after it), so an
        agent can leave a blocked task with nothing new alone instead of re-reading it
        and writing another checkpoint on every wake. The counts are independent of the
        actions: a task with changes requested AND open checkpoint items counts in both.
        """
        actor = agent.get('actor') or agent.get('id')
        counts = {'claimable': 0, 'claimed': 0, 'changes_requested': 0,
                  'awaiting_review': 0, 'blocked': 0, 'in_progress': 0, 'awaiting_integration': 0,
                  'review_recommended': 0, 'to_review': 0}
        own_actions = []
        claimable_actions = []
        review_actions = []
        truncated = False
        for project_id in agent.get('projects') or []:
            if not self._agent_may_read(principal, project_id):
                continue
            try:
                read = self._agent_project_tasks(project_id)
                own = self._agent_own_tasks(project_id, actor, many=many)
            except HttpError:
                # A project the principal can no longer open simply drops out.
                continue
            if not read['complete'] or not own['complete']:
                truncated = True
            for task in own['tasks']:
                counts['claimed'] += 1
                review = task.get('review_state')
                is_open = task.get('status') != 'closed'
                open_items = task.get('open_items',0)
                unreadable = is_open and open_items is None
                blocked = is_open and type(open_items) is int and open_items > 0
                in_progress = (is_open and not blocked and not unreadable and not task.get('contribution_id')
                               and review in (None, 'none'))
                integrating = review in ('awaiting-integration', 'approved')
                counts['changes_requested'] += review == 'changes-requested'
                counts['awaiting_review'] += review == 'awaiting-review'
                counts['awaiting_integration'] += integrating
                counts['blocked'] += blocked
                counts['in_progress'] += in_progress
                details = {'requests': list(task.get('pending_change_requests') or [])[:20], 'open_items': open_items,
                           'blocked_since': task.get('checkpoint_at') if blocked else None,
                           'newer_activity': task.get('newer_activity') if blocked else None}
                if review == 'changes-requested':
                    own_actions.append(self._agent_action(
                        1, 'changes-requested', project_id, task,
                        'A reviewer requested changes on this contribution.', **details))
                elif review == 'error':
                    own_actions.append(self._agent_action(
                        2, 'review-error', project_id, task,
                        'Review state error: an operator must reconcile the malformed review history.',
                        who='operator', **details))
                elif unreadable:
                    own_actions.append(self._agent_action(
                        2,'checkpoint-error',project_id,task,
                        'Checkpoint history could not be read; an operator must reconcile it.',
                        who='operator',**details))
                elif blocked:
                    own_actions.append(self._agent_action(
                        2, 'blocked', project_id, task,
                        'The latest checkpoint left unresolved items.', **details))
                elif in_progress:
                    own_actions.append(self._agent_action(
                        3, 'in-progress', project_id, task,
                        'Claimed by this agent and not delivered yet.', **details))
                elif review == 'awaiting-review':
                    own_actions.append(self._agent_action(
                        5, 'awaiting-review', project_id, task,
                        'Waiting for a human review decision.', **details))
                elif integrating:
                    own_actions.append(self._agent_action(
                        5, 'awaiting-integration', project_id, task,
                        'Approved; waiting for the coordinator to integrate it. Nothing for the agent to do.',
                        **details))
                elif is_open or review in ACTIVE_REVIEW_STATES:
                    action = (next_action(task) if review in ('withdrawn', 'superseded', 'integrated',
                                                             'legacy-review-ready') else None)
                    action = action or {'who': 'owner', 'text': 'Check the task and its review history'}
                    own_actions.append(self._agent_action(
                        5, 'review-state', project_id, task,
                        'Review state %s: %s.' % (review or 'unknown', action['text']),
                        who=action['who'], **details))
            for task in read['tasks']:
                if task.get('status') == 'open' and task.get('assignee') is None:
                    counts['claimable'] += 1
                    if len(claimable_actions) < AGENT_CLAIMABLE_LIMIT:
                        claimable_actions.append(self._agent_action(
                            4, 'claimable-task', project_id, task,
                            'Open, unclaimed work the agent may take.'))
            # Review work for this agent, from the same snapshot (kittrial-5bb.115): one call.
            review_actions += self._agent_review_actions(principal, agent, project_id, read['tasks'], counts)
        # The count is exact over every page; only the collected suggestions are capped,
        # so a long claimable list can never hide the agent's own feedback.
        if counts['claimable'] > len(claimable_actions):
            truncated = True
        review_actions = self._capped_review_actions(review_actions)
        if counts['review_recommended'] + counts['to_review'] > len(review_actions):
            truncated = True
        actions = own_actions + review_actions + claimable_actions
        actions.sort(key=lambda a: (a['priority'], self.AGENT_KIND_ORDER.get(a['kind'], 0), a['project'], a['task']))
        if len(actions) > AGENT_ACTION_LIMIT:
            actions = actions[:AGENT_ACTION_LIMIT]
            truncated = True
        if counts['changes_requested']:
            state = 'changes-requested'
        elif counts['blocked']:
            state = 'blocked'
        elif counts['in_progress']:
            state = 'working'
        elif counts['awaiting_review']:
            state = 'waiting-review'
        elif counts['awaiting_integration']:
            state = 'waiting-integration'
        elif counts['claimed']:
            state = 'working'
        else:
            state = 'idle'
        return {'state': state, 'summary': self._agent_summary(state, counts) + self._agent_review_summary(counts),
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
            return ('%d claimed task(s) in flight, %d not delivered yet; %d claimable.'
                    % (counts['claimed'], counts.get('in_progress', 0), counts['claimable']))
        if state == 'waiting-review':
            return ('%d contribution(s) waiting for a human review decision.'
                    % counts['awaiting_review'])
        if state == 'waiting-integration':
            return ('%d approved contribution(s) waiting for integration; nothing for the agent to do.'
                    % counts.get('awaiting_integration', 0))
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
        # kittrial-5bb.48: the secret never reaches curl's argv. curl reads the header
        # from the per-agent config file the owner stored (``-K``); no variable is
        # expanded onto the command line.
        secret_file = agent_secret_file(agent['name'])
        return ("Open your agent folder%s for agent '%s' (%s). %s\n"
                "Your secret is in %s (never print or copy it). Then run:\n"
                "  PowerShell:  curl.exe -fsS -K \"%s\" %s/v1/agents/me/next\n"
                "  macOS/Linux: curl -fsS -K %s %s/v1/agents/me/next"
                % (where, agent['name'], agent['id'], what, secret_file['windows'],
                   secret_file['windows_powershell'], server, secret_file['posix'], server))

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
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']), write=True)
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
        # The full snapshot and the review states come from the principal's short
        # read cache (see _cached_read): on the canonical binding one page otherwise
        # costs a ``bd list`` plus up to QUEUE_MAX_PAGES ``work`` reads. Rows are copied
        # because the review state is merged into them below.
        snapshot = self._cached_read('tasks', ctx.params['pid'],
                                     lambda: self.backend.read_tasks(ctx.params['pid']),
                                     shared=True)
        rows = [dict(t) if isinstance(t, dict) else t for t in snapshot.get('items') or []]
        if filters:
            rows = [t for t in rows if isinstance(t, dict)]
            complete = self._with_review_states(ctx.params['pid'], rows, shared=True)
            rows = [t for t in rows if task_matches(t, filters)]
            result = {'items': rows[state['o']:state['o'] + limit], 'total': len(rows)}
        else:
            result = {'items': rows[state['o']:state['o'] + limit],
                      'total': snapshot.get('total', len(rows))}
            complete = self._with_review_states(ctx.params['pid'], result['items'],
                                                shared=True)
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
        row = self.backend.get_task(ctx.params['pid'], ctx.params['tid'])
        self._refuse_record_anchor(row)
        return 200, self._task_views([row])[0]

    @staticmethod
    def _refuse_record_anchor(row, write=False):
        """A record anchor is not a task: the task, brief and history routes answer
        404 for it, never its row or raw record comments, and the task write routes
        (PATCH, claim, checkpoints, reviews) refuse it the same way (kittrial-5bb.64).

        The project's merge slot is not a task either (kittrial-5bb.113). The read
        routes answer 404 for it; a write route says what it is, so an agent that was
        once offered it learns why the claim is refused."""
        if is_record_anchor(row):
            raise not_found('Task not found')
        if is_merge_slot(row):
            if write:
                raise conflict(merge_slot_sentence(row.get('id')))
            raise not_found('Task not found')

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/references')
    def references_list(self, ctx):
        """The reference catalog (.41 slice 1), read-only at CAP_READ.

        Query: `tag` (comma-separated, all must match), `owner`, `state`, `due`,
        `authority` (repository, url or attested), `limit` and the page `cursor`. Statements are untrusted text: they are returned as data
        and never placed in an error body or an audit record.
        """
        self._project(ctx, CAP_READ)
        limit, state = self._page(ctx, ctx.query)
        args = ['--limit', str(limit), '--offset', str(state['o'])]
        for tag in [t for t in (ctx.query.get('tag') or '').split(',') if t]:
            args += ['--tag', tag]
        for name in ('owner', 'state', 'due', 'authority'):
            if ctx.query.get(name):
                args += ['--' + name, ctx.query[name]]
        import reference_records
        try:
            options = reference_records.parse_list_options(args)
        except ValueError as error:
            raise invalid(str(error).replace('ref list: ', ''))
        result = dict(self.backend.references(ctx.params['pid'], options))
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             state['o'] + limit)
                                 if result.get('next_offset') is not None else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/references/(?P<key>' + REFERENCE_KEY + r')')
    def references_get(self, ctx):
        self._project(ctx, CAP_READ)
        return 200, self.backend.reference(ctx.params['pid'], ctx.params['key'])

    # -- contributed requirement proposals (.58 slice 1b, kittrial-5bb.70) -----------
    #
    # Identity is server-bound here, so what is a host command over SSH is a route:
    # a member proposes (`proposals.write`), a member with `reviews.approve` triages and
    # decides. Every write goes through the canonical endpoint, which re-validates the
    # live authority and applies the same rules as the host commands (compare-and-swap,
    # the state machine, the no-self rules). Proposal text is untrusted: it is returned
    # only as bounded excerpt objects and never enters an error body or an audit record.
    PROPOSAL_SUBMIT_FIELDS = ('target', 'text', 'rationale', 'evidence', 'attachments', 'supersedes')
    PROPOSAL_REVISE_FIELDS = ('key', 'revision', 'expected_sha256', 'target', 'text', 'rationale', 'evidence',
                              'attachments')
    PROPOSAL_DISPOSE_FIELDS = ('previous', 'proposal_sha256', 'to_state', 'reason', 'question', 'duplicate_of',
                               'escalation', 'decision', 'incorporation')

    def _proposals_backend(self):
        if not getattr(self.backend, 'PROPOSALS', False):
            raise not_implemented('Requirement proposals are native records; they are not available on this '
                                  'backend')

    def _proposal_body(self, ctx, allowed, where):
        payload = dict(ctx.payload or {})
        operation_id = payload.pop('operation_id', None)
        unknown = sorted(set(payload) - set(allowed))
        if unknown:
            # Field names only: a value may be proposal text.
            raise invalid('%s does not take: %s' % (where, ', '.join(unknown)[:200]))
        if operation_id is not None and (not isinstance(operation_id, str) or not SAFE_ID.match(operation_id)):
            raise invalid('operation_id must be a short identifier')
        return payload, operation_id

    def _proposal_view(self, ctx, capabilities, item):
        """One proposal item or record as THIS caller may see it (design 6.3, 6.4).

        A rejection reason, a coordinator question and an escalation question are
        returned only to the submitter and to members with `reviews.approve`; everyone
        else gets `null` and `withheld: true`. The comparison uses server-bound
        identity: the caller's account (for an agent, its owner's).
        """
        item = dict(item)
        submitter = item.get('submitter')
        if isinstance(submitter, str) and submitter.startswith('account:'):
            account = submitter[len('account:'):]
            item['submitter_name'] = self.service.actor_names([account]).get(account, None)
        # "Mine" needs the proposal to be verified as the account's: one that merely names
        # it, or whose later revision someone else wrote, does not show its coordinator
        # text to that account (review 01a10262).
        mine = submitter == 'account:' + ctx.principal.user_id and item.get('identity') == 'verified'
        item['mine'] = mine
        # Display names for the people in the timeline (account ids), best effort.
        entries = [entry for entry in [item.get('disposition')] + list(item.get('timeline') or [])
                   if isinstance(entry, dict)]

        def decider(entry):
            identity = (entry.get('escalation') or {}).get('owner_identity')
            return identity[len('account:'):] if isinstance(identity, str) and identity.startswith('account:')                 else None
        names = self.service.actor_names([entry.get('actor') for entry in entries]
                                         + [decider(entry) for entry in entries])

        def named(disposition):
            if not isinstance(disposition, dict):
                return disposition
            actor = disposition.get('actor')
            shown = dict(disposition, actor_name=names.get(actor, actor) if isinstance(actor, str) else None)
            if decider(disposition):
                shown['escalation'] = dict(shown['escalation'], owner_name=names.get(decider(disposition)))
            return shown
        if 'disposition' in item:
            item['disposition'] = named(item['disposition'])
        if isinstance(item.get('timeline'), list):
            item['timeline'] = [named(entry) for entry in item['timeline']]
        if mine or CAP_APPROVE in capabilities:
            return item

        def withhold(disposition):
            if not isinstance(disposition, dict):
                return disposition
            shown = dict(disposition)
            hidden = shown.get('reason') is not None or shown.get('question') is not None
            shown['reason'] = shown['question'] = None
            if isinstance(shown.get('escalation'), dict):
                hidden = hidden or shown['escalation'].get('question') is not None
                shown['escalation'] = dict(shown['escalation'], question=None)
            shown['withheld'] = bool(hidden or shown.get('withheld'))
            return shown
        if 'disposition' in item:
            item['disposition'] = withhold(item['disposition'])
        if isinstance(item.get('timeline'), list):
            item['timeline'] = [withhold(entry) for entry in item['timeline']]
        return item

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/proposals')
    def proposals_submit(self, ctx):
        """Submit a proposal, or revise your own (a body with `key`).

        `submitter` is bound to the signed-in account; for an agent credential it is the
        agent's owner and the agent is recorded. Neither is caller-supplied. A worker
        credential cannot propose.
        """
        self._proposals_backend()
        self._project(ctx, CAP_PROPOSALS)
        principal = ctx.principal
        if principal.via == 'credential' and not principal.agent_id:
            raise forbidden('A worker credential cannot submit a proposal; a member session or an agent '
                            'credential can')
        revise = 'key' in (ctx.payload or {})
        fields, operation_id = self._proposal_body(
            ctx, self.PROPOSAL_REVISE_FIELDS if revise else self.PROPOSAL_SUBMIT_FIELDS,
            'A proposal %s (submitter is bound to the signed-in account)' % ('revision' if revise else 'submission'))
        body = dict(fields, schema_version=1, operation='revise' if revise else 'submit',
                    submitter='account:' + principal.user_id, operation_id=operation_id)
        pid = ctx.params['pid']

        def submit():
            result = self.backend.invoke('proposals.submit', principal, pid,
                                         {'command': body['operation'], 'body': body}, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_PROPOSALS)
            return result, result
        return self._mutate(ctx, 'proposals.submit', pid, submit, status=200 if revise else 201,
                            capability=CAP_PROPOSALS, serialize=False, canonical=True)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/proposals')
    def proposals_list(self, ctx):
        """The proposal queue at CAP_READ: `state`, `target`, `limit` and the page `cursor`."""
        self._proposals_backend()
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        limit, state = self._page(ctx, ctx.query)
        args = ['list', '--limit', str(limit), '--offset', str(state['o'])]
        import proposal_records
        wanted = ctx.query.get('state')
        if wanted:
            if wanted not in proposal_records.STATES:
                raise invalid('state must be one of ' + ', '.join(proposal_records.STATES))
            args += ['--state', caller_arg(wanted, 'state')]
        target = ctx.query.get('target')
        if target:
            if not proposal_records.valid_target_filter(target):
                raise invalid('target must be a requirement key or a requirement area')
            args += ['--target', caller_arg(target, 'target')]
        order = ctx.query.get('order')
        if order:
            if order not in ('oldest', 'newest'):
                raise invalid('order must be oldest or newest')
            args += ['--order', caller_arg(order, 'order')]
        if ctx.query.get('mine'):
            # The caller's own proposals in this project: the identity is the session's
            # account (for an agent, its owner's) and cannot be named.
            if ctx.query['mine'] != '1':
                raise invalid('mine must be 1')
            # Read through `proposal mine`: launched by this service it lists VERIFIED
            # proposals only, the rule of /v1/me/contributions. A proposal that merely
            # names the account (an SSH submission) is not the caller's and is not counted
            # (kittrial-5bb.102).
            args[0] = 'mine'
            args += ['--submitter', 'account:' + ctx.principal.user_id]
        result = dict(self.backend.proposal_read(pid, args))
        capabilities = self.service.capabilities_for(ctx.principal, pid)
        result['items'] = [self._proposal_view(ctx, capabilities, item) for item in result.get('items') or []]
        result['next_cursor'] = (make_cursor(ctx.principal, pid, ctx.query, result['next_offset'])
                                 if result.get('next_offset') is not None else None)
        result['can_triage'] = CAP_APPROVE in capabilities
        result['can_propose'] = CAP_PROPOSALS in capabilities
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/proposals/(?P<key>' + PROPOSAL_KEY + r')')
    def proposals_get(self, ctx):
        """One proposal: the newest revision, the disposition timeline and the derived links."""
        self._proposals_backend()
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        args = ['get', caller_arg(ctx.params['key'], 'key')]
        if ctx.query.get('history'):
            if not re.fullmatch(r'[0-9]{1,2}', ctx.query['history']):
                raise invalid('history must be a number from 1 to 50')
            args += ['--history', caller_arg(ctx.query['history'], 'history')]
        capabilities = self.service.capabilities_for(ctx.principal, pid)
        result = self._proposal_view(ctx, capabilities, self.backend.proposal_read(pid, args, missing=True))
        result['can_triage'] = CAP_APPROVE in capabilities
        return 200, result

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/proposals/(?P<key>' + PROPOSAL_KEY + r')/dispositions')
    def proposals_dispose(self, ctx):
        """Triage (`operation: review`, the default) or the owner decision (`decide`).

        A signed-in member with `reviews.approve`; no credential ever holds it. The body
        carries the compare-and-swap pair the detail read returned (`previous`,
        `proposal_sha256`) and `to_state` with its one required field. The endpoint
        applies the rules of the host commands: the transition must be legal, the caller
        is not the submitter or the author, and a decider is not the escalator.
        """
        self._proposals_backend()
        self._project(ctx, CAP_APPROVE)
        principal = ctx.principal
        if principal.via == 'credential':
            raise forbidden('A credential cannot record a proposal disposition')
        payload = dict(ctx.payload or {})
        command = payload.pop('operation', 'review')
        if command not in ('review', 'decide'):
            raise invalid('operation must be review or decide')
        ctx.payload = payload
        fields, operation_id = self._proposal_body(ctx, self.PROPOSAL_DISPOSE_FIELDS, 'A proposal disposition')
        body = dict(fields, schema_version=1, operation=command, key=ctx.params['key'], operation_id=operation_id)
        pid = ctx.params['pid']

        def dispose():
            result = self.backend.invoke('proposals.dispose', principal, pid, {'command': command, 'body': body},
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=CAP_APPROVE)
            return result, result
        return self._mutate(ctx, 'proposals.dispose', pid, dispose, status=201, capability=CAP_APPROVE,
                            serialize=False, canonical=True, reason='%s %s' % (command, ctx.params['key']))

    @route('GET', r'/v1/me/contributions')
    def me_contributions(self, ctx):
        """The signed-in person's own proposals across their projects (design 7.1).

        Session only: the identity is the session's account and cannot be named. One
        read per project, at most ME_WORK_MAX_PROJECTS projects. Newest first, with a
        page `cursor`: each read takes the newest `offset + limit` of a project, and the
        merged list is sliced, so the order is exact across projects. The log pages
        through the newest ME_CONTRIBUTIONS_MAX proposals; beyond that it says
        `truncated`. Reading changes nothing.
        """
        self._proposals_backend()
        principal = ctx.principal
        if principal.via == 'credential':
            raise forbidden('Session authority required')
        limit, state = self._page(ctx, ctx.query)
        offset = state['o']
        if offset >= ME_CONTRIBUTIONS_MAX:
            # Not a cursor this route issued: it never points past the newest
            # ME_CONTRIBUTIONS_MAX (a cursor is not signed, so the offset is checked).
            raise conflict('Cursor is stale or belongs to a different query')
        wanted = min(offset + limit, ME_CONTRIBUTIONS_MAX)
        projects = [p for p in self.service.list_projects(principal)
                    if not p.get('archived') and principal.user_id in (p.get('members') or [])]
        truncated = len(projects) > ME_WORK_MAX_PROJECTS
        items, unavailable, total = [], [], 0
        for project in projects[:ME_WORK_MAX_PROJECTS]:
            capabilities = self.service.capabilities_for(principal, project['id'])
            if CAP_READ not in capabilities:
                continue
            try:
                read = self.backend.proposal_read(project['id'], [
                    'mine', '--submitter', 'account:' + principal.user_id, '--limit', str(wanted),
                    '--order', 'newest'])
            except HttpError as error:
                unavailable.append({'project': project['id'], 'reason': error.code})
                continue
            total += read.get('total') or 0
            for item in read.get('items') or []:
                items.append(dict(self._proposal_view(ctx, capabilities, item), project=project['id'],
                                  project_name=project['name']))
        items.sort(key=lambda item: (item.get('submitted_at') or '', item.get('key') or ''), reverse=True)
        if offset and offset >= total and not unavailable:
            raise conflict('Cursor is stale or belongs to a different query')
        more = offset + limit < total
        reachable = offset + limit < ME_CONTRIBUTIONS_MAX
        truncated = truncated or (more and not reachable)
        return 200, {'items': items[offset:offset + limit], 'total': total, 'truncated': truncated,
                     'next_cursor': make_cursor(principal, None, ctx.query, offset + limit)
                     if more and reachable else None,
                     'unavailable': unavailable,
                     'identity': 'account:' + principal.user_id,
                     'generated_at': now_iso(self.service._now())}

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/brief')
    def tasks_brief(self, ctx):
        """The task page in one read: row, current checkpoint, review chain, facts.

        Readable by any project member (``CAP_READ``), exactly like the task and its
        history. Reading never acknowledges anything.
        """
        self._project(ctx, CAP_READ)
        pid, tid = ctx.params['pid'], ctx.params['tid']
        brief = self.backend.task_brief(pid, tid)
        self._refuse_record_anchor(brief.get('task'))
        review = brief['review']
        checkpoint = brief.get('checkpoint')
        contribution = review.get('contribution')
        actors = [brief['task'].get('assignee')]
        actors += [checkpoint.get('author')] if checkpoint else []
        actors += [contribution.get('author')] if contribution else []
        actors += [r.get('author') for r in review.get('requests') or []]
        # Only a recommendation by a different person from the contribution's author is
        # shown (see _independent); the assignee is checked when one is written, not here,
        # as in the canonical reader. When the newest one is left out, the newest of the
        # others is shown in its place: the backend gives the newest few in full
        # (kittrial-5bb.115 review). Past those, one is still named and has no text here.
        parties = self._read_parties(brief['task'].get('assignee'), (contribution or {}).get('author'))
        standing = [entry for entry in review.get('recommendations') or [] if isinstance(entry, dict)]
        kept = set(self._independent([entry.get('author') for entry in standing], parties)) if parties else set()
        shown = [entry for entry in standing if entry.get('author') in kept]
        newest = review.get('recommendation')
        if not (newest and newest.get('author') in kept):
            newest = next((dict(entry) for entry in shown if 'summary' in entry), None)
        review['recommendation'] = newest
        review['recommendations'] = [{'id': entry.get('id'), 'author': entry.get('author'), 'at': entry.get('at')}
                                     for entry in shown]
        advice = [review.get('recommendation')] if review.get('recommendation') else []
        advice += review.get('recommendations') or []
        actors += [entry.get('author') for entry in advice]
        names = self.service.actor_names(actors)
        task = dict(brief['task'], review_state=review.get('state') or 'none')
        brief['task'] = self._task_view(task, names)
        if checkpoint:
            checkpoint['author_name'] = names.get(checkpoint.get('author'))
        if contribution:
            contribution['author_name'] = names.get(contribution.get('author'))
        for request in review.get('requests') or []:
            request['author_name'] = names.get(request.get('author'))
        for entry in advice:
            entry['author_name'] = names.get(entry.get('author'))
        base = '/v1/projects/%s/tasks/%s' % (pid, tid)
        brief['links'] = {'task': base, 'history': base + '/history',
                          'reviews': base + '/reviews', 'checkpoints': base + '/checkpoints'}
        # Where the project's repository is (kittrial-5bb.118): a label the project's
        # owner recorded. Data for the reader, never an instruction; null when not set.
        brief['project_repository'] = self.service.project_view(ctx.principal, pid).get('repository')
        # The same note wherever the value reaches an agent: it is a label, not an instruction.
        brief['project_repository_note'] = self.service.REPOSITORY_NOTE if brief['project_repository'] else None
        brief['checkpoint_template'] = self._checkpoint_template(brief, base, brief.pop('carry_open_items', []))
        brief['generated_at'] = now_iso(self.service._now())
        return 200, brief

    #: The fields of a checkpoint that may be left out over HTTP, with what is sent for them.
    CHECKPOINT_DEFAULTS = (('source_commit', ''), ('branch', ''), ('open_items', []), ('resolved', []))

    @staticmethod
    def _checkpoint_template(brief, base, carry=()):
        """The checkpoint record to send for this task at this moment (kittrial-5bb.113).

        An agent fills the four texts and posts ``body`` to ``send_to``. ``previous`` and
        ``activity_cursor`` are already those of this read; they go stale when the task
        changes, and the refusal then says to read the brief again. ``open_items`` already
        holds every open item of the previous checkpoint exactly as recorded, source
        included: sent as it is, the record carries them all forward.
        """
        import briefing
        limits = dict(briefing.CHECKPOINT_FIELD_LIMITS)
        current = brief.get('checkpoint') or {}
        keys = ('id', 'kind', 'text', 'source')
        carried = [{key: item.get(key) for key in keys} for item in carry or []]
        template = {
            'send_to': base + '/checkpoints', 'method': 'POST',
            'body': {'schema_version': 1, 'previous': current.get('id'),
                     'activity_cursor': brief.get('activity_cursor'),
                     'intent': '', 'acceptance': '', 'summary': '', 'next_action': '',
                     'source_commit': '', 'branch': '', 'open_items': carried, 'resolved': []},
            'carried_open_items': len(carried),
            'required': ['intent', 'acceptance', 'summary', 'next_action'],
            'optional': {'source_commit': 'the commit the work is at; leave out when there is none',
                         'branch': 'the branch; leave out when there is none',
                         'open_items': 'what is unresolved: the ones already in body, unchanged, plus any new one; '
                                       'leave out only when nothing is',
                         'resolved': 'open items of the previous checkpoint that this one resolves: take the item '
                                     'out of open_items and name its id here; leave out when none'},
            'limits': {name: '<= %d characters' % limits[name]
                       for name in ('intent', 'acceptance', 'summary', 'next_action', 'source_commit', 'branch')},
            'open_item': {'id': 'a short id of your choosing', 'kind': sorted(briefing.KINDS),
                          'text': '<= %d characters' % briefing.CHECKPOINT_TEXT_LIMIT,
                          'source': 'where it came from, <= %d characters' % briefing.CHECKPOINT_SOURCE_LIMIT},
            'resolved_item': {'id': 'the id of the open item', 'reason': '<= %d characters' % briefing.CHECKPOINT_TEXT_LIMIT,
                              'evidence': '<= %d characters' % briefing.CHECKPOINT_SOURCE_LIMIT},
            'items_max': briefing.CHECKPOINT_ITEMS_MAX,
            'note': 'Every open item of the previous checkpoint must be carried forward unchanged or resolved. '
                    'body.open_items already holds them all, complete with source. '
                    'A refusal names every problem with the record at once.'}
        if carry is None:
            # More open items than could be read in one go, or the task changed meanwhile.
            template['carried_open_items'] = None
            template['note'] = ('The open items of the previous checkpoint could not all be read, so body.open_items '
                                'is empty and this record would be refused: read the brief again. '
                                + template['note'])
        return template

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/claim')
    def tasks_claim(self, ctx):
        self._project(ctx, CAP_TASKS)
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']), write=True)
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
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']), write=True)
        payload = self._task_payload(ctx)
        payload.setdefault('schema_version', 1)
        # Over HTTP these four may be left out (kittrial-5bb.113): an agent with no commit
        # yet, or nothing unresolved, need not send empty values. The canonical record is
        # unchanged: what is left out is sent as the empty value.
        for name, empty in self.CHECKPOINT_DEFAULTS:
            payload.setdefault(name, type(empty)())
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))

        def add():
            try:
                result = self.backend.invoke('checkpoints.add', ctx.principal, ctx.params['pid'],
                                             payload, ctx.idempotency_key,
                                             target=ctx.route_target, authorize=ctx.authorize,
                                             capability=CAP_CHECKPOINTS)
            except HttpError as refusal:
                # The canonical refusal names every problem with the record. Hand them over
                # as a list too, so an agent need not parse the sentence.
                if refusal.status == 422 and isinstance(refusal.detail, str):
                    import briefing
                    said = refusal.detail[len('ValueError: '):] if refusal.detail.startswith('ValueError: ') \
                        else refusal.detail
                    refusal.problems = briefing.split_problems(said)
                raise
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
        recommending = payload.get('operation') == 'recommend'
        current = None
        if recommending:
            # A field this kit does not know is refused, not dropped, by the one rule for
            # every review operation below (kittrial-5bb.110): at most five plain names.
            # One read serves the record-anchor check and the person rule below.
            current = self.backend.task_brief(ctx.params['pid'], ctx.params['tid'])
            self._refuse_record_anchor(current.get('task'), write=True)
        else:
            self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']), write=True)
        if 'actor' in payload:
            payload['actor'] = self.service.bind_actor(ctx.principal, payload.get('actor'))
        operation = payload.get('operation')
        if isinstance(operation, str) and operation in EndpointBackend.REVIEW_FIELDS:
            # Unknown and misplaced fields are refused, not dropped (kittrial-5bb.110
            # item 4). The allowed set is the canonical field set for THIS operation
            # plus the documented transport/presentation keys, so a `disposition` on a
            # request-changes or a top-level `severity` is a 422 instead of a silent
            # 201. An operation the canonical writer does not know is left to the
            # backend, which names it in a fixed sentence.
            #
            # A key whose VALUE is null is ABSENT, not a misplaced field (kittrial-5bb.110
            # items 1 and 2): the released Client at cbf6d01 and at b0a4fbd sends every
            # optional field of every operation as null (one 16-key union), and counting
            # those nulls here refused switch on/off, contribute, request-changes,
            # respond, approve, withdraw and recommend alike. The check therefore runs on
            # the SUPPLIED keys only. The nulls stay in the payload: the canonical build
            # below drops a null optional field itself and keeps the null on the required
            # fields whose ABSENCE -- unlike their null -- is an invalid field set.
            allowed = ({'schema_version', 'operation', 'operation_id', 'previous'}
                       | set(EndpointBackend.REVIEW_FIELDS[operation])
                       | set(EndpointBackend.REVIEW_OPTIONAL_FIELDS.get(operation, ()))
                       | set(REVIEW_HTTP_FIELDS)
                       | set(REVIEW_HTTP_OPERATION_FIELDS.get(operation, ())))
            # `previous` is deliberately NOT removed for REVIEW_NO_PREVIOUS: a
            # recommendation is a record beside the chain and carries none, but the
            # released clients send the key (null, or the chain's latest comment id) for
            # every operation, so it is accepted and IGNORED here (the canonical body
            # builder omits it for `recommend`; kittrial-5bb.110 items 1 and 2).
            supplied = {key for key, value in payload.items() if value is not None}
            unknown = sorted(str(key) for key in supplied - allowed)
            if unknown:
                raise invalid('Unsupported review payload field(s) for %s: %s'
                              % (operation, unsupported_fields_text(unknown)))

        def add():
            if recommending:
                # Independence by PERSON, which only the web service can know; the
                # canonical write then applies the name rule and every other rule.
                parties = [(current.get('task') or {}).get('assignee'),
                           ((current.get('review') or {}).get('contribution') or {}).get('author')]
                actor = payload.get('actor') or ctx.principal.actor or ctx.principal.user_id
                if not self._independent([actor], parties) or \
                        not self._independent([ctx.principal.user_id], parties):
                    raise forbidden(self.NOT_INDEPENDENT)
                # One standing recommendation for a contribution from each PERSON
                # (kittrial-5bb.154): a second one, by the same actor or by another agent of
                # the same person, is refused and nothing is stored. Read from the brief, so
                # it is every standing one, whether or not this service counts it.
                standing = [entry.get('author') for entry in (current.get('review') or {}).get('recommendations') or []
                            if isinstance(entry, dict) and isinstance(entry.get('author'), str)]
                own = [name for name in standing if not self._independent([name], [actor, ctx.principal.user_id])]
                if own:
                    shown = self.service.actor_names(own[:1]).get(own[0]) or own[0]
                    raise conflict(self.ALREADY_RECOMMENDED % shown, {'recommended_by': own[0]})
            if payload.get('operation') == 'respond':
                # Only the task's assignee (the contributor of the current revision)
                # may respond to requested changes. Both backends enforce it at the
                # write too; checking here gives the caller a clear 403 instead of the
                # canonical validator's generic refusal. It runs inside the mutation, so
                # an exact retry still replays a committed response.
                task = self.backend.get_task(ctx.params['pid'], ctx.params['tid'])
                actor = payload.get('actor') or ctx.principal.actor or ctx.principal.user_id
                if not isinstance(task, dict) or not task.get('assignee') or \
                        task['assignee'] != actor:
                    raise forbidden('Only the task assignee may respond to requested changes')
            try:
                result = self.backend.invoke('reviews.add', ctx.principal, ctx.params['pid'],
                                             payload, ctx.idempotency_key,
                                             target=ctx.route_target, authorize=ctx.authorize,
                                             capability=capability)
            except HttpError as refusal:
                # A refused recommendation says which rule it hit: the canonical sentence is
                # fixed text of this kit (it names the contribution id the caller sent and
                # nothing else of theirs), so it is the message, not only the detail.
                if recommending and refusal.status == 422 and isinstance(refusal.detail, str) \
                        and refusal.detail.startswith('ValueError: '):
                    raise invalid(refusal.detail[len('ValueError: '):], refusal.detail) from None
                # So does a refused follow-on base (kittrial-5bb.158): the sentence says which
                # base is acceptable, why this one is not and what an operator does about it.
                sentence = EndpointBackend.base_refusal(refusal.detail) if refusal.status == 422 else None
                if sentence is not None:
                    raise invalid(sentence, refusal.detail) from None
                raise
            return result, result
        return self._mutate(ctx, 'reviews.add', ctx.params['pid'], add, status=201,
                            capability=capability, serialize=False, canonical=True)

    #: Said to somebody whose own party (themselves, their owner, their owner's other agent)
    #: has a standing recommendation for the contribution. It names the two ways on from there.
    ALREADY_RECOMMENDED = ('%s has already recommended this contribution, and that recommendation stands until the '
                           'contribution is revised or decided. To ask for changes instead, request changes: the '
                           'recommendation then stops counting. A recommendation cannot be withdrawn.')

    #: Every field a recommendation may carry over HTTP (``task_id`` is set by the route).
    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/history')
    def tasks_history(self, ctx):
        self._project(ctx, CAP_READ)
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']))
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
        assigned, to_review, unavailable, classified = [], [], [], []
        generated_at = now_iso(self.service._now())
        now = agent_prompts.parse_time(generated_at)
        blocked = self._agent_blocked_tasks()
        reads = []
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
            reads.append((project, capabilities, read))
        details = self._me_work_details(reads, actor)
        for project, capabilities, read in reads:
            items = [dict(item, **details[(project['id'], item.get('id'))])
                     if (project['id'], item.get('id')) in details else item
                     for item in read['items']]
            blocked |= {pid_tid[1] for pid_tid, detail in details.items()
                        if pid_tid[0] == project['id'] and detail.get('blocked')}
            names = self.service.actor_names([i.get('assignee') for i in items]
                                             + [i.get('contribution_author') for i in items])
            # What this person could recommend: someone else's contribution, by person, that
            # nobody of theirs has recommended yet (the rule of _agent_review_actions). "Yet" is
            # read from every name on the row, counted or not (kittrial-5bb.147).
            reviewable = {i.get('id') for i in items if CAP_REVIEWS in capabilities
                          and self._independent([actor], [i.get('assignee'), i.get('contribution_author')])
                          and not self._own_party_recommended(i, actor)}
            classified.append(agent_prompts.classify(project, capabilities, items,
                                                     actor, blocked, now, names, reviewable))
            for item in items:
                row = dict(item, project_name=project['name'])
                if item.get('id') in blocked and item.get('status') != 'closed':
                    row['blocked'] = True
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
        # One copyable prompt per agent the person owns, built from exactly this data.
        # The address comes only from --public-url, never from the request's Host
        # header (which a client controls); otherwise the placeholder, like the setup
        # snippet.
        server = self.service.public_url or '<ORCHESTRA_SERVER_URL>'
        owner_name = principal.display_name or principal.user_id
        # Each agent's prompt covers only the projects that agent is granted.
        prompts = [agent_prompts.build_prompt(
            agent, owner_name,
            [p for p in classified if p['id'] in set(agent.get('projects') or [])],
            generated_at, server)
            for agent in agents if agent.get('owner') == principal.user_id]
        return 200, {'assigned': self._task_views(assigned[:MAX_PAGE]),
                     'to_review': self._task_views(to_review[:MAX_PAGE]),
                     'agents': agents, 'agent_prompts': prompts,
                     'truncated': truncated, 'unavailable': unavailable,
                     'generated_at': generated_at}

    def _me_work_details(self, reads, actor):
        """Fill the canonical queue's gaps for the rows that matter most, within a bound.

        The canonical ``work`` projection gives a review state and a count of pending
        request items per task, but no request item ids, no review times and no
        checkpoint state. A backend that offers ``task_detail`` (the canonical binding:
        one ``brief`` read per task) is asked for at most :data:`ME_WORK_DETAIL_MAX`
        tasks per request, highest value first: the caller's own changes-requested
        tasks (pending item ids), contributions awaiting the caller's review (wait
        time), the caller's own claimed tasks, then other claimed tasks where the
        caller approves (blocked). Each detail is cached per principal like the queue
        (``READ_CACHE_SECONDS``). Rows past the bound keep "read the task brief" and
        "wait time unknown", and are simply not marked blocked.
        """
        if not hasattr(self.backend, 'task_detail'):
            return {}
        ranked = []
        for order, (project, capabilities, read) in enumerate(reads):
            approver = CAP_APPROVE in capabilities
            for item in read['items']:
                if item.get('status') == 'closed' or not item.get('id'):
                    continue
                state = item.get('review_state')
                mine = item.get('assignee') == actor
                if state == 'changes-requested' and mine:
                    rank = 0
                elif state in ('awaiting-review', 'legacy-review-ready') and approver:
                    rank = 1
                elif state in (None, 'none') and mine:
                    rank = 2
                elif state in (None, 'none') and approver and item.get('assignee'):
                    rank = 3
                else:
                    continue
                ranked.append((rank, order, str(item['id']), project['id']))
        ranked.sort()
        details = {}
        for _, _, task_id, project_id in ranked[:ME_WORK_DETAIL_MAX]:
            try:
                detail = self._cached_read(
                    'detail:' + task_id, project_id,
                    lambda p=project_id, t=task_id: self.backend.task_detail(p, t),
                    shared=True)
            except HttpError:
                continue
            if isinstance(detail, dict):
                details[(project_id, task_id)] = {k: v for k, v in detail.items()
                                                  if v is not None}
        return details

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


#: The exit status of ``main`` when the port to listen on is taken. The office supervisor
#: reads it to say so in one line (``office_service.WEB_PORT_TAKEN`` is the same number).
EXIT_PORT_TAKEN = 4

#: How long a client may take over each thing the service waits for from it: completing the
#: TLS handshake, sending a request (its line and headers; then its body), and taking a
#: response. Also how long an idle keep-alive connection is kept. Then the connection is closed.
CLIENT_SECONDS = 30
#: Connections served at once, one thread each. A connection beyond it is closed at once.
#: The bound is what keeps silent connections from using up the process's file descriptors
#: (1024 by default on Linux), which the endpoint's own processes and files need too.
CONNECTION_LIMIT = 200
#: Of those, how many one client address may have at once (kittrial-5bb.170 item 2): without
#: it one address that keeps reopening silent connections holds every place for as long as it
#: likes. Half of the places: an office behind one address is the ordinary case (a browser
#: uses up to six connections while a page loads), and one address still cannot take them
#: all; two acting together can. 0: no limit per address (the settings do not offer it:
#: they take 1 to CONNECTION_LIMIT).
ADDRESS_LIMIT = 100
#: Said to a request that came through a trusted proxy while its forwarded address already
#: has that many requests being served.
ADDRESS_BUSY = 'Too many requests from your address are being served at once. Send this one again in a moment.'


class _WatchedWriter:
    """The handler's ``wfile``: every write is under the client deadline (a client that stops reading)."""

    def __init__(self, inner, server, connection):
        self._inner, self._server, self._connection = inner, server, connection

    def write(self, data):
        self._server.watch(self._connection, self._server.client_seconds)
        try:
            return self._inner.write(data)
        except TimeoutError:
            # The socket's own timeout: the client, not a lock wait (which is answered "busy").
            raise ConnectionAbortedError('the client did not take the response') from None
        finally:
            self._server.unwatch(self._connection)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class GuardedServer(ThreadingHTTPServer):
    """One thread per connection, with what an open port needs (kittrial-5bb.163 review).

    * The listening socket is never a TLS socket: ``accept`` returns at once and the
      handshake is the connection's own business (``ApiHandler.setup``).
    * ``watch``/``unwatch``: a connection the service is waiting on has a deadline. One
      reaper thread shuts down a connection whose deadline has passed, which ends the wait
      in its thread whatever it was (handshake, request line, headers, body, a write). A
      shutdown, because a socket timeout bounds each read and not the whole wait: a client
      that sends a byte now and then would stay. The socket has a timeout as well, a
      little longer (``ApiHandler.setup``): on Windows a shutdown from another thread
      reaches the client at once but does not end a read that is already waiting, and the
      timeout does, at most one more such time later. On Linux the shutdown ends it.
    * At most ``connection_limit`` connections at once; one more is closed at once.
    * At most ``address_limit`` of them from one client address (``address_group``); one more
      from it is closed at once, before a thread or a handshake is spent on it. A peer named
      as a trusted proxy is not limited as an address, since every client behind it arrives
      from it: there the limit is per forwarded address and per request being served
      (``request_begins``), which only ``ApiHandler`` can know, from the request's headers.
    """
    daemon_threads = True
    request_queue_size = 128
    tls_context = None
    client_seconds = CLIENT_SECONDS
    connection_limit = CONNECTION_LIMIT
    address_limit = ADDRESS_LIMIT
    trusted_proxies = ()
    REAP_EVERY = 0.25
    TIMEOUT_MARGIN = 2.0
    TLS_LINE_EVERY = 5.0
    ADDRESS_LINE_EVERY = 60.0

    def __init__(self, *args, **kwargs):
        # Before the base class binds: when the bind fails it closes the server, and
        # ``server_close`` must find what it reads. Made after the bind, the missing field
        # turned "Address already in use" into an AttributeError (kittrial-5bb.180).
        self._stopping = threading.Event()
        super().__init__(*args, **kwargs)
        self._guard = threading.Lock()
        self._deadlines = {}
        self._open = 0
        self.cut_off = 0                 # connections closed for being too slow
        self.turned_away = 0             # connections closed for being over the limit
        self.turned_away_for_address = 0  # of those, and requests refused, for the limit per address
        self._said_no_thread = False
        self._said_limit = False
        self._serving = {}              # thread -> (its connection, the address group it is counted in or None)
        self._by_address = {}            # address group -> its open connections
        self._requests_by_address = {}   # forwarded address group -> its requests being served
        self._address_said, self._address_unsaid = None, 0
        self._begun = set()              # of those threads, the ones start() has returned for
        self._tls_said, self._tls_unsaid = None, 0
        self._reaper = threading.Thread(target=self._reap, name='connection-reaper', daemon=True)
        self._reaper.start()

    def watch(self, connection, seconds):
        with self._guard:
            self._deadlines[connection] = time.monotonic() + seconds

    def unwatch(self, connection):
        with self._guard:
            self._deadlines.pop(connection, None)

    def _reap(self):
        import socket
        while not self._stopping.wait(self.REAP_EVERY):
            now = time.monotonic()
            with self._guard:
                late = [connection for connection, deadline in self._deadlines.items() if deadline <= now]
                for connection in late:
                    del self._deadlines[connection]
                self.cut_off += len(late)
            for connection in late:
                try:
                    # The plain shutdown also for a TLS socket: it ends the other thread's wait
                    # without touching the TLS state that thread is using.
                    socket.socket.shutdown(connection, socket.SHUT_RDWR)
                except OSError:
                    pass
            # A thread that was started and is no longer alive while its connection is still
            # registered died before it served (out of memory in its first steps).
            # "Was started" is: start() has returned for it. Not "has an ident": a thread has
            # its ident a few steps BEFORE it is marked started, and is_alive() is False until
            # that mark, so a thread that was only starting looked dead, and an honest
            # connection was closed under it (kittrial-5bb.175).
            with self._guard:
                dead = [(thread, entry[0]) for thread, entry in self._serving.items()
                        if thread in self._begun and not thread.is_alive()]
            for thread, request in dead:
                self._gave_up(thread, request, 'the thread ended before it served')

    def open_connections(self, address=None):
        """How many connections are open: all of them, or those counted for ``address``."""
        with self._guard:
            return self._open if address is None else self._by_address.get(address_group(address), 0)

    def _limited_as(self, client_address):
        """The group ``client_address`` is counted in, or None when it is not limited as an address."""
        if not self.address_limit:
            return None
        try:
            peer = client_address[0]
        except (IndexError, TypeError):
            return None
        if any(address_matches(peer, network) for network in self.trusted_proxies):
            return None
        return address_group(peer)

    def _address_turned_away(self, group, counted, done):
        """Count it and make its line (the caller holds the lock): at most one every ADDRESS_LINE_EVERY seconds."""
        self.turned_away_for_address += 1
        now = time.monotonic()
        if self._address_said is not None and now - self._address_said < self.ADDRESS_LINE_EVERY:
            self._address_unsaid += 1
            return None
        unsaid, self._address_unsaid, self._address_said = self._address_unsaid, 0, now
        return ('connections: %s has %d %s, the limit for one address; further ones from it are %s%s'
                % (ascii(str(group))[:60], self.address_limit, counted, done,
                   ' (%d more such, from any address, since the last such line)' % unsaid if unsaid else ''))

    def request_begins(self, group):
        """A request that came through a trusted proxy for ``group``: False when it has its share already."""
        with self._guard:
            if not self.address_limit:
                return True
            if self._requests_by_address.get(group, 0) < self.address_limit:
                self._requests_by_address[group] = self._requests_by_address.get(group, 0) + 1
                return True
            line = self._address_turned_away(group, 'requests being served', 'answered 503')
        if line:
            print(line, file=sys.stderr, flush=True)
        return False

    def request_ends(self, group):
        with self._guard:
            left = self._requests_by_address.get(group, 0) - 1
            if left > 0:
                self._requests_by_address[group] = left
            else:
                self._requests_by_address.pop(group, None)

    def _left(self, thread):
        """Free the place of ``thread``'s connection (the caller holds the lock): its request, or None."""
        self._begun.discard(thread)
        entry = self._serving.pop(thread, None)
        if entry is None:
            return None
        request, group = entry
        self._open -= 1
        if group is not None:
            left = self._by_address.get(group, 0) - 1
            if left > 0:
                self._by_address[group] = left
            else:
                self._by_address.pop(group, None)
        return request

    def process_request(self, request, client_address):
        group = self._limited_as(client_address)
        line = None
        with self._guard:
            over = self._open >= self.connection_limit
            if over:
                self.turned_away += 1
                if not self._said_limit:
                    self._said_limit = True
                    line = ('connections: the limit of %d open connections was reached; further ones are closed '
                            'at once (said once)' % self.connection_limit)
            elif group is not None and self._by_address.get(group, 0) >= self.address_limit:
                over = True
                self.turned_away += 1
                line = self._address_turned_away(group, 'connections open', 'closed at once')
            else:
                self._open += 1
                if group is not None:
                    self._by_address[group] = self._by_address.get(group, 0) + 1
        if over:
            if line:
                print(line, file=sys.stderr, flush=True)
            self.shutdown_request(request)
            return
        # The thread is started here and not by the base class, so that it is known: one that
        # could not be started, or that died before it served (no memory for it), must not
        # keep its connection open and its place taken for ever.
        thread = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
        with self._guard:
            self._serving[thread] = (request, group)
        try:
            thread.start()
        except RuntimeError as failed:
            # "can't start new thread": the process is at a limit of its own (memory, threads)
            # below the connection limit. The connection is closed like one over the limit,
            # and it is said once, in one line: not a traceback for each (review of .163).
            self._gave_up(thread, request, ascii(str(failed))[:80])
        except BaseException:
            with self._guard:
                self._left(thread)
            raise
        else:
            with self._guard:
                if thread in self._serving:           # it may have served and gone already
                    self._begun.add(thread)

    def _gave_up(self, thread, request, why):
        """No thread serves ``request``: close it, free its place, and say so the first time."""
        with self._guard:
            if self._left(thread) is None:
                return
            self.turned_away += 1
            first = not self._said_no_thread
            self._said_no_thread = True
            open_now = self._open
        if first:
            print('connections: no thread could be started for a connection with %d open (%s); such '
                  'connections are closed at once (said once)' % (open_now, why), file=sys.stderr, flush=True)
        self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._guard:
                self._left(threading.current_thread())

    def handle_error(self, request, client_address):
        """A client that went away or was cut off is not an error of the service: no traceback."""
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, ssl.SSLError, TimeoutError)):
            if str(error).startswith('TLS handshake not completed'):
                # One line for the operator (a certificate the client does not accept shows
                # here), and at most one every TLS_LINE_EVERY seconds: a port scan is not a
                # line for each connection. The next line says how many were not shown.
                now = time.monotonic()
                with self._guard:
                    quiet = self._tls_said is not None and now - self._tls_said < self.TLS_LINE_EVERY
                    if quiet:
                        self._tls_unsaid += 1
                    else:
                        unsaid, self._tls_unsaid, self._tls_said = self._tls_unsaid, 0, now
                if not quiet:
                    print('tls: %s: %s%s' % (ascii(str(client_address[0]))[:60], ascii(str(error))[:200],
                                             ' (and %d more since the last such line)' % unsaid if unsaid else ''),
                          file=sys.stderr, flush=True)
            return
        super().handle_error(request, client_address)

    def server_close(self):
        # Also for a server that never finished starting (the base class calls this when its
        # bind fails): nothing here may need a field that is made later.
        self._stopping.set()
        super().server_close()


def quiet_memory_errors():
    """Out of memory in a thread's first steps is one line, said once, not a traceback each time.

    The interpreter reports it as an exception nobody can catch ("Exception ignored in thread
    started by"), once per connection under a memory limit. The connection itself is closed by
    the server's reaper. Everything else that cannot be raised is reported as before.
    """
    usual = sys.unraisablehook
    said = []

    def hook(unraisable):
        if isinstance(unraisable.exc_value, MemoryError):
            if not said:
                said.append(True)
                print('memory: the process ran out of memory while starting a thread; its limit is below what the '
                      'connections it is asked to serve need (said once)', file=sys.stderr, flush=True)
            return
        usual(unraisable)
    sys.unraisablehook = hook
    return hook


def create_server(service, backend, *, host='127.0.0.1', port=0, trusted_proxies=(),
                  max_body=MAX_BODY_BYTES, certfile=None, keyfile=None,
                  allow_plaintext_non_loopback=False, web_root=DEFAULT_WEB_ROOT,
                  client_seconds=None, connection_limit=None, address_limit=None):
    """Bind the service. Refuse a non-loopback plaintext listener unless explicitly allowed."""
    loopback = host in LOOPBACK
    if not loopback and certfile is None and not allow_plaintext_non_loopback:
        raise ValueError('Refusing plaintext on a non-loopback interface; supply TLS or '
                         'explicitly allow disposable plaintext')
    context = None
    if certfile:
        # Before anything is bound: a certificate or key that cannot be used stops the start.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile)
    if ':' in host:
        server_class = type('GuardedServer6', (GuardedServer,), {'address_family': __import__('socket').AF_INET6})
    else:
        server_class = GuardedServer
    httpd = server_class((host, port), build_handler(service, backend,
                                                     trusted_proxies=trusted_proxies,
                                                     max_body=max_body,
                                                     web_root=web_root))
    # The TLS context is the connection's (ApiHandler.setup): the listening socket stays plain.
    httpd.tls_context = context
    if client_seconds is not None:
        httpd.client_seconds = client_seconds
    if connection_limit is not None:
        httpd.connection_limit = connection_limit
    if address_limit is not None:
        httpd.address_limit = address_limit
    # Who is not limited as an address: the same peers whose forwarded headers are believed.
    httpd.trusted_proxies = tuple(trusted_proxies or ())
    return httpd


def runtime_service_lock(root):
    """Take the supervisor's runtime lock, or return None when a service holds it.

    ``office_service.py run`` holds an exclusive flock on ``<root>/office-service.lock``
    for its whole life. Taking the same lock here makes bootstrap and a running
    service mutually exclusive: bootstrap is refused while a service runs (a running
    service keeps the state in memory and writes it back, so an account added
    underneath it is silently lost), and a service cannot start underneath a bootstrap
    in progress. The caller holds the returned file descriptor until the bootstrap is
    written, then releases it.

    Raises ``ValueError`` on a platform without ``fcntl``, because the office service
    is a POSIX deployment and silently skipping the guard would reintroduce the loss.
    """
    try:
        import fcntl
    except ImportError:
        raise ValueError('Bootstrapping needs the runtime lock, which requires a POSIX host') from None
    path = Path(root)/'office-service.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def build_backend(service, args):
    """Select the canonical backend. ``endpoint`` is the documented Linux service."""
    if args.backend == 'endpoint':
        return EndpointBackend(args.endpoint_python, args.endpoint, args.root,
                               service=service, actor_namespace=args.actor_namespace,
                               timeout=args.endpoint_timeout, create_timeout=getattr(args, 'create_timeout', 900))
    return InProcessBackend(service)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Orchestra authenticated HTTP service')
    parser.add_argument('--state', required=True, help='private service state path (outside source)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', help='TLS certificate (PEM)')
    parser.add_argument('--key', help='TLS private key (PEM)')
    parser.add_argument('--allow-plaintext-on-network', action='store_true',
                        help='serve plain HTTP on a host that is not loopback (no --cert): passwords and session '
                             'cookies then cross the network unencrypted. Refused without this flag.')
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
    parser.add_argument('--create-timeout', type=int, default=900,
                        help='seconds one project creation may take (never less than --endpoint-timeout); a '
                             'creation is slower the more project databases the server holds')
    parser.add_argument('--max-body', type=int, default=MAX_BODY_BYTES)
    parser.add_argument('--connections-per-address', type=int, default=ADDRESS_LIMIT, metavar='N',
                        help='connections one client address may have open at once, of the %d the service '
                             'serves (default %d; 1 to %d). Behind a --trusted-proxy: requests being served at '
                             'once for one forwarded address.' % (CONNECTION_LIMIT, ADDRESS_LIMIT, CONNECTION_LIMIT))
    parser.add_argument('--logins-per-address', type=int, default=Service.LOGINS_PER_ADDRESS, metavar='N',
                        help='log-ins one client address may have in flight at once, of the %d in all '
                             '(default %d; 1 to %d)' % (Service.LOGINS_AT_ONCE, Service.LOGINS_PER_ADDRESS,
                                                        Service.LOGINS_AT_ONCE))
    parser.add_argument('--public-url',
                        help='canonical base URL of this service, used only to render '
                             'copyable agent setup/resume snippets (e.g. https://host)')
    parser.add_argument('--bootstrap-user', help='one-time operator bootstrap superuser')
    parser.add_argument('--list-unconfirmed-projects', action='store_true',
                        help='print the project records the endpoint backend will not serve (an upgrade '
                             'check: a superuser confirms or archives each one), then exit; read-only')
    web = parser.add_mutually_exclusive_group()
    web.add_argument('--web-root', default=str(DEFAULT_WEB_ROOT),
                     help='directory holding the browser interface served at / '
                          '(default: the kit web/ directory)')
    web.add_argument('--no-web', action='store_true',
                     help='serve only the JSON API; do not serve the browser interface')
    args = parser.parse_args(argv)

    if args.list_unconfirmed_projects:
        # Read-only: the state document is read as JSON, without opening the store (which
        # may migrate or write), so it is safe beside a running service and never
        # creates a state file from a mistyped path.
        if not Path(args.state).is_file():
            parser.error('--list-unconfirmed-projects needs an existing --state file')
        document = json.loads(Path(args.state).read_text(encoding='utf-8'))
        if not isinstance(document, dict) or not isinstance(document.get('projects'), dict):
            parser.error('--state is not a service state document')
        import types
        items = unusable_projects(types.SimpleNamespace(state=document))
        print(json.dumps({'items': items, 'total': len(items)}, indent=2))
        return 0
    if args.bootstrap_user:
        # Checked before the state store is opened, so a refusal neither reads nor
        # writes the state a running service is about to save over.
        if not args.root:
            parser.error('--bootstrap-user needs --root: the runtime lock is what proves no service is running')
        if not Path(args.root).is_dir():
            # One plain sentence, checked before the lock is opened: os.open on a path
            # under a missing root raises FileNotFoundError, and the review asked for a
            # sentence instead of a traceback (kittrial-5bb.162 item bootstrap-docs-and-root).
            print('Refusing to bootstrap %s: --root must name an existing runtime directory, and %s is not one.'
                  % (args.bootstrap_user, args.root), file=sys.stderr)
            return 1
        lock_fd = runtime_service_lock(args.root)
        if lock_fd is None:
            print('Refusing to bootstrap %s: a service is running for runtime %s (it holds '
                  'office-service.lock). Stop the service and bootstrap while it is stopped; a running '
                  'service keeps its state in memory and writes it back, so the new account would be lost.'
                  % (args.bootstrap_user, args.root), file=sys.stderr)
            return 1
        import getpass
        try:
            store = Store(args.state)
            password = getpass.getpass('New superuser password: ')
            Service.bootstrap_superuser(store, args.bootstrap_user, password)
        finally:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        print('Bootstrapped %s' % args.bootstrap_user)
        return 0
    if not 1 <= args.connections_per_address <= CONNECTION_LIMIT:
        parser.error('--connections-per-address must be a whole number from 1 to %d (the connections served in all)'
                     % CONNECTION_LIMIT)
    if not 1 <= args.logins_per_address <= Service.LOGINS_AT_ONCE:
        parser.error('--logins-per-address must be a whole number from 1 to %d (the log-ins in flight in all)'
                     % Service.LOGINS_AT_ONCE)
    store = Store(args.state)
    if args.backend == 'endpoint' and (not args.endpoint or not args.root):
        parser.error('--backend endpoint requires --endpoint and --root '
                     '(use --backend inprocess only for a disposable local check)')
    trusted = list(args.trusted_proxy)
    if args.trust_proxy and 'localhost' not in trusted:
        trusted.append('localhost')
    service = Service(store, public_url=args.public_url)
    service.LOGINS_PER_ADDRESS = args.logins_per_address
    backend = build_backend(service, args)
    for line in operator_allowlist_warnings(args.root if args.backend == 'endpoint' else None):
        print(line, file=sys.stderr)
    try:
        httpd = create_server(service, backend, host=args.host, port=args.port,
                              trusted_proxies=trusted, max_body=args.max_body,
                              certfile=args.cert, keyfile=args.key,
                              allow_plaintext_non_loopback=args.allow_plaintext_on_network,
                              web_root=None if args.no_web else args.web_root,
                              address_limit=args.connections_per_address)
    except OSError as failed:
        # On Windows a port another program holds for itself alone is refused as "access", not "in use".
        if failed.errno != errno.EADDRINUSE and getattr(failed, 'winerror', None) != 10013:
            raise
        # One line and a status of its own, not a traceback: the commonest reason a service
        # does not start, and the operator must be able to read it.
        print('orchestra-http: port %d on %s is taken: something else is listening on it, so the web service did '
              'not start. Stop that, or start this service on another port.' % (args.port, args.host),
              file=sys.stderr, flush=True)
        return EXIT_PORT_TAKEN
    if args.host not in LOOPBACK and not args.cert:
        print('WARNING: serving plain HTTP on %s:%d. Passwords and session cookies cross the network unencrypted. '
              'Use --cert and --key for HTTPS.' % (args.host, httpd.server_address[1]), file=sys.stderr, flush=True)
    print('orchestra-http listening on %s:%d (backend=%s, web=%s)'
          % (args.host, httpd.server_address[1], args.backend,
             'off' if args.no_web else args.web_root))
    quiet_memory_errors()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
