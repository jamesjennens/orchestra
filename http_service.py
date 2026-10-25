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
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('http_service.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer; on an office installation that is the bundled interpreter, INSTALL_ROOT/current/python-runtime/....\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import base64
import binascii
import errno
import hashlib
import hmac
import ipaddress
import json
import os
from server_address import ps_quote, sh_quote
import record_json
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import actor_names
import agent_prompts
import bd_refusals
import claim_limit
import project_setup
import project_coordination
import task_files
import rooms
from coordination import MERGE_SLOT_SUFFIX, is_merge_slot, merge_slot_sentence
from reserved_comments import (ANCHOR_READ_IDS_MAX, carries_record_label, hide_records,
                               is_record_anchor)
from http_auth import (AGENT_ROLES, AGENT_SECRET_ENV, agent_secret_file, CAP_ACCOUNTS_ADMIN, CAP_AGENT_SELF, CAP_AGENTS,
                       CAP_APPROVE,
                       CAP_CHECKPOINTS, CAP_FEEDBACK, CAP_PROPOSALS, CAP_ROOMS_POST, CAP_COORDINATE,
                       CAP_RELEASE,
                       CAP_PROJECT_ADMIN, CAP_PROJECT_CREATE, CAP_PROJECT_HOST_CREATE, CAP_READ,
                       CAP_RECORDS, CAP_REVIEWS,
                       CAP_TASKS, MAX_PASSWORD, RESULT_RETENTION_SECONDS, HttpError, Service, Store,
                       address_group, authority_request, busy, canonical_json, conflict, forbidden, invalid, not_found,
                       not_implemented, now_iso, request_hash, session_limit_seconds, unauthenticated,
                       uncertain, unsupported, validate_agent_credential_days, validate_agent_role,
                       validate_claim_limit, validate_password_min_length)
from version import report as version_report

KIT_VERSION = '0.1.0'
#: Where the installed kit is: the documents this service shows are read from it.
KIT_DIRECTORY = Path(__file__).resolve().parent
#: The mark ``/healthz`` carries so a probe can tell this service from whatever else answers on
#: the port (kittrial-5bb.168 item 8): its name, version and build id. Read once, then reused.
_SERVICE_IDENTITY = None
#: The header an operator turns on with ``--strict-transport-security`` (the private setting
#: ``strict_transport_security``). Off by default (kittrial-5bb.168 item 6): a self-signed first
#: install must not pin a browser to HTTPS for the host, and an office may sit behind a proxy.
HSTS_HEADER = 'max-age=31536000'


def service_identity():
    """What only this service says on ``/healthz``: its name, version and build id."""
    global _SERVICE_IDENTITY
    if _SERVICE_IDENTITY is None:
        info = version_report(KIT_DIRECTORY)
        _SERVICE_IDENTITY = {'service': 'orchestra-office',
                             'version': info['version'], 'build_id': info['build_id']}
    return dict(_SERVICE_IDENTITY)


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
#: Cap on the fall-through candidates a REFUSED claim offers (kittrial-5bb.6 review item
#: 4). It is deliberately far smaller than the attention cap: a refusal is an error
#: answer, not a listing, and a hundred candidate titles with a hostile one among them is
#: both a size and a prompt-injection problem. When the cap cuts the list, the detail says
#: so (``candidates_truncated``) instead of silently dropping rows.
CLAIM_CANDIDATES_LIMIT = 10
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
# A reference or capability catalog key (reference_records.KEY, capability_records.KEY): lowercase and dotted.
REFERENCE_KEY = r'[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+'
CAPABILITY_KEY = REFERENCE_KEY
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
        # kittrial-5bb.370: a task's files have their own store and route now; the task-create
        # field still answers 501 (design 3a: version 1 does not bind it), so no client changes.
        raise not_implemented('Task creation does not take attachments; create the task, then '
                              'attach each file with PUT /v1/projects/PID/tasks/TID/files')
    return normalized


# ------------------------------------------------------------------ cursors
_FALLBACK_CURSOR_KEY = secrets.token_hex(32)
CURSOR_BAD_SIGNATURE = 'Cursor signature is invalid; start the read again without a cursor.'


def cursor_scope(query):
    """The part of the query a cursor is bound to: the page size, not the cursor itself.

    Hashing the whole query would bind the cursor to itself and make every second
    page fail as stale.
    """
    return {key: value for key, value in query.items() if key != 'cursor'}


def cursor_binding(principal, project_id, query):
    """Compact 16-char digest binding a cursor to the caller, project and query."""
    return request_hash({'u': principal.user_id, 'p': project_id,
                         'q': cursor_scope(query)})[:16]


def _cursor_mac(key, body):
    secret = (key if isinstance(key, str) and key else _FALLBACK_CURSOR_KEY).encode('utf-8')
    digest = hmac.new(secret, canonical_json(body).encode('utf-8'), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def make_cursor(principal, project_id, query, offset, extra=None, *, key=None):
    payload = {'b': cursor_binding(principal, project_id, query), 'o': offset}
    if extra is not None:
        payload['x'] = extra
    payload['s'] = _cursor_mac(key, payload)
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def read_cursor(principal, project_id, query, cursor, *, key=None):
    """Return ``{'o': offset, 'x': route-specific continuation}`` for one request.

    Signed cursors carry an HMAC (``s``) computed with the installation's ``cursor_key``
    in service state (never in the cursor); a bad signature is refused with one sentence.
    Old unsigned cursors matching the caller, project and query simply restart the read
    (``{'o': 0, 'x': None}``).
    """
    if cursor is None:
        return {'o': 0, 'x': None}
    if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
        raise conflict('Invalid cursor')
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode('ascii'))
    except (ValueError, binascii.Error, UnicodeError):
        raise conflict('Invalid cursor')
    try:
        data = record_json.loads(raw)
    except (ValueError, UnicodeError):
        if raw.startswith(b'{"b":') and b'"s":' in raw:
            raise conflict(CURSOR_BAD_SIGNATURE)
        raise conflict('Invalid cursor')
    if not isinstance(data, dict):
        raise conflict('Invalid cursor')
    expected_binding = cursor_binding(principal, project_id, query)
    if 's' not in data:
        # An old unsigned cursor (or one with its signature omitted) never advances the
        # offset: once its binding and offset shape check out, it simply restarts the read.
        if type(data.get('o')) is not int or data['o'] < 0:
            raise conflict('Invalid cursor')
        if 'b' in data and set(data) <= {'b', 'o', 'x'}:
            if data.get('b') != expected_binding:
                raise conflict('Cursor is stale or belongs to a different query')
            return {'o': 0, 'x': None}
        if set(data) <= {'u', 'p', 'q', 'o', 'x'} and 'u' in data and 'q' in data:
            allowed_projects = ({project_id, project_id.split('/', 1)[0]}
                                if isinstance(project_id, str) else {project_id})
            if (data.get('u') != principal.user_id
                    or data.get('p') not in allowed_projects
                    or data.get('q') != request_hash(cursor_scope(query))[:16]):
                raise conflict('Cursor is stale or belongs to a different query')
            return {'o': 0, 'x': None}
        raise conflict('Invalid cursor')
    if not set(data) <= {'b', 'o', 'x', 's'} or not isinstance(data.get('s'), str) or not data['s']:
        raise conflict(CURSOR_BAD_SIGNATURE)
    unsigned = {k: v for k, v in data.items() if k != 's'}
    if not hmac.compare_digest(data['s'], _cursor_mac(key, unsigned)):
        raise conflict(CURSOR_BAD_SIGNATURE)
    if (data.get('b') != expected_binding
            or type(data.get('o')) is not int or data['o'] < 0):   # a bool is not an offset
        raise conflict('Cursor is stale or belongs to a different query')
    return {'o': data['o'], 'x': data.get('x')}


def _logical_entries(items):
    """Group ``items`` into logical entries (fragments of one entry stay together and end at ``continued: False``)."""
    logical = []
    current = []
    for item in items:
        current.append(item)
        if not (isinstance(item, dict) and item.get('continued')):
            logical.append(current)
            current = []
    if current:
        logical.append(current)
    return logical


def _logical_entry_count(items):
    """How many completed logical entries ``items`` holds."""
    return sum(1 for item in items if not (isinstance(item, dict) and item.get('continued')))


def _slice_logical_entries(items, offset, limit):
    """Flatten logical entries ``offset:offset + limit`` from ``items``."""
    return [item for group in _logical_entries(items)[offset:offset + limit] for item in group]


def _pack_history_cursor(cursor, project_id, task_id, recent):
    """Compact a canonical ``briefing.history_page`` token by omitting route-known fields."""
    if cursor is None or not isinstance(cursor, str):
        return cursor
    try:
        from briefing import untoken
        c = untoken(cursor)
        if (not isinstance(c, dict) or c.get('v') != 1 or c.get('kind') != 'history'
                or c.get('project') != str(project_id) or c.get('task') != str(task_id)
                or c.get('since') is not None or c.get('entry_kind') is not None
                or c.get('order', 'oldest') != ('newest' if recent else 'oldest')):
            return cursor
        snap_bytes = bytes.fromhex(c['snapshot'])
        snap_b64 = base64.urlsafe_b64encode(snap_bytes).decode('ascii').rstrip('=')
        packed = [snap_b64, c['index'], c['offset']]
        scp = c.get('scp')
        if scp is not None:
            prefix = str(task_id) + '-'
            if isinstance(scp, str) and scp.startswith(prefix):
                scp = '~' + scp[len(prefix):]
            packed.append(scp)
        return packed
    except Exception:
        return cursor


def _unpack_history_cursor(packed, project_id, task_id, recent):
    """Restore a canonical ``briefing.history_page`` token from :func:`_pack_history_cursor`."""
    if packed is None or isinstance(packed, str):
        return packed
    if not isinstance(packed, list) or len(packed) not in (3, 4):
        raise conflict('Invalid cursor')
    snap_b64, index, start = packed[0], packed[1], packed[2]
    if not isinstance(snap_b64, str) or type(index) is not int or type(start) is not int:
        raise conflict('Invalid cursor')
    try:
        from briefing import token
        padded = snap_b64 + '=' * (-len(snap_b64) % 4)
        digest = base64.urlsafe_b64decode(padded.encode('ascii')).hex()
        if len(digest) != 64:
            raise ValueError('bad snapshot digest')
        c = {'v': 1, 'kind': 'history', 'project': str(project_id), 'task': str(task_id),
             'snapshot': digest, 'since': None, 'index': index, 'offset': start}
        if recent:
            c['order'] = 'newest'
        if len(packed) == 4 and packed[3] is not None:
            scp = packed[3]
            if not isinstance(scp, str):
                raise ValueError('bad scp')
            if scp.startswith('~'):
                scp = str(task_id) + '-' + scp[1:]
            c['scp'] = scp
        return token(c)
    except Exception:
        raise conflict('Invalid cursor') from None


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


#: The response header that carries the server's time of a write that was carried out, on
#: every such answer whatever the shape of its body (kittrial-5bb.97).
SERVER_TIME_HEADER = 'X-Server-Time'

#: The endpoint's ``server_time`` of the write the current thread's request carried out, if
#: any (set by ``EndpointBackend._checked``, read and cleared by ``ApiHandler._mutate``).
WRITTEN = threading.local()


def written_at(service):
    """The time for a write answer: the endpoint's when it carried the write out, else the service's clock.

    UTC with its offset, whole seconds: the endpoint's format (``http_authority.server_time``).
    """
    at = getattr(WRITTEN, 'at', None)
    WRITTEN.at = None
    if isinstance(at, str):
        return at
    if at is False:
        return None                                   # an endpoint's stored answer that has no time
    from http_authority import server_time
    return server_time(service._now())


class InProcessBackend:
    """Disposable canonical operations used for local validation.

    Every ``invoke`` is idempotent for a (principal, project, route, key) tuple: the
    committed result is stored and returned on an exact retry, so an uncertain
    response can be reconciled without duplicating a record.
    """

    ROUTES = ('tasks.create', 'tasks.update', 'tasks.claim', 'tasks.comment', 'checkpoints.add',
              'reviews.add', 'feedback.add', 'projects.create', 'projects.archive',
              'members.set', 'members.remove', 'credentials.issue', 'credentials.revoke',
              'proposals.submit', 'proposals.dispose', 'lifecycle.scope', 'lifecycle.fact',
              'files.record', 'files.withdraw')
    #: Where this service-local backend keeps task files (kittrial-5bb.370): one folder per
    #: project under it. None (the default) means it has no store and the routes answer 501;
    #: a test or a local trial sets it.
    files_root = None
    # Contributed requirement proposals are native records behind the canonical endpoint;
    # this service-local backend has no store for them (the routes answer 501).
    PROPOSALS = False
    RECORDS = False
    # Everything here is service-local, so a project is created by the HTTP route itself.
    PROJECT_CREATE = 'create'
    #: This backend has no host tracker, so a credential's name is judged by its shape and the
    #: service's namespace only; no row rule is applied or recorded (kittrial-5bb.188 item 4).
    ACTOR_ROWS = False
    #: Where the disposable backend keeps the empty-wake host log (kittrial-5bb.397):
    #: one folder per project under it. None (the default) keeps no log, exactly as it
    #: keeps no project directory today.
    wake_root = None

    def __init__(self, service, actor_namespace=None, *, delivery_roots=(), delivery_serve_max_bytes=None):
        import actor_names
        from review_workflow import DELIVERY_SERVE_MAX_BYTES
        self.service = service
        self.actor_namespace = actor_namespace or actor_names.SERVICE_NAMESPACE
        self.faults = {}
        self.delivery_roots = tuple(delivery_roots or ())
        self.delivery_serve_max_bytes = (
            DELIVERY_SERVE_MAX_BYTES if delivery_serve_max_bytes is None else int(delivery_serve_max_bytes)
        )

    def wake_project_dir(self, project_id):
        """The scratch project folder for the empty-wake log, or ``None``."""
        if self.wake_root is None:
            return None
        return Path(self.wake_root) / str(project_id)

    def get_delivery_roots(self):
        return tuple(self.delivery_roots or ())

    def get_delivery_serve_max_bytes(self):
        return int(self.delivery_serve_max_bytes)

    def fail_next(self, route, times=1):
        """Test hook: commit, then raise :class:`UncertainOutcome` for the next call(s)."""
        self.faults[route] = times

    def actor_standing(self, project_id, names, rows=False, own=()):
        """For each name: why a worker credential may not write under it, or None.

        This backend has no host, so no session actors, no operator list and no tracker rows:
        what is left of ``actor_names.collision`` is the shape of a session actor, a
        look-alike name, and the namespace this service was started with (``rows`` and ``own``
        are accepted and ignored, so the routes need not know which backend they are on).
        """
        import actor_names
        service = (self.actor_namespace, actor_names.SERVICE_NAMESPACE)
        return {name: actor_names.collision(name, service=service) for name in names}

    def guidance_state(self, project_id, actor):
        """No guidance store in this backend: answer ``None``, never an error.

        This service-local backend has no host project directory, so it cannot hold the
        standing guidance at all. ``None`` means "not readable here" and the agent
        routes add no guidance item; the canonical endpoint backend
        (:meth:`EndpointBackend.guidance_state`) is the one that reads it.
        """
        return None

    #: The per-project claiming setting (kittrial-5bb.352) this backend remembers in
    #: its canonical state when it has no host project directory. Absent means open.
    CLAIMING_DEFAULT = {'claiming': 'open', 'set_by': None, 'set_at': None,
                        'set_via': None, 'previous': None, 'history': [],
                        'source': 'default'}

    def claiming_state(self, project_id):
        values = self.state.get('claiming')
        entry = values.get(project_id) if isinstance(values, dict) else None
        if not isinstance(entry, dict) or entry.get('claiming') not in ('open', 'assigned'):
            return dict(self.CLAIMING_DEFAULT)
        result = {key: entry.get(key) for key in
                  ('claiming', 'set_by', 'set_at', 'set_via', 'previous', 'history')}
        result['source'] = 'record'
        return result

    def set_claiming(self, project_id, value, actor, via='web'):
        if value not in ('open', 'assigned'):
            raise ValueError("The claiming setting must be 'open' or 'assigned'")
        current = self.claiming_state(project_id)
        previous = None
        history = list(current.get('history') or [])
        if current.get('source') == 'record':
            previous = {key: current.get(key) for key in
                        ('claiming', 'set_by', 'set_at', 'set_via')}
            history.append(previous)
        import datetime
        record = {'claiming': value, 'set_by': actor, 'set_via': via,
                  'set_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'previous': previous, 'history': history[-20:]}
        values = self.state.setdefault('claiming', {})
        if not isinstance(values, dict):
            values = self.state['claiming'] = {}
        values[project_id] = record
        result = {key: record.get(key) for key in
                  ('claiming', 'set_by', 'set_at', 'set_via', 'previous', 'history')}
        result['source'] = 'record'
        return result

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
                # A stored answer: the time kept with it, and none where none was kept. Never
                # the clock now, which is the time of the retry (kittrial-5bb.97).
                WRITTEN.at = self.service.result_written_at(result_key) or False
                return self.service.result_get(result_key)
            # The at-use rule the endpoint applies, applied here too (kittrial-5bb.188 item 4):
            # this backend has no endpoint, so without this an earlier credential under a name
            # it refuses at issue goes on writing. It is after the stored answer, so an exact
            # retry of a write that already committed still returns that answer. What it knows
            # is the name's shape, a look-alike, and the namespace the service was started
            # with; it has no host lists and no tracker rows.
            if getattr(principal, 'via', None) == 'credential':
                import actor_names
                namespace = getattr(principal, 'actor', None)
                reason = self.actor_standing(project_id, [namespace]).get(namespace)
                if reason is not None:
                    raise forbidden(actor_names.refusal(namespace, reason))
            result = (self._lifecycle_write(route, principal, project_id, payload, result_key)
                      if route.startswith('lifecycle.') else self._dispatch(route, principal, project_id, payload))
            from http_authority import server_time
            WRITTEN.at = server_time(self.service._now())
            if result_key is not None:
                self.service.result_put(result_key, result, written_at=WRITTEN.at)
            self.service.store.save()
        if self.faults.get(route, 0) > 0:
            self.faults[route] -= 1
            raise UncertainOutcome()
        return result

    # -- canonical operations --------------------------------------------------
    def lifecycle_read(self, principal, project_id, task_id):
        import lifecycle
        from requirements import content_hash
        with self.service.store.lock:
            self.service.check_authority(principal, project_id, CAP_READ)
            task = self._task(project_id, task_id)
            memory = task.get('web_lifecycle') or {}
            scope = memory.get('scope')
            token = content_hash(scope) if scope is not None else None
            facts = {d: dict(value='unknown', event_id=None, evidence=[], provenance=None)
                     for d in lifecycle.DIMENSIONS}
            facts.update((memory.get('scopes', {}).get(token) or {}).get('facts') or {})
            contributions = self.state.get('contributions', {}).get(task_id, [])
            contribution = next((r for r in reversed(contributions) if r.get('kind') == 'contribution'), None)
            try:
                self.service.check_authority(principal, project_id, CAP_COORDINATE)
                coordinate = True
            except HttpError:
                coordinate = False
            ordinary_reason = None if coordinate and scope is not None else (
                'Set a lifecycle scope first' if coordinate else 'Current coordinate permission is required')
            affordances = {d: dict(allowed=ordinary_reason is None, reason=ordinary_reason)
                           for d in lifecycle.DIMENSIONS}
            for d in ('reviewed', 'integrated'):
                affordances[d] = dict(allowed=False, reason=ordinary_reason or
                                     'The disposable backend has no host merge slot')
            affordances['integrated']['passed'] = dict(allowed=False,
                reason='The disposable backend has no host integration mark writer')
            return dict(task=task_id, scope=scope, scope_token=token, facts=facts,
                        contribution=contribution, standing_approval=None,
                        approval_reason='Use the host backend to record reviewed or integrated facts',
                        scope_write=dict(allowed=coordinate, reason=None if coordinate else
                                         'Current coordinate permission is required'),
                        affordances=affordances, mark_writer_enabled=False)

    def integration_check(self, principal, project_id, task_id, target=None):
        """The same four-part read on the disposable backend (kittrial-5bb.374).

        This backend has no host merge slot, approval chain reader or mark family, so
        the checks that need them are refused with that reason rather than invented.
        It writes nothing, exactly as the host read does.
        """
        import lifecycle
        import web_lifecycle
        from requirements import content_hash
        body = dict(operation='check', task=task_id)
        if target is not None:
            body['target'] = target
        try:
            web_lifecycle.validate(body, None, None)
        except web_lifecycle.LifecycleError as error:
            raise invalid(str(error)) from None
        with self.service.store.lock:
            self.service.check_authority(principal, project_id, CAP_READ)
            task = self._task(project_id, task_id)
            memory = task.get('web_lifecycle') or {}
            scope = memory.get('scope')
            token = content_hash(scope) if scope is not None else None
            facts = {d: {'value': 'unknown', 'event_id': None}
                     for d in lifecycle.DIMENSIONS}
            facts.update((memory.get('scopes', {}).get(token) or {}).get('facts') or {})
            contributions = self.state.get('contributions', {}).get(task_id, [])
            record = next((r for r in reversed(contributions)
                           if r.get('kind') == 'contribution'), None)
            contribution = (dict(comment_id=record.get('id'), commit=record.get('commit'),
                                 base_commit=record.get('base_commit'),
                                 author=record.get('actor'), summary=record.get('summary'))
                            if record else None)
            commit = (contribution or {}).get('commit')
            source_allowed = bool(scope is not None and commit
                                  and scope.get('source_commit') == commit)
            checks = [
                dict(check='approval', allowed=False,
                     reason='Use the host backend to record reviewed or integrated facts'),
                dict(check='source_commit', allowed=source_allowed,
                     reason=None if source_allowed else
                     'The full source commit must match the current approved contribution',
                     source_commit=(scope or {}).get('source_commit'), contribution_commit=commit),
                dict(check='slot', allowed=False,
                     reason='The disposable backend has no host merge slot', holder=None, target=None),
                dict(check='hold', allowed=True, reason=None),
                dict(check='reverted', allowed=True, reason=None,
                     integration_commit=(scope or {}).get('integration_commit'), reverted_commits=[]),
            ]
            base = '/v1/projects/%s/tasks/%s' % (project_id, task_id)
            scope_event = memory.get('scope_event_id') if scope is not None else None
            steps = [dict(step='scope', route='POST ' + base + '/lifecycle/scope',
                          state='committed' if scope is not None else 'not-attempted',
                          operation_id=(scope_event[4:] if isinstance(scope_event, str)
                                        and scope_event.startswith('lif_') else None),
                          fact=scope_event)]
            for dimension in ('reviewed', 'integrated'):
                passed = facts[dimension]['value'] == 'passed'
                fact = facts[dimension].get('event_id') if passed else None
                steps.append(dict(step=dimension,
                                  route='POST ' + base + '/lifecycle/facts/' + dimension,
                                  state='committed' if passed else 'not-attempted',
                                  operation_id=(fact[4:] if isinstance(fact, str)
                                                and fact.startswith('lif_') else None),
                                  fact=fact))
            steps.append(dict(step='integrated-mark',
                              route='POST ' + base + '/lifecycle/facts/integrated',
                              automatic=True, state='not-attempted', operation_id=None,
                              fact=None))
            steps.append(dict(step='slot-release',
                              route='POST /v1/projects/%s/merge-slot/release' % project_id,
                              state='not-held', operation_id=None, fact=None,
                              operation_id_note='The server records no identity for a slot release.',
                              fact_note='A slot release has no native fact; the server records only the operation audit.'))
            return dict(task=dict(id=task.get('id'), title=task.get('title'),
                                  status=task.get('status'), assignee=task.get('assignee')),
                        contribution=contribution, review_state=None,
                        target=target, checks=checks,
                        unchecked=[dict(row) for row in
                                   (dict(check='base_membership', known=False,
                                         reason='the server has no code-host access'),
                                    dict(check='tests', known=False,
                                         reason='the server has no code-host access'),
                                    dict(check='pushed_result', known=False,
                                         reason='the server has no code-host access'))],
                        next=steps, merge_ready=all(row['allowed'] for row in checks))

    def _lifecycle_write(self, route, principal, project_id, payload, operation_id):
        import web_lifecycle
        from requirements import content_hash
        self.service.check_authority(principal, project_id, CAP_COORDINATE)
        task = self._task(project_id, payload['task_id'])
        operation = route.split('.', 1)[1]
        body = {key: payload[key] for key in web_lifecycle.FIELDS[operation] | web_lifecycle.OPTIONAL[operation]
                if key in payload}
        body.update(operation=operation, task=payload['task_id'])
        actor = payload['actor']
        try:
            _, native = web_lifecycle.validate(body, actor, operation_id)
        except web_lifecycle.LifecycleError as error:
            raise invalid(str(error)) from None
        memory = task.get('web_lifecycle') or dict(scope=None, scopes={})
        scope = memory['scope']
        token = content_hash(scope) if scope is not None else None
        if operation == 'scope' and payload['expected_scope_token'] != token:
            raise conflict('Lifecycle scope changed; read it again before writing')
        if operation == 'fact' and scope != payload['scope']:
            raise conflict('Lifecycle scope changed; use the current scope')
        if native['dimension'] in ('reviewed', 'integrated'):
            # This validation backend has no host slot or mark family. It cannot
            # simulate an integration that production would refuse or attest it.
            failure = HttpError(503, 'unavailable', 'Reviewed and integrated facts require the host backend, '
                                'its current approved contribution and task-held merge slot')
            failure.nothing_done = True
            raise failure
        token = content_hash(native['scope'])
        record = memory['scopes'].setdefault(token, dict(scope=native['scope'], facts={}))
        event_id = 'lif_' + operation_id
        if operation == 'scope':
            memory['scope'] = native['scope']
            memory['scope_event_id'] = event_id
        else:
            fact = dict(value=native['value'], event_id=event_id, evidence=native['evidence'],
                        provenance=native['provenance'])
            for key in ('note', 'trigger'):
                if key in native:fact[key] = native[key]
            record['facts'][native['dimension']] = fact
        task['web_lifecycle'] = memory
        task['version'] += 1
        self._event(project_id, task['id'], route, principal, actor)
        return dict(event_id=event_id, task=task['id'], dimension=native['dimension'], value=native['value'],
                    operation_id=operation_id, actor=actor, scope_token=token)

    def _dispatch(self, route, principal, project_id, payload):
        handler = {
            'tasks.create': self._task_create,
            'tasks.update': self._task_update,
            'tasks.claim': self._task_claim,
            'tasks.comment': self._task_comment,
            'checkpoints.add': self._checkpoint_add,
            'reviews.add': self._review_add,
            'feedback.add': self._feedback_add,
            'files.record': self._files_write,
            'files.withdraw': self._files_write,
        }.get(route)
        if handler is None:
            raise invalid('Backend route is not implemented for HTTP mapping')
        return handler(principal, project_id, payload)

    # -- task files (kittrial-5bb.370) ---------------------------------------------------------
    def files_project(self, project_id):
        if self.files_root is None:
            return None
        path = Path(self.files_root) / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def files_limits(self):
        return dict(task_files.LIMIT_DEFAULTS)

    def rooms_limits(self, warnings=None):
        return dict(rooms.LIMIT_DEFAULTS)

    def files_discard(self, project_id, sha256):
        # r3 (review discard-race): under the lock every record is written under (invoke holds it around apply), so a
        # record of the same bytes that is being written is finished, and seen, before the blob may be removed.
        project = self.files_project(project_id)
        if project is None:
            return False
        # kittrial-5bb.380 (review of .370 r3): the wait for the lock is bounded; a blob not removed in time stays an
        # orphan, which `admin.py task-files purge` removes once it is a day old.
        if not self.service.store.lock.acquire(timeout=DISCARD_LOCK_SECONDS):
            raise TimeoutError('the store lock was not free within %d s' % DISCARD_LOCK_SECONDS)
        try:
            return task_files.discard(project, sha256)
        finally:
            self.service.store.lock.release()

    def _files_write(self, principal, project_id, payload):
        # The same task_files.apply the endpoint action runs, over this backend's own rows:
        # ``invoke`` holds the store lock and ran the route's authority check first.
        project = self.files_project(project_id)
        if project is None:
            raise not_implemented(FILES_UNAVAILABLE)
        task = self._task(project_id, payload['task_id'])
        actor = payload.get('actor') or principal.actor

        def run(argv):
            if argv[:1] == ['show']:
                return json.dumps(dict(task, issue_type=task.get('issue_type') or 'task'))
            if argv[:2] == ['comments', 'add'] and len(argv) >= 4:
                record = dict(id='comment_' + secrets.token_hex(8), issue_id=task['id'], author=actor,
                              text=argv[3], created_at=now_iso(self.service._now()))
                task.setdefault('comments', []).append(record)
                self._event(project_id, task['id'], 'comment', principal, actor)
                return json.dumps(record)
            raise ValueError('unexpected native command')
        try:
            status, result = task_files.apply(file_body(payload), actor, run, project,
                                              lambda: now_iso(self.service._now()), limit=self.files_limits())
        except task_files.Refusal as refusal:
            raise files_error(refusal) from None
        return dict(result, status=status)

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
        # A null priority is absent, not a value (kittrial-5bb.183 review item 4): the route
        # guard and the endpoint backend already read it that way, and every released client
        # sends null where it has no value, so bd's default 2 applies on both backends.
        priority = payload.get('priority')
        if priority is None:
            priority = 2
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
        for field in ('title', 'description', 'status', 'priority'):
            if field in payload and payload[field] is not None:
                value = payload[field]
                if field == 'title' and (not isinstance(value, str) or not value.strip() or len(value) > 200):
                    raise invalid('Task title must be 1-200 characters')
                if field == 'description' and (not isinstance(value, str) or len(value) > 20000):
                    raise invalid('Task description is too long')
                if field == 'status' and value not in ('open', 'closed'):
                    raise invalid('Task status must be open or closed')
                if field == 'priority' and (type(value) is not int or not 0 <= value <= 4):
                    raise invalid('Task priority must be an integer 0-4')
                task[field] = value.strip() if isinstance(value, str) else value
        task['version'] += 1
        self._event(project_id, task['id'], 'task-updated', principal)
        return dict(task)

    def _task_claim(self, principal, project_id, payload):
        task = self._task(project_id, payload.get('task_id'))
        claimant = payload.get('actor') or principal.actor
        if task['status'] != 'open':
            raise conflict('Task is not open')
        # The claim ROUTE gates the open dependency (kittrial-5bb.293 rev4): the
        # backend does not repeat the check, so removing the route refusal is caught
        # by the route test (mutant g4).
        if task['assignee'] is not None and task['assignee'] == claimant:
            # One's own task, claimed again: answered as a real installation answers it (bd's
            # claim), 200 and nothing changed (kittrial-5bb.187). It was 409 here.
            return dict(task)
        if task['assignee'] is not None:
            raise conflict('Task is already claimed')
        task['assignee'] = claimant
        task['version'] += 1
        self._event(project_id, task['id'], 'task-claimed', principal, task['assignee'])
        return dict(task)

    def _task_comment(self, principal, project_id, payload):
        task = self._task(project_id, payload['task_id'])
        # The disposable backend uses the task's existing native-shaped comments.
        record = dict(id='comment_' + secrets.token_hex(8), issue_id=task['id'],
                      author=principal.actor, text=payload['text'],
                      created_at=now_iso(self.service._now()))
        task.setdefault('comments', []).append(record)
        self._event(project_id, task['id'], 'comment', principal)
        self.state['events'][-1]['detail'] = record['text']
        return record

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
        if 'lane_state' in payload:
            import lane_state
            declared = dict(payload['lane_state']) if isinstance(payload['lane_state'], dict) else payload['lane_state']
            try:
                lane_state.validate(declared, task['id'])
            except ValueError as error:
                raise invalid(str(error)) from None
            record['lane_state'] = dict(declared, checkpoint=record['id'])
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
            repository = payload.get('repository')
            repository = repository.strip() if isinstance(repository, str) else ''
            if isinstance(delivery, dict) and delivery.get('kind') == 'remote':
                delivery_block = {'kind': 'remote', 'remote': delivery['remote'].strip(), 'branch': branch.strip()}
            elif isinstance(delivery, dict) and delivery.get('kind') == 'bundle':
                delivery_block = {'kind': 'bundle', 'path': str(delivery.get('path') or ''), 'sha256': digest}
            elif branch is not None:
                delivery_block = {'kind': 'remote', 'remote': '', 'branch': branch}
            else:
                delivery_block = {'kind': 'bundle', 'path': '', 'sha256': digest}
            record = {'id': 'con_' + secrets.token_hex(6), 'task_id': task['id'],
                      'kind': 'contribution', 'commit': commit, 'base_commit': base_commit,
                      'repository': repository, 'delivery': delivery_block,
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
        actor = payload.get('actor') or principal.actor
        # An exact retry is answered as recorded whatever the Idempotency-Key (kittrial-5bb.164
        # item 1): the canonical writer's operation-id identity, not the transport key, is what
        # says "this is the same recommendation". Same id with other content is refused, as
        # canonically. Checked before every rule below, as ``review_recommendations.execute`` does.
        operation_id = payload.get('operation_id')
        if isinstance(operation_id, str) and operation_id:
            for stored in (self.state.get('recommendations', {}).get(task['id']) or []):
                if stored.get('operation_id') != operation_id:
                    continue
                if stored.get('actor') == actor and stored.get('contribution_id') == payload.get('contribution') \
                        and stored.get('commit') == payload.get('commit') \
                        and stored.get('verdict') == payload.get('verdict') \
                        and stored.get('summary') == summary.strip() and stored.get('items') == items:
                    return {'recommendation': self._recommendation_view(stored), 'reconciled': True,
                            'comment_id': stored['id'], 'task_version': task['version']}
                # The canonical writer's own sentence, and its 422: the operation id is the
                # record's identity, so it may not carry other content.
                raise invalid('Operation ID already used with different payload or actor')
        try:
            plain_text(summary, 'summary')
            for item in items:
                plain_text(item['text'], 'note text')
        except ValueError as error:
            raise invalid(str(error))
        if author_key(actor) in (author_key(contribution['actor']), author_key(task.get('assignee'))):
            raise forbidden('Nobody recommends their own contribution')
        if author_key(actor) in {author_key(record['actor']) for record in self._standing_recommendations(task, records)}:
            raise conflict(rec.ALREADY_RECOMMENDED % actor)
        record = {'id': 'rec_' + secrets.token_hex(6), 'task_id': task['id'], 'kind': 'recommendation',
                  'contribution_id': contribution['id'], 'commit': contribution['commit'],
                  'verdict': payload['verdict'], 'summary': summary.strip(), 'items': items, 'actor': actor,
                  'operation_id': operation_id,
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
        """The recommendation RECORDS that stand for the current contribution, newest first.

        EVERY standing one, not only those a read shows: the write check and the operation-id
        retry of :meth:`_recommend` need the whole list (kittrial-5bb.164), and the readers cut
        it to ``review_recommendations.READ_MAX`` themselves. The records, not views, so a
        caller has each one's ``operation_id`` as well as its author.
        """
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
        return sorted(newest.values(), key=lambda record: (record['after'], record['created_at']), reverse=True)

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

    def references_find(self, project_id, phrase, limit=5):
        return {'schema_version': 1, 'phrase': phrase, 'total': 0, 'items': [],
                'coverage': 'the in-process backend holds no native reference records'}

    def capabilities(self, project_id, options):
        return {'schema_version': 1, 'total': 0, 'items': [], 'next_offset': None,
                'coverage': 'the in-process backend holds no native capability records'}

    def capability(self, project_id, key):
        raise not_found('Capability not found')

    def capabilities_find(self, project_id, phrase, limit=5):
        return {'schema_version': 1, 'phrase': phrase, 'total': 0, 'items': [],
                'coverage': 'the in-process backend holds no native capability records'}

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

    def task_delivery_record(self, project_id, task_id, contribution_id=None):
        task = self._task(project_id, task_id)
        if isinstance(task, dict) and task.get('malformed'):
            raise unreadable_row(task.get('id') or task_id)
        if is_record_anchor(task) or is_merge_slot(task):
            raise not_found('Task not found')
        records = self.state.get('contributions', {}).get(task['id'], [])
        contributions = []
        revision = 0
        for rec in records:
            if not isinstance(rec, dict) or rec.get('kind') != 'contribution':
                continue
            revision += 1
            delivery = rec.get('delivery')
            if not isinstance(delivery, dict):
                if rec.get('branch') is not None:
                    delivery = {
                        'kind': 'remote',
                        'remote': rec.get('remote') or '',
                        'branch': rec['branch'],
                    }
                else:
                    delivery = {
                        'kind': 'bundle',
                        'path': rec.get('bundle_path') or '',
                        'sha256': rec.get('bundle_sha256') or '',
                    }
            contributions.append({
                'project': project_id,
                'task': task['id'],
                'contribution': rec['id'],
                'revision': revision,
                'commit': rec['commit'],
                'base_commit': rec['base_commit'],
                'repository': rec.get('repository') or '',
                'author': rec.get('actor') or '',
                'at': rec.get('created_at') or '',
                'delivery': dict(delivery),
            })
        if not contributions:
            raise not_found('Contribution not found')
        if contribution_id is None:
            return contributions[-1]
        for item in contributions:
            if item['contribution'] == contribution_id:
                return item
        raise not_found('Contribution not found')

    def task_history(self, project_id, task_id, limit, offset, canonical=None,
                     recent=False, since_checkpoint=False):
        """The task's events, oldest first or (``recent``) newest first. This backend has no
        continuation cursor, so only the first page is reachable: the route says so in
        ``truncated`` when more exists (kittrial-5bb.353). ``since_checkpoint`` keeps the
        events after the newest ``checkpoint-added`` (all of them when there is none)."""
        self._task(project_id, task_id)
        events = [e for e in self.state.get('events', [])
                  if e.get('project') == project_id and e.get('task') == task_id]
        checkpoint = None
        if since_checkpoint:
            marks = [i for i, e in enumerate(events) if e.get('action') == 'checkpoint-added']
            if marks:
                checkpoint = events[marks[-1]]
                events = events[marks[-1] + 1:]
        logical = _logical_entries(events)
        if recent:
            logical = logical[::-1]
        selected = [item for group in logical[offset:offset + limit] for item in group]
        result = {'items': selected, 'total': len(logical),
                  'canonical_cursor': None, 'activity_cursor': None}
        if since_checkpoint:
            result.update({'since_checkpoint': checkpoint.get('time') if checkpoint else None,
                           'checkpoint_author': checkpoint.get('actor') if checkpoint else None,
                           'checkpoint_timestamp': checkpoint.get('time') if checkpoint else None,
                           'checkpoint_coverage': None, 'unverified_entries': 0})
        return result

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
        records_standing = self._standing_recommendations(task, records)
        standing = [self._recommendation_view(record) for record in records_standing]
        import review_recommendations as rec
        return {'state': task.get('review_state') or 'none', 'contribution': contribution,
                # Additive (kittrial-5bb.115): advice to approve the current contribution.
                'recommendation': standing[0] if standing else None,
                # Each in full: the brief route leaves out the author's own person and shows the next.
                'recommendations': [dict(r) for r in standing[:rec.READ_MAX]],
                # Additive (kittrial-5bb.164): EVERY standing one's author and operation id,
                # uncapped, for the write check (the readers cut the list above for display).
                'recommendation_keys': [{'author': record['actor'], 'operation_id': record.get('operation_id')}
                                        for record in records_standing],
                'requests': requests,
                'open_requests': sum(1 for r in requests if r['status'] == 'open'),
                'latest_id': records[-1]['id'] if records else None,
                'latest_at': records[-1]['created_at'] if records else None,
                'revisions': len(contributions)}

    def task_brief(self, project_id, task_id):
        """Everything the task page shows, from one read of the in-process state."""
        task = self._task(project_id, task_id)
        from briefing import ContextPointers
        pointer_rows=[row for row in self.state.get('tasks',{}).values() if row.get('project_id')==project_id]
        pointers=ContextPointers(pointer_rows,project_id,operators=())
        checkpoints = self.state.get('checkpoints', {}).get(task_id) or []
        checkpoint = None
        if checkpoints:
            last = checkpoints[-1]
            items = [{'id': item.get('id'),
                      'kind': item.get('kind') or 'item',
                      'text': item.get('text'), 'source': item.get('source')}
                     for item in last.get('open_items') or []]
            checkpoint = {'id': last['id'], 'at': last['created_at'], 'author': last['actor'],
                          'summary': last['summary'], 'next_action': last.get('next_action'),
                          'open_items': items,
                          'pointer_items': pointers.item_pointers(items,'source'),
                          'resolved': [dict(item) for item in last.get('resolved') or []],
                          'resolved_pointer_items': pointers.item_pointers(last.get('resolved') or [],'evidence'),
                          # This backend holds the checkpoint in memory and always reads
                          # every item, so the shown list is the whole list, with no note.
                          'open_items_shown': len(items), 'open_items_total': len(items)}
        return {'task': dict(task), 'checkpoint': checkpoint, 'review': self._review_view(task),
                'carry_open_items': [dict(item) for item in checkpoints[-1].get('open_items') or []
                                     if isinstance(item, dict)] if checkpoints else [],
                # The disposable backend records no lifecycle evidence, so every fact is
                # honestly unknown rather than inferred from the review state.
                'lifecycle': {}, 'depends_on': [],
                # This backend keeps no activity cursor; its checkpoints need none.
                'activity_cursor': None,
                'newer': self._agent_activity_summary(task, checkpoints[-1]) if checkpoints else None,
                **self._activity_blocks(task, checkpoints[-1] if checkpoints else None)}

    def _activity_blocks(self, task, checkpoint):
        """The task read's ``activity`` and ``changes_since_checkpoint`` (kittrial-5bb.354).

        ``activity`` is the very block this backend's agent rows carry
        (:meth:`_agent_fields`), so the task read and next/wait cannot disagree. This
        model keeps no per-entry digests and no tracker history, so the section lists the
        events after the checkpoint event (coverage unknown) and says the task fields are
        not known here; the canonical backend reads both from the tracker.
        """
        from briefing import changes_since_checkpoint
        import field_history as field_versions
        if checkpoint is None:
            return {'activity': None,
                    'changes_since_checkpoint': changes_since_checkpoint(
                        {'task': task['id'], 'entries': []}, None, None, None)}
        review = self._review_view(task)
        fields = self._agent_fields(task, review)
        activity = {name: fields.get(name) for name in (
            'newer_activity', 'newer_activity_marker', 'newer_activity_fields',
            'wait_newer_activity_marker',
            'newer_activity_by_others', 'newer_activity_own',
            'newer_activity_coverage', 'unresolved_directions')}
        events = [e for e in self.state.get('events') or [] if e.get('task') == task['id']]
        anchors = [i for i, e in enumerate(events) if e.get('action') == 'checkpoint-added'
                   and e.get('actor') == checkpoint.get('actor')
                   and e.get('time', '') >= checkpoint.get('created_at', '')]
        after = events[anchors[-1] + 1:] if anchors else []
        entries = [{'entry_id': '%s-e%d' % (task['id'], index), 'kind': e.get('action') or 'event',
                    'author': e.get('actor') or 'unknown', 'timestamp': e.get('time'),
                    'body': e.get('detail') or e.get('action') or ''}
                   for index, e in enumerate(after, start=(anchors[-1] + 1) if anchors else 0)]
        section = changes_since_checkpoint(
            {'task': task['id'], 'entries': entries}, None,
            {'comment_id': checkpoint.get('id'), 'author': checkpoint.get('actor'),
             'timestamp': checkpoint.get('created_at')},
            task.get('assignee') or checkpoint.get('actor'), current=bool(anchors) and not entries,
            fields_note=field_versions.UNKNOWN_NOTE % 'this backend keeps no tracker history')
        return {'activity': activity, 'changes_since_checkpoint': section}

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
        from claim_limit import actor_holds
        rows = []
        for task in self._project_rows(project_id):
            if not task.get('assignee') or (actor is not None and not actor_holds(task.get('assignee'), actor)):
                continue
            review = self._review_view(task)
            from review_state import active_work
            if not active_work(task.get('status'), review['state'], review['open_requests']):
                continue
            rows.append({'id': task['id'], 'title': task.get('title'), 'status': task.get('status'),
                         'assignee': task.get('assignee'), 'review_state': review['state'],
                         'contribution_id': (review['contribution'] or {}).get('id'),
                         # Open requested-change ITEMS (the canonical `pending_review_items`),
                         # so the move sentence's N matches the canonical `work` row.
                         'pending_review_items': review['open_requests'],
                         **self._agent_fields(task, review)})
        return {'tasks': rows, 'complete': True}

    def _agent_fields(self, task, review):
        saved=(self.state.get('checkpoints') or {}).get(task['id'])
        last=saved[-1] if isinstance(saved,list) and saved and isinstance(saved[-1],dict) else None
        unreadable=saved is not None and (not isinstance(saved,list) or
                   (bool(saved) and (last is None or not isinstance(last.get('open_items'),list))))
        at=last.get('created_at') if last and not unreadable else None
        # The superseding-redelivery fact of the canonical row (kittrial-5bb.253 item 1), so
        # the next-actions answer can hand it to `review_move` as the `work` row does.
        open_requests=[r for r in review['requests'] if r['status']=='open']
        contribution_id=(review['contribution'] or {}).get('id')
        # The one rule from work.py, not a copy (kittrial-5bb.264 item 4).
        from work import is_redelivery
        from briefing import MARKER_DIGEST, fields_digest
        redelivered=is_redelivery(contribution_id, open_requests)
        # This disposable model keeps no per-entry checkpoint digests, so its coverage
        # is unknown and the stable marker is honestly null (kittrial-5bb.351); the
        # counts are the same summary its brief route already answers with.
        activity=self._agent_activity_summary(task,last) if last and not unreadable else None
        return {'assignee_comment_pending':self._assignee_comment_pending(task) if not unreadable else False,
                'pending_change_requests':[r['id'] for r in review['requests'] if r['status']=='open'][:20],
                'open_items':None if unreadable else (len(last.get('open_items') or []) if last else 0),
                'blocking_items':None if unreadable else sum(item.get('kind') in ('blocker','dependency')
                                                            for item in (last.get('open_items') or []) if isinstance(item,dict)) if last else 0,
                'redelivered':redelivered,
                'checkpoint_at':at,
                'newer_activity':self._other_agent_activity(task,last) if last and not unreadable else None,
                'newer_activity_marker':None,
                # The current title/description/acceptance digest (kittrial-5bb.367 F2b):
                # this model carries the fields even though it records no per-entry digests.
                'newer_activity_fields':fields_digest(task)[:MARKER_DIGEST] if last and not unreadable else None,
                # The checkpoint-independent wait marker (kittrial-5bb.369): the newest
                # entry by anybody but the assignee, folded with the fields digest. This
                # model's events carry their author, so the own checkpoint never moves it.
                # Computed without a checkpoint too, so the first one is invisible to a wait.
                'wait_newer_activity_marker':self._wait_activity_marker(task),
                'newer_activity_by_others':(activity or {}).get('other_count'),
                'newer_activity_own':(activity or {}).get('own_count'),
                'newer_activity_coverage':(activity or {}).get('coverage'),
                'unresolved_directions':None}

    def _assignee_comment_pending(self, task):
        from briefing import pending_other_comments, same_activity_actor, comment_author_allowed
        from review_workflow import EXTRA
        owner = task.get('assignee')
        if task.get('status') == 'closed' or not owner:
            return False
        entries = []
        acknowledged = None
        for index, event in enumerate(self.state.get('events') or []):
            if event.get('task') != task['id']:
                continue
            action = event.get('action')
            if action not in EXTRA and action not in ('comment', 'contribution', 'checkpoint-added'):
                continue
            identity = str(event.get('entry_id') or '%s-e%d' % (task['id'], index))
            entries.append(dict(entry_id=identity, kind='comment' if action == 'comment' else 'event',
                                body=event.get('detail'), author=event.get('actor')))
            if action in ('checkpoint-added', 'respond') and same_activity_actor(event.get('actor'), owner):
                acknowledged = identity
        return pending_other_comments(entries, owner, acknowledged=acknowledged,
            authority=lambda author: comment_author_allowed(author, operators=(), authority=self.service.state,
                                                           project=task.get('project_id')))

    def _wait_activity_marker(self, task):
        """The stable wait marker of one in-process task (kittrial-5bb.369).

        The same definition the canonical snapshot uses: the newest event by anybody
        other than the assignee, folded with the current fields digest by
        :func:`briefing.activity_marker`. This model has no per-entry checkpoint
        digests, but its events name their actor, so the agent's own checkpoint event
        is its own entry and never moves the marker.
        """
        from briefing import activity_marker, fields_digest, same_activity_actor
        events = [(index, event) for index, event in enumerate(self.state.get('events') or [])
                  if isinstance(event, dict) and event.get('task') == task['id']]
        others = [(index, event) for index, event in events
                  if not same_activity_actor(event.get('actor'),task.get('assignee'))]
        newest = max(others, key=lambda pair: (pair[1].get('time') or '', pair[0])) if others else None
        entry = None
        if newest is not None:
            entry = dict(newest[1])
            entry['entry_id'] = str(newest[1].get('entry_id') or '%s-e%d' % (task['id'], newest[0]))
        return activity_marker(entry, fields_digest(task))

    def _other_agent_activity(self, task, checkpoint):
        from briefing import same_activity_actor
        events=[e for e in self.state.get('events') or [] if e.get('task')==task['id']]
        # Real writes have an ordered checkpoint event. Do not count that event
        # itself, even when an owner wrote the checkpoint for the assignee.
        anchors=[i for i,e in enumerate(events) if e.get('action')=='checkpoint-added'
                 and e.get('actor')==checkpoint.get('actor')
                 and e.get('time','')>=checkpoint.get('created_at','')]
        if anchors:
            return any(not same_activity_actor(e.get('actor'),task.get('assignee')) for e in events[anchors[-1]+1:])
        # Older or directly imported in-process checkpoints have no event anchor.
        # Only attributed review records can establish external activity there.
        at=checkpoint.get('created_at')
        return bool(at and any(r.get('created_at','')>at and not same_activity_actor(r.get('actor'),task.get('assignee'))
                               for r in self.state.get('contributions',{}).get(task['id'],[])))

    def _agent_activity_summary(self, task, checkpoint):
        """Bounded event references for the disposable backend, without digest claims.

        The native backend supplies its canonical summary instead. This model has
        no per-entry checkpoint digests, so imported/evicted history stays unknown.
        Reading this projection never incorporates or acknowledges an event.
        """
        from briefing import NEWER_MAX, clip, same_activity_actor
        events = [e for e in self.state.get('events') or [] if e.get('task') == task['id']]
        anchors = [i for i, e in enumerate(events) if e.get('action') == 'checkpoint-added'
                   and e.get('actor') == checkpoint.get('actor')
                   and e.get('time', '') >= checkpoint.get('created_at', '')]
        if not anchors:
            return {'coverage': 'unknown', 'own_count': None, 'other_count': None,
                    'entries': [], 'omitted': None,
                    'note': 'Checkpoint event history is unavailable; read task history.'}
        remaining = events[anchors[-1] + 1:]
        others = [e for e in remaining if not same_activity_actor(e.get('actor'),task.get('assignee'))]
        authors = sorted({e.get('actor') for e in others if e.get('actor')})
        return {'coverage': 'unknown', 'own_count': len(remaining) - len(others),
                'other_count': len(others),
                'other_authors': {'items': [clip(a, 96) for a in authors[:NEWER_MAX]],
                                  'omitted': max(0, len(authors) - NEWER_MAX)},
                'entries': [{'kind': e.get('action'), 'timestamp': e.get('time'),
                             'author': clip(e.get('actor') or '', 96)} for e in remaining[:NEWER_MAX]],
                'omitted': max(0, len(remaining) - NEWER_MAX),
                'note': 'Event history has no per-entry digests. Reading clears nothing; reconcile with task history.'}

    def lanes_read(self, project_id, principal, args, actor, scope='own'):
        """One page of the lanes board over this backend's own rows (kittrial-5bb.263).

        The board's aging and its three flags live in ``lanes.py`` only, so this backend
        feeds that ONE read with its own rows and computes neither itself. It keeps no
        session registry, so every lane is keyed by its assignee actor -- exactly how the
        read treats a task that has no registered lane -- and an actor's lane therefore
        carries the same name and id.
        """
        from lanes import read as lanes_read
        rows = [dict(task, issue_type=task.get('issue_type') or 'task',
                     labels=task.get('labels') or [], dependencies=task.get('dependencies') or [],
                     comments=task.get('comments') or [])
                for task in self._project_rows(project_id)]
        return lanes_read(rows, actor, {'schema_version': 1, 'records': {}}, list(args),
                          journal=None, scope=scope)

    def coordinator_wait(self, project_id, principal, idle_minutes):
        """The disposable model's queue and board at one locked state."""
        import waiting
        from lanes import LANES_LIMIT_MAX
        from work import move_decision
        with self.service.store.lock:
            actor = self.service.bind_actor(principal, None)
            rows = []
            for item in self.review_queue(project_id)['items']:
                row = waiting.normalize_item(dict(item, task=item['id']))
                row['whose_move'], _text, row['next_actor'] = move_decision(
                    row['review_state'], row['task'], claim_limit.actor_holds(row['owner'], actor),
                    pending_items=row['pending_review_items'], redelivered=bool(row['redelivered']),
                    assignee=row['owner'], closed=row['status'] == 'closed', claimable=item['claimable'],
                    comment_pending=attention_field(item, 'assignee_comment_pending') is True)
                rows.append(row)
            return waiting.coordinator_view(
                rows,
                self.lanes_read(project_id, principal, ['--idle-minutes', str(idle_minutes),
                                            '--limit', str(LANES_LIMIT_MAX)], actor, scope='all'))

    def review_queue(self, project_id):
        """Every task with current work, highest-attention review states first.

        Mirrors ``work.queue``: a closed task stays listed only while its review is
        still active. The project is read once; the route pages the result.
        """
        items = []
        snapshot = self.read_tasks(project_id)['items']
        from work import dependency_index, open_dependency_ids
        dep_index = dependency_index(snapshot)
        for task in snapshot:
            task = dict(task, open_dependencies=open_dependency_ids(task, snapshot, index=dep_index))
            review = self._review_view(task)
            from review_state import active_work
            if not active_work(task.get('status'), review['state'], review['open_requests']):
                continue
            items.append(queue_item(
                project_id, task, review['state'], review['contribution'],
                review['open_requests'],
                pending_request_ids=[r['id'] for r in review['requests'] if r['status'] == 'open'],
                waiting_since=review['latest_at'],
                attention=self._agent_fields(task, review),
                recommended_by=[r['author'] for r in review['recommendations']],
                contribution_author=(review['contribution'] or {}).get('author'),
                claimable=claimable_task(task, review,
                                          claiming=self.claiming_state(project_id)['claiming'])))
        items.sort(key=queue_order)
        return {'items': items, 'complete': True, 'warnings': []}


#: Review-state filter vocabulary, including open tasks awaiting integration.
#: Closed-task eligibility comes from review_state.active_work, not this vocabulary.
#: ``approved`` is the in-process name for ``awaiting-integration``.
#: ``withdrawn``/``superseded`` are additive (kittrial-5bb.94): the canonical ``work``
#: queue lists such a task while a blocking item is still open, so the HTTP queue and
#: its ``state=`` filter accept them too or the two transports disagree
#: (kittrial-5bb.110 item 9).
ACTIVE_REVIEW_STATES = ('changes-requested', 'error', 'awaiting-review', 'legacy-review-ready',
                        'awaiting-integration', 'approved', 'withdrawn', 'superseded', 'held')
QUEUE_PRIORITY = {'changes-requested': 0, 'error': 1, 'awaiting-review': 2,
                  'legacy-review-ready': 2, 'awaiting-integration': 3, 'approved': 3,
                  'held': 3}

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


def refuse_ambiguous_query(raw_query, names):
    """Refuse a blank or repeated query value instead of quietly taking the first.

    ``_dispatch`` builds ``ctx.query`` with ``keep_blank_values=False`` and keeps only
    ``v[0]``, so a handler cannot tell ``?form=`` from no form at all, or the first
    ``?idle_minutes=15`` from two of them. The routes that read these names re-parse the
    raw query with ``keep_blank_values=True`` and refuse a malformed value here, before
    reading it (kittrial-5bb.389; the same reading as ``project_guidance_status``).
    A parameter that is absent is left alone: the route's own default still applies.
    """
    pairs = parse_qs(raw_query, keep_blank_values=True)
    for name in names:
        values = pairs.get(name)
        if values is None:
            continue
        if len(values) != 1:
            raise HttpError(400, 'invalid_request', 'Use %s only once' % name)
        if values[0] == '':
            raise HttpError(400, 'invalid_request', '%s must not be blank' % name)


def refuse_unknown_fields(payload, allowed, where, takes=None):
    """Refuse a body field the route does not take, in the task-change shape (kittrial-5bb.183).

    Several write routes accepted a request with a field they do not take and dropped the
    field silently (task create, claim, checkpoint, member, worker credential, agent and
    account). Every route that takes a caller body now refuses such a field with 422,
    naming it and the fields the route does take, exactly as a task change has since
    kittrial-5bb.181. ``where`` names the route in a sentence, ``allowed`` is the set it
    accepts and ``takes`` the (usually equal) list the refusal names -- a task change takes
    ``version`` and ``actor`` as transport fields but does not name them as its fields.
    The caller runs this after authorization and before the effect, so nothing is reserved
    and the answer is never 5xx or "may have committed".
    """
    if not isinstance(payload, dict):
        raise invalid('%s must be a JSON object' % where)
    named = tuple(allowed if takes is None else takes)
    unknown = sorted(str(name) for name in set(payload) - set(allowed))
    if not unknown:
        return
    # The names in the detail are cut as the sentence cuts them (review of kittrial-5bb.181, done
    # for a task change in kittrial-5bb.185 and here for every route that uses this): a name is
    # given back only when it is a plain identifier, never a 300-character or a made-up one whole.
    raise invalid('%s does not take: %s. %s takes: %s'
                  % (where, unsupported_fields_text(unknown), where, ', '.join(named)),
                  {'unsupported': [name if UNSUPPORTED_FIELD_NAME.match(name) else '<non-identifier name>'
                                   for name in unknown[:UNSUPPORTED_FIELDS_SHOWN]],
                   'takes': list(named)})


def queue_order(item):
    # Among contributions that await review, one a reviewer recommends approving comes
    # first: it is the one an owner can act on at once (kittrial-5bb.115).
    return (QUEUE_PRIORITY.get(item['review_state'], 4), 0 if item.get('recommended') else 1, str(item['id']))


def queue_item(project_id, task, review_state, contribution, open_requests,
               pending_request_ids=None, waiting_since=None, integration=None,
               integration_warnings=None, attention=None, recommended_by=None, contribution_author=None,
               claimable=None, followon=None):
    """One review-queue row, in the shape both backends return.

    ``pending_request_ids`` and ``waiting_since`` (time of the latest review record)
    are filled where the backend knows them; the canonical ``work`` projection
    reports only a count of pending items, so there they stay empty/``None`` here and
    ``GET /v1/me/work`` fills a bounded number of rows from ``task_detail``.

    ``integration`` and ``integration_warnings`` are additive (kittrial-5bb.52):
    the canonical backend passes the ``work`` row's integration block and its
    any-pass-wins disagreement warnings through; the disposable backend records no
    lifecycle evidence and leaves them ``None``/empty.

    ``claimable`` is additive (kittrial-5bb.6): the one bounded interpretation of
    claimable work (``work.claimable``), so agent attention, My work and the claim
    fall-through offer exactly the rows the canonical ``work --claimable`` view
    offers. Advisory, never a reservation. ``None`` means the row's endpoint did not
    state it (a staged upgrade or rollback): the older "open and unassigned" reading
    is kept, so such an endpoint loses no suggestion it used to offer.
    """
    if claimable is None:
        claimable = task.get('status') == 'open' and task.get('assignee') is None
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
            'claimable': bool(claimable),
            # What agent attention needs from the canonical ``work`` row (kittrial-5bb.114):
            # the request-changes record ids, and the latest checkpoint's open items,
            # time and whether anything is newer. None where the backend does not say.
            'attention': attention,
            # Additive (kittrial-5bb.343): the newest recorded hand-back block from the
            # canonical `work` row, so a web queue row says why the move is the
            # assignee's (or why an inert record does not move it).
            'followon': followon,
            # Additive (kittrial-5bb.115): reviewers who recommend approving the current
            # contribution while nobody has decided. Never an approval.
            'recommended': bool(recommended_by),
            'recommended_by': list(recommended_by or []),
            # Additive (kittrial-5bb.115 review): who delivered the current contribution, so
            # the person rule can be applied to a row as it is to a brief.
            'contribution_author': contribution_author}


def claimable_task(task, review, claiming='open'):
    """One in-process task's bounded claimability, from the shared ``work.claimable``.

    The disposable backend knows each row's issue type directly; the canonical backend
    reads the ``claimable`` field the ``work`` projection already computed, so both
    answer the same question (kittrial-5bb.6 deliverable b).
    """
    from work import claimable
    contribution = review.get('contribution') if isinstance(review, dict) else None
    return claimable(task.get('issue_type'), task.get('status'), task.get('assignee'),
                     (contribution or {}).get('id'),
                     review.get('state') if isinstance(review, dict) else None,
                     labels=task.get('labels'),
                     open_dependencies=task.get('open_dependencies') or None,
                     claiming=claiming)


def attention_blocking_items(fields):
    """Validate blocking count; older endpoints supplied only open_items.

    An explicitly unreadable blocking count stays unknown. Only omission uses
    the older endpoint's open-item policy.
    """
    value = fields.get('blocking_items', fields.get('open_items', 0))
    return value if type(value) is int and value >= 0 else None


def attention_field(row, name):
    """One attention field from a queue row itself, else from its nested attention block.

    A canonical ``work`` row carries the checkpoint attention fields at the top level;
    the web service's queue row carries them under ``attention``. The wait route reads
    either shape, so a bridge and the SSH client see one marker (kittrial-5bb.351).
    """
    if isinstance(row, dict):
        value = row.get(name)
        if value is not None:
            return value
        attention = row.get('attention')
        if isinstance(attention, dict):
            return attention.get(name)
    return None


def holder_move(status, task_id, assignee, review_state, open_requests, contribution_id,
                redelivered=None, claimable_value=None, comment_pending=False):
    """The HOLDER's move reading for a projection row, from the ONE shared decision.

    A page shows a task's move for the actor who holds it, and the work row that names
    the same reading is ``work --owner ACTOR``. This calls the same
    ``work.move_decision`` the queue and the lane board use with the holder as the
    subject, so an open task assigned to the holder with no checkpoint and no
    contribution by them reads ``you``/``assignee`` on the page exactly as it does in
    the queue (kittrial-5bb.288) and the two readings cannot drift apart.
    """
    from work import move_decision
    pending = open_requests if type(open_requests) is int and open_requests >= 0 else 0
    return move_decision(review_state, task_id, True, pending_items=pending,
                         redelivered=bool(redelivered), assignee=assignee,
                         closed=status == 'closed', claimable=claimable_value, comment_pending=comment_pending)


def own_queue_tasks(queue, actor=None):
    """Split one current review snapshot by assignee, retaining unknown checkpoint state."""
    from claim_limit import actor_holds
    tasks=[]
    for row in queue.get('items') or []:
        if not isinstance(row,dict) or not row.get('assignee') or (actor is not None and not actor_holds(row.get('assignee'), actor)):
            continue
        fields=row.get('attention') if isinstance(row.get('attention'),dict) else {}
        move, move_text, move_role = holder_move(
            row.get('status'), row.get('id'), row.get('assignee'), row.get('review_state'),
            row.get('open_requests'), (row.get('contribution') or {}).get('id'),
            fields.get('redelivered'), row.get('claimable'), fields.get('assignee_comment_pending') is True)
        tasks.append({'id':row.get('id'),'title':row.get('title'),'status':row.get('status'),
                      'assignee':row.get('assignee'),'review_state':row.get('review_state'),
                      'contribution_id':(row.get('contribution') or {}).get('id'),
                      # The holder's own move reading, from the same decision as the
                      # canonical `work --owner ACTOR` row (kittrial-5bb.288), so the
                      # agents page and the bridge show what the queue row shows.
                      'whose_move':move,'next':move_text,'next_actor':move_role,
                      'assignee_comment_pending':fields.get('assignee_comment_pending') is True,
                      'pending_change_requests':list(fields.get('pending_change_requests') or [])[:20],
                      # The canonical item count, so the move sentence says the same N as the
                      # `work` row it came from (kittrial-5bb.249).
                      'pending_review_items':row.get('open_requests'),
                      'open_items':fields.get('open_items',0),
                      'blocking_items':attention_blocking_items(fields),
                      # The superseding-redelivery fact the canonical row carries
                      # (kittrial-5bb.253 item 1), for `review_move`.
                      'redelivered':fields.get('redelivered'),
                      'checkpoint_at':fields.get('checkpoint_at'),
                      'newer_activity':fields.get('newer_activity'),
                      # The stable marker and its counts (kittrial-5bb.351): a wait row
                      # carries them, so a marker change alone moves the fingerprint.
                      'newer_activity_marker':fields.get('newer_activity_marker'),
                      'newer_activity_fields':fields.get('newer_activity_fields'),
                      # The checkpoint-independent wait marker (kittrial-5bb.369).
                      'wait_newer_activity_marker':fields.get('wait_newer_activity_marker'),
                      'newer_activity_by_others':fields.get('newer_activity_by_others'),
                      'newer_activity_own':fields.get('newer_activity_own'),
                      'newer_activity_coverage':fields.get('newer_activity_coverage'),
                      'unresolved_directions':fields.get('unresolved_directions')})
        if row.get('lifecycle_reasons'):
            tasks[-1]['lifecycle_reasons'] = dict(row['lifecycle_reasons'])
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

    ROUTES = InProcessBackend.ROUTES + (
        'tasks.assign', 'tasks.release', 'merge-slot.acquire',
        'merge-slot.release', 'merge-slot.force-release', 'merge-slot.create',
        # The release routes (kittrial-5bb.378): one canonical release-query or one
        # release-deploy group per request. The endpoint holds the host tracker.
        'release.query', 'release.deploy',
        # The coordinator's recorded hand-back (kittrial-5bb.343). Both operations run
        # the one canonical `assignee-followon` action, which calls the same library
        # function as `coordinator followon`.
        'followons.record', 'followons.withdraw',
        # Files attached to a task (kittrial-5bb.370): the canonical `task-file` action.
        'files.record', 'files.withdraw',
        'references.propose', 'references.revise', 'references.accept', 'references.retire',
        'capabilities.propose', 'capabilities.revise', 'capabilities.accept', 'capabilities.retire',
        'capabilities.verify', 'capabilities.alias.propose', 'capabilities.alias.reject',
    )
    #: Routes with no canonical command yet, and why. Fails closed rather than
    #: pretending the service store is canonical.
    UNRESOLVED = {
        'feedback.add': 'the canonical feedback command ships with kittrial-5bb.13',
    }
    READ_UNRESOLVED = {
        'list_feedback': 'the canonical feedback command ships with kittrial-5bb.13',
    }
    PROPOSALS = True
    RECORDS = True
    #: The endpoint owns the project's tracker rows and reads them when asked to
    #: (kittrial-5bb.188 item 1): a credential issued through this backend records that the
    #: name was verified row-free, so its ordinary writes need no such read.
    ACTOR_ROWS = True

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

    def files_project(self, project_id):
        # The project's own directory (kittrial-5bb.370), the place every other project-owned
        # file of this service is read from (claiming, guidance). The name's shape and a
        # symlinked project directory are refused by admin.project_dir.
        import admin
        try:
            path = admin.project_dir(Path(self.root), project_id)
        except ValueError:
            raise not_found('Project not found') from None
        if not path.is_dir():
            raise not_found('Project not found')
        return path

    def files_limits(self, warnings=None):
        return task_files.limits(Path(self.root), warnings=warnings)

    def rooms_limits(self, warnings=None):
        return rooms.limits(Path(self.root), warnings=warnings)

    def files_discard(self, project_id, sha256):
        # r3 (review discard-race): the endpoint's task-file action reads the blob, writes the comment and the journal
        # entry while it holds the project's .coordination.lock EXCLUSIVELY; this takes the same lock, so another
        # upload's record of the same bytes is either finished (and its entry keeps the blob) or not yet begun (and it
        # then finds the blob gone and answers "upload it again"). Never a record whose bytes vanished under it.
        # kittrial-5bb.380 (review of .370 r3): the wait is bounded (DISCARD_LOCK_SECONDS); a blob not removed in time
        # stays an orphan, which `admin.py task-files purge` removes once it is a day old.
        import fcntl
        project = self.files_project(project_id)
        with (project / '.coordination.lock').open('a') as lock:
            ends = time.monotonic() + DISCARD_LOCK_SECONDS
            locked = False
            for _ in range(int(DISCARD_LOCK_SECONDS / 0.05) + 2):   # a bounded number of tries, never an open loop
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= ends:
                        break
                    time.sleep(0.05)
            if not locked:
                raise TimeoutError('the project lock was not free within %d s' % DISCARD_LOCK_SECONDS)
            return task_files.discard(project, sha256)

    def assignment_roster(self, principal, project_id, offset=0, limit=200):
        return self._run('assignment-roster', project_id, self._actor(principal),
                         [json.dumps(dict(offset=offset,limit=limit))],
                         authority=authority_request(principal, project_id, CAP_COORDINATE,
                                                     now=self.service._expiry_now()),
                         require_authority=True)

    def merge_slot_read(self, principal, project_id):
        return self._run('web-merge-slot', project_id, self._actor(principal),
                         [json.dumps({'operation': 'check'})],
                         authority=authority_request(principal, project_id, CAP_READ,
                                                     now=self.service._expiry_now()),
                         require_authority=True)

    def lifecycle_read(self, principal, project_id, task_id):
        return self._run('web-lifecycle', project_id, self._actor(principal),
                         [json.dumps(dict(operation='read', task=task_id))],
                         authority=authority_request(principal, project_id, CAP_READ,
                                                     now=self.service._expiry_now()),
                         require_authority=True)

    def integration_check(self, principal, project_id, task_id, target=None):
        """The read-only merge check (kittrial-5bb.374). One read, no identity, no write."""
        body = dict(operation='check', task=task_id)
        if target is not None:
            body['target'] = target
        return self._run('web-lifecycle', project_id, self._actor(principal),
                         [json.dumps(body)],
                         authority=authority_request(principal, project_id, CAP_READ,
                                                     now=self.service._expiry_now()),
                         require_authority=True)

    def lifecycle_replay_guard(self, principal, project_id, task_id, dimension):
        state = self.lifecycle_read(principal, project_id, task_id)
        sentence = (state.get('hold_refusals') or {}).get(dimension)
        if sentence:
            raise conflict(sentence)

    # -- transport -------------------------------------------------------------
    # A project's tasks live in a canonical Beads project that only an operator creates on
    # the host (admin.py add-project), so the HTTP route only REGISTERS an existing one
    # (kittrial-5bb.80). The HTTP project id is the canonical project name.
    PROJECT_CREATE = 'register'

    def actor_standing(self, project_id, names, rows=False, own=()):
        """For each name: why a worker credential may not write under it, or None (kittrial-5bb.184).

        One read through the endpoint, which owns the session registry, the operator and
        verifier lists and (when ``rows`` asks) the project's tracker rows, and answers with the
        rule, never with the names. ``rows`` is one ``bd export --all``: the issue route asks for
        it, the credential listing does not (kittrial-5bb.188 item 1). ``own`` are the lifetimes
        of earlier credentials of the same name whose rows are not held against the one being
        issued (item 3). This service's own namespace is judged here, because only the service
        knows what it was started with.
        """
        import actor_names
        names = [name for name in names if isinstance(name, str) and name]
        if not names:
            return {}
        # ``rows`` is True (every row) or the instant the credential was issued (the rows older
        # than it); both travel as the endpoint's ``tracker`` value, which owns the distinction.
        extra = {'tracker': rows} if rows else None
        if rows and own:
            extra['own'] = [list(window) for window in own]
        answer = self._run('actor-standing', project_id, self.actor_namespace + '/read', names,
                           extra=extra)
        found = answer.get('names') if isinstance(answer, dict) else None
        if not isinstance(found, dict):
            raise uncertain('The host did not say which names are taken')
        return {name: found.get(name) or actor_names.collision(name, service=(self.actor_namespace, actor_names.SERVICE_NAMESPACE))
                for name in names}

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
            said = (reply.get('stderr') or '').strip().split('\n')[-1]
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
                  authority=None, require_authority=False, route=None, check_usable=True, timeout=None,
                  extra=None):
        """One answer of the canonical endpoint; its mark of a configuration fault is raised here.

        Here and not where a route reads the answer, so that no route can hand the
        endpoint's line (the path of the file, the parser's or the system's words) to the
        person: some routes read a return code of 2 their own way (kittrial-5bb.156 review).
        ``extra`` adds one named field to the request the service sends (kittrial-5bb.188:
        ``tracker`` asks the host to read the project's rows for this answer).
        """
        reply = self._ask(action, project, actor, args, attachments, operation_id, authority,
                          require_authority, route, check_usable, timeout, extra)
        if isinstance(reply, dict) and reply.get('returncode') == 2 and reply.get('fault') == 'configuration':
            # The server's own configuration file cannot be read: the endpoint's line names the
            # file and the parser's words. Those are for the operator, in this service's log;
            # the person is told who to ask. Nothing was carried out.
            said = (reply.get('stderr') or '').strip().split('\n')
            print('configuration: the endpoint could not read the deployment configuration for %s: %s'
                  % (action or 'a request', ascii(said[-1][:600]) if said else '(nothing)'), file=sys.stderr, flush=True)
            refused = HttpError(503, 'server_configuration', self.CONFIGURATION_UNREADABLE)
            refused.nothing_done = True
            raise refused
        return reply

    def _ask(self, action, project, actor, args, attachments, operation_id, authority,
             require_authority, route, check_usable, timeout, extra=None):
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
        if extra:
            payload.update({key: value for key, value in extra.items() if value is not None})
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
                '--authority-lock', self.authority_lock,
                '--service-namespace', self.actor_namespace]
        if require_authority:
            argv.append('--require-authority')
        if action == 'owner-requirements':
            argv.extend(['--terminal-endpoint-timeout', str(timeout or self.timeout)])
        try:
            if action == 'owner-requirements':
                import endpoint_process
                completed = endpoint_process.run(argv, input=json.dumps(payload),
                                                 timeout=timeout or self.timeout)
            else:
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
             authority=None, require_authority=False, route=None, extra=None):
        reply = self._endpoint(action, project, actor, args, attachments,
                               operation_id=operation_id, authority=authority,
                               require_authority=require_authority, route=route, extra=extra)
        if (action in ('assignment-roster','task-assign','task-release') and isinstance(reply, dict)
                and reply.get('returncode') == 2 and reply.get('assignment_status') in (403, 404, 409, 422, 503)):
            status = reply['assignment_status']
            error = HttpError(status, 'unavailable' if status == 503 else 'forbidden' if status == 403
                              else 'not_found' if status == 404 else 'conflict' if status == 409 else 'invalid',
                              (reply.get('stderr') or 'Assignment roster cannot be read').strip())
            error.nothing_done = True
            raise error
        if action == 'web-merge-slot' and isinstance(reply, dict) and reply.get('merge_slot_status'):
            status = reply['merge_slot_status']
            error = HttpError(status, 'conflict' if status == 409 else 'invalid' if status == 422 else
                              'forbidden' if status == 403 else 'unavailable',
                              (reply.get('stderr') or 'Merge slot cannot be read').strip())
            error.nothing_done = True
            raise error
        if action == 'web-lifecycle' and isinstance(reply, dict) and reply.get('lifecycle_status'):
            status = reply['lifecycle_status']
            error = HttpError(status, {403: 'forbidden', 404: 'not_found', 409: 'conflict',
                                      422: 'invalid'}.get(status, 'unavailable'),
                              (reply.get('stderr') or 'Lifecycle state cannot be read').strip())
            error.nothing_done = True
            raise error
        if action == 'task-file' and isinstance(reply, dict) and reply.get('returncode') == 2 and \
                isinstance(reply.get('task_file_status') or reply.get('records_status'), int):
            # The action's own refusal (kittrial-5bb.370): nothing was written, and the status is
            # the one task_files gave it.
            status = reply.get('task_file_status') or reply.get('records_status')
            text = (reply.get('stderr') or 'Task file operation refused').strip().split('\n')[-1]
            if text.startswith('ValueError: '):
                text = text[len('ValueError: '):]
            error = HttpError(status, FILE_CODES.get(status, 'invalid'), text)
            error.nothing_done = True
            raise error
        if action == 'web-release' and isinstance(reply, dict) and (
                reply.get('release_status') or reply.get('records_status')):
            # The release routes (kittrial-5bb.378): a live-authority refusal, a hold, a
            # stale target or a release-writer refusal keeps its own status instead of
            # collapsing into 422. Both keys are read: an identity check reports the
            # shared ledger key, the route's own errors report release_status.
            status = reply.get('release_status') or reply.get('records_status')
            error = HttpError(status, {403: 'forbidden', 404: 'not_found', 409: 'conflict',
                                      422: 'invalid', 503: 'unavailable'}.get(status, 'unavailable'),
                              (reply.get('stderr') or 'Release facts cannot be recorded').strip())
            error.nothing_done = True
            raise error
        if action in ('ref', 'capability') and isinstance(reply, dict) and reply.get('records_status'):
            status = reply['records_status']
            text = (reply.get('stderr') or 'Record operation refused').strip().split('\n')[-1]
            for prefix in ('ValueError: ', 'RecordsRefusal: '):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
            error = HttpError(status, {403: 'forbidden', 404: 'not_found', 409: 'conflict',
                                      422: 'invalid'}.get(status, 'unavailable'),
                              text)
            error.nothing_done = True
            raise error
        # Without a descriptor the service only reads (every write it sends carries one).
        return self._checked(reply, action, reading=authority is None)

    #: How much of a canonical refusal's last line is handed on. A checkpoint refusal lists
    #: every problem with the record (kittrial-5bb.113), so it gets room for all of them.
    DETAIL_LIMIT = 200
    DETAIL_LIMITS = {'checkpoint': 6000, 'ref': 600, 'capability': 600}
    #: The follow-on base refusal is handed on whole (kittrial-5bb.158): cut at 200 it lost the
    #: reason, the recorder's name and what an operator must do.
    BASE_REFUSAL_LIMIT = 1500
    #: A refusal by one of the coordinator rules is handed on whole too (kittrial-5bb.197 review
    #: request 01a12894, item 2): it must name the setting and the way to turn it off.
    RULE_REFUSAL_LIMIT = 1500

    @classmethod
    def _detail_limit(cls, action, said, refused=None):
        """How much of a canonical refusal's last line is handed on.

        The larger limit is for the kit's own sentence only, never for a caller's text echoed
        back at length: the line must BE the follow-on base refusal, whole, as
        ``review_workflow.BASE_REFUSAL`` describes it (the kit's words, hexadecimal commit
        ids, a recorder name of a constrained shape), or the endpoint marks it as a
        coordinator-rule refusal. Those are fixed kit text naming the setting. Anything else keeps the limit
        it had.
        """
        if action == 'review' and cls.base_refusal(said) is not None:
            return cls.BASE_REFUSAL_LIMIT            # the longest form of the sentence is well below it
        if refused == 'coordinator-rule':
            return cls.RULE_REFUSAL_LIMIT
        return cls.DETAIL_LIMITS.get(action, cls.DETAIL_LIMIT)

    @staticmethod
    def base_refusal(said):
        """The follow-on base refusal in ``said`` (a canonical refusal line), or None when it is not one, whole."""
        from review_workflow import BASE_REFUSAL
        text = said[len('ValueError: '):] if isinstance(said, str) and said.startswith('ValueError: ') else None
        return text if text is not None and BASE_REFUSAL.fullmatch(text) else None

    @classmethod
    def _checked(cls, reply, action=None, reading=False):
        """The payload of one canonical reply, or the HttpError its return code means.

        ``reading`` says that the request could not have written: its failure is then never
        "the outcome may be unknown" (kittrial-5bb.185: a change of a task that does not exist
        began with a read of it, bd said it found none, and the caller was told that the change
        may have been made).
        """
        code = reply.get('returncode') if isinstance(reply, dict) else None
        if code == 0 and isinstance(reply.get('server_time'), str):
            # The endpoint's time of the write it carried out, for the body of the answer of
            # the request this thread is serving (kittrial-5bb.97).
            WRITTEN.at = reply['server_time']
        elif code == 0 and reply.get('replayed') is True:
            # A stored answer from before the time was kept: no time is known, and the
            # service's clock now would be the time of the retry.
            WRITTEN.at = False
        stderr = (reply.get('stderr') or '') if isinstance(reply, dict) else ''
        stdout = (reply.get('stdout') or '') if isinstance(reply, dict) else ''
        if isinstance(reply, dict) and reply.get('fault') == 'tracker':
            # The endpoint could not read the project's tracker: a nonzero bd exit, a line cut
            # short, text that is not rows, or a project whose metadata records no server
            # coordinates (kittrial-5bb.188 item 1; kittrial-5bb.202 rev-3 item 2). The endpoint
            # marks it a host fault, and the `reading` path above already decides what such a
            # failure means: 503, nothing was changed, the key stays free. Never 422 "the
            # request was rejected".
            raise cls._unread()
        if isinstance(reply, dict) and reply.get('fault') == 'unreadable-rows':
            # The export was read but holds a row that cannot be parsed: for the
            # worker-credential name rule the whole tracker is then not read, because the
            # unreadable row may be the row that holds the name (kittrial-5bb.221 r2). Not
            # transient either: an operator must repair the row, so the caller gets the
            # endpoint's own sentence naming the row ids and the repair, its own code, and
            # never UNREAD's "try again shortly" (kittrial-5bb.243 item N7).
            raise cls._rows_unreadable(reply.get('unreadable_rows'), reply.get('unreadable_total'))
        if isinstance(reply, dict) and reply.get('fault') == 'merge-slot':
            # The tracker was read but carries no merge-slot row: rows that came back without
            # it, or a readable tracker with no rows at all, the plain `bd init` shape
            # (kittrial-5bb.202 item 1 and rev-2's empty-project decision). It is not the
            # transient tracker fault: trying again never helps and only an operator's
            # merge-create puts the row back, so the caller gets that sentence and its own
            # code, never UNREAD's "try again shortly".
            raise cls._merge_slot_missing()
        if code == 126:
            # The endpoint re-validated live authority immediately before the effect
            # and refused it. Nothing was written.
            detail = stderr.strip().split('\n')[-1][:200] if stderr.strip() else None
            if (reply.get('authority_status') or 403) == 401:
                raise unauthenticated(detail or 'Authentication is no longer valid')
            raise forbidden(detail or 'Authority was revoked before the canonical write')
        if code == 124:
            if reading:
                raise cls._unread()
            # 124 is "the identity is reserved and the outcome is unknown" (run_guarded):
            # a real timeout of the endpoint or of bd, OR any other exception that followed
            # a possible write (kittrial-5bb.173 item 3). Only the first is a timeout; the
            # rest get their own sentence so a person is not told the command timed out.
            if 'Command timed out' in stderr or '(TimeoutExpired:' in stderr:
                # The first is endpoint.py's own timeout (outside run_guarded); the second is a
                # bd timeout caught inside run_guarded and reported as Effect raised after the
                # reservation (TimeoutExpired: ...) -- still a timeout (review 01a1288a).
                raise uncertain('Canonical command timed out; outcome may be unknown')
            if 'Connection timed out' in stderr:
                # A socket timeout after the reservation (F2): the same class as a timed-out
                # command, not a failure whose cause is unknown.
                raise uncertain('Canonical command timed out; outcome may be unknown')
            if 'is committed' in stderr or 'committed but' in stderr:
                # The write WAS committed; only its response was not kept (F1). The reader
                # must reload, not wonder whether anything happened.
                raise uncertain('Canonical command was committed; reload to see its result')
            if 'no effect was attempted' in stderr:
                # Journal full before any effect: nothing was reserved and nothing was tried.
                raise uncertain('Canonical command could not be reserved; no effect was attempted')
            if 'identity reserved' in stderr:
                # A repeat of a reserved key (F1): the outcome of the first attempt is
                # unknown, not a failure of this one after it began.
                raise uncertain('Canonical command was already reserved; reconcile state before retrying')
            if not stderr.strip():
                # An empty stderr (F1) is not a known failure after a reservation.
                raise uncertain('Canonical command returned no explanation; outcome may be unknown')
            raise uncertain('Canonical command failed after it was reserved; outcome may be unknown')
        if code == 75:
            # The endpoint was occupied (a wait for a lock ran out): the request may simply be
            # sent again. Its line names the lock file it waited for, a path of the host, so it
            # goes to this service's log for the operator and the person gets the service's own
            # sentence (kittrial-5bb.149).
            cls._log_busy(action, stderr)
            raise busy()
        if code:
            said = stderr.strip().split('\n')[-1] if stderr.strip() else None
            detail = said[:cls._detail_limit(action, said, reply.get('refused'))] if said else None
            if code == 2:
                if reply.get('refused') == 'coordinator-rule' and isinstance(detail, str) \
                        and detail.startswith('ValueError: '):
                    raise invalid(detail[len('ValueError: '):], detail)
                if action in ('ref', 'capability') and isinstance(detail, str):
                    clean = detail
                    for prefix in ('ValueError: ', 'RecordsRefusal: '):
                        if clean.startswith(prefix):
                            clean = clean[len(prefix):]
                            break
                    if (clean.startswith(('Unknown reference key ', 'Unknown capability key '))
                            or 'no revision record yet' in clean):
                        raise not_found(clean)
                    if any(token in clean for token in (
                            'already exists', 'is not the newest', 'does not match',
                            'is retired', 'already accepted', 'different result',
                            'collides with', 'has no revision', 'No pending alias',
                            'already carries', 'already matches', 'was rejected for',
                            'has no proposed draft')):
                        raise conflict(clean)
                    raise invalid(clean, detail)
                if reply.get('refused') == 'not-found':
                    # bd found no row of that name (bd_refusals): the thing asked about is not there.
                    raise not_found('Task not found')
                if reply.get('refused') == bd_refusals.CLAIMED:
                    # bd's claim found the task in somebody else's hands. The route names the
                    # holder as people know them; here it is the label the tracker has.
                    held_by = bd_refusals.holder(detail)
                    refusal = conflict('Task is already claimed', {'held_by': held_by})
                    refusal.held_by = held_by             # None when the holder is not an actor label: nobody is named
                    raise refusal
                if reply.get('refused') == bd_refusals.NOT_CLAIMABLE:
                    state = bd_refusals.status(detail)
                    raise conflict('Task is not open%s' % (' (it is %s)' % state.replace('_', ' ') if state else ''),
                                   {'status': state})
                raise invalid('Canonical command rejected the request', detail)
            if reading:
                raise cls._unread()
            raise uncertain('Canonical command failed; outcome may be unknown')
        return _canonical_payload(stdout)

    #: Said when a read of the tracker failed: nothing was changed, and nothing is to reconcile.
    UNREAD = 'The tracker could not be read just now. Nothing was changed; try again shortly.'

    @classmethod
    def _unread(cls):
        failure = HttpError(503, 'unavailable', cls.UNREAD)
        failure.nothing_done = True
        return failure

    #: Said when the tracker was read but carries no merge-slot row: rows without it, or no rows
    #: at all (kittrial-5bb.202 item 1; rev-3 item 3(c) corrected "rows came back but").
    #: Not transient: the row will not appear by retrying, and only an operator's merge-create
    #: puts it back. Its own code, so a caller can tell it from UNREAD.
    MERGE_SLOT_MISSING = ("The project's tracker holds no merge slot row, so it was not read as a whole "
                          "tracker. An operator must run the merge-create operation, which creates the "
                          "slot, then try again.")

    @classmethod
    def _merge_slot_missing(cls):
        failure = HttpError(503, 'merge_slot_missing', cls.MERGE_SLOT_MISSING)
        failure.nothing_done = True
        return failure

    #: Said when the tracker was read but holds a row that cannot be parsed: for the
    #: worker-credential name rule the whole tracker is then not read (the unreadable row
    #: may be the row that holds the name; kittrial-5bb.221 r2). Not transient: an operator
    #: must repair the row. Its own code, so a caller can tell it from UNREAD. The endpoint's
    #: own sentence, which names the row ids, is preferred over this fixed one
    #: (kittrial-5bb.243 item N7).
    ROWS_UNREADABLE = ("The project's tracker holds a row that cannot be read, so it was not read as a whole "
                       "tracker: the unreadable row may be the row that holds a name. An operator must repair "
                       "the row (for deeply nested metadata: bd update ID --unset-metadata KEY) if a second "
                       "try gives the same answer, then try again.")

    @classmethod
    def _rows_unreadable(cls, ids=None, total=None):
        # The answer is built from the fault's own fields, never from a stderr tail a row
        # could shape (r2 review item 1): the endpoint sends the ids it named, already
        # bounded to the tracker's id shape, and the total it counted, and the sentence is
        # composed here, naming the ids and then 'and K more' as the endpoint's sentence
        # does (r3 review item 2).
        import actor_names
        named = [i for i in (ids or ()) if isinstance(i, str) and actor_names.ROW_ID.fullmatch(i)][:actor_names.NAMED_ROWS_MAX]
        count = total if isinstance(total, int) and total >= 0 else (len(named) if named else None)
        failure = HttpError(503, 'unreadable_rows', cls.ROWS_UNREADABLE)
        if named:
            more = '' if count is None or count <= len(named) else ', and %d more' % (count - len(named))
            failure = HttpError(503, 'unreadable_rows', cls.ROWS_UNREADABLE.replace(
                'holds a row that cannot be read',
                'holds unreadable row(s) %s%s' % (', '.join(named), more), 1))
        failure.nothing_done = True
        failure.unreadable_rows = named
        failure.unreadable_total = count
        return failure

    #: Said when the endpoint reports that the server's configuration file cannot be read.
    CONFIGURATION_UNREADABLE = ("The server's configuration cannot be read, so this request was not carried out. "
                                'Ask an operator of the server to look.')

    @staticmethod
    def _log_busy(action, stderr):
        """Which lock, and how long: the endpoint's own line, for the operator, in the service log."""
        said = (stderr or '').strip().split('\n')
        print('busy: the endpoint answered return code 75 for %s: %s'
              % (action or 'a request', ascii(said[-1][:400]) if said else '(nothing)'), file=sys.stderr, flush=True)

    def _settle_actor_rows(self, principal, project_id):
        """Judge an unmarked credential's name by the tracker once and keep the outcome.

        Kittrial-5bb.188 item 5. A credential issued before the row rule has no
        ``actor_rows_checked`` mark, so every one of its writes would read the whole tracker
        (measured at 3,000 rows: 3.8-4.1 s against 0.6 s). At its first write through this
        backend the name is judged against the rows older than its issuance, exactly as the
        endpoint judges it, and the outcome is kept ON THE CREDENTIAL: ``actor_rows_checked``
        when the name is free, or ``actor_rows_refused`` (the rule word) when it is held. Every
        later write reads no tracker, and a refused one is refused without one.

        The keeping is not reachable from a request: there is no credential-update route, the
        issue route accepts no such field, and this method is the only writer of the two marks.
        It runs from the service, which owns the state, never from the endpoint (the endpoint
        only reads the state file; a write there would be lost by the next service save).
        A concurrent first write may run the export twice and both answer the same way; the
        mark written by either is the same, so the result is idempotent. A name a cheap rule (a
        session, an operator, a verifier, the service's namespace, a look-alike) already refuses
        is left unmarked: the endpoint decides it every write with no tracker read, and a mark
        would wrongly say the row rule had been judged.
        """
        if getattr(principal, 'via', None) != 'credential':
            return
        import actor_names
        credential_id = getattr(principal, 'credential_id', None)
        with self.service.store.lock:
            credential = self.service.state['credentials'].get(credential_id)
            if not isinstance(credential, dict) or credential.get('actor_rows_checked') \
                    or credential.get('actor_waived') or credential.get('actor_rows_refused'):
                return
            namespace = credential.get('actor')
            if not isinstance(namespace, str) or not namespace:
                return                          # writes under its issuer's own account id
            issued = credential.get('created_at')
            rows = issued if isinstance(issued, str) and issued else True
            own = actor_names.own_intervals(self.service.state.get('credentials') or {}, namespace,
                                            credential.get('user_id'), credential.get('project_id'),
                                            exclude=credential_id)
        if self.actor_standing(project_id, [namespace]).get(namespace) is not None:
            return                              # a cheap rule decides it every write; no row read
        reason = self.actor_standing(project_id, [namespace], rows=rows, own=own).get(namespace)
        with self.service.store.lock:
            credential = self.service.state['credentials'].get(credential_id)
            if not isinstance(credential, dict) or credential.get('actor_rows_checked') \
                    or credential.get('actor_waived') or credential.get('actor_rows_refused'):
                return                          # another first write settled it meanwhile
            if reason is None:
                credential['actor_rows_checked'] = True
            elif reason == actor_names.ROWS:
                credential['actor_rows_refused'] = actor_names.ROWS
            else:
                return                          # a cheap rule again: leave it unmarked
            self.service.store.save()

    # -- mutations -------------------------------------------------------------
    def invoke(self, route, principal, project_id, payload, key, target=None, authorize=None,
               capability=None):
        if route not in self.ROUTES:
            raise ValueError('Unknown canonical route %r' % (route,))
        if route in self.UNRESOLVED:
            raise not_implemented('Canonical backend cannot perform %s: %s'
                                  % (route, self.UNRESOLVED[route]))
        result_key = self._result_key(principal, project_id, route, key, target) if key else None
        cached = None
        with self.service.store.lock:
            if authorize is not None:
                authorize()
            if result_key is not None and self.service.has_result(result_key):
                cached = (self.service.result_get(result_key), self.service.result_written_at(result_key))
        if cached is not None:
            if route == 'lifecycle.fact' and payload.get('dimension') in ('reviewed','integrated'):
                self.lifecycle_replay_guard(principal, project_id, payload['task_id'], payload['dimension'])
            elif route == 'merge-slot.acquire':
                self.lifecycle_replay_guard(principal, project_id, payload['task_id'], 'merge-acquire')
            # Keep the original time and response after the current native hold check.
            WRITTEN.at = cached[1] or False
            return cached[0]
        # The row rule for a credential issued before it is settled ONCE, here, before the
        # write is sent: the credential then carries the outcome and no later write reads
        # the tracker (kittrial-5bb.188 item 5).
        self._settle_actor_rows(principal, project_id)
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
            if self.service.approval_by_another_party and (
                    route.startswith('references.') or route.startswith('capabilities.')):
                authority['approval_by_another_party'] = True
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
                # With the endpoint's time of the write (``_checked`` left it for this thread).
                self.service.result_put(result_key, result, written_at=getattr(WRITTEN, 'at', None))
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
        # Verifier attestations and integration holds (kittrial-5bb.10) are deliberately
        # ABSENT: they are host-only in this kit (item 6), so the web route refuses them
        # with its own sentence in `_command` rather than mapping them to a capability.
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
                         'open_items', 'resolved', 'lane_state')

    def _command(self, route, principal, project_id, payload, operation_id=None):
        """Build the canonical action and argv for one authorized mutation.

        The argv uses the endpoint's ``@attachment:`` transport (never a raw
        ``--body-file``/``--file`` flag, which ``endpoint.execute`` rejects) and the
        review/checkpoint bodies carry exactly the fields the canonical validators
        require — including ``schema_version`` and ``operation_id`` — and no
        operation-inappropriate nulls.
        """
        if route == 'tasks.release':
            # The canonical release action derives the holder from the authenticated
            # caller; the route sends the task, the reason and the row's current assignee
            # when held under a sub-label (kittrial-5bb.301, kittrial-5bb.300).
            body = {'task_id': payload['task_id'], 'reason': payload['reason']}
            if payload.get('expected_assignee') is not None:
                body['expected_assignee'] = payload['expected_assignee']
            return 'task-release', project_id, [json.dumps(body, ensure_ascii=False)], {}
        if route == 'tasks.assign':
            from task_assignment import REQUEST_FIELDS
            fields={k:payload[k] for k in REQUEST_FIELDS if k not in ('operation_id','task','schema_version')}
            fields.update(schema_version=1,operation_id=operation_id,task=payload['task_id'])
            return 'task-assign', project_id, [json.dumps(fields,ensure_ascii=False)], {}
        if route.startswith('lifecycle.'):
            import web_lifecycle
            operation = route.split('.', 1)[1]
            fields = {key: payload[key] for key in web_lifecycle.FIELDS[operation] | web_lifecycle.OPTIONAL[operation]
                      if key in payload}
            fields.update(operation=operation, task=payload['task_id'])
            return 'web-lifecycle', project_id, [json.dumps(fields)], {}
        if route.startswith('files.'):
            # A task file's record or withdrawal (kittrial-5bb.370). The bytes are stored and
            # checked already; the action reads the row live and writes under the project lock.
            return 'task-file', project_id, [json.dumps(file_body(payload), ensure_ascii=False)], {}
        if route.startswith('release.'):
            # The release routes (kittrial-5bb.378): one body carrying the unchanged
            # host field set; the endpoint supplies the actor and the operation id from
            # the request envelope, exactly as the lifecycle routes do.
            import web_release
            operation = route.split('.', 1)[1]
            body = {'operation': operation}
            if operation == 'query':
                body['scope'] = payload['scope']
            else:
                for key in web_release.DEPLOY_FIELDS | web_release.DEPLOY_OPTIONAL:
                    if key in payload:
                        body[key] = payload[key]
            return 'web-release', project_id, [json.dumps(body, ensure_ascii=False)], {}
        if route.startswith('followons.'):
            # The coordinator's recorded hand-back (kittrial-5bb.343): the canonical
            # `assignee-followon` action derives every record fact under the project
            # lock, so only the operation, the caller's text/reason and the route's
            # own task/target travel.
            operation = route.split('.', 1)[1]
            body = {'schema_version': 1, 'operation': operation, 'task': payload['task_id']}
            if operation == 'record':
                body['text'] = payload['text']
            else:
                body['target'] = payload['target']
                body['reason'] = payload['reason']
            return 'assignee-followon', project_id, [json.dumps(body, ensure_ascii=False)], {}
        if route.startswith('merge-slot.'):

            operation = route.split('.', 1)[1]
            fields = {'operation': operation}
            if operation == 'acquire':fields.update(task=payload['task_id'], target=payload['target'])
            if operation == 'force-release':
                fields.update(expected_holder=payload['expected_holder'], reason=payload['reason'])
                if 'expected_task' in payload:fields['expected_task'] = payload['expected_task']
            return 'web-merge-slot', project_id, [json.dumps(fields)], {}
        task_id = payload.get('task_id')
        if route == 'tasks.create':
            title = str(payload.get('title') or '')
            if title.startswith(('-', '@')):
                # The title is a positional argument of the native create: a leading dash
                # would be read as a flag, a leading @ as the attachment transport.
                raise invalid('A task title cannot start with "-" or "@"')
            args = ['create', title, '--json']
            if payload.get('priority') is not None:
                # The priority the caller chose is taken for real (kittrial-5bb.183 item 1):
                # it used to be dropped here and the task was made with bd's default. bd's
                # own default still applies when the caller sends none.
                args[-1:-1] = ['--priority', str(payload['priority'])]
            description = payload.get('description')
            if description and str(description).strip():
                # A blank description is no description (as PATCH treats it as "clear"):
                # the endpoint refuses an empty body file.
                args[-1:-1] = ['@attachment:0']
                return ('bd', project_id, args,
                        {'0': {'flag': '--body-file', 'text': description}})
            return 'bd', project_id, args, {}
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
            if payload.get('priority') is not None:
                # Priority is a change like any other (kittrial-5bb.183 item 1). Kept beside
                # --json, as every other flag, so the native parser reads it as a flag.
                args[-1:-1] = ['--priority', str(payload['priority'])]
            if len(args) == 3:
                # Nothing to change: bd answers `update ID --json` with the words "No updates
                # specified" and exit 0, which is not an answer this service can read, so it
                # was taken for a write that may have been made (kittrial-5bb.181). The route
                # refuses such a body with the fields it takes; this is the same refusal for
                # any other caller of the backend. Nothing is sent.
                raise invalid('Nothing to change. A task change takes: title, description, '
                              'status, priority')
            return 'bd', project_id, args, attachments
        if route == 'tasks.claim':
            # bd's own claim, for the actor the request runs under (kittrial-5bb.187). It checks
            # and writes in one step: a row that is somebody else's or is not open is refused,
            # and of several claims of one free row exactly one is carried out. The plain update
            # of status and assignee this sent before took the task from whoever held it and
            # reopened a closed one.
            return 'bd', project_id, ['update', str(task_id), '--claim', '--json'], {}
        if route == 'tasks.comment':
            # Literal text, including leading flags and attachment-like strings.
            return ('bd', project_id,
                    ['comments', 'add', str(task_id), '@attachment:0', '--json'],
                    {'0': {'flag': '--file', 'text': payload['text']}})
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
            if not isinstance(operation, str):
                raise invalid('Unsupported review operation; expected one of: %s'
                              % ', '.join(sorted(self.REVIEW_FIELDS)))
            if operation in ('attest', 'hold', 'release-hold'):
                # Verifier attestations and integration holds are HOST-ONLY in this kit
                # (kittrial-5bb.10 item 6). The verifiers and operators lists are host
                # authority and this route cannot judge them, so it refuses these three
                # with a sentence instead of mapping them to a capability any caller with
                # reviews.write would then hold.
                raise invalid('The review operation %s is host-only in this kit: a listed verifier records an '
                              'attestation with `admin.py attest-delivery` on the coordination host, and a '
                              'listed operator sets or releases an integration hold with the host review '
                              'transport. It is not accepted over the web route.' % operation)
            if operation not in self.REVIEW_FIELDS:
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
        if route.startswith('references.') or route.startswith('capabilities.'):
            body = dict(payload['body'])
            body['operation_id'] = body.get('operation_id') or operation_id
            if not body['operation_id']:
                raise invalid('Send an Idempotency-Key header or an operation_id')
            if isinstance(body.get('acceptance'), dict) and not body['acceptance'].get('decision_id'):
                body['acceptance'] = dict(body['acceptance'], decision_id=body['operation_id'])
            action_name = 'ref' if route.startswith('references.') else 'capability'
            return (action_name, project_id, [payload['command'], '@attachment:0'],
                    {'0': {'flag': '--file', 'text': json.dumps(body, ensure_ascii=False)}})
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
        if options.get('waiting'):
            args.append('--waiting')
        return self._ref_read(project_id, args)

    def reference(self, project_id, key):
        """One entry through `ref get`; an unknown or unfinished key is a 404."""
        return self._ref_read(project_id, ['get', caller_arg(key, 'key')], missing=True)

    def references_find(self, project_id, phrase, limit=5):
        return self._ref_read(project_id, ['find', caller_arg(phrase, 'q'), '--limit', str(limit)])

    def capabilities(self, project_id, options):
        """One page of the capability catalog through the endpoint's read-only `capability list`."""
        args = ['list', '--limit', str(options['limit']), '--offset', str(options['offset'])]
        for tag in options.get('tags') or []:
            args += ['--tag', caller_arg(tag, 'tag')]
        for name in ('owner', 'state'):
            if options.get(name):
                args += ['--' + name, caller_arg(options[name], name)]
        if options.get('pointers'):
            args.append('--pointers')
        if options.get('waiting'):
            args.append('--waiting')
        return self._capability_read(project_id, args)

    def capability(self, project_id, key):
        """One entry through `capability get`; an unknown or unfinished key is a 404."""
        return self._capability_read(project_id, ['get', caller_arg(key, 'key')], missing=True)

    def capabilities_find(self, project_id, phrase, limit=5):
        return self._capability_read(project_id, ['find', caller_arg(phrase, 'q'), '--limit', str(limit)])

    def lanes_read(self, project_id, principal, args, actor, scope):
        """One page of the lanes board through the endpoint's ``work lanes`` action.

        ``args`` are the read's own arguments -- ``--idle-minutes``, ``--limit``,
        ``--offset`` -- handed on unchanged, so the threshold the page asked for is the
        threshold the ONE read of the board ages and flags by (kittrial-5bb.263; there is no
        second implementation of either on this side). ``scope`` is the ROUTE's isolation,
        not the transport's: this service calls the endpoint without a bound key, so the
        endpoint would answer ``'all'`` for it, and the request's ``lanes_scope`` asks for
        ``'own'`` instead. The endpoint honours only that narrowing, and its own routes keep
        computing their scope themselves.
        """
        reply = self._endpoint('work', project_id, actor, ['lanes'] + list(args),
                               authority=authority_request(principal, project_id, CAP_READ,
                                                           now=self.service._expiry_now()),
                               require_authority=True, extra={'lanes_scope': scope})
        payload = self._checked(reply, reading=True)
        if not isinstance(payload, dict):
            raise uncertain('The canonical lanes read returned an unexpected shape')
        return payload

    def wake_project_dir(self, project_id):
        """The project directory the empty-wake host log lives under (kittrial-5bb.397).

        It is the SAME project directory claiming, guidance and the lane board already
        use, and ``admin.project_dir`` refuses a symlinked one. A project without a
        directory (or an unreadable root) records nothing rather than failing a wait.
        """
        import admin
        try:
            path = admin.project_dir(Path(self.root), project_id)
        except ValueError:
            return None
        return path if path.is_dir() else None

    def coordinator_wait(self, project_id, principal, idle_minutes):
        """One canonical wait read: queue and lanes share the endpoint's export."""
        reply = self._endpoint('wait', project_id, self.service.bind_actor(principal, None),
                               ['--coordinator', '--idle-minutes', str(idle_minutes)],
                               authority=authority_request(principal, project_id, CAP_COORDINATE,
                                                           now=self.service._expiry_now()),
                               extra={'lanes_scope': 'all'})
        payload = self._checked(reply, reading=True)
        if not isinstance(payload, dict) or payload.get('form') != 'coordinator':
            raise uncertain('The canonical coordinator wait returned an unexpected shape')
        return payload

    def requirements_read(self, project_id, args):
        reply=self._endpoint('requirements',project_id,self.actor_namespace+'/read',args)
        if isinstance(reply,dict) and reply.get('returncode')==2 and 'Requirement was not found' in (reply.get('stderr') or ''):
            raise not_found('Requirement not found')
        return self._checked(reply, reading=True)

    def requirement_operator_audits(self, project_id):
        from requirement_terminal import operator_audits
        return operator_audits(Path(self.root)/'projects'/project_id, project_id)

    def owner_requirements(self, principal, project_id, action, fields, key, task=None):
        operation_id=self._result_key(principal,project_id,'requirements.'+action,key,task)
        # `coordinate`: an owner holds it, a coordinator holds it by role, and an agent
        # credential holds it only through the project's coordinator grant
        # (kittrial-5bb.278). The endpoint re-decides the same capability.
        authority=authority_request(principal,project_id,CAP_COORDINATE,now=self.service._expiry_now())
        args=[action]+([task] if task is not None else [])
        attachment={'payload':{'flag':'--file','text':json.dumps(fields,ensure_ascii=False)}}
        reply=self._endpoint('owner-requirements',project_id,principal.user_id,args,attachment,
                             operation_id=operation_id,authority=authority,require_authority=True,
                             route='requirements.'+action)
        if isinstance(reply,dict) and reply.get('returncode')==2:
            detail=reply.get('stderr') or ''
            if ('Requirements changed. Reload and try again.' in detail
                    or 'Operation identity reused with a different request' in detail):
                raise conflict('Requirements changed. Reload and try again.')
        return self._checked(reply)

    def owner_requirement_recoveries(self, principal, project_id):
        authority=authority_request(principal,project_id,CAP_COORDINATE,now=self.service._expiry_now())
        reply=self._endpoint('owner-requirements',project_id,principal.user_id,['recoveries'],
                             authority=authority,require_authority=True,route='requirements.recoveries')
        return self._checked(reply,reading=True)

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
        payload = self._checked(reply, 'ref', reading=True)
        if not isinstance(payload, dict):
            raise uncertain('Canonical reference read returned an unexpected shape')
        return payload

    def _capability_read(self, project_id, args, missing=False):
        reply = self._endpoint('capability', project_id, self.actor_namespace + '/read', args)
        code = reply.get('returncode') if isinstance(reply, dict) else None
        stderr = (reply.get('stderr') or '') if isinstance(reply, dict) else ''
        if code == 2 and 'Unknown action' in stderr:
            raise not_implemented('The canonical endpoint is older than this HTTP service and has '
                                  'no capability catalog; install the same kit for both')
        if code == 2 and missing and ('Unknown capability key' in stderr
                                      or 'no revision record yet' in stderr):
            raise not_found('Capability not found')
        payload = self._checked(reply, 'capability', reading=True)
        if not isinstance(payload, dict):
            raise uncertain('Canonical capability read returned an unexpected shape')
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

    def _read_deployment_setting(self, fn):
        from admin import ConfigurationUnreadable
        try:
            return fn(Path(self.root))
        except ConfigurationUnreadable as exc:
            print(
                'configuration: the service could not read the deployment configuration: %s'
                % ascii(str(exc)[:600]),
                file=sys.stderr,
                flush=True,
            )
            refused = HttpError(503, 'server_configuration', self.CONFIGURATION_UNREADABLE)
            refused.nothing_done = True
            raise refused from None

    def get_delivery_roots(self):
        from admin import delivery_roots
        return self._read_deployment_setting(delivery_roots)

    def get_delivery_serve_max_bytes(self):
        from admin import delivery_serve_max_bytes
        return self._read_deployment_setting(delivery_serve_max_bytes)

    def task_delivery_record(self, project_id, task_id, contribution_id=None):
        reply = self._endpoint(
            'delivery-record',
            project_id,
            self.actor_namespace + '/read',
            [str(task_id)],
        )
        code = reply.get('returncode') if isinstance(reply, dict) else None
        stderr = (reply.get('stderr') or '') if isinstance(reply, dict) else ''
        if code == 2 and 'Unknown action' in stderr:
            raise not_implemented(
                'The canonical endpoint is older than this HTTP service and has no delivery '
                'record read; install the same kit for both'
            )
        payload = self._checked(reply, 'delivery-record', reading=True)
        if not isinstance(payload, dict):
            raise uncertain('Canonical delivery record read returned an unexpected shape')
        if not payload.get('found'):
            raise not_found('Task not found')
        if payload.get('malformed'):
            raise unreadable_row(payload.get('id') or task_id)
        if payload.get('project') not in (None, project_id) or payload.get('task') not in (None, task_id):
            raise not_found('Task not found')
        contributions = [c for c in (payload.get('contributions') or []) if isinstance(c, dict)]
        if not contributions:
            raise not_found('Contribution not found')
        if contribution_id is None:
            return contributions[-1]
        for item in contributions:
            if item.get('contribution') == contribution_id:
                return item
        raise not_found('Contribution not found')

    def task_history(self, project_id, task_id, limit, offset, canonical=None,
                     recent=False, since_checkpoint=False):
        """Page the canonical snapshot cursor, never asking for more than its limit.

        ``recent`` reads the same snapshot newest first (the canonical ``--recent``, whose
        cursor walks toward older entries and keeps a long comment's fragments together in
        body order); ``since_checkpoint`` passes ``--since-checkpoint``, whose boundary the
        canonical cursor carries (kittrial-5bb.353).

        The canonical ``history`` action accepts 1..20 entries per call and binds its
        continuation cursor to a snapshot digest. Accumulating exact-size canonical
        pages keeps the HTTP offset slice correct for any page size (the previous
        ``--limit offset+limit`` form was rejected for every page above 20).

        A canonical continuation cursor already points AT the first entry the previous
        page did not return, so the entries collected from it begin at the absolute
        ``offset`` the cursor carries: the page is the FIRST ``limit`` of them. Slicing
        them with the absolute offset again dropped a whole page (kittrial-5bb.273: a
        five-comment task at ``limit=2`` returned entries 1-2 and then entry 5 only,
        never 3 and 4).

        A long comment is several consecutive entries: they share ``entry_id`` and carry
        ``body_offset`` (the fragment's character offset in the whole body) and
        ``continued`` (more of the same body follows). The canonical page boundary can
        fall at a different character than a whole read does; concatenating the fragments
        in page order reconstructs the body exactly, whatever page size a walk uses.
        """
        needed = limit if canonical else offset + limit
        collected = []
        completed = 0
        cursor = _unpack_history_cursor(canonical, project_id, task_id, recent)
        total = 0
        activity = None
        selection = {}
        pages = 0
        while completed < needed and pages < 500:
            pages += 1
            argv = [str(task_id), '--recent' if recent else '--limit',
                    str(min(20, needed - completed)), '--json']
            if since_checkpoint:
                argv.append('--since-checkpoint')
            if cursor:
                argv += ['--cursor', cursor]
            reply = self._run('history', project_id, self.actor_namespace + '/read', argv)
            if isinstance(reply, list):
                collected.extend(reply)
                total = len(_logical_entries(reply))
                cursor = None
                break
            entries = reply.get('entries') or []
            if reply.get('activity_cursor'):
                activity = reply['activity_cursor']
            if since_checkpoint:
                selection = {key: reply.get(key) for key in (
                    'since_checkpoint', 'checkpoint_author', 'checkpoint_timestamp',
                    'checkpoint_coverage', 'unverified_entries')}
            collected.extend(entries)
            completed = _logical_entry_count(collected)
            total = reply.get('total_entries', len(_logical_entries(collected)))
            cursor = reply.get('next_cursor')
            if cursor is None or not entries:
                break
        items = (_slice_logical_entries(collected, 0, limit) if canonical
                 else _slice_logical_entries(collected, offset, limit))
        return dict({'items': items,
                     'total': total,
                     'canonical_cursor': _pack_history_cursor(cursor, project_id, task_id, recent),
                     'activity_cursor': activity}, **selection)

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
        # The coordinator's recorded hand-back (kittrial-5bb.343) travels with the
        # brief's review projection; My work merges it onto the queue row.
        if isinstance(data.get('followon'), dict):
            detail['followon'] = data['followon']
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
            pointer_items={}
            carry = self._open_items_in_full(project_id, task_id, point.get('comment_id'), unresolved,pointer_items)
            shown_items = [{'id': item.get('id'), 'kind': item.get('kind'),
                            'text': item.get('text'), 'source': item.get('source')}
                           for item in unresolved.get('items') or []
                           if isinstance(item, dict)]
            shown = len(shown_items)
            total = unresolved.get('total')
            # The page shows the first ten; carry_open_items is the complete list. Say
            # which is which, or an agent reads the first ten as the whole set
            # (kittrial-5bb.356). The note appears when a list was cut, or when the
            # complete list could not be read at all.
            note = None
            if isinstance(total, int) and shown < total:
                note = ('The first %d of %d; carry_open_items %s' % (
                    shown, total,
                    'has all %d, in order, to carry forward unchanged.' % total
                    if carry is not None else 'could not all be read: read the brief again.'))
            elif carry is None:
                note = 'carry_open_items could not all be read: read the brief again.'
            checkpoint = {'id': point.get('comment_id'), 'at': point.get('timestamp'),
                          'author': point.get('author'), 'summary': data.get('current_position'),
                          'next_action': data.get('next_action'),
                          'branch': point.get('branch'), 'source_commit': point.get('source_commit'),
                          'intent': (data.get('intent') or {}).get('text'),
                          'acceptance': (data.get('acceptance') or {}).get('text'),
                          'newer_activity': point.get('newer_activity'),
                          'open_items': shown_items,
                          'pointer_items': pointer_items if carry is not None else
                                           {item['id']:pointer_items[item['id']] for item in shown_items if item['id'] in pointer_items},
                          'resolved': (data.get('resolved') or {}).get('items',[]),
                          'resolved_pointer_items': (data.get('resolved') or {}).get('pointer_items',{}),
                          'resolved_total': (data.get('resolved') or {}).get('total',0),
                          'resolved_omitted': (data.get('resolved') or {}).get('omitted',0),
                          'open_items_shown': shown,
                          'open_items_total': total,
                          **({'open_items_note': note} if note else {})}
            from briefing import bound_context_pointer_display
            checkpoint['pointer_items'],checkpoint['resolved_pointer_items']=bound_context_pointer_display(
                [checkpoint['pointer_items'],checkpoint['resolved_pointer_items']])
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
        for dimension, fact in (data.get('lifecycle') or {}).items():
            if isinstance(fact,dict) and fact.get('reason'):
                lifecycle[dimension]['reason']=fact['reason']
                lifecycle[dimension]['note']=fact['reason']
        dependencies = (data.get('dependencies') or {}).get('items') or []
        # kittrial-5bb.293 item 1: when a field is cut, the HTTP brief names the GET
        # route that returns the full description and carries the link. Like the
        # canonical brief, the block is present ONLY when a field was actually cut,
        # so an uncut brief carries no pointer bytes on either route.
        read_whole = ({'route': 'GET /v1/projects/%s/tasks/%s' % (project_id, task),
                       'link': '/v1/projects/%s/tasks/%s' % (project_id, task),
                       'note': 'The complete text: GET /v1/projects/%s/tasks/%s.' % (project_id, task)}
                      if data.get('read_whole') else None)
        return {**({'assignment_records':data['assignment_records']} if data.get('assignment_records') else {}),
                'task': task, 'checkpoint': checkpoint, 'carry_open_items': carry,
                'review': {'state': review.get('review_state') or 'none',
                           'waiting_for_approval':review.get('waiting_for_approval'),
                           'approval_requires_other_party':bool(review.get('approval_requires_other_party')),
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
                           # Additive (kittrial-5bb.164): EVERY standing one's author and
                           # operation id, uncapped, for the write check. ``None`` on an endpoint
                           # that predates the field, so the caller falls back to the capped
                           # names above (and an exact retry is then refused as before).
                           'recommendation_keys': ([{'author': entry.get('author'),
                                                     'operation_id': entry.get('operation_id')}
                                                    for entry in review['recommendation_keys']
                                                    if isinstance(entry, dict)]
                                                   if isinstance(review.get('recommendation_keys'), list) else None),
                           'next_actor': review.get('next_actor') or 'unknown',
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
                'next_action': data.get('next_action'),
                'activity_cursor': data.get('activity_cursor'),
                # The coordinator's recorded hand-back (kittrial-5bb.343), the same
                # additive block a work row carries.
                'followon': data.get('followon'),
                # kittrial-5bb.293 item 1: the pointer block built above is carried
                # only when a field was actually cut.
                **({'read_whole': read_whole} if read_whole else {}),
                'warnings': data.get('warnings') or [],
                # The canonical brief already bounds refs, authors and omissions;
                # preserve its coverage/unknown counts rather than infer from time.
                'newer': data.get('newer'),
                # kittrial-5bb.354: the ONE newer-activity definition (the same fields a
                # work row, next and wait carry) and the bounded 'changes since your
                # checkpoint' section, exactly as the canonical brief computed them. An
                # older endpoint omits both: null, never inferred here.
                'activity': data.get('activity') if isinstance(data.get('activity'), dict) else None,
                'changes_since_checkpoint': data.get('changes_since_checkpoint')
                if isinstance(data.get('changes_since_checkpoint'), dict)
                else self._no_checkpoint_section(task_id) if data.get('checkpoint') is None else None}

    @staticmethod
    def _no_checkpoint_section(task):
        """The section's own sentence for a task with no checkpoint: the canonical brief
        leaves the block out then (it keeps the compact size), and that is true of an older
        endpoint's brief too."""
        from briefing import changes_since_checkpoint
        return changes_since_checkpoint({'task': task, 'entries': []}, None, None, None)

    def _open_items_in_full(self, project_id, task_id, checkpoint_id, unresolved, pointer_items=None):
        """Every open item of the current checkpoint, as recorded, or None when they could not all be read.

        The next checkpoint must carry each one forward unchanged, ``source`` included, so
        the checkpoint template needs all of them (kittrial-5bb.113 review). The task page
        shows the first ten. A task with more costs ONE further ``brief`` read that asks for
        all of them (a checkpoint holds at most 100). An endpoint older than that page size
        refuses it, and the items are then read ten at a time, at most ``OPEN_ITEM_PAGES``
        reads. A checkpoint written between two reads makes the list unusable, and the
        template then says to read again.
        """
        if pointer_items is not None:
            pointer_items.clear()
            pointer_items.update(unresolved.get('pointer_items') or {})
        items = [{key:value for key,value in item.items() if key not in ('pointers','pointers_omitted')} for item in unresolved.get('items') or [] if isinstance(item, dict)]
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
            if pointer_items is not None:
                pointer_items.clear()
                pointer_items.update(every.get('pointer_items') or {})
            return [{key:value for key,value in item.items() if key not in ('pointers','pointers_omitted')} for item in every.get('items') or [] if isinstance(item, dict)]
        for _ in range(self.OPEN_ITEM_PAGES):
            if offset is None:
                return items
            page = self._run('brief', project_id, self.actor_namespace + '/read',
                             [str(task_id), '--json', '--items-limit', str(self.BRIEF_ITEMS),
                              '--items-offset', str(offset)])
            if not isinstance(page, dict) or (page.get('checkpoint') or {}).get('comment_id') != checkpoint_id:
                return None
            more = page.get('unresolved') or {}
            if pointer_items is not None:
                pointer_items.update(more.get('pointer_items') or {})
            items += [{key:value for key,value in item.items() if key not in ('pointers','pointers_omitted')} for item in more.get('items') or [] if isinstance(item, dict)]
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
            said = (reply.get('stderr') or '').strip().split('\n')
            sentence = project_creation.busy_sentence(said[-1]) if said else None
            if sentence is None:
                # A wait for a lock ran out before the creation's own code answered: the
                # endpoint's line names the lock file. The general sentence, and the log.
                self._log_busy('create-project %s' % name, reply.get('stderr'))
                raise busy(retry_after=60)
            raise busy(sentence, retry_after=60)
        if isinstance(reply, dict) and reply.get('returncode') == 2:
            import project_creation
            said = (reply.get('stderr') or '').strip().split('\n')
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

    def guidance_command(self, principal, project_id, operation, body=None, text=None, key=None):
        """One canonical guidance operation, with the real authenticated identity."""
        import web_guidance
        capability = web_guidance.CAPABILITIES[operation]
        writing = operation in ('set', 'clear', 'ack')
        operation_id = self._result_key(principal, project_id, 'guidance.' + operation, key, None) if writing else None
        if writing:
            self._settle_actor_rows(principal, project_id)
        payload = dict(body or {}, operation=operation)
        attachments = {'text': {'flag': '--file', 'text': text}} if operation == 'set' else {}
        try:
            reply = self._endpoint('web-guidance', project_id, self._actor(principal), [json.dumps(payload)],
                                   attachments, operation_id=operation_id,
                                   authority=authority_request(principal, project_id, capability,
                                                               now=self.service._expiry_now()),
                                   require_authority=True, route='guidance.' + operation)
        except HttpError as error:
            if not writing and error.status == 503 and error.code == 'uncertain':
                refused = HttpError(503, 'unavailable', 'Guidance or onboarding could not be read. Nothing was changed; try again shortly.')
                refused.nothing_done = True
                raise refused from None
            raise
        return self._checked(reply, reading=not writing)

    def guidance_replay_guard(self, principal, project_id, operation):
        # Re-check live canonical authority before a service-cache replay. A revoked
        # coordinator grant must not replay a cached coordinate operation.
        return self.guidance_command(principal, project_id,
                                     'read' if operation == 'ack' else 'coordinate-check')

    def set_onboarding(self, principal, project_id, text, key):
        """An owner sets (``text``) or clears (``None``) the project's onboarding text.

        One service-only ``set-onboarding`` endpoint action. The endpoint re-checks the
        coordination capability under the authority lock and applies the same
        size limit as ``admin.py set-onboarding`` plus the plain-text rule. The text
        travels as an attachment, never on a command line.
        """
        operation_id = self._result_key(principal, project_id, 'projects.onboarding', key, None)
        authority = authority_request(principal, project_id, CAP_COORDINATE, now=self.service._expiry_now())
        attachments = {} if text is None else {'text': {'flag': '--file', 'text': text}}
        reply = self._endpoint('set-onboarding', project_id, self._actor(principal),
                               ['clear'] if text is None else ['set'], attachments, operation_id=operation_id,
                               authority=authority, require_authority=True, route='projects.onboarding')
        if isinstance(reply, dict) and reply.get('returncode') == 2:
            said = (reply.get('stderr') or '').strip().split('\n')
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
            said = (reply.get('stderr') or '').strip().split('\n')
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

    def guidance_state(self, project_id, actor):
        """One actor's guidance version block (kittrial-5bb.325).

        Read from the project's coordination sidecar under this service's runtime root
        (``<root>/projects/<project>``), the same files the host's ``setup-status`` read
        uses, with the same module the endpoint uses, so the web agent routes cannot
        disagree with ``work``, ``brief`` or ``session resume`` about the current version,
        this actor's acknowledgement, or the one move item. It costs no endpoint read, so
        the one-cheap-read rule of the polled routes (kittrial-5bb.281/.285) still holds.
        Version and command only: the guidance TEXT never enters an agent answer.
        """
        import guidance as guidance_module
        project_path = Path(self.root) / 'projects' / project_id
        return guidance_module.brief_block(project_path, actor)

    def claiming_state(self, project_id):
        """The project's claiming setting, read FRESH from its directory (kittrial-5bb.352).

        It lives outside the tracker, so the tracker's read cache (keyed on the dolt
        commit) must never be used for it: every listing and every claim gate reads the
        file again. An absent file is open; a damaged one reads as the restrictive
        'assigned' with a warning, never as the permissive default.
        """
        import claiming as claiming_module
        return claiming_module.read(Path(self.root) / 'projects' / project_id)

    def set_claiming(self, project_id, value, actor, via='web'):
        """Record the setting under the project's coordination lock (kittrial-5bb.352)."""
        import fcntl
        import claiming as claiming_module
        project_path = Path(self.root) / 'projects' / project_id
        with (project_path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return claiming_module.write(project_path, value, actor, via=via)

    #: How long the host's session names of one project are reused (kittrial-5bb.198). A task
    #: list names its assignees on every read, and a session registered just now may show by
    #: its id for this long.
    HOST_NAMES_SECONDS = 30

    def host_coordination(self, project_id, fresh=False):
        """The installation's operators and the project's sessions (kittrial-5bb.198).

        One read-only ``host-coordination`` endpoint action, sent without an authority block:
        ``{'operators': [...], 'sessions': [{'actor', 'name', 'principal'}] or None,
        'sessions_note'}``. Kept for :data:`HOST_NAMES_SECONDS` per project unless ``fresh``.
        An endpoint that predates the action refuses it as an unknown action (``HttpError``).
        """
        cache = self.__dict__.setdefault('_host_coordination', {})
        now = time.monotonic()
        held = cache.get(project_id)
        if not fresh and held is not None and now - held[0] < self.HOST_NAMES_SECONDS:
            return held[1]
        result = self._run('host-coordination', project_id, self.actor_namespace + '/read', [])
        if not isinstance(result, dict):
            raise HttpError(502, 'bad_gateway', 'The server answered the host-coordination read with no document')
        cache[project_id] = (now, result)
        return result

    def host_projects(self):
        """The runtime's initialized projects whose creation finished (kittrial-5bb.198; superusers).

        One read-only ``host-projects`` endpoint action, sent without an authority block.
        """
        result = self._run('host-projects', None, self.actor_namespace + '/read', [])
        names = result.get('projects') if isinstance(result, dict) else None
        if not isinstance(names, list):
            raise HttpError(502, 'bad_gateway', 'The server answered the host-projects read with no list')
        return [name for name in names if isinstance(name, str)]

    def review_states(self, project_id, queue=None):
        """Review state per task from the canonical ``work`` projection.

        ``bd list`` rows carry no review state, so the task list merges this in. The
        ``work`` queue lists every open task and every closed task whose review is
        still actionable; a closed task it omits may retain historical approval,
        so its review is reported as unknown rather than guessed. ``complete`` is false when the
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
        """The attention fields of one canonical ``work`` row, type-checked."""
        open_items = row.get('open_items', 0)
        blocking_items = attention_blocking_items(row)
        redelivered = row.get('redelivered')
        raw_pending = row.get('pending_change_requests')
        if isinstance(raw_pending, (str, bytes)) or not hasattr(raw_pending, '__iter__'):
            raw_pending = []
        return {'assignee_comment_pending':row.get('assignee_comment_pending') is True,
                'pending_change_requests': [value for value in raw_pending
                                            if isinstance(value, str)][:20],
                'open_items': open_items if type(open_items) is int and open_items>=0 else None,
                'blocking_items':blocking_items if type(blocking_items) is int and blocking_items>=0 else None,
                # An older endpoint omits it: unknown, which `review_move` reads as not redelivered
                # (kittrial-5bb.253 item 1).
                'redelivered': redelivered if type(redelivered) is bool else None,
                'checkpoint_at': row.get('checkpoint_at') if isinstance(row.get('checkpoint_at'), str) else None,
                'newer_activity': row.get('newer_activity') if type(row.get('newer_activity')) is bool else None,
                # The stable marker and the counts it was computed with (kittrial-5bb.351):
                # the bridge wakes on a marker change even while the boolean stays true.
                # A legacy checkpoint (unknown coverage) yields a null marker, so a
                # reader falls back to `newer_activity`.
                'newer_activity_marker': row.get('newer_activity_marker')
                if isinstance(row.get('newer_activity_marker'), str) else None,
                # The current fields digest alone (kittrial-5bb.367 F2b).
                'newer_activity_fields': row.get('newer_activity_fields')
                if isinstance(row.get('newer_activity_fields'), str) else None,
                # The checkpoint-independent wait marker (kittrial-5bb.369): a worker's
                # wait fingerprints this, so its own checkpoint does not wake it.
                'wait_newer_activity_marker': row.get('wait_newer_activity_marker')
                if isinstance(row.get('wait_newer_activity_marker'), str) else None,
                'newer_activity_by_others': row.get('newer_activity_by_others')
                if type(row.get('newer_activity_by_others')) is int and row.get('newer_activity_by_others') >= 0 else None,
                'newer_activity_own': row.get('newer_activity_own')
                if type(row.get('newer_activity_own')) is int and row.get('newer_activity_own') >= 0 else None,
                'newer_activity_coverage': row.get('newer_activity_coverage')
                if isinstance(row.get('newer_activity_coverage'), str) else None,
                'unresolved_directions': row.get('unresolved_directions')
                if type(row.get('unresolved_directions')) is int and row.get('unresolved_directions') >= 0 else None}

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
                             # The queue row's canonical open-item count (kittrial-5bb.249).
                             'pending_review_items': item.get('open_requests'),
                             'open_items': attention.get('open_items',0),
                             'blocking_items':attention_blocking_items(attention),
                             'assignee_comment_pending':attention.get('assignee_comment_pending') is True,
                             # The superseding-redelivery fact (kittrial-5bb.253 item 1).
                             'redelivered': attention.get('redelivered'),
                             'checkpoint_at': attention.get('checkpoint_at'),
                             'newer_activity': attention.get('newer_activity'),
                             # The stable newer-activity marker and its counts
                             # (kittrial-5bb.351), carried exactly as `work` read them.
                             'newer_activity_marker': attention.get('newer_activity_marker'),
                             'newer_activity_fields': attention.get('newer_activity_fields'),
                             # The checkpoint-independent wait marker (kittrial-5bb.369).
                             'wait_newer_activity_marker': attention.get('wait_newer_activity_marker'),
                             'newer_activity_by_others': attention.get('newer_activity_by_others'),
                             'newer_activity_own': attention.get('newer_activity_own'),
                             'newer_activity_coverage': attention.get('newer_activity_coverage'),
                             'unresolved_directions': attention.get('unresolved_directions')})
                if item.get('lifecycle_reasons'):
                    rows[-1]['lifecycle_reasons'] = dict(item['lifecycle_reasons'])
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
            from claim_limit import actor_holds
            for row in page.get('items') or []:
                if not isinstance(row, dict) or not actor_holds(row.get('owner'), actor):
                    continue
                rows.append(dict({'id': row.get('task'), 'title': row.get('title'), 'status': row.get('status'),
                                  'assignee': row.get('owner'), 'review_state': row.get('review_state'),
                                  'contribution_id': row.get('contribution_id'),
                                  # The canonical open-item count, so the move sentence's N
                                  # matches the `work` row (kittrial-5bb.249).
                                  'pending_review_items': row.get('pending_review_items')},
                                 **self._attention_fields(row)))
            offset = page.get('next_offset')
            if offset is None:
                complete = True
                break
        return {'tasks': rows, 'complete': complete}

    def review_queue(self, project_id, stop_if_over_bound=False):
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
                                        if isinstance(row.get('contribution_author'), str) else None,
                                        claimable=row.get('claimable')
                                        if isinstance(row.get('claimable'), bool) else None,
                                        followon=row.get('followon')
                                        if isinstance(row.get('followon'), dict) else None))
            offset = page.get('next_offset')
            if offset is None:
                complete = True
                break
            # Over-bound single-agent reads recover own tasks separately. Stop
            # once claimable suggestions fill; a review-heavy first page must
            # not hide all unclaimed work. Keep the same bounded page walk.
            claimable = sum(item.get('claimable') is True for item in items)
            if (stop_if_over_bound and type(page.get('total')) is int
                    and page['total'] > self.QUEUE_MAX_PAGES * MAX_PAGE
                    and claimable >= min(AGENT_CLAIMABLE_LIMIT, AGENT_ACTION_LIMIT)):
                break
        items.sort(key=queue_order)
        return {'items': items, 'complete': complete, 'warnings': sorted(warnings)}

    def agent_snapshot(self,project_id):
        return self.review_queue(project_id,stop_if_over_bound=True)


#: What a row that cannot be read is called, in the words of the endpoint's readers
#: (kittrial-5bb.141): the row is named, marked, and nothing of it is trusted.
MALFORMED_ROW = 'Malformed issue row'
#: Quotes with what they hold, and the brackets: all that matters for finding where one
#: element of a JSON text ends. Each match consumes its characters, so a scan is one pass.
_JSON_PIECE = re.compile(r'"(?:\\.|[^"\\])*"|[\[\]{}:,]', re.DOTALL)
#: The only id a marker row may carry: the shape of a tracker id (the same bound as the
#: endpoint's readers use), so that what is named in the list and put into a link is an id
#: and not whatever text stood there.
_ROW_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')


def unreadable_row(task_id):
    """409 for a task whose row exists and cannot be read (kittrial-5bb.169)."""
    return HttpError(409, 'unreadable_row',
                     'Task %s exists, but its row cannot be read (it is malformed or nested too deeply). '
                     'Ask an operator of the server to repair it.' % task_id,
                     {'task': task_id, 'state': 'unreadable'})


def session_label(item):
    """How a host session is shown (kittrial-5bb.198): its registered name and the principal it is
    mapped to, ``'Cloud lane (lane cloud)'`` or ``'Review (person james)'``; None when the host
    knows neither, so the caller keeps the id."""
    if not isinstance(item, dict):
        return None
    name = item.get('name') if isinstance(item.get('name'), str) and item.get('name').strip() else None
    principal = item.get('principal') if isinstance(item.get('principal'), str) else None
    mapped = None
    if principal and ':' in principal:
        kind, _, who = principal.partition(':')
        mapped = '%s %s' % (kind, who)
    if name and mapped:
        return '%s (%s)' % (name, mapped)
    if mapped:
        return 'a session of %s' % mapped
    return name


def _row_id(text):
    """The id a row's text gives at its own top level, without parsing the row; None when it gives none.

    Only a string value of the key ``id`` of the outermost object is read; what is nested
    below is skipped by counting brackets. One pass, no recursion.
    """
    depth, key, expect_value = 0, None, False
    for match in _JSON_PIECE.finditer(text):
        piece = match.group()
        if piece in '[{':
            depth += 1
            key, expect_value = None, False
        elif piece in ']}':
            depth -= 1
            if depth <= 0:
                break
        elif depth != 1:
            continue
        elif piece == ':':
            expect_value = key is not None
        elif piece == ',':
            key, expect_value = None, False
        elif piece[0] == '"':
            try:
                value = json.loads(piece)
            except ValueError:
                value = None
            if expect_value:
                if key == 'id':
                    return value if isinstance(value, str) and _ROW_ID.match(value) else None
                key, expect_value = None, False
            else:
                key = value
    return None


def _marker(text):
    """The row the service shows in place of one it does not read: its id, and nothing else of it.

    Not its title: the title is the stored text of a row nobody has read, control
    characters and all, and the id is what names the row and what the operator needs.
    A piece without an id of a tracker id's shape at its own top level is not a row at
    all, and the answer it stands in is refused.
    """
    task_id = _row_id(text)
    if task_id is None:
        raise ValueError('an element nested too deeply to read is not a tracker row (it has no id)')
    return {'id': task_id, 'malformed': True, 'unreadable': True, 'error': MALFORMED_ROW, 'status': 'unknown'}


def _elements(text):
    """Where each element of a JSON array of objects begins and ends; None when the text is not exactly that.

    One pass that counts brackets outside string literals, so a row nested thousands of
    levels deep costs what its length costs and no recursion. Exactly that: ``[``, objects
    separated by one comma each, ``]``, and white space. Anything else between the rows
    or around the list (a number, a word, a second comma, text after the end) is what
    ``json.loads`` refuses or is not a list of rows, and is refused here too.
    """
    stripped = text.strip()
    if stripped[:1] != '[':
        return None
    offset = len(text) - len(text.lstrip())
    spans, depth, start, last, closed = [], 0, None, offset + 1, None
    for match in _JSON_PIECE.finditer(text, offset):
        piece = match.group()
        if piece in '[{':
            depth += 1
            if depth == 2:
                if piece != '{':
                    return None
                gap = text[last:match.start()].strip()
                if gap != (',' if spans else ''):
                    return None
                start = match.start()
        elif piece in ']}':
            depth -= 1
            if depth == 1 and start is not None:
                spans.append((start, match.end()))
                start, last = None, match.end()
            if depth == 0:
                closed = match
                break
            if depth < 0:
                return None
    if closed is None or closed.group() != ']' or text[last:closed.start()].strip() or text[closed.end():].strip():
        return None
    return spans


def _row(piece):
    """One element of the tracker's list: the row, or its marker when it nests deeper than the kit reads."""
    if record_json.nesting(piece, record_json.ROW_NESTING_MAX) > record_json.ROW_NESTING_MAX:
        return _marker(piece)
    try:
        return json.loads(piece)
    except RecursionError:
        # An interpreter that gives up below the kit's own bound: the row is unreadable here.
        return _marker(piece)


def _parse_native(text):
    """Parse one JSON text of the tracker; a row nested deeper than the kit reads becomes a marker row.

    The rule is the kit's own, the one the endpoint's readers have (kittrial-5bb.141):
    a row nested deeper than ``record_json.ROW_NESTING_MAX`` is not read. It is counted,
    not tried: what ``json.loads`` can parse differs from one interpreter to the next
    (3.10 gives up at about a thousand levels with ``RecursionError``, which is how one such
    row made the task list answer 500; 3.13 parses 3,000 levels), and a rule that
    depended on it marked a row on one host and read it on another (review of
    kittrial-5bb.169).

    A text whose deepest nesting is within the bound is parsed whole, as before. Otherwise
    it is taken apart: each row is judged on its own, one that is too deep is named and
    marked instead of failing the read, and every other row is parsed. Raises
    ``ValueError`` for a text that is not JSON, and for a deep text that is not a single
    row or a clean list of rows.
    """
    if record_json.nesting(text, record_json.ROW_NESTING_MAX) <= record_json.ROW_NESTING_MAX:
        try:
            return json.loads(text)
        except RecursionError:
            pass                                      # the interpreter gives up below the bound: row by row
    if text.lstrip()[:1] == '{':
        # One row printed alone: it must be a whole object, and nothing else, to be called a row.
        if _elements('[' + text + ']') is None:
            raise ValueError('not a whole JSON object')
        return _row(text)
    spans = _elements(text)
    if spans is None:
        raise ValueError('not a list of tracker rows')
    return [_row(text[start:end]) for start, end in spans]


def _canonical_payload(stdout):
    """Parse the JSON body a canonical command returned, tolerating NDJSON."""
    text = (stdout or '').strip()
    if not text:
        raise uncertain('Canonical command returned no data; outcome may be unknown')
    try:
        return _parse_native(text)
    except ValueError:
        for line in reversed(text.split('\n')):
            line = line.strip()
            if line[:1] in ('{', '['):
                try:
                    return _parse_native(line)
                except ValueError:
                    continue
    raise uncertain('Canonical command returned unparsable data; outcome may be unknown')


#: The label prefix ``requirement_http.py`` puts on the owner's "Accept requirement"
#: decision row (``owner-requirement-decision:<content hash>``, created with
#: ``--type decision`` when the signed-in owner accepts a requirement's exact content
#: in simple mode). ``next_action`` points that row at the Requirements page it is
#: resolved from (kittrial-5bb.277 third review).
REQUIREMENT_DECISION_LABEL = 'owner-requirement-decision:'


def next_action(task, claiming='open'):
    """Who acts next on a task, derived only from its status, assignee and review state."""
    state = task.get('review_state')
    from review_state import active_work
    if not active_work(task.get('status'), state, task.get('open_requests', 0)):
        return None
    if state is None:
        # Unknown (the canonical projection did not cover this row): say nothing
        # rather than invite a claim or delivery on work that may be under review.
        return None
    if state == 'integrated':
        # kittrial-5bb.336: the coordinator's move - close the task or record what
        # remains - never the agent's own action list.
        return {'who': 'owner', 'text': 'The contribution is integrated; close the task or record what remains'}
    if state in ('awaiting-review', 'legacy-review-ready'):
        return {'who': 'owner', 'text': 'Review the delivered contribution'}
    if state == 'changes-requested':
        return {'who': 'assignee', 'text': 'Address the requested changes'}
    if state in ('approved', 'awaiting-integration'):
        return {'who': 'owner', 'text': 'Integrate the approved contribution'}
    if state == 'held':
        return {'who': 'coordinator', 'text': 'Release the integration hold or record a corrected verification'}
    if state == 'error':
        return {'who': 'owner', 'text': 'Needs operator attention'}
    if task.get('assignee'):
        return {'who': 'assignee', 'text': 'Deliver a contribution'}
    # The last offer is a claim, so it follows the same claimability predicate the
    # queue, the agent attention list and the claim route use (kittrial-5bb.277 review
    # item 2). A requirement record, an epic, a decision or another row the queue never
    # offers says what it is for instead of inviting a claim the route refuses with 409.
    from work import claimable, open_dependency_sentence, requirement_record
    open_deps = task.get('open_dependencies') or []
    if not claimable(task.get('issue_type'), task.get('status'), task.get('assignee'),
                     task.get('contribution_id'), state, labels=task.get('labels'),
                     open_dependencies=open_deps, claiming=claiming):
        if open_deps:
            return {'who': 'owner', 'text': open_dependency_sentence(open_deps) or 'Blocked'}
        if str(claiming or 'open') == 'assigned' and not task.get('assignee'):
            from claiming import EMPTY_SENTENCE
            return {'who': 'coordinator', 'text': EMPTY_SENTENCE}
        if requirement_record(task.get('labels')):
            return {'who': 'owner',
                    'text': "Requirement record: edit it on the project's Requirements page"}
        if str(task.get('issue_type') or 'task') == 'epic':
            return {'who': 'owner', 'text': 'This is an epic, not a unit of work: work its tasks'}
        if any(isinstance(label, str) and label.startswith(REQUIREMENT_DECISION_LABEL)
               for label in (task.get('labels') or ())):
            # The owner's "Accept requirement" row is a decision record made from the
            # project's Requirements page. Name where it is resolved instead of only
            # saying what it is not (kittrial-5bb.277 third review). The sentence is
            # deliberately about the row and its page, not about who acted: the label
            # alone does not evidence an actor, and a real-bd test writes a decoy
            # decision row carrying it (tests/test_requirement_owner_real_bd.py).
            return {'who': 'owner',
                    'text': "Requirement decision: resolve it on the project's Requirements page"}
        if str(task.get('issue_type') or 'task') == 'decision':
            return {'who': 'owner', 'text': 'This is a decision record, not a unit of work'}
        return {'who': 'owner', 'text': 'This row is not offered as claimable work'}
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
#: The routes that read their own body as a stream (kittrial-5bb.370): the task-file upload
#: only. Every other route's body is read whole, bounded by ``max_body``, before it runs.
STREAMED = set()


def route(method, pattern, *, anonymous=False, csrf=True, stream=False):
    compiled = re.compile('^' + pattern + '$')

    def register(handler):
        ROUTES.append((method, compiled, handler.__name__, anonymous, csrf))
        if stream:
            STREAMED.add(handler.__name__)
        return handler
    return register


@contextmanager
def project_room_lock(project):
    """Hold a project's ``.coordination.lock`` exclusively for one rooms operation.

    The cross-platform lock is ``http_authority.file_lock``: it uses ``fcntl`` on Linux
    and ``msvcrt`` on Windows, so importing http_service and serving rooms both work on
    Windows (review b2). ``fcntl`` is never imported here at import time.
    """
    from http_authority import file_lock
    with file_lock(Path(project) / '.coordination.lock'):
        yield


#: Said when the service is moving as many files as it allows (kittrial-5bb.370, design 4).
TRANSFERS_BUSY = ('The server is moving as many files as it allows at once (%s); send this again in a moment.')


class TransferGate:
    # At most ``max_transfers`` uploads and downloads in the service and
    # ``max_transfers_per_actor`` for one caller at once (design 4: the service has no
    # per-actor rate limit, so these two caps are the bound). Taken before a byte moves.

    def __init__(self):
        self._lock = threading.Lock()
        self._total = 0
        self._by = {}
        self._projects = {}         # r2 (review s1): one project's share of the service's transfers

    def take(self, key, limit, project=None):
        with self._lock:
            if self._total >= limit['max_transfers']:
                raise busy(TRANSFERS_BUSY % task_files.setting('max_transfers'), retry_after=1)
            if self._by.get(key, 0) >= limit['max_transfers_per_actor']:
                raise busy(TRANSFERS_BUSY % task_files.setting('max_transfers_per_actor'), retry_after=1)
            if project is not None and self._projects.get(project, 0) >= limit['max_transfers_per_project']:
                raise busy(TRANSFERS_BUSY % task_files.setting('max_transfers_per_project'), retry_after=1)
            self._total += 1
            self._by[key] = self._by.get(key, 0) + 1
            if project is not None:
                self._projects[project] = self._projects.get(project, 0) + 1

    def give(self, key, project=None):
        with self._lock:
            self._total -= 1
            for table, name in ((self._by, key), (self._projects, project)):
                if name is None:
                    continue
                left = table.get(name, 1) - 1
                if left > 0:
                    table[name] = left
                else:
                    table.pop(name, None)

    def held(self):
        with self._lock:
            return self._total, dict(self._by)

    def projects(self):
        with self._lock:
            return dict(self._projects)


#: One gate for handlers made without :func:`build_handler` (a test's own subclass).
DEFAULT_TRANSFERS = TransferGate()
#: kittrial-5bb.380: the lingering closes of answered uploads now running in this process, and the counts
#: of those done and of those closed at once because LINGER_MAX were running.
LINGERING = {'now': 0, 'lingered': 0, 'hard': 0}
#: kittrial-5bb.380 (review of .370 r3): how long a refused upload's discard waits for the project's lock.
DISCARD_LOCK_SECONDS = 10
LINGERING_LOCK = threading.Lock()
FILES_UNAVAILABLE = 'Task files are not available on this server.'
FILE_CODES = {400: 'invalid', 403: 'forbidden', 404: 'not_found', 409: 'conflict', 411: 'length_required',
              413: 'payload_too_large', 422: 'invalid', 501: 'not_implemented', 503: 'unavailable',
              507: 'insufficient_storage'}


def files_error(refusal):
    # A task_files.Refusal as the HttpError the route answers with.
    error = HttpError(refusal.status, refusal.code, str(refusal))
    if refusal.retry_after:
        error.retry_after = refusal.retry_after
    error.nothing_done = True
    return error


def file_body(payload):
    # The canonical task-file request from a files.* route payload (no actor, no route data).
    body = {'schema_version': 1, 'operation': payload['operation'], 'task': payload['task_id'],
            'sha256': payload['sha256']}
    if payload['operation'].endswith('-record'):
        body.update(name=payload['name'], bytes=payload['bytes'], media_type=payload['media_type'],
                    direction=payload['direction'], note=payload.get('note'))
    else:
        body['reason'] = payload['reason']
    return body


#: What an HTTP agent needs before a guidance item is a move it can actually make: a
#: read of the project's guidance and an acknowledgement of a version
#: (kittrial-5bb.212). The probes below are matched against this service's own route
#: table, so the item returns BY ITSELF the day those routes ship and cannot come back
#: before (kittrial-5bb.334). One place to extend if .212 chooses another ack path.
GUIDANCE_READ_PROBE = '/v1/projects/probe/guidance'
GUIDANCE_ACK_PROBES = ('/v1/projects/probe/guidance/ack',
                       '/v1/projects/probe/guidance/acknowledge')


def serves_web_guidance(routes=None):
    """(read, ack): does this service serve both agent guidance routes over HTTP?

    The registered route table is the authority, not a hand-kept flag: an installation
    whose kit serves the two routes shows the item exactly as kittrial-5bb.325 made it,
    and one that does not is spared an item its web agents cannot clear. ``routes`` is
    for tests and defaults to the table this module has registered.
    """
    read = ack = False
    for verb, pattern, _name, _anonymous, _csrf in (ROUTES if routes is None else routes):
        if verb == 'GET' and pattern.match(GUIDANCE_READ_PROBE):
            read = True
        elif verb == 'POST' and any(pattern.match(probe) for probe in GUIDANCE_ACK_PROBES):
            ack = True
    return read, ack


class ApiHandler(BaseHTTPRequestHandler):
    """One request per call. Class attributes are supplied by :func:`build_handler`."""

    service = None
    backend = None
    web_root = None
    read_cache = None
    read_cache_lock = None
    trusted_proxies = ()
    max_body = MAX_BODY_BYTES
    #: The task-file transfer gate (kittrial-5bb.370); one per configured service.
    transfers = None
    #: Send ``Strict-Transport-Security`` on HTTPS answers (kittrial-5bb.168 item 6). False
    #: unless the operator's setting turns it on.
    hsts = False
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
        except ssl.SSLError as failed:
            # The wrap looks at what the client has sent already; what it refuses there is a
            # handshake that did not come about, and has the line of one.
            raise ConnectionAbortedError('TLS handshake not completed: %s'
                                         % (str(failed)[:120] or type(failed).__name__)) from None
        except OSError as failed:
            said = str(failed)[:120] or type(failed).__name__
            if isinstance(failed, ConnectionError) or failed.errno in SOCKET_GONE:
                # The socket is closed already (the client reset it, or the server gave the
                # connection up): there is nobody to serve, and it is not an error of the
                # service. Never a traceback out of the thread (kittrial-5bb.175).
                raise ConnectionAbortedError('the connection was closed before it was served: %s' % said) from None
            # Anything else is a fault of the server itself (no descriptor left for the TLS
            # object, say): the connection cannot be served either, but the operator must be
            # able to see why. One line, rate-limited in ``GuardedServer.handle_error``
            # (kittrial-5bb.177: it was silent, and before kittrial-5bb.175 a traceback each).
            raise ConnectionAbortedError('%s%s' % (SETUP_FAILED, said)) from None
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
        self._written_at = None                         # one connection serves request after request
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
            stream = name in STREAMED
            if stream:
                # The streamed upload's answer always ends the connection: a refusal before its
                # body was read, or a body that was not what its Content-Length said, must never
                # leave bytes behind that the next request on this connection would be read from.
                self._say_close = True
                self._body_unread = True              # until the route has read it (kittrial-5bb.380)
            payload, auth_source = self._read_request(method, anonymous, params, csrf,
                                                      request_id, stream=stream)
            # The semantic route target is the method plus the concrete path (with the
            # real account/project/task id substituted). It is the idempotency
            # namespace, so two different targets never share a receipt.
            route_target = '%s %s' % (method, path)
            ctx = RouteContext(self._principal, payload, query, params, request_id,
                               self._idempotency_key(), self._body_hash, auth_source,
                               self._is_secure(), route_target)
            begun = (name, ctx.idempotency_key is not None)
            status, response = getattr(self, name)(ctx)
            if status is not None:              # None: the route sent its own answer (a file download)
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
        finally:
            if getattr(self, '_body_unread', False):
                self._body_unread = False
                self._linger()

    #: kittrial-5bb.380: how long, and how much, an answered upload's unread body is still read and dropped, and
    #: how many such closes may run at once in the service; beyond that the connection is closed at once.
    LINGER_SECONDS = 5
    LINGER_BYTES = 64 * 1024 * 1024
    LINGER_MAX = 8

    def _linger(self):
        # The answer to an upload refused before its body was read (401, 403, 413, 503...) is sent, and the
        # connection is then closed with that body still arriving. Closed at once, the unread bytes make the
        # system answer with a reset, which can destroy the answer before the client has read it, so the client
        # sees a cut connection instead of the refusal. As a web server's lingering close does, the write side
        # is shut and what still arrives is read and dropped, for a short, bounded time.
        # It runs after the route has returned: no transfer slot and no lock is held here. Bounded per connection
        # (LINGER_SECONDS, LINGER_BYTES) and in total (LINGER_MAX at once): many refused uploads at once cannot tie
        # up the service's threads; beyond the cap the connection is closed at once (LINGERING counts both).
        self.close_connection = True
        try:
            self.wfile.flush()
        except (OSError, ValueError):
            return
        with LINGERING_LOCK:
            if LINGERING['now'] >= self.LINGER_MAX:
                LINGERING['hard'] += 1
                return
            LINGERING['now'] += 1
            LINGERING['lingered'] += 1
        try:
            connection = self.connection
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                return
            ends = time.monotonic() + self.LINGER_SECONDS
            taken = 0
            try:
                while taken < self.LINGER_BYTES:
                    left = ends - time.monotonic()
                    if left <= 0:
                        break
                    connection.settimeout(left)
                    piece = connection.recv(65536)
                    if not piece:
                        break
                    taken += len(piece)
            except OSError:
                pass
        finally:
            with LINGERING_LOCK:
                LINGERING['now'] -= 1

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

    def _read_request(self, method, anonymous, params, csrf, request_id, stream=False):
        body = b'' if stream else self._read_body()
        payload = None
        if body:
            content_type = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if content_type != 'application/json':
                raise unsupported('Content-Type must be application/json')
            try:
                # Bounded nesting (kittrial-5bb.108): a body nested deeper than the kit ever
                # writes is refused here, as bad JSON, and never reaches a route or the
                # operation journal. It used to be an HTTP 500.
                # Duplicate requirement fields must not silently choose one of
                # two reasons or successors before closed-schema validation.
                if re.fullmatch(r'/v1/projects/[^/]+/requirements(?:/.*)?', urlsplit(self.path).path):
                    from export_requirements import _unique
                    payload = record_json.loads(body, object_pairs_hook=_unique)
                else:
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

    #: r3 (review N1): how long after a too-slow upload's deadline the server still waits to write its 408.
    ANSWER_MARGIN = 5

    def parse_request(self):
        # r3 (review N4): one request's Expect: 100-continue never outlives it; http.server notes it while it parses.
        self._expect_100 = False
        return super().parse_request()

    def handle_expect_100(self):
        # r2 (review s4c): never "100 Continue" before the route has authorized the request and checked its limits.
        # http.server answers it while it parses the request line; here it is only noted, and _continue sends it
        # where the body is about to be read: at once for a JSON body (as before), and for the streamed upload only
        # after every check passed. A refused upload is answered with its refusal and the connection closed.
        self._expect_100 = True
        return True

    def _continue(self):
        if getattr(self, '_expect_100', False):
            self._expect_100 = False
            self.send_response_only(100)
            self.end_headers()

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
        self._continue()
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

    def _send_hsts(self):
        """``Strict-Transport-Security``, only on an HTTPS answer and only when it is turned on."""
        if getattr(self, 'hsts', False) and getattr(self.server, 'tls_context', None) is not None:
            self.send_header('Strict-Transport-Security', HSTS_HEADER)

    def _send_json(self, status, body):
        self._expect_100 = False            # r2 (s4c): one request's Expect ends with its answer
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
        self._send_hsts()
        self.send_header('X-Request-Id', getattr(self, '_current_request_id', '') or '')
        if getattr(self, '_written_at', None):
            # Set only for the answer of a write that was carried out; _dispatch clears it for the next request.
            self.send_header(SERVER_TIME_HEADER, self._written_at)
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
        self._send_hsts()
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
                canonical=False, reason=None, refused_reason=None,
                audit_success=True, state_saved=None, audit_metadata=None, replay_check=None):
        """Run one authorized, idempotent mutation.

        ``capability`` names the authority the route needs: one capability, or a tuple
        of them all required (kittrial-5bb.378 review s1: a release deploy needs
        ``coordinate`` plus the release authority). The idempotency key is
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
        if isinstance(capability, str):
            ctx.authorize = (lambda: self.service.check_authority(
                ctx.principal, project_id, capability, allow_self_user=allow_self_user))
        else:
            # Every named capability is re-checked inside the same in-lock boundary, so a
            # two-capability route cannot pass on one of them being revoked in between.
            needed = tuple(capability)

            def authorize():
                decision = None
                for name in needed:
                    decision = self.service.check_authority(
                        ctx.principal, project_id, name, allow_self_user=allow_self_user)
                return decision
            ctx.authorize = authorize
        idem_route = '%s %s' % (route_name, ctx.route_target)
        key = ctx.idempotency_key if idempotent else None
        digest = None
        if key is not None:
            kind, value, response = self.service.idempotency_reserve(
                ctx.principal, project_id, idem_route, key, ctx.body_hash,
                canonical=canonical and serialize is False)
            if kind == 'replay':
                if replay_check is not None:
                    ctx.authorize()
                    replay_check()
                # An exact retry must not re-deliver a one-time secret; the caller
                # marks that with replay_status (200 metadata-only for issue routes).
                # The header carries the time of the write, as kept with the stored answer.
                self._written_at = self.service.idempotency_written_at(ctx.principal, project_id, idem_route, key)
                return (replay_status or value), response
            digest = value
            # 'new' or 'reconcile': both proceed with the reserved digest; a canonical
            # reconcile re-invokes with the durable operation identity.
        WRITTEN.at = None
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
            event = self.service.audit(ctx.request_id, ctx.principal, route_name, 'unknown',
                                       project_id=project_id, reason=reason or 'uncertain')
            # The lifecycle write records the step's task, dimension and deterministic
            # operation identity on an uncertain answer too, so the integration-check
            # ledger can say which step to resume with the same identity
            # (kittrial-5bb.383). Other routes carry no such metadata.
            if audit_metadata:
                event.update(audit_metadata)
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
            if not audit_success:
                self._audited_refusal = True
            # The refusal is the answer whether or not its audit entry can be saved now
            # (kittrial-5bb.156): nothing was carried out, so a lock wait that runs out here
            # must not turn it into "cannot say". The entry is written with the next save.
            self.service.store.save_soon('the audit entry of a refusal')
            raise
        except Exception:
            self.service.idempotency_release(digest)
            raise
        # The server's time of the write, at the top level of the answer and of what is kept for
        # a retry, so that the same request sent again is answered with the time the write was
        # carried out (kittrial-5bb.97). An answer that is not an object carries none.
        at = written_at(self.service)
        for body in (public, stored):
            if at is not None and isinstance(body, dict) and 'server_time' not in body:
                body['server_time'] = at
        # And in a header, whatever the shape of the body: a JSON list or an empty answer has
        # no field for it. An answer that already carries a time (a canonical retry answered
        # from the endpoint's journal) keeps that one in both places.
        if isinstance(public, dict) and isinstance(public.get('server_time'), str):
            at = public['server_time']
        self._written_at = at
        with self.service.store.lock:
            if audit_success:
                event = self.service.audit(ctx.request_id, ctx.principal, route_name, 'committed',
                                           project_id=project_id, reason=reason)
                if audit_metadata:
                    event.update(audit_metadata)
            if state_saved:
                # Credential issuance/revocation owns a persistence boundary. A
                # crash before its receipt must not replay a nonexistent credential.
                self.service.store.save()
                state_saved()
                self.service.idempotency_commit(digest, status, stored, written_at=at)
            else:
                # Preserve ordinary routes' receipt-first retry contract: a failed
                # state save must not strand their key as in-progress for 24 hours.
                self.service.idempotency_commit(digest, status, stored, written_at=at)
                self.service.store.save()
            self._forget_cached_reads(ctx.principal, project_id)
        return status, public

    # -- files attached to a task (kittrial-5bb.370; docs/TASK_FILES_DESIGN.md) -------------------
    #
    # Who may (1a A, 1b A, read live on every request): a COORDINATOR is a caller for whom
    # ``Service.check_authority(CAP_COORDINATE)`` passes in the project (the coordinator and owner
    # roles, an agent's live coordinator grant, a superuser); the ASSIGNEE is the caller whose
    # acting actor (its own label, or ``?actor=`` bound by ``Service.bind_actor``) is exactly the
    # live row's assignee of a task that is not closed. Attach to-worker: a coordinator. Attach
    # from-worker: the assignee. Download: either. Withdraw: the file's author or a coordinator.
    # List: every reader of the project (the records are in the task's history anyway).

    def _files_project(self, ctx):
        place = getattr(self.backend, 'files_project', None)
        project = place(ctx.params['pid']) if place is not None else None
        if project is None:
            raise not_implemented(FILES_UNAVAILABLE)
        return project

    def _files_limits(self):
        read = getattr(self.backend, 'files_limits', None)
        if read is None:
            return dict(task_files.LIMIT_DEFAULTS)
        warnings = []
        try:
            limit = read(warnings=warnings)
        except TypeError:
            limit = read()
        for line in warnings:
            print('task files: ' + ascii(line[:300]), file=sys.stderr, flush=True)
        return limit

    def _files_actor(self, ctx):
        bound = self.service.bind_actor(ctx.principal, ctx.query.get('actor'))
        return bound or ctx.principal.actor or ctx.principal.user_id

    def _files_row(self, ctx):
        # The task row read LIVE for this request (a fresh tracker read, never the read cache).
        row = self.backend.get_task(ctx.params['pid'], ctx.params['tid'])
        if not isinstance(row, dict) or row.get('issue_type') == 'event' or is_merge_slot(row):
            raise not_found('Task not found')
        self._refuse_record_anchor(row)
        return row

    def _files_coordinates(self, ctx):
        try:
            self.service.check_authority(ctx.principal, ctx.params['pid'], CAP_COORDINATE)
            return True
        except HttpError:
            return False

    def _files_key(self, ctx):
        # Whose share of the transfers this is: the credential, else the account. A credential
        # cannot widen its share by naming another sub-label.
        return ctx.principal.credential_id or ctx.principal.user_id or 'anonymous'

    def _files_target(self, ctx, sha256=None):
        return 'task=%s sha256=%s' % (ctx.params['tid'], sha256 or ctx.params.get('sha') or '-')

    def _files_audit(self, ctx, action, outcome, reason, actor=None):
        # r2 (review s4e): an audit line that cannot be recorded is said in the service's log, with what it was
        # about (never a secret: the action, outcome, task and digest); the answer itself is not changed by it.
        try:
            with self.service.store.lock:
                self.service.audit(ctx.request_id, ctx.principal, action, outcome,
                                   project_id=ctx.params['pid'], reason=reason, actor=actor)
                self.service.store.save_soon('the audit entry of a task file')
            return True
        except Exception as failed:
            print('audit: the %s line (%s, %s) of request %s could not be recorded: %s'
                  % (action, outcome, ascii(str(reason)[:200]), ctx.request_id,
                     ascii(str(failed)[:200]) or type(failed).__name__), file=sys.stderr, flush=True)
            return False

    def _files_refused(self, ctx, action, error, sha256=None):
        # One audit line for a refused upload, download or withdrawal, then the refusal itself.
        if isinstance(error, task_files.Refusal):
            error = files_error(error)
        if isinstance(error, HttpError):
            self._files_audit(ctx, action, 'denied' if error.status in (401, 403) else 'rejected',
                              '%s %s' % (error.code, self._files_target(ctx, sha256)))
            self._audited_refusal = True
        return error

    def _files_reader(self, length, limit):
        # read(n): at most n bytes of the body. Each wait on the client is bounded by the client deadline AND by
        # what is left of the whole transfer's time (r2, review s1: transfer_grace_seconds + length /
        # min_transfer_bytes_per_second), so a client that trickles is cut off and its slot given back.
        guarded = getattr(self, '_guarded', False)
        ends = time.monotonic() + task_files.transfer_seconds(length, limit)

        def read(count):
            left = ends - time.monotonic()
            if left <= 0:
                raise task_files.too_slow(length, limit)
            wait = max(0.05, min(self.server.client_seconds, left)) if guarded else left
            if guarded:
                # r3 (review N1): the reaper only after a margin; the socket's own timeout below ends the wait at the
                # deadline, so the 408 can still be written before the connection is cut.
                self.server.watch(self.connection, wait + self.ANSWER_MARGIN)
            before = self.connection.gettimeout()
            try:
                # The socket's own timeout too: on Windows the reaper's shutdown does not end a wait that is under
                # way in another thread, the timeout does.
                self.connection.settimeout(wait)
                # r3 (review N1): one receive at a time (read1), so the deadline is looked at between small pieces;
                # read(count) would keep waiting for all of them, and a trickle never lets the timeout fire.
                read1 = getattr(self.rfile, 'read1', None)
                chunk = read1(count) if read1 is not None else self.rfile.read(count)
            except TimeoutError:
                # The socket's own timeout: the transfer's time is up (it can fire a moment before the clock below
                # says so). Never taken for a body that ended early (400).
                raise task_files.too_slow(length, limit) from None
            except OSError:
                chunk = b''
            finally:
                try:
                    self.connection.settimeout(before)
                except OSError:
                    pass
                if guarded:
                    self.server.unwatch(self.connection)
            if time.monotonic() >= ends:
                raise task_files.too_slow(length, limit)
            return chunk
        return read

    def _files_send(self, chunk, ends):
        # One step of a download under what is left of its time (r2, review s1): a client that reads too slowly
        # is cut off by the server's reaper; the caller gives the slot back and closes the connection.
        inner = getattr(self.wfile, '_inner', self.wfile)
        left = ends - time.monotonic()
        if left <= 0:
            raise ConnectionAbortedError('the download was slower than the server allows')
        guarded = getattr(self, '_guarded', False)
        wait = max(0.05, min(self.server.client_seconds, left)) if guarded else left
        if guarded:
            self.server.watch(self.connection, wait)
        before = self.connection.gettimeout()
        try:
            self.connection.settimeout(wait)           # as in the reader: a send that is stuck ends on Windows too
            inner.write(chunk)
        except (TimeoutError, OSError):
            raise ConnectionAbortedError('the client did not take the download') from None
        finally:
            try:
                self.connection.settimeout(before)
            except OSError:
                pass
            if guarded:
                self.server.unwatch(self.connection)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/files')
    def tasks_files_list(self, ctx):
        project = self._files_project(ctx)
        self._project(ctx, CAP_READ)
        actor = self._files_actor(ctx)
        row = self._files_row(ctx)
        may = self._files_coordinates(ctx) or task_files.is_assignee(row, actor)
        try:
            items = task_files.listing(project, ctx.params['tid'], may)
        except task_files.Refusal as refusal:
            raise files_error(refusal) from None
        return 200, {'task': ctx.params['tid'], 'items': items, 'total': len(items), 'may_download': may}

    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/files', stream=True)
    def tasks_file_put(self, ctx):
        action = 'tasks.files.upload'
        try:
            if self.headers.get('Transfer-Encoding') is not None:
                # r2 (review s4b): http.server does not undo chunked framing, which would be stored as the file.
                raise HttpError(400, 'invalid', 'An upload is sent with a Content-Length, not with Transfer-Encoding; '
                                'nothing was stored.')
            project = self._files_project(ctx)
            record, _role = self._project(ctx, CAP_TASKS)
            if isinstance(record, dict) and record.get('archived'):
                raise conflict('The project is archived: its files can still be downloaded, and no new file is '
                               'attached.')
            name = task_files.clean_name(ctx.query.get('name'))
            direction = task_files.check_direction(ctx.query.get('direction'))
            media_type = task_files.check_media_type(ctx.query.get('media_type'))
            note = task_files.check_text(ctx.query.get('note'), 'note', task_files.NOTE_LIMIT)
            expected = self.headers.get('X-Orchestra-SHA256')
            if expected is not None:
                expected = task_files.check_sha256(expected.strip().lower())
            said = self.headers.get('Content-Length')
            if said is None:
                raise HttpError(411, 'length_required', 'An upload needs a Content-Length: the server checks the '
                                'limits before it reads a byte.')
            try:
                length = int(said)
            except ValueError:
                length = -1
            if length < 0 or str(length) != said.strip():
                raise invalid('Invalid Content-Length')
            actor = self._files_actor(ctx)
            row = self._files_row(ctx)
            if direction == task_files.TO_WORKER:
                if not self._files_coordinates(ctx):
                    raise forbidden('Only the project\'s coordinators attach files to the worker.')
                operation, capability = 'coordinator-record', CAP_COORDINATE
            else:
                if not task_files.is_assignee(row, actor):
                    raise forbidden('Only the task\'s current assignee attaches files from the worker, and only '
                                    'while the task is open.')
                operation, capability = 'assignee-record', CAP_TASKS
            limit = self._files_limits()
            tid = ctx.params['tid']
            if expected is not None:
                # A retry of a file already attached under this name: answered from the record,
                # without reading the body again (design 3a: an idempotent 200).
                known = task_files.read_entry(project, tid, expected)
                if known is not None and known.get('withdrawn') is None and known['name'] == name and \
                        task_files.blob_size(project, expected) == known['bytes']:
                    return 200, dict(task_files.view(known), replayed=True)
            task_files.check_room(project, tid, length, limit)
            gate = self.transfers or DEFAULT_TRANSFERS
            key = self._files_key(ctx)
            gate.take(key, limit, ctx.params['pid'])
            try:
                self._continue()                        # r2 (s4c): only now may the client send the body
                sha256, size, created = task_files.receive(self._files_reader(length, limit), length, project, expected)
                self._body_unread = False
            finally:
                gate.give(key, ctx.params['pid'])
        except (HttpError, task_files.Refusal) as error:
            raise self._files_refused(ctx, action, error) from None
        payload = {'operation': operation, 'task_id': tid, 'sha256': sha256, 'name': name, 'bytes': size,
                   'media_type': media_type, 'direction': direction, 'note': note, 'actor': actor}
        ctx.body_hash = request_hash({key: value for key, value in payload.items() if key != 'actor'})

        def write():
            result = self.backend.invoke('files.record', ctx.principal, ctx.params['pid'], payload,
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=capability)
            return result, result
        try:
            status, result = self._mutate(ctx, action, ctx.params['pid'], write, capability=capability,
                                          status=201, serialize=False, canonical=True,
                                          reason=self._files_target(ctx, sha256))
        except HttpError as refused:
            # r2 (review s2): a record that was refused leaves no blob behind that this upload stored and no record
            # names. Only for a definite refusal: a 5xx that does not say nothing was done may follow a written
            # record (a retry settles it), so its blob stays.
            if created and (refused.status < 500 or getattr(refused, 'nothing_done', False)):
                # kittrial-5bb.380 (review of .370 r3): the refusal is the answer. A discard that fails (the lock not
                # free in time, a disk error) is said in the service's log and keeps the blob as an orphan, which
                # `admin.py task-files purge` removes once it is a day old; it never replaces the refusal.
                try:
                    self.backend.files_discard(ctx.params['pid'], sha256)
                except Exception as failed:
                    print('task files: the upload %s of request %s was refused and its stored bytes were kept (task-files '
                          'purge removes them after a day): %s' % (sha256, ctx.request_id,
                                                                   ascii(str(failed)[:200]) or type(failed).__name__),
                          file=sys.stderr, flush=True)
            raise
        if isinstance(result, dict):
            result = dict(result)
            status = result.pop('status', status)
        return status, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/files/(?P<sha>[0-9a-f]{64})')
    def tasks_file_get(self, ctx):
        action = 'tasks.files.download'
        sha256 = ctx.params['sha']
        try:
            project = self._files_project(ctx)
            record, _role = self._project(ctx, CAP_READ)
            actor = self._files_actor(ctx)
            row = self._files_row(ctx)
            coordinates = self._files_coordinates(ctx)
            if not (coordinates or task_files.is_assignee(row, actor)):
                raise forbidden('Only the task\'s current assignee and the project\'s coordinators may download its '
                                'files.')
            if isinstance(record, dict) and record.get('archived') and not coordinates:
                # r2 (review s4d): an archived project is read-only; its files stay downloadable by its coordinators.
                raise forbidden('The project is archived: only its coordinators download its files.')
            limit = self._files_limits()
            entry, handle = task_files.download_target(project, ctx.params['tid'], sha256)
        except (HttpError, task_files.Refusal) as error:
            raise self._files_refused(ctx, action, error, sha256) from None
        gate = self.transfers or DEFAULT_TRANSFERS
        key = self._files_key(ctx)
        try:
            with handle:
                size = entry['bytes']
                try:
                    gate.take(key, limit, ctx.params['pid'])
                except HttpError as error:
                    raise self._files_refused(ctx, action, error, sha256) from None
                sent = 0
                ends = time.monotonic() + task_files.transfer_seconds(size, limit)
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', task_files.OPAQUE)
                    self.send_header('Content-Length', str(size))
                    self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(entry['name'], safe=''))
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Security-Policy', "sandbox; default-src 'none'")
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('X-Orchestra-SHA256', sha256)
                    self._send_hsts()
                    self.send_header('X-Request-Id', getattr(self, '_current_request_id', '') or '')
                    self.end_headers()
                    while sent < size:
                        chunk = handle.read(min(task_files.STEP, size - sent))
                        if not chunk:
                            break
                        self._files_send(chunk, ends)
                        sent += len(chunk)
                except ConnectionError:
                    self.close_connection = True       # cut off, too slow, or gone: nothing more to answer
                finally:
                    gate.give(key, ctx.params['pid'])
                    self._files_audit(ctx, action, 'committed' if sent == size else 'interrupted',
                                      '%s bytes=%d' % (self._files_target(ctx, sha256), sent), actor=actor)
                if sent != size:
                    self.close_connection = True
        except HttpError:
            raise
        return None, None

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/files/(?P<sha>[0-9a-f]{64})/withdraw')
    def tasks_file_withdraw(self, ctx):
        action = 'tasks.files.withdraw'
        sha256 = ctx.params['sha']
        try:
            project = self._files_project(ctx)
            record, _role = self._project(ctx, CAP_TASKS)
            if isinstance(record, dict) and record.get('archived'):
                raise conflict('The project is archived: its files are read-only and none is withdrawn.')
            payload = dict(ctx.payload or {})
            refuse_unknown_fields(payload, ('reason',), 'A file withdrawal')
            reason = task_files.check_text(payload.get('reason'), 'reason', task_files.REASON_LIMIT, required=True)
            actor = self._files_actor(ctx)
            self._files_row(ctx)
            entry = task_files.read_entry(project, ctx.params['tid'], sha256)
            if entry is None or entry.get('withdrawn') is not None:
                raise not_found('No such file on this task.')
            if self._files_coordinates(ctx):
                operation, capability = 'coordinator-withdraw', CAP_COORDINATE
            elif entry.get('by') == actor:
                operation, capability = 'author-withdraw', CAP_TASKS
            else:
                raise forbidden('A file is withdrawn by the one who attached it, or by a coordinator.')
        except (HttpError, task_files.Refusal) as error:
            raise self._files_refused(ctx, action, error, sha256) from None
        body = {'operation': operation, 'task_id': ctx.params['tid'], 'sha256': sha256, 'reason': reason,
                'actor': actor}

        def write():
            result = self.backend.invoke('files.withdraw', ctx.principal, ctx.params['pid'], body,
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=capability)
            return result, result
        status, result = self._mutate(ctx, action, ctx.params['pid'], write, capability=capability,
                                      status=200, serialize=False, canonical=True,
                                      reason=self._files_target(ctx, sha256))
        if isinstance(result, dict):
            result = dict(result)
            status = result.pop('status', status)
        return status, result

    def require(self, ctx, capability):
        """Authorize one read/route capability against live authority."""
        return self.service.check_authority(ctx.principal, ctx.params.get('pid'), capability)

    def _project(self, ctx, capability=CAP_READ):
        return self.service.check_authority(ctx.principal, ctx.params['pid'], capability)

    #: What an answer is when a backend (a test double from an older slice) has no
    #: claiming setting: absent means open, today's behaviour.
    CLAIMING_DEFAULT = {'claiming': 'open', 'set_by': None, 'set_at': None,
                        'set_via': None, 'previous': None, 'history': [],
                        'source': 'default'}

    def _claiming_state(self, project_id):
        """The project's claiming setting from the backend, or the open default.

        Both shipped backends implement ``claiming_state`` (kittrial-5bb.352); a backend
        double that predates it keeps answering as an open project rather than failing
        a listing, exactly as a project with no file reads.
        """
        read = getattr(self.backend, 'claiming_state', None)
        if read is None:
            return dict(self.CLAIMING_DEFAULT)
        return read(project_id)

    def _claiming_mode(self, project_id):
        return self._claiming_state(project_id)['claiming']

    def _task_payload(self, ctx):
        payload = dict(ctx.payload or {})
        payload['task_id'] = ctx.params['tid']
        return payload

    #: ONE TABLE of what each write route takes from the caller's own body (kittrial-5bb.183
    #: item 2). A field outside the route's set is refused with 422, naming it and the fields
    #: the route takes, exactly as a task change has since kittrial-5bb.181 -- it is never
    #: dropped silently and never answered 5xx or "may have committed". Each set is what the
    #: named clients in this repository send today (web/js/api.js, http_client.py), so no
    #: client that exists here is broken by the refusal. These sets are the whole table; the
    #: review, project PATCH, onboarding and proposals routes already refuse the same way.
    TASK_CREATE_FIELDS = ('title', 'description', 'priority', 'attachments', 'actor')
    TASK_CLAIM_FIELDS = ('actor',)
    #: What a claim release takes from the caller's own body (kittrial-5bb.301): a short
    #: reason, and nothing else. The holder is derived from the authenticated caller, never
    #: submitted, so no caller can release somebody else's claim by naming them.
    TASK_RELEASE_FIELDS = ('reason',)
    #: The longest reason a release may carry. Short by design: it is a note to the next
    #: holder, not a message.
    CLAIM_RELEASE_REASON_MAX = 120
    CHECKPOINT_BODY_FIELDS = EndpointBackend.CHECKPOINT_FIELDS + ('actor',)
    MEMBER_FIELDS = ('role',)
    CREDENTIAL_ISSUE_FIELDS = ('label', 'scopes', 'actor', 'allow_actor', 'allow_actor_reason')
    AGENT_CREATE_FIELDS = ('name', 'tool', 'working_directory', 'machine', 'notes',
                           'projects', 'scopes', 'owner', 'claim_limit', 'role')
    AGENT_UPDATE_FIELDS = ('name', 'tool', 'working_directory', 'machine', 'notes',
                           'projects', 'enabled', 'claim_limit', 'role')
    #: The agent's OWN route (``PATCH /v1/agents/me``, kittrial-5bb.302, .329) takes the
    #: fields an agent may change on its own record: its claim limit (downward only),
    #: its working_directory (when the extension or agent records the open workspace folder),
    #: and its role ('worker' or 'reviewer').
    AGENT_ME_UPDATE_FIELDS = ('claim_limit', 'working_directory', 'role')
    AGENT_CREDENTIAL_FIELDS = ('label', 'scopes')
    ACCOUNT_CREATE_FIELDS = ('username', 'display_name')

    #: What a task change may change, and what else its body may carry: ``version`` is the
    #: in-process backend's check that the task was not changed meanwhile (the endpoint backend
    #: has none and does not read it), ``actor`` the attribution label, bound as on a claim.
    TASK_CHANGES = ('title', 'description', 'status', 'priority')
    TASK_CHANGE_FIELDS = TASK_CHANGES + ('version', 'actor')

    def _task_change(self, ctx):
        """The body of a task change, or the refusal that says what is wrong with it.

        Refused here, before the idempotency key is reserved and before anything is sent to
        the backend, so the same key serves the corrected request (kittrial-5bb.181). A field
        the route does not take was dropped silently, and a body that changed nothing made
        the endpoint backend send bd an update with nothing in it: bd answers that with a
        sentence, the sentence was read as an answer that could not be understood, and the
        caller was told that the change may have been made and to try again with the same
        key, for ever.
        """
        payload = self._task_payload(ctx)
        refuse_unknown_fields({name: value for name, value in payload.items() if name != 'task_id'},
                              self.TASK_CHANGE_FIELDS, 'A task change', self.TASK_CHANGES)
        takes = 'A task change takes: %s' % ', '.join(self.TASK_CHANGES)
        if all(payload.get(name) is None for name in self.TASK_CHANGES):
            raise invalid('Nothing to change. %s' % takes, {'takes': list(self.TASK_CHANGES)})
        title, description, status, priority = (payload.get(name) for name in self.TASK_CHANGES)
        if title is not None:
            self._task_title(title)
        if description is not None and not isinstance(description, str):
            raise invalid('Task description must be text')
        if status is not None and status not in ('open', 'closed'):
            raise invalid('Task status must be open or closed')
        if priority is not None and (type(priority) is not int or not 0 <= priority <= 4):
            raise invalid('Task priority must be an integer 0-4')
        self._bind_task_actor(ctx, payload)
        return payload

    #: bd's own limit for a title (bd 1.2.2: "title must be 500 characters or less"). A longer
    #: one on a change is not even refused by bd's check: the database refuses it.
    TASK_TITLE_MAX = 500

    def _task_title(self, title):
        """A task's title is text, not blank, and no longer than bd takes; else the refusal."""
        # Not blank: at least one character that can be seen. A title of spaces, or of one control
        # character (review of kittrial-5bb.185: bd stores it), is no title.
        if not isinstance(title, str) or not any(ch.isprintable() and not ch.isspace() for ch in title):
            raise invalid('Task title must be text and not empty')
        if len(title) > self.TASK_TITLE_MAX:
            raise invalid('Task title must be %d characters or less (it has %d)' % (self.TASK_TITLE_MAX, len(title)))

    def _bind_task_actor(self, ctx, payload):
        """An ``actor`` in the body of a task write is the caller's own label or it is refused.

        The canonical write runs under ``payload['actor']`` when there is one. Task create and
        task change handed the caller's value over as it came, so a member could have a task
        made, or changed, under any name that does not have the shape of a web account: a host
        session's, an operator's (kittrial-5bb.181). Bound as a claim, a checkpoint and a
        review bind it.
        """
        if 'actor' in payload:
            bound = self.service.bind_actor(ctx.principal, payload.pop('actor'))
            if bound is not None:
                payload['actor'] = bound

    def _paged(self, ctx, items, limit, state, **extra):
        """One bounded page of an in-memory list, with a cursor bound to this query."""
        offset = state['o']
        body = {'items': items[offset:offset + limit], 'total': len(items)}
        body['next_cursor'] = (make_cursor(ctx.principal, ctx.params.get('pid'), ctx.query,
                                           offset + limit, key=self.service.cursor_key())
                               if offset + limit < len(items) else None)
        body.update(extra)
        return body

    def _task_view(self, task, names, tasks=None, project_id=None, claiming=None):
        """A task row plus presentation fields (assignee name, next action, requirement
        marker and claimability). Additive: every field a route already returned stays.

        The requirement marker and the claimability answer come from the same two
        predicates the work queue, the agent attention list and the claim route use
        (``work.requirement_record`` / ``work.claimable``), so a reader that sees the
        tasks list cannot disagree with the queue about what is claimable work
        (kittrial-5bb.277). The list route feeds its rows through here, so a web Tasks
        page can mark a requirement record plainly instead of offering it as a task.

        ``claiming`` is the project's claiming setting (kittrial-5bb.352), read fresh
        when the caller does not pass it; under ``assigned`` an unassigned row is not
        claimable and its next action names the coordinators instead of inviting a claim.
        """
        from work import claimable, requirement_record
        if claiming is None:
            claiming = (self._claiming_mode(project_id)
                        if project_id else 'open')
        view = dict(task)
        assignee = view.get('assignee')
        view['assignee_name'] = names.get(assignee, assignee) if assignee else None
        view['next_action'] = next_action(view, claiming=claiming)
        view['requirement_record'] = requirement_record(view.get('labels'))
        from work import dependency_index, open_dependency_ids
        # A list view passes its snapshot; a single-task view passes none, and the
        # dependency entries themselves carry the other end's status (bd show),
        # so the block is visible with no extra read (kittrial-5bb.293 rev4).
        snapshot = tasks if isinstance(tasks, list) else None
        view['open_dependencies'] = open_dependency_ids(task, snapshot,
            index=dependency_index(snapshot) if snapshot is not None else None)
        view['claimable'] = claimable(view.get('issue_type'), view.get('status'), assignee,
                                      view.get('contribution_id'), view.get('review_state'),
                                      labels=view.get('labels'),
                                      open_dependencies=view['open_dependencies'] or None,
                                      claiming=claiming)
        return view

    def _task_views(self, tasks, project_id=None):
        names = self._actor_names([t.get('assignee') for t in tasks], project_id)
        claiming = (self._claiming_mode(project_id)
                    if project_id else 'open')
        return [self._task_view(t, names, tasks=tasks, claiming=claiming) for t in tasks]

    def _actor_names(self, actors, project_id=None):
        """Display names, as :meth:`Service.actor_names` gives them, and a host session's too.

        A session actor of the project (``session-<uuid>``) that is no account or agent is
        shown by its registered session name and the principal it is mapped to, as the host
        reports them in the read-only ``host-coordination`` action (kittrial-5bb.198), instead
        of by its id. The id stays in the answer beside the name. With no project, no host, an
        older host or a registry that cannot be read, the id is shown as before.
        """
        names = self.service.actor_names(actors)
        waiting = [actor for actor in actors if isinstance(actor, str) and names.get(actor) == actor
                   and actor_names.SESSION_ACTOR.fullmatch(actor)]
        read = getattr(self.backend, 'host_coordination', None)
        if project_id is None or not waiting or read is None:
            return names
        try:
            sessions = read(project_id).get('sessions') or []
        except HttpError:
            return names
        known = {item.get('actor'): item for item in sessions if isinstance(item, dict)}
        for actor in waiting:
            shown = session_label(known.get(actor))
            if shown:
                names[actor] = shown
        return names

    #: Said when a recommendation is refused because it is not independent of the author.
    NOT_INDEPENDENT = ('A recommendation must be independent of the author: the contribution\'s author, the '
                       'task\'s assignee, the person who owns the agent that delivered it, and that person\'s '
                       'other agents cannot recommend it')

    #: The same with the installation's setting on (kittrial-5bb.199): a worker credential is a reason too,
    #: in both directions -- work delivered under your credential, and a credential you issued recommending
    #: your own work (round 2, N6).
    NOT_INDEPENDENT_PARTY = (NOT_INDEPENDENT + '; and on this server neither can the account that issued the worker '
                             'credential it was delivered under, nor that account\'s agents, nor a worker credential '
                             'issued by the contribution\'s author or by the task\'s assignee')

    #: Said to an approver of the party the work belongs to (the setting of kittrial-5bb.199). Which of
    #: the three is said depends on what is true; each ends with OWN_PARTY_NEXT.
    OWN_PARTY = ('This contribution was delivered by your own party: you, one of your agents, or a name that a '
                 'worker credential you issued holds or has held.')
    OWN_PARTY_SHARED = ('This contribution was delivered under %s, a name that worker credentials of more than one '
                        'account have held. One of those accounts is yours, and nothing on the record says which '
                        'credential wrote it, so it counts as the work of each of them.')
    OWN_PARTY_ASSIGNEE = ('This task is assigned to your own party (you, one of your agents, or a name of a worker '
                          'credential you issued). Another account delivered the contribution, but nobody approves '
                          'work on a task their own party holds.')
    OWN_PARTY_NEXT = (' Somebody of another party who may approve here must approve it: another owner of this '
                      'project, or a superuser. If there is nobody else, a superuser makes a second account an owner '
                      'of this project (Members) and that person approves; where the only owner is also the only '
                      'superuser, that second account has to be made first.')

    #: The same four, said to an AGENT that holds the coordinator grant (kittrial-5bb.209). It is judged
    #: by party whatever the installation's setting for people says.
    OWN_PARTY_AGENT = (
        'This contribution was delivered by this agent\'s own party: its account, one of that account\'s agents, or a '
        'name that a worker credential of that account holds or has held.',
        'This contribution was delivered under %s, a name that worker credentials of more than one account have held. '
        'One of those accounts is this agent\'s, and nothing on the record says which credential wrote it, so it '
        'counts as the work of each of them.',
        'This task is assigned to this agent\'s own party (its account, one of that account\'s agents, or a name of a '
        'worker credential of that account). Another account delivered the contribution, but nobody approves work on '
        'a task their own party holds.',
        ' An agent never approves work of its own party, whatever this server\'s setting for people says: somebody of '
        'another party who may approve here must approve it (another coordinator or owner of this project, or a '
        'superuser).')

    def _own_party_refusal(self, names, assignee, author, project, agent=False):
        """The sentence for an approver whose party the work is, or None when it is not.

        ``names`` are the approver: the name it acts under and the account behind the request.
        The author is looked at first; the assignee only when the author is another party.
        """
        own, shared_name, held, what_next = self.OWN_PARTY_AGENT if agent else (
            self.OWN_PARTY, self.OWN_PARTY_SHARED, self.OWN_PARTY_ASSIGNEE, self.OWN_PARTY_NEXT)
        if not self._independent_all(names, [author], project, agent):
            shared = len(self.service.actor_parties(author, project, agent)) > 1 if isinstance(author, str) else False
            return (shared_name % author if shared else own) + what_next
        if not self._independent_all(names, [assignee], project, agent):
            return held + what_next
        return None

    def _independent_all(self, names, parties, project=None, full=False):
        """Whether EVERY one of ``names`` is of another party than all of ``parties``."""
        names = [name for name in names if isinstance(name, str) and name]
        return len(self._independent(names, parties, project, full)) == len(names)

    def _independent(self, actors, parties, project=None, full=False):
        """The ``actors`` who are a different PERSON from every one of ``parties``.

        The canonical rule compares actor names, and a person and their agents are
        different names. The web service knows who owns each agent, so here a person,
        their agent, and two agents of one person are all the same party
        (kittrial-5bb.115). With the installation's setting on, work under a worker
        credential belongs to the account that issued it as well (``Service.actor_parties``;
        ``project`` says where the work of ``parties`` was written).
        """
        taken = set()
        for party in parties:
            if isinstance(party, str) and party:
                taken |= self.service.actor_parties(party, project, full)
        return [actor for actor in actors
                if isinstance(actor, str) and not (self.service.actor_parties(actor, project, full) & taken)]

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

    def _independent_queue(self, read, project=None):
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
                kept = self._independent(names, [author], project)
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

    def _own_party_recommended(self, row, actor, project=None):
        """Whether ``actor``'s own PERSON has already recommended the row's delivery (kittrial-5bb.154).

        The one answer to "already recommended?" for an agent's actions and for My work: the
        actor itself, the person who owns it, or another agent of that person. Independence
        is by person everywhere else in the review rules, and a second agent of the same
        person adds no independent reading. Read from every name on the row, counted or not.
        """
        names = self._every_recommender(row)
        return len(self._independent(names, [actor], project)) != len(names)

    def _review_queue(self, project_id, shared=False):
        """One review-queue read of a project per request (and, when ``shared`` and the
        backend allows it, reused for ``READ_CACHE_SECONDS`` by the same principal).

        Authorization is never cached: callers re-check live authority first.
        """
        return self._cached_read('queue', project_id,
                                 lambda: self._independent_queue(self.backend.review_queue(project_id), project_id), shared)

    def _claim_candidates(self, project_id, exclude, limit=None):
        """Bounded claimable rows to fall through to when a claim is refused.

        The same rows the canonical ``work --claimable`` view lists (kittrial-5bb.6
        deliverable b), taken from this request's bounded queue read, in the queue's own
        order and excluding the task just refused. Advisory only: a candidate is not a
        reservation and the next claim is still atomic. A read failure supplies no
        candidates and never changes the refusal.

        Returns ``(candidates, truncated)``: at most ``limit`` (default
        :data:`CLAIM_CANDIDATES_LIMIT`, ten) rows, and whether the read held more
        claimable rows than the cap allowed, so the caller can say the list was cut
        (kittrial-5bb.6 review item 4).
        """
        try:
            read = self._review_queue(project_id)
        except (HttpError, ValueError):
            return [], False
        if not isinstance(read, dict):
            return [], False
        bound = CLAIM_CANDIDATES_LIMIT if limit is None else limit
        found = []
        for task in read.get('items') or []:
            if not isinstance(task, dict) or task.get('id') == exclude:
                continue
            if task.get('claimable') is True:
                found.append({'task': task.get('id'), 'title': task.get('title')})
        return found[:bound], len(found) > bound

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
    # -- project rooms (kittrial-5bb.385, docs/PROJECT_ROOMS_DESIGN.md section 10) ---------------
    #: Said when the backend has no host project folder to keep rooms beside.
    ROOMS_UNAVAILABLE = 'Rooms are not available on this server.'

    def _rooms_project(self, ctx):
        place = getattr(self.backend, 'files_project', None)
        project = place(ctx.params['pid']) if place is not None else None
        if project is None:
            raise not_implemented(self.ROOMS_UNAVAILABLE)
        return project

    def _rooms_limits(self):
        read = getattr(self.backend, 'rooms_limits', None)
        if read is None:
            return dict(rooms.LIMIT_DEFAULTS)
        warnings = []
        try:
            values = read(warnings=warnings)
        except TypeError:
            values = read()
        for line in warnings:
            print('rooms: ' + ascii(line[:300]), file=sys.stderr, flush=True)
        return values

    def _rooms_lock(self, project):
        # Imported here, not at module top: http_service must import on Windows (the kit's
        # tests import it there), and the project lock is a Linux runtime path, as in
        # admin.py and the task-file routes (kittrial-5bb.352's CI lesson).
        return project_room_lock(project)

    def _rooms_actor(self, ctx, role):
        user = self.service.state['users'].get(ctx.principal.user_id) or {}
        name = user.get('display_name') or user.get('username') or ctx.principal.user_id
        return {'id': ctx.principal.user_id, 'name': str(name)[:200], 'role': role}

    def _rooms_refusal(self, error):
        return HttpError(error.status, error.code, str(error))

    def _rooms_read_allowed(self, ctx):
        """Refuse a room read from an agent credential (review s3, design R1).

        Room text must not reach task-capable credentials. This slice already refuses a
        credential write; the read routes (list, read and export) need the same refusal,
        or a worker credential granted CAP_READ can read every room.
        """
        if ctx.principal.via == 'credential':
            raise forbidden('Rooms are for people in this slice: an agent credential cannot read a room.')
        return self._project(ctx, CAP_READ)

    def _rooms_write(self, ctx, action, capability, fn, *, status=200, reason=None,
                     stored=None):
        # Rooms are for people in this slice: the new rooms.post capability is on no
        # credential scope, and a coordinator grant must not become a room-write grant.
        if ctx.principal.via == 'credential':
            raise forbidden('Rooms are for people in this slice: an agent credential cannot write to a room.')
        record, _role = self._project(ctx, CAP_READ)
        if isinstance(record, dict) and record.get('archived'):
            raise conflict('The project is archived: its rooms can be read and exported, and no room is written.')
        def write():
            project = self._rooms_project(ctx)
            try:
                with self._rooms_lock(project):
                    result = fn(project)
            except rooms.Refusal as refusal:
                raise self._rooms_refusal(refusal) from None
            # Review b1: a post's stored answer must not keep the message text for 24 h.
            # The route can replace what is kept for a retry with a metadata-only view;
            # the caller still receives the full result now.
            return result, (stored(result) if stored is not None else result)
        return self._mutate(ctx, action, ctx.params['pid'], write, capability=capability,
                            serialize=False, status=status, reason=reason)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/rooms')
    def rooms_list(self, ctx):
        project = self._rooms_project(ctx)
        record, role = self._rooms_read_allowed(ctx)
        pairs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(pairs) - {'archived'} or any(len(values) != 1 for values in pairs.values()):
            raise invalid('Use only one archived flag')
        archived = pairs.get('archived', ['0'])[0] in ('1', 'true', 'yes')
        try:
            with self._rooms_lock(project):
                items = rooms.list_rooms(project, include_archived=archived)
        except rooms.Refusal as refusal:
            raise self._rooms_refusal(refusal) from None
        capabilities = self.service.capabilities_for(ctx.principal, ctx.params['pid'])
        return 200, {'items': items, 'total': len(items),
                     'can_post': CAP_ROOMS_POST in capabilities,
                     'can_coordinate': CAP_COORDINATE in capabilities}

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/rooms')
    def rooms_create(self, ctx):
        self._rooms_project(ctx)
        _record, role = self._project(ctx, CAP_COORDINATE)
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) != {'name'}:
            raise invalid('Send exactly: name')
        limits_values = self._rooms_limits()
        actor = self._rooms_actor(ctx, role)
        def write(project):
            return rooms.create(project, payload['name'], actor, limits_values)
        return self._rooms_write(ctx, 'rooms.create', CAP_COORDINATE, write, status=201,
                                 reason='room create')

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/rooms/(?P<rid>' + ID + r')')
    def rooms_read(self, ctx):
        project = self._rooms_project(ctx)
        self._rooms_read_allowed(ctx)
        pairs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(pairs) - {'since', 'limit'} or any(len(values) != 1 for values in pairs.values()):
            raise invalid('Use only one since and limit')
        try:
            since = int(pairs.get('since', ['0'])[0])
            limit = int(pairs.get('limit', ['100'])[0])
        except (ValueError, TypeError):
            raise invalid('Use an integer since and limit') from None
        try:
            with self._rooms_lock(project):
                return 200, rooms.read_messages(project, ctx.params['rid'], since, limit)
        except rooms.Refusal as refusal:
            raise self._rooms_refusal(refusal) from None

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/rooms/(?P<rid>' + ID + r')/messages')
    def rooms_post(self, ctx):
        self._rooms_project(ctx)
        _record, role = self._project(ctx, CAP_ROOMS_POST)
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) != {'text'}:
            raise invalid('Send exactly: text')
        limits_values = self._rooms_limits()
        actor = self._rooms_actor(ctx, role)
        operation_id = ctx.idempotency_key or ctx.request_id
        def write(project):
            return rooms.post(project, ctx.params['rid'], payload['text'], operation_id,
                              actor, limits_values)
        # Review b1: keep only the message identity for a retry, never its text. After the
        # message is moderated a replay must not answer with the original text from the
        # server's 24-hour idempotency record (or carry it in the state file and backups).
        def stored(result):
            return {key: result.get(key) for key in ('sequence', 'kind', 'created_at')}
        return self._rooms_write(ctx, 'rooms.post', CAP_ROOMS_POST, write, status=201,
                                 reason='room %s' % ctx.params['rid'], stored=stored)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/rooms/(?P<rid>' + ID + r')/archive')
    def rooms_archive(self, ctx):
        self._rooms_project(ctx)
        _record, role = self._project(ctx, CAP_COORDINATE)
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) - {'reason'}:
            raise invalid('A room archive takes only an optional reason')
        actor = self._rooms_actor(ctx, role)
        operation_id = ctx.idempotency_key or ctx.request_id
        def write(project):
            return rooms.archive(project, ctx.params['rid'], actor, operation_id,
                                 payload.get('reason'))
        return self._rooms_write(ctx, 'rooms.archive', CAP_COORDINATE, write,
                                 reason='room %s' % ctx.params['rid'])

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/rooms/(?P<rid>' + ID + r')/messages/(?P<seq>[0-9]{1,12})/moderate')
    def rooms_moderate(self, ctx):
        self._rooms_project(ctx)
        _record, role = self._project(ctx, CAP_COORDINATE)
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) != {'reason'}:
            raise invalid('Send exactly: reason')
        actor = self._rooms_actor(ctx, role)
        operation_id = ctx.idempotency_key or ctx.request_id
        sequence = int(ctx.params['seq'])
        def write(project):
            return rooms.moderate(project, ctx.params['rid'], sequence, payload['reason'],
                                  operation_id, actor, self._rooms_limits())
        return self._rooms_write(ctx, 'rooms.moderate', CAP_COORDINATE, write, status=201,
                                 reason='room %s message %d' % (ctx.params['rid'], sequence))

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/rooms/(?P<rid>' + ID + r')/export')
    def rooms_export(self, ctx):
        project = self._rooms_project(ctx)
        self._rooms_read_allowed(ctx)
        pairs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(pairs) - {'format'} or any(len(values) != 1 for values in pairs.values()):
            raise invalid('Use only one format')
        wanted = pairs.get('format', ['markdown'])[0]
        if wanted not in ('markdown', 'jsonl'):
            raise invalid('format must be markdown or jsonl')
        try:
            with self._rooms_lock(project):
                text = (rooms.export_markdown(project, ctx.params['rid']) if wanted == 'markdown'
                        else rooms.export_jsonl(project, ctx.params['rid']))
                room = rooms.read_room(project, ctx.params['rid'])
        except rooms.Refusal as refusal:
            raise self._rooms_refusal(refusal) from None
        return 200, {'room': rooms._room_view(room), 'format': wanted, 'text': text}

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
                     'user': dict(result['user'],
                                  password_min_length=self.service.password_min_length)}

    @route('GET', r'/v1/sessions/current')
    def sessions_whoami(self, ctx):
        principal = ctx.principal
        body = {'user': {'id': principal.user_id,
                         'username': self.service.username_of(principal.user_id),
                         'display_name': principal.display_name,
                         'superuser': principal.superuser,
                         # Additive (kittrial-5bb.363): the page states the server's real
                         # password rule, and asks twice wherever a password is set.
                         'password_min_length': self.service.password_min_length},
                'via': principal.via,
                'credential': principal.credential_id,
                'project': principal.credential_project,
                # How "New project" works here (kittrial-5bb.80): `create` (service-local
                # backend), `register` (a superuser registers an existing canonical
                # project) or `operator-only` (an operator creates it on the host).
                'project_create': (
                    'create' if getattr(self.backend, 'PROJECT_CREATE', 'create') == 'create' else
                    'register' if principal.superuser and principal.via != 'credential' else 'operator-only')}
        if principal.credential_id:
            expiry = self._credential_expiry_view(principal)
            if expiry is not None:
                body['credential_expiry'] = expiry
        # Additive (kittrial-5bb.330): a browser session also reads the installation's idle
        # window and this session's deadlines here. This read is the light authenticated
        # touch the agent set-up wizard keeps the session alive with, and the deadline is
        # what it warns before. A credential has no browser session and adds nothing.
        session_expiry = self.service.session_expiry_view(principal)
        if session_expiry is not None:
            body.update(session_expiry)
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

    def _credential_expiry_view(self, principal):
        """The caller's own credential expiry, or ``None`` when it has none to show.

        The same secret-free shape both ``GET /v1/sessions/current`` and
        ``GET /v1/agents/me`` answer (kittrial-5bb.294 item 1). A worker credential has
        no agent identity and no view, exactly as before; an agent credential reads its
        own stored deadline, capped at 366 days from issuance on read (item 4). This
        never looks at any other credential.
        """
        credential_id = getattr(principal, 'credential_id', None)
        credential = self.service.state['credentials'].get(credential_id) if credential_id else None
        if credential and credential.get('agent_id'):
            return self.service.agent_credential_expiry_view(credential)
        return None

    @route('POST', r'/v1/accounts')
    def accounts_create(self, ctx):
        payload = ctx.payload or {}
        return self._mutate(ctx, 'accounts.create', None,
                            lambda: self._account_created(ctx, payload), status=201,
                            capability=CAP_ACCOUNTS_ADMIN,
                            idempotent=bool(ctx.idempotency_key))

    def _account_created(self, ctx, payload):
        # A field the route does not take is refused, not dropped (kittrial-5bb.183 item 2).
        refuse_unknown_fields(payload, self.ACCOUNT_CREATE_FIELDS, 'An account creation')
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
        metadata = {}
        def disable():
            metadata.update(self._revocation_metadata(user_id=ctx.params['uid']))
            return self._account_disabled(ctx)
        return self._mutate(ctx, 'accounts.disable', None,
                            disable, audit_metadata=metadata,
                            capability=CAP_ACCOUNTS_ADMIN,
                            allow_self_user=ctx.params['uid'],
                            idempotent=bool(ctx.idempotency_key))

    def _account_disabled(self, ctx):
        result = self.service.disable_user(ctx.principal, ctx.params['uid'])
        return result, result

    def _revocation_metadata(self, *, agent_id=None, user_id=None):
        """IDs affected by disable, captured under the mutation lock before pruning."""
        agents = self.service.state['agents']
        agent_ids = [agent_id] if agent_id is not None else sorted(
            aid for aid, agent in agents.items() if agent.get('owner') == user_id)
        credential_ids = sorted(cid for cid, credential in self.service.state['credentials'].items()
                                if not credential.get('revoked') and
                                (credential.get('agent_id') == agent_id if agent_id is not None
                                 else credential.get('user_id') == user_id))
        return {'agent_ids': agent_ids, 'target_credential_ids': credential_ids}

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
                                  '(Register a project, with this name), or retire it on the server.',
                             finish=None, remove=None) for item in waiting]
        # Initialized on the host, not registered here and with no creation record (kittrial-5bb.198):
        # made before creation records existed, restored from a backup, or its record set aside.
        # From the read-only ``host-projects`` action; what cannot be read is said, not hidden.
        recorded = {item.get('project') for item in found['items'] + found['created']}
        unrecorded, listed = [], 'available'
        read = getattr(self.backend, 'host_projects', None)
        try:
            for project in read() if read else ():
                if project not in registered and project not in recorded:
                    unrecorded.append({'project': project, 'state': 'unrecorded-unregistered', 'by': None,
                                       'by_name': None, 'stage': None, 'started_at': None, 'stopped_at': None,
                                       'what': self.UNRECORDED, 'finish': None, 'remove': None})
        except HttpError as error:
            listed = 'unavailable' if 'Unknown action' in str(error.detail or '') else 'unknown'
        if read is None:
            listed = 'unavailable'
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
        return 200, {'items': items, 'unregistered': unregistered, 'unrecorded': unrecorded,
                     'host_projects': listed, 'server': server, 'host': 'available'}

    #: An initialized project on the host with no creation record and no registration here.
    UNRECORDED = ('It is a project on the server with no creation record (made before records existed, restored '
                  'from a backup, or its record was set aside), and it is not registered here. Register it as a '
                  'superuser (Register a project, with this name), or retire it on the server.')

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
            # While a host creation RUNS for this name it is already being made (kittrial-5bb.225
            # item 6): say that, not that an operator must create it first.
            import project_creation
            try:
                running = project_creation.running_name(self.backend.root)
            except (OSError, ValueError, AttributeError):
                running = None
            if running == project_id:
                raise conflict('Project %s is being created on this server now; register it when that '
                               'finishes.' % project_id)
            raise invalid('No canonical project %s exists on the coordination host. An operator creates it there '
                          'first (admin.py add-project %s), then you register it here.' % (project_id, project_id))

        def register():
            # The registration mark is written inside create_project's own save
            # (kittrial-5bb.255), so a kill or a failed save cannot leave the project
            # record on disk without it.
            self.service.create_project(principal, name, project_id, registered_by=principal.user_id)
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

    # -- the kit's documents for members (kittrial-5bb.226) --------------------------------
    #
    # How a worker finds its work, how a coordinator reaches it, and the prompts a worker is
    # started and woken with. The same files the client's ``docs`` serves, read by the same
    # reader, so the page shows what the kit serves. For any signed-in member, a viewer
    # included, and for an agent's credential; nothing of a project and no secret is in them.
    @route('GET', r'/v1/docs')
    def docs_list(self, ctx):
        import onboarding
        return 200, {'items': [{key: value for key, value in onboarding.member_document(KIT_DIRECTORY, name).items()
                                if key != 'text' and key != 'prompt'} for name in onboarding.WEB_DOCUMENTS],
                     'server_url': self.service.public_url or None}

    @route('GET', r'/v1/docs/(?P<name>[a-z][a-z-]{0,40})')
    def docs_get(self, ctx):
        import onboarding
        name = ctx.params['name']
        if name not in onboarding.WEB_DOCUMENTS:
            raise not_found('No such document. GET /v1/docs lists the documents this service serves.')
        try:
            document = onboarding.member_document(KIT_DIRECTORY, name)
        except (OSError, ValueError) as failed:
            raise HttpError(503, 'unavailable', 'The document %s could not be read from the installed kit (%s).'
                            % (name, type(failed).__name__))
        # The address this service is configured with, for the page to put into a prompt.
        # Never the request's Host header, which a client controls.
        return 200, dict(document, server_url=self.service.public_url or None)

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
            # What a failed effect save must put back (kittrial-5bb.255): a save that
            # raises would leave these in memory, and the next save of anything would
            # make an unaudited confirmation durable. Credential issuance rolls its own
            # record back the same way.
            before = {name: (name in record, record.get(name))
                      for name in ('confirmed_by', 'confirmed_at', 'registered_by')}
            record['confirmed_by'] = principal.user_id
            record['confirmed_at'] = now_iso(self.service._now())
            # Backfill the mark the register route writes: a record a superuser stands
            # behind stays usable even if that superuser is later demoted (kittrial-5bb.90).
            # A record that already names its registrant keeps it.
            record.setdefault('registered_by', principal.user_id)
            try:
                # A replayed success must describe a durable confirmation, including
                # the registration mark that survives the registrant's demotion.
                self.service.store.save()
            except BaseException:
                for name, (present, value) in before.items():
                    if present:
                        record[name] = value
                    else:
                        record.pop(name, None)
                raise
            result = {'id': project_id, 'name': record.get('name'), 'usable': True,
                      'confirmed_by': principal.user_id, 'confirmed_at': record['confirmed_at'],
                      'created_by': item['created_by'], 'members': item['members']}
            return result, result
        return self._mutate(ctx, 'projects.confirm', None, confirm, capability=CAP_ACCOUNTS_ADMIN,
                            reason='confirm ' + project_id)

    def _lifecycle_backend(self):
        if not hasattr(self.backend, 'lifecycle_read'):
            raise not_implemented('Lifecycle facts are unavailable on this server')

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/lifecycle')
    def lifecycle_get(self, ctx):
        self._project(ctx, CAP_READ)
        self._lifecycle_backend()
        result = self.backend.lifecycle_read(ctx.principal, ctx.params['pid'], ctx.params['tid'])
        result['current_actor'] = self.service.bind_actor(ctx.principal, None)
        return 200, result

    def _lifecycle_write(self, ctx, operation):
        import web_lifecycle
        # _mutate can replay a completed HTTP receipt before invoking its
        # callback. Even that read of an old success requires today's authority.
        self._project(ctx, CAP_COORDINATE)
        metadata = {}
        fields = web_lifecycle.FIELDS[operation] - {'task', 'dimension'}
        optional = web_lifecycle.OPTIONAL[operation]
        route_name = 'lifecycle.' + operation
        def write():
            self._lifecycle_backend()
            if ctx.idempotency_key is None:
                raise invalid('Send an Idempotency-Key and reuse it with the identical request after an uncertain outcome')
            payload = dict(ctx.payload or {})
            refuse_unknown_fields(payload, fields | optional, 'Lifecycle ' + operation)
            if set(payload) - optional != fields:
                raise invalid('Supply exactly ' + ', '.join(sorted(fields)))
            actor = self.service.bind_actor(ctx.principal, None)
            checked = dict(payload, operation=operation, task=ctx.params['tid'])
            if operation == 'fact':checked['dimension'] = ctx.params['dimension']
            try:
                web_lifecycle.validate(checked, actor, 'http-validation')
            except web_lifecycle.LifecycleError as error:
                raise invalid(str(error)) from None
            payload.update(task_id=ctx.params['tid'], actor=actor)
            if operation == 'fact':payload['dimension'] = ctx.params['dimension']
            result = self.backend.invoke(route_name, ctx.principal, ctx.params['pid'], payload,
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=CAP_COORDINATE)
            metadata.update({key: result.get(key) for key in
                             ('operation_id', 'actor', 'task', 'dimension', 'value', 'event_id',
                              'scope_token', 'mark_sha256')})
            with self.service.store.lock:
                existing = any(event.get('action') == route_name
                               and event.get('project_id') == ctx.params['pid']
                               and event.get('operation_id') == result['operation_id']
                               and event.get('outcome') == 'committed'
                               for event in self.service.state['audit'])
                if not existing:
                    event = self.service.audit(ctx.request_id, ctx.principal, route_name, 'committed',
                                               project_id=ctx.params['pid'])
                    event.update(metadata)
                try:
                    self.service.store.save()
                except (OSError, TimeoutError):
                    raise UncertainOutcome() from None
            return result, result
        def replay_check():
            if (operation == 'fact' and ctx.params['dimension'] in ('reviewed','integrated')
                    and hasattr(self.backend, 'lifecycle_replay_guard')):
                self.backend.lifecycle_replay_guard(ctx.principal, ctx.params['pid'],
                    ctx.params['tid'], ctx.params['dimension'])
        # The deterministic operation identity the backend will use, recorded on an
        # uncertain (124 at the endpoint / 503 here) answer so the ledger can name the
        # step and the identity to retry (kittrial-5bb.383).
        ledger_audit = None
        if ctx.idempotency_key is not None and hasattr(self.backend, '_result_key'):
            ledger_audit = dict(task=ctx.params['tid'],
                                dimension=('lifecycle-scope' if operation == 'scope'
                                           else ctx.params['dimension']),
                                operation_id=self.backend._result_key(
                                    ctx.principal, ctx.params['pid'], route_name,
                                    ctx.idempotency_key, ctx.route_target))
        return self._mutate(ctx, route_name, ctx.params['pid'], write, capability=CAP_COORDINATE,
                            status=201, serialize=False, canonical=True, audit_success=False,
                            replay_check=replay_check, audit_metadata=ledger_audit)

    #: Lifecycle audit actions whose last HTTP response the step ledger reports.
    LEDGER_ACTIONS = ('lifecycle.scope', 'lifecycle.fact')

    def _ledger_uncertain(self, project_id, task_id, steps):
        """Mark a not-yet-committed step whose last HTTP answer was uncertain.

        The canonical native rows stay authoritative: a step with a native fact keeps
        its `committed` state and fact id (and reports the last response beside it).
        A step with no native write but a lifecycle audit `unknown` outcome reads
        `unknown`, carrying the deterministic operation identity the backend used, so
        a coordinator retries that identity instead of minting a new one
        (kittrial-5bb.383). Audit rows without the task/dimension metadata added for
        this ledger are skipped: they cannot be attributed to a step.
        """
        latest = {}
        for event in self.service.state['audit']:
            if (event.get('project_id') != project_id
                    or event.get('action') not in self.LEDGER_ACTIONS
                    or event.get('task') != task_id
                    or not isinstance(event.get('operation_id'), str)):
                continue
            if event.get('action') == 'lifecycle.scope':
                step = 'scope'
            else:
                step = event.get('dimension')
                if step not in ('reviewed', 'integrated'):
                    continue
            latest[step] = event
        for row in steps:
            event = latest.get(row.get('step'))
            if event is None:
                continue
            row['response_state'] = event.get('outcome')
            if row.get('state') != 'committed' and event.get('outcome') == 'unknown':
                row['state'] = 'unknown'
                row['operation_id'] = event['operation_id']
        return steps

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/integration-check')
    def integration_check_get(self, ctx):
        """The read-only merge check (kittrial-5bb.374): project read, no write."""
        self._project(ctx, CAP_READ)
        if not hasattr(self.backend, 'integration_check'):
            raise not_implemented('The integration check is unavailable on this server')
        refuse_unknown_fields(ctx.query, ('target',), 'Integration check query')
        target = ctx.query.get('target')
        result = self.backend.integration_check(ctx.principal, ctx.params['pid'],
                                                ctx.params['tid'], target)
        result['next'] = self._ledger_uncertain(ctx.params['pid'], ctx.params['tid'],
                                                result.get('next') or [])
        result['current_actor'] = self.service.bind_actor(ctx.principal, None)
        return 200, result

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/lifecycle/scope')
    def lifecycle_scope(self, ctx):
        return self._lifecycle_write(ctx, 'scope')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/lifecycle/facts/(?P<dimension>[a-z-]+)')
    def lifecycle_fact(self, ctx):
        return self._lifecycle_write(ctx, 'fact')

    # -- release query and one release group (kittrial-5bb.378) --------------------
    #
    # Slice 2 of docs/design/WEB_COORDINATOR_INTEGRATION.md, section 4. The server never
    # chooses release membership (it has no checkout): the caller resolves targets with
    # git and sends ONE group, at most 200 targets; the endpoint re-verifies every named
    # target against its own export and skips a target that is not integrated there. The
    # query is the read-only pre-flight: the host-issued reverted set and the recorded
    # live releases for one environment, writing nothing.
    def _release_backend(self, route):
        if not hasattr(self.backend, 'ROUTES') or route not in self.backend.ROUTES:
            raise not_implemented('Release facts are unavailable on this server')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/releases/query')
    def release_query(self, ctx):
        import web_release
        # A coordinator read: the query writes nothing and discloses only the reverted
        # set and the environment's live releases this project already knows.
        self._project(ctx, CAP_COORDINATE)
        try:
            _, fields = web_release.fields(dict(ctx.payload or {}, operation='query'))
        except web_release.ReleaseError as error:
            raise invalid(str(error)) from None
        self._release_backend('release.query')
        result = self.backend.invoke('release.query', ctx.principal, ctx.params['pid'], fields,
                                     None, target=ctx.route_target, capability=CAP_COORDINATE)
        return 200, result

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/releases')
    def release_deploy(self, ctx):
        import web_release
        # The project's release authority AND coordination, checked live (design section
        # 4): an owner or superuser holds both. An agent reaches it only with the project's
        # release grant AND its coordinator grant, because a release is a decision plus a
        # coordination step. A coordinator alone, or a release grant alone, is refused
        # before the body is even read. An archived project records nothing new.
        project, _role = self._project(ctx, CAP_COORDINATE)
        self._project(ctx, CAP_RELEASE)
        if isinstance(project, dict) and project.get('archived'):
            raise conflict('This project is archived; release facts cannot be recorded')
        try:
            _, fields = web_release.fields(dict(ctx.payload or {}, operation='deploy'))
        except web_release.ReleaseError as error:
            raise invalid(str(error)) from None
        self._release_backend('release.deploy')
        metadata = {}

        def write():
            if ctx.idempotency_key is None:
                raise invalid('Send an Idempotency-Key and reuse it with the identical request '
                              'after an uncertain outcome')
            result = self.backend.invoke('release.deploy', ctx.principal, ctx.params['pid'], fields,
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=CAP_RELEASE)
            metadata.update({key: result.get(key) for key in
                             ('operation_id', 'release_id', 'environment', 'groups_completed',
                              'groups_total')})
            # The audit is a shared capped list: keep counts and only the first few ids of
            # the up-to-200 skipped/flags rows. The answer itself still carries every row.
            skipped = result.get('skipped') or []
            flags = result.get('flags') or []
            metadata.update(skipped_count=len(skipped),
                            skipped_first=[row.get('task') for row in skipped[:5]],
                            flags_count=len(flags),
                            flags_first=[row.get('task') for row in flags[:5]])
            with self.service.store.lock:
                existing = any(event.get('action') == 'releases.deploy'
                               and event.get('project_id') == ctx.params['pid']
                               and event.get('operation_id') == result.get('operation_id')
                               and event.get('outcome') == 'committed'
                               for event in self.service.state['audit'])
                if not existing:
                    event = self.service.audit(ctx.request_id, ctx.principal, 'releases.deploy', 'committed',
                                               project_id=ctx.params['pid'])
                    event.update(metadata)
                try:
                    self.service.store.save()
                except (OSError, TimeoutError):
                    raise UncertainOutcome() from None
            return result, result
        return self._mutate(ctx, 'releases.deploy', ctx.params['pid'], write,
                            capability=(CAP_COORDINATE, CAP_RELEASE), status=201, serialize=False,
                            canonical=True, audit_success=False)

    def _merge_slot_backend(self):
        if not hasattr(self.backend, 'merge_slot_read'):
            raise not_implemented('Merge-slot coordination is unavailable on this server')

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/merge-slot')
    def merge_slot_get(self, ctx):
        self._project(ctx, CAP_READ)
        self._merge_slot_backend()
        result = self.backend.merge_slot_read(ctx.principal, ctx.params['pid'])
        result['current_actor'] = self.service.bind_actor(ctx.principal, None)
        def allowed(capability):
            try:
                self.service.check_authority(ctx.principal, ctx.params['pid'], capability)
                return True
            except HttpError:
                return False
        result['can_coordinate'] = allowed(CAP_COORDINATE)
        result['can_force_release'] = allowed(CAP_PROJECT_ADMIN) and ctx.principal.via == 'session'
        # The holder as people know it (kittrial-5bb.198): an account's or agent's name, or a host
        # session's name and principal. `holder` stays the exact name a forced release compares.
        holder = result.get('holder')
        result['holder_name'] = self._actor_names([holder], ctx.params['pid']).get(holder) if isinstance(holder, str) else None
        return 200, result

    def _merge_slot_write(self, ctx, operation):
        capability = CAP_PROJECT_ADMIN if operation in ('create', 'force-release') else CAP_COORDINATE
        metadata = {}
        fields = {'acquire': ('task_id', 'target'), 'release': (),
                  'force-release': ('expected_holder', 'reason'), 'create': ()}[operation]
        # expected_task: the task the page saw held (kittrial-5bb.268 item 4); optional.
        optional = ('expected_task',) if operation == 'force-release' else ()
        def write():
            self._merge_slot_backend()
            if ctx.idempotency_key is None:
                raise invalid('Send an Idempotency-Key and reuse it with the identical request after an uncertain outcome')
            payload = dict(ctx.payload or {})
            refuse_unknown_fields(payload, fields + optional, 'Merge-slot ' + operation)
            if set(payload) - set(optional) != set(fields):
                raise invalid('Supply exactly ' + (', '.join(fields) or 'an empty body')
                              + (' (and optionally ' + ', '.join(optional) + ')' if optional else ''))
            from web_merge_slot import validate, SlotError
            checked = dict(payload, operation=operation)
            if operation == 'acquire':checked['task'] = checked.pop('task_id')
            try:
                validate(checked)
            except SlotError as error:
                raise invalid(str(error)) from None
            payload['actor'] = self.service.bind_actor(ctx.principal, None)
            result = self.backend.invoke('merge-slot.' + operation, ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=capability)
            metadata.update({key: result.get(key) for key in
                             ('operation_id', 'actor', 'task_id', 'holder_before', 'holder_after', 'reason')})
            with self.service.store.lock:
                # A crash after the audit save but before the HTTP receipt leaves
                # the canonical identity reconcilable. Do not append another
                # committed event when that exact operation is retried.
                existing = any(event.get('action') == 'merge-slot.' + operation
                               and event.get('project_id') == ctx.params['pid']
                               and event.get('operation_id') == result['operation_id']
                               and event.get('outcome') == 'committed'
                               for event in self.service.state['audit'])
                if not existing:
                    event = self.service.audit(ctx.request_id, ctx.principal, 'merge-slot.' + operation,
                                               'committed', project_id=ctx.params['pid'])
                    event.update(metadata)
                try:
                    self.service.store.save()
                except (OSError, TimeoutError):
                    raise UncertainOutcome() from None
            return result, result
        # Persist the audit before the HTTP receipt. A lost canonical response
        # reconciles through the existing guarded receipt, with its original
        # before/after holder metadata, without releasing a later holder.
        def replay_check():
            if operation == 'acquire' and hasattr(self.backend, 'lifecycle_replay_guard'):
                self.backend.lifecycle_replay_guard(ctx.principal, ctx.params['pid'],
                    ctx.payload['task_id'], 'merge-acquire')
        return self._mutate(ctx, 'merge-slot.' + operation, ctx.params['pid'], write,
                            capability=capability, serialize=False, canonical=True,
                            audit_success=False, replay_check=replay_check)

    def _followon_write(self, ctx, operation):
        """Record or withdraw the coordinator's hand-back (kittrial-5bb.343).

        Both routes take the SAME ``CAP_COORDINATE`` gate as every other coordinator
        write and both call the canonical `assignee-followon` action, which runs the
        one library function the confined coordinator command runs. The record's
        authority is its host journal entry, so a plain comment posted by any other
        route stays inert even if it carries the reserved prefix.

        ``record`` takes exactly ``text``; ``withdraw`` takes exactly ``reason`` and
        names the follow-on in the path. The server derives the contribution, the
        integration record it cites and the native order under the project lock; the
        caller never supplies those.
        """
        pid = ctx.params['pid']
        if operation == 'record':
            route_name = 'followons.record'
            fields = ('text',)
            status = 201
            where = 'A hand-back'
        else:
            route_name = 'followons.withdraw'
            fields = ('reason',)
            status = 200
            where = 'A hand-back withdrawal'
        if not hasattr(self.backend, 'ROUTES') or route_name not in self.backend.ROUTES:
            raise not_implemented('Recording a hand-back is unavailable on this server')
        self._project(ctx, CAP_COORDINATE)

        def write():
            if ctx.idempotency_key is None:
                raise invalid('Send an Idempotency-Key and reuse it with the identical request '
                              'after an uncertain outcome')
            payload = dict(ctx.payload or {})
            refuse_unknown_fields(payload, fields, where)
            if set(payload) != set(fields):
                raise invalid('Supply exactly ' + ', '.join(fields))
            body = {'task_id': ctx.params['tid'], fields[0]: payload[fields[0]]}
            if operation == 'withdraw':
                body['target'] = ctx.params['fid']
            result = self.backend.invoke(route_name, ctx.principal, pid, body,
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=CAP_COORDINATE)
            return result, result
        return self._mutate(ctx, route_name, pid, write, capability=CAP_COORDINATE,
                            status=status, serialize=False, canonical=True)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID
                  + r')/assignee-followon')
    def task_followon_record(self, ctx):
        return self._followon_write(ctx, 'record')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID
                  + r')/assignee-followon/(?P<fid>' + ID + r')/withdraw')
    def task_followon_withdraw(self, ctx):
        return self._followon_write(ctx, 'withdraw')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/merge-slot/acquire')
    def merge_slot_acquire(self, ctx):
        return self._merge_slot_write(ctx, 'acquire')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/merge-slot/release')
    def merge_slot_release(self, ctx):
        return self._merge_slot_write(ctx, 'release')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/merge-slot/force-release')
    def merge_slot_force_release(self, ctx):
        return self._merge_slot_write(ctx, 'force-release')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/merge-slot/create')
    def merge_slot_create(self, ctx):
        return self._merge_slot_write(ctx, 'create')

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')')
    def project_get(self, ctx):
        self._project(ctx, CAP_READ)
        result = self._usable(self.service.project_view(ctx.principal, ctx.params['pid']))
        # The per-project claiming setting (kittrial-5bb.352), read FRESH from the
        # project's directory: it lives outside the tracker and is not cached. The
        # Settings page also learns whether this caller may change it.
        result['claiming'] = self._claiming_state(ctx.params['pid'])
        result['can_coordinate'] = self._guidance_can_coordinate(ctx)
        return 200, result

    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/claiming')
    def project_claiming(self, ctx):
        """Set the project's claiming mode (kittrial-5bb.352).

        Owners and coordinators (CAP_COORDINATE, which includes an owner and an agent
        holding the coordinator grant) may set it; workers, reviewers and viewers may
        not. The write records who set it, when and by which route, is audited, and is
        carried by the coordination backup.
        """
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        if set(payload) != {'claiming'}:
            raise invalid("Send exactly the field to change: claiming")
        value = payload['claiming']
        if value not in ('open', 'assigned'):
            raise invalid("claiming must be 'open' or 'assigned'")
        actor = self.service.bind_actor(ctx.principal, None)
        write = getattr(self.backend, 'set_claiming', None)
        if write is None:
            raise not_implemented('This server cannot store the claiming setting')

        def update():
            result = write(ctx.params['pid'], value, actor, via='web')
            return result, result
        return self._mutate(ctx, 'projects.claiming', ctx.params['pid'], update,
                            capability=CAP_COORDINATE,
                            audit_metadata={'claiming': value})

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

    def _guidance_project(self, ctx, capability=CAP_READ):
        self._project(ctx, capability)
        if not hasattr(self.backend, 'guidance_command'):
            raise not_implemented('This server has no canonical guidance adapter')

    def _guidance_can_coordinate(self, ctx):
        try:
            self.service.check_authority(ctx.principal, ctx.params['pid'], CAP_COORDINATE)
            return True
        except HttpError:
            return False

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/guidance')
    def project_guidance(self, ctx):
        self._guidance_project(ctx)
        result = self.backend.guidance_command(ctx.principal, ctx.params['pid'], 'read')
        return 200, dict(result, can_coordinate=self._guidance_can_coordinate(ctx))

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/guidance/status')
    def project_guidance_status(self, ctx):
        self._guidance_project(ctx, CAP_COORDINATE)
        pairs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(pairs) - {'offset', 'limit'} or any(len(v) != 1 for v in pairs.values()):
            raise invalid('Use only one offset and limit')
        try:
            offset = int(pairs.get('offset', ['0'])[0])
            limit = int(pairs.get('limit', ['50'])[0])
        except (ValueError, TypeError):
            raise invalid('Use an integer offset and limit') from None
        if not 0 <= offset <= 500 or not 1 <= limit <= 100:
            raise invalid('Use offset 0..500 and limit 1..100')
        return 200, self.backend.guidance_command(ctx.principal, ctx.params['pid'], 'status',
                                                  body=dict(offset=offset, limit=limit))

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/onboarding/read')
    def project_onboarding_read(self, ctx):
        """Every current project member reads the WHOLE document, including operator text."""
        self._guidance_project(ctx)
        result = self.backend.guidance_command(ctx.principal, ctx.params['pid'], 'onboarding')
        return 200, dict(result, can_coordinate=self._guidance_can_coordinate(ctx))

    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/guidance')
    def project_guidance_set(self, ctx):
        return self._guidance_write(ctx, 'set')

    @route('DELETE', r'/v1/projects/(?P<pid>' + ID + r')/guidance')
    def project_guidance_clear(self, ctx):
        return self._guidance_write(ctx, 'clear')

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/guidance/ack')
    def project_guidance_ack(self, ctx):
        return self._guidance_write(ctx, 'ack')

    def _guidance_write(self, ctx, operation):
        capability = CAP_READ if operation == 'ack' else CAP_COORDINATE
        self._guidance_project(ctx, capability)
        payload = ctx.payload if isinstance(ctx.payload, dict) else {}
        fields = {'set': {'text'}, 'ack': {'version'}, 'clear': set()}[operation]
        if set(payload) != fields or (ctx.payload is not None and not isinstance(ctx.payload, dict)):
            raise invalid('Send exactly: ' + (', '.join(sorted(fields)) or 'an empty object'))
        import web_guidance
        body = {k: v for k, v in payload.items() if k != 'text'}
        try:
            web_guidance.validate(dict(body, operation=operation),
                {'text': {'flag': '--file', 'text': payload['text']}} if operation == 'set' else {})
        except ValueError as error:
            raise invalid(str(error)) from None
        key = ctx.idempotency_key or ctx.request_id
        def write():
            try:
                result = self.backend.guidance_command(ctx.principal, ctx.params['pid'], operation,
                                                        body=body, text=payload.get('text'), key=key)
            except HttpError as failure:
                if failure.status == 503 and not getattr(failure, 'nothing_done', False):
                    raise UncertainOutcome() from None
                raise
            return result, result
        def replay_check():
            self.backend.guidance_replay_guard(ctx.principal, ctx.params['pid'], operation)
        return self._mutate(ctx, 'guidance.' + operation, ctx.params['pid'], write, capability=capability,
                            serialize=False, canonical=True, reason=operation, replay_check=replay_check)

    def _onboarding_owner(self, ctx):
        # The existing edit route retains operator-text withholding; the member read
        # is a distinct route. Granted personal coordinator agents may edit too.
        self._project(ctx, CAP_COORDINATE)
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
        if hasattr(self.backend, 'guidance_command'):
            return 200, self.backend.guidance_command(ctx.principal, ctx.params['pid'], 'onboarding-edit')
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
        if ctx.payload is not None and (not isinstance(ctx.payload, dict) or ctx.payload):
            raise invalid('Send an empty object to clear onboarding')
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
        def replay_check():
            if hasattr(self.backend, 'guidance_replay_guard'):
                self.backend.guidance_replay_guard(ctx.principal, ctx.params['pid'], 'onboarding')
        return self._mutate(ctx, 'projects.onboarding', ctx.params['pid'], write, capability=CAP_COORDINATE,
                            serialize=False, canonical=True, reason='account=%s %s' % (ctx.principal.user_id, size),
                            replay_check=replay_check)

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

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/coordination')
    def project_coordination_read(self, ctx):
        """Who coordinates the project and the rules that bind them (kittrial-5bb.198).

        For anyone who may read the project; it changes nothing. The installation's rules
        come from the host through the read-only ``setup-status`` endpoint action, of
        which only the rules block is returned.
        """
        self._project(ctx, CAP_READ)
        body = project_coordination.view(self, ctx.principal, ctx.params['pid'])
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
            # A field the route does not take is refused, not dropped (kittrial-5bb.183 item 2).
            refuse_unknown_fields(payload, self.MEMBER_FIELDS, 'A member change')
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
        # A credential issued, before kittrial-5bb.184, under a name that is somebody else's on
        # the host is refused when it writes. The list says so, with what to do, so that its
        # owner need not find out from a worker's failure. `actor_refused` is the reason or null.
        named = sorted({item['actor'] for item in items if item.get('actor') and not item.get('revoked')})
        standing = self.backend.actor_standing(ctx.params['pid'], named) if named else {}
        # A credential a first write already settled as refused by the row rule carries the mark
        # on its record; the list reads it, so the owner is told without a second tracker read
        # (kittrial-5bb.188 revision-3 item 3(3)).
        stored = self.service.state.get('credentials') if isinstance(self.service.state, dict) else {}
        for item in items:
            waived = item.get('actor_waived')
            if isinstance(waived, dict) and not item.get('revoked'):
                # A superuser allowed this name on purpose (kittrial-5bb.188 item 4): it is not
                # colliding and not refused, and the owner is told who allowed it and when.
                who = waived.get('by_name') or waived.get('by') or 'a superuser'
                when = waived.get('at') or 'an unrecorded date'
                item['actor_refused'] = None
                item['actor_allowed'] = 'allowed by %s on %s' % (who, when)
                continue
            reason = standing.get(item.get('actor')) if not item.get('revoked') else None
            if reason is None and not item.get('revoked'):
                record = stored.get(item.get('id')) if isinstance(stored, dict) else None
                mark = record.get('actor_rows_refused') if isinstance(record, dict) else None
                if isinstance(mark, str) and mark:
                    reason = mark
            item['actor_refused'] = None if reason is None else actor_names.refusal(item['actor'], reason)
        return 200, self._paged(ctx, items, limit, state)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/worker-credentials')
    def credential_issue(self, ctx):
        payload = ctx.payload or {}
        # Only who may issue one is told whether a name is taken: the answer says that a name
        # is an operator's or a session's, which is not for every member to probe.
        self._project(ctx, CAP_PROJECT_ADMIN)
        named = payload.get('actor')
        # An operator's allowance for a name the project's tracker already holds (kittrial-5bb.188
        # item 1): a superuser only, and only for the row rule -- never for a session, an
        # operator, a verifier or the service's own namespace. The waiver must say why it was
        # used (item 4): the reason travels to the audit entry, the stored record carries its
        # own mark, and the owner's list and admin.py credential-actors show it as allowed.
        allowed = payload.get('allow_actor') is True
        if 'allow_actor' in payload and not isinstance(payload.get('allow_actor'), bool):
            raise invalid('allow_actor must be true or false')
        waiver = payload.get('allow_actor_reason')
        if 'allow_actor_reason' in payload and waiver is not None and not isinstance(waiver, str):
            raise invalid('allow_actor_reason must be a short text')
        if not allowed and 'allow_actor_reason' in payload and waiver is not None:
            raise invalid('allow_actor_reason is only for a superuser waiver; send allow_actor: true too')
        if allowed:
            if not ctx.principal.superuser:
                raise forbidden('Only a superuser may allow a name the project\'s tracker already holds')
            if not isinstance(waiver, str) or not waiver.strip():
                raise invalid('allow_actor requires allow_actor_reason: say why the name is allowed')
            waiver = waiver.strip()
            if len(waiver) > 500:
                raise invalid('allow_actor_reason must be at most 500 characters')
            # The reason is shown to other people (the audit entry, the record, the owner's list
            # and the operator's listing), so it passes the kit's plain-text rule -- the same one
            # `guidance` applies: no control, bidi, zero-width, C1, tag or variation-selector
            # characters (kittrial-5bb.188 revision-3 item 3(2)). Tab, newline and carriage
            # return stay allowed, exactly as the guidance rule allows them.
            from review_workflow import plain_text
            try:
                plain_text(waiver, 'allow_actor_reason')
            except ValueError as error:
                raise invalid(str(error))
        checked = False
        waived_mark = None
        if isinstance(named, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,63}', named):
            # A worker credential writes under this name, and the tracker's rows carry the name
            # and nothing else: under a host actor's name it IS that actor (kittrial-5bb.184).
            # A name the tracker already holds is refused too (kittrial-5bb.188 item 1), which is
            # one bd export for this project. Refused before the key is reserved; the endpoint
            # refuses the same again at use.
            own = actor_names.own_intervals(self.service.state.get('credentials') or {}, named,
                                            ctx.principal.user_id, ctx.params['pid'])
            reason = self.backend.actor_standing(ctx.params['pid'], [named],
                                                 rows=self.backend.ACTOR_ROWS, own=own).get(named)
            if reason is None:
                checked = self.backend.ACTOR_ROWS
            elif allowed and reason == actor_names.ROWS:
                # The waiver is its own mark, never actor_rows_checked: an honestly checked
                # name and an allowed one are told apart on the record (item 4).
                waived_mark = {'by': ctx.principal.user_id, 'reason': waiver}
            else:
                raise invalid(actor_names.refusal(named, reason, actor_names.CHOOSE), {'actor': named, 'rule': reason})

        def issue():
            # A field the route does not take is refused, not dropped (kittrial-5bb.183 item 2).
            refuse_unknown_fields(payload, self.CREDENTIAL_ISSUE_FIELDS, 'A worker credential issue')
            self._require_usable(ctx.params['pid'])
            result = self.service.issue_credential(ctx.principal, ctx.params['pid'],
                                                   label=payload.get('label'),
                                                   scopes=payload.get('scopes'),
                                                   actor=payload.get('actor'),
                                                   request_id=ctx.request_id,
                                                   actor_checked=checked,
                                                   actor_waived=waived_mark)
            public = {'operation': 'credentials.issue', 'credential': result}
            stored = {'operation': 'credentials.issue',
                      'credential': {k: v for k, v in result.items() if k != 'secret'},
                      'secret_available': False}
            return public, stored
        audit_reason = None if waived_mark is None else \
            'waiver used for worker credential actor %s: %s' % (named, waived_mark['reason'])
        return self._mutate(ctx, 'credentials.issue', ctx.params['pid'], issue, status=201,
                            capability=CAP_PROJECT_ADMIN, replay_status=200, reason=audit_reason)

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
        # The agent record only (kittrial-5bb.303): this route is a polled whoami, so it
        # makes NO work read. The counted limit/held/available view needs the bounded
        # current-work snapshot, so it stays on ``/v1/agents/me/next``, which already
        # reads it; here the limit and who may raise it come from the record and one
        # constant (kittrial-5bb.276/302). The secretless ``setup`` bundle (kittrial-5bb.329)
        # is built from the in-memory record and project metadata only, so the VS Code
        # extension can write ``.orchestra/agent.json``, ``.orchestra/AGENT.md`` and
        # ``.gitignore`` into the open workspace folder directly from this route.
        body = {'agent': agent,
                'setup': self.service.agent_setup(agent, None),
                'claim_limit': self._agent_claim_limit_record_view(agent),
                'links': {'self': '/v1/agents/me', 'next': '/v1/agents/me/next'},
                'generated_at': now_iso(self.service._now())}
        # The calling credential's OWN expiry, in exactly the shape /v1/sessions/current
        # answers (kittrial-5bb.294 item 1): the agent set-up prompt and the bridge
        # extension read this route, and the read caps the stored expiry at 366 days from
        # issuance (item 4). No new disclosure: it is the caller's credential only, never
        # another credential's.
        expiry = self._credential_expiry_view(ctx.principal)
        if expiry is not None:
            body['credential_expiry'] = expiry
            body['expires_soon'] = bool(expiry.get('expires_soon'))
        return 200, body

    @route('PATCH', r'/v1/agents/me')
    def agents_me_update(self, ctx):
        """The agent's own record updates (kittrial-5bb.302, .329).

        Takes ``claim_limit`` (lowering only: a whole number from 1 up to the value the
        agent holds now; raising it stays the owner's move, refused with a sentence that
        names the owner's account or a superuser and the My agents page) and/or
        ``working_directory`` (so the extension or agent can record or update the open
        workspace folder). The identity is the caller's own credential, never a body
        field, so this route cannot touch another agent's record; the owner's route
        (``PATCH /v1/agents/{id}``) is unchanged.
        """
        payload = dict(ctx.payload or {})
        refuse_unknown_fields(payload, self.AGENT_ME_UPDATE_FIELDS, 'An agent self-change')
        if not any(field in payload for field in self.AGENT_ME_UPDATE_FIELDS):
            raise invalid('Nothing to change. An agent self-change takes: %s'
                          % ', '.join(self.AGENT_ME_UPDATE_FIELDS))
        has_limit = 'claim_limit' in payload
        has_dir = 'working_directory' in payload
        has_role = 'role' in payload
        action = ('agents.claim_limit.lower' if has_limit and not has_dir and not has_role
                  else 'agents.working_directory.set' if has_dir and not has_limit and not has_role
                  else 'agents.role.set' if has_role and not has_limit and not has_dir
                  else 'agents.me.update')
        metadata = {}

        def update_self():
            result = self.service.update_own_agent(ctx.principal, payload)
            # Who changed what, on the committed audit event (kittrial-5bb.302 item 4, .329).
            metadata.update({'agent': result['agent']['id'], 'actor': ctx.principal.actor})
            if has_limit:
                metadata.update({'old_limit': result['old_limit'],
                                 'new_limit': result['new_limit']})
            if has_dir:
                metadata.update({'old_working_directory': result['old_working_directory'],
                                 'new_working_directory': result['new_working_directory']})
            if has_role:
                metadata.update({'old_role': result['old_role'],
                                 'new_role': result['new_role']})
            return result, result
        return self._mutate(ctx, action, None, update_self,
                            capability=CAP_AGENT_SELF, audit_metadata=metadata)

    @route('GET', r'/v1/agents/me/next')
    def agents_me_next(self, ctx):
        role_override = validate_agent_role(ctx.query['role']) if 'role' in ctx.query else None
        agent = self.service.agent_for_credential(ctx.principal)
        attention = self._agent_attention(ctx.principal, agent, role=role_override)
        # `repository` is where the project's owner says its code lives (kittrial-5bb.118):
        # a label, null when not recorded. Data for the agent, never an instruction.
        projects = [{'id': p['id'], 'name': p['name'], 'repository': p.get('repository')}
                    for p in self.service.list_projects(ctx.principal)]
        return 200, {
            'agent': agent,
            'attention': self._agent_attention_view(agent, attention),
            # The agent's own claim limit and what holds it (kittrial-5bb.276), so an
            # agent can see what is in the way without reading the queue itself.
            'claim_limit': self._agent_claim_limit_view(agent, attention),
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

    @route('GET', r'/v1/agents/me/wait')
    def agents_me_wait(self, ctx):
        """The agent's own move read for a blocking wait (kittrial-5bb.285).

        The bridge extension (or any web agent) polls this route and blocks in ITS own
        loop; the SSH client's ``client.py wait`` blocks in the client and polls the
        endpoint's ``wait`` action. Both compute the same projection with
        ``waiting.worker_view`` over the canonical rows, so one task's fingerprint means
        the same fact on either route: the caller decides whether to sleep, the server
        never does, and nothing is scheduled.

        ONE cheap read per granted project (the same bounded current-work snapshot
        ``/v1/agents/me/next`` already uses, reused within the request); the whose-move
        decision is the ONE ``work.move_decision``, so this route cannot offer a task the
        canonical queue would not call the agent's move.
        """
        import waiting
        from work import move_decision
        agent = self.service.agent_for_credential(ctx.principal)
        actor = agent.get('actor') or agent.get('id')
        refuse_ambiguous_query(urlsplit(self.path).query, ('form', 'since', 'idle_minutes'))
        # Both forms share this query, so a malformed threshold is refused even on the
        # worker form, where the value is otherwise unused (kittrial-5bb.389).
        self._lanes_idle_minutes(ctx.query.get('idle_minutes'))
        form = ctx.query.get('form', 'worker')
        if form not in ('worker', 'coordinator'):
            raise HttpError(400, 'invalid_request', 'form must be worker or coordinator')
        if form == 'coordinator':
            return self._agent_coordinator_wait(ctx, agent)
        since = ctx.query.get('since')
        # Review n4: the worker form always ignored an unusable since; the record-only
        # feature made it a 400.  Keep the old worker behaviour and ignore it (the
        # coordinator form still refuses it, as it always did).
        if since is not None and not waiting.is_fingerprint(since):
            since = None
        rows = []
        #: One entry per project this read covered: its full queue rows (the closest
        #: HTTP fact to the CLI's ``work --mine`` list) and the rows that are its move.
        #: kittrial-5bb.397 records an empty wake from these without another read.
        project_reads = []
        #: The claiming setting of every project this read covered (kittrial-5bb.352
        #: review item S1): the same fresh per-project read the endpoints and
        #: /v1/agents/me/next use, so an empty wait under 'assigned' carries the
        #: sentence saying who hands work out instead of looking idle.
        claiming_modes = {}
        for project_id in agent.get('projects') or []:
            if not self._agent_may_read(ctx.principal, project_id):
                continue
            claiming_modes[project_id] = self._claiming_mode(project_id)
            # The guidance acknowledgement is a move row too (kittrial-5bb.325), so a web
            # agent's wait wakes for a new version and its fingerprint changes with it;
            # the row is the same one /v1/agents/me/next shows as its first next_action.
            # Withheld while this service has no HTTP guidance read/ack route to clear it
            # (kittrial-5bb.334): a row that cannot be acted on must not be a wake.
            item = self._agent_guidance_item(project_id, actor)
            if item is not None:
                rows.append(dict(item, project=project_id))
            read = self._agent_project_tasks(project_id)
            project_rows = []
            project_move_rows = []
            for task in read.get('tasks') or []:
                if not isinstance(task, dict):
                    continue
                state = task.get('review_state')
                pending = task.get('pending_review_items')
                if not isinstance(pending, int):
                    pending = task.get('open_requests')
                if not isinstance(pending, int):
                    pending = 0
                holds = claim_limit.actor_holds(task.get('assignee'), actor)
                move, _text, next_actor_value = move_decision(
                    state, task.get('id'), holds,
                    pending_items=pending, redelivered=bool(task.get('redelivered')),
                    assignee=task.get('assignee'), closed=task.get('status') == 'closed',
                    claimable=task.get('claimable') if isinstance(task.get('claimable'), bool) else None,
                    comment_pending=attention_field(task, 'assignee_comment_pending') is True)
                if not holds and move != 'you':
                    continue
                row = dict(task, task=task.get('id'), project=project_id,
                           whose_move=move, next_actor=next_actor_value,
                           # The wait row carries the marker and its counts
                           # (kittrial-5bb.351): a new other-party entry, or an edit to
                           # it, moves the fingerprint even while the boolean stays true.
                           newer_activity_marker=attention_field(task, 'newer_activity_marker'),
                           newer_activity_fields=attention_field(task, 'newer_activity_fields'),
                           # The checkpoint-independent marker the worker's wait
                           # fingerprints (kittrial-5bb.369): the agent's own checkpoint
                           # does not move it.
                           wait_newer_activity_marker=attention_field(task, 'wait_newer_activity_marker'),
                           newer_activity_by_others=attention_field(task, 'newer_activity_by_others'),
                           newer_activity_own=attention_field(task, 'newer_activity_own'),
                           newer_activity_coverage=attention_field(task, 'newer_activity_coverage'),
                           unresolved_directions=attention_field(task, 'unresolved_directions'))
                project_rows.append(row)
                if move == 'you':
                    project_move_rows.append(row)
                    rows.append(row)
            project_reads.append((project_id, project_rows, project_move_rows))
        # The project's claiming setting on the wait read (kittrial-5bb.352 review item
        # S1). The wait can span projects, so report one mode only when they agree;
        # under any 'assigned' project an empty wait carries the empty-list sentence,
        # exactly as the agent next/attention loop decides it.
        modes = set(claiming_modes.values())
        claiming = (next(iter(modes)) if len(modes) == 1
                    else ('assigned' if 'assigned' in modes else None))
        empty_message = None
        if not rows and 'assigned' in modes:
            from claiming import EMPTY_SENTENCE
            empty_message = EMPTY_SENTENCE
        view = waiting.worker_view(rows, claiming=claiming, empty_message=empty_message)
        if since is not None:
            view['changed'] = since != view['fingerprint']
        # kittrial-5bb.397: one bounded host log per project, written only for the actor
        # this credential resolves to. Any error is a warning on the answer, never a
        # failed wait; no tracker record is written.  It runs on every poll, exactly as
        # the CLI's read does (review n2); the shared observer skips an unchanged write.
        import wake_log
        warnings = []
        for project_id, project_rows, project_move_rows in project_reads:
            try:
                project_dir = self.backend.wake_project_dir(project_id)
            except (OSError, ValueError):
                project_dir = None
            if project_dir is None:
                continue
            warnings.extend(wake_log.observe(
                project_dir, actor, since,
                dict(view, items=waiting.movement(project_move_rows),
                     fingerprint=view['fingerprint']),
                project_rows))
        if warnings:
            view['warnings'] = warnings
        view['agent'] = agent.get('id')
        view['generated_at'] = now_iso(self.service._now())
        view['manual_cadence'] = ('Read this route again to see a change; it never blocks, '
                                  'never sleeps and schedules nothing. --since is the caller\'s '
                                  'own memory of the last fingerprint it acted on.')
        return 200, view

    def _agent_coordinator_wait(self, ctx, agent):
        """Live project grants bound this read, never the service's host identity."""
        import waiting
        refuse_unknown_fields(ctx.query, ('form', 'since', 'idle_minutes'), 'Coordinator wait query')
        since = ctx.query.get('since')
        if since is not None and not waiting.is_fingerprint(since):
            raise HttpError(400, 'invalid_request', 'since must be a wait fingerprint')
        idle_minutes = self._lanes_idle_minutes(ctx.query.get('idle_minutes'))
        projects = []
        for project_id in sorted(set(agent.get('projects') or [])):
            try:
                self.service.check_authority(ctx.principal, project_id, CAP_COORDINATE)
            except HttpError as error:
                if error.status not in (403, 404):
                    raise
                continue
            projects.append(project_id)
        if not projects:
            raise forbidden('A current project coordinator grant is required for coordinator wait')
        if not hasattr(self.backend, 'coordinator_wait'):
            raise not_implemented('Coordinator wait is unavailable on this server')
        reads = [(project, self.backend.coordinator_wait(project, ctx.principal, idle_minutes))
                 for project in projects]
        # A one-project fingerprint is EXACTLY the CLI fact. Multiple projects have
        # an explicit project namespace, including empty projects and grant changes.
        if len(reads) == 1:
            view = dict(reads[0][1])
        else:
            triggers = {name: [] for name in waiting.COORDINATOR_TRIGGERS}
            for project, read in reads:
                for name in triggers:
                    triggers[name].extend(dict(row, project=project)
                                          for row in read['triggers'][name])
            view = {'schema': waiting.WAIT_SCHEMA, 'form': 'coordinator', 'triggers': triggers,
                    'items': [dict(row, trigger=name) for name in triggers for row in triggers[name]],
                    'fingerprint': waiting.digest([{'project': project, 'fingerprint': read['fingerprint']}
                                                   for project, read in reads])}
        view['projects'] = projects
        view['agent'] = agent.get('id')
        view['generated_at'] = now_iso(self.service._now())
        if since is not None:
            view['changed'] = since != view['fingerprint']
        view['manual_cadence'] = ('Read this route again with form=coordinator and since=FINGERPRINT; '
                                  'it never blocks, sleeps or schedules work.')
        return 200, view

    @route('POST', r'/v1/agents')
    def agents_create(self, ctx):
        payload = dict(ctx.payload or {})

        def create():
            # The caller's right over every named project is judged before the route's
            # field list is answered (kittrial-5bb.183 review item 1): a project the
            # caller cannot see keeps the service's own 404, exactly as the same body
            # without the unknown field does. Only then is a field the route does not
            # take refused, not dropped (kittrial-5bb.183 item 2).
            owner = self.service.agent_creation_owner(ctx.principal, payload.get('owner', ctx.principal.user_id))
            self._require_grantable(ctx.principal, payload.get('projects'), (), owner_id=owner)
            self.service.check_agent_grant(ctx.principal, payload.get('projects'), owner_id=owner)
            refuse_unknown_fields(payload, self.AGENT_CREATE_FIELDS, 'An agent creation')
            claim_limit_value = (validate_claim_limit(payload.get('claim_limit'))
                                 if 'claim_limit' in payload else None)
            role_value = (validate_agent_role(payload.get('role'))
                          if 'role' in payload else None)
            result = self.service.create_agent(
                ctx.principal, name=payload.get('name'), tool=payload.get('tool'),
                working_directory=payload.get('working_directory'),
                machine=payload.get('machine'), notes=payload.get('notes'),
                projects=payload.get('projects'), scopes=payload.get('scopes'),
                request_id=ctx.request_id, owner=owner, claim_limit=claim_limit_value,
                role=role_value)
            return result, _redact_agent_secret(result)
        def replay_check():
            owner = self.service.agent_creation_owner(ctx.principal, payload.get('owner', ctx.principal.user_id))
            self._require_grantable(ctx.principal, payload.get('projects'), (), owner_id=owner)
            self.service.check_agent_grant(ctx.principal, payload.get('projects'), owner_id=owner)
        return self._mutate(ctx, 'agents.create', None, create, status=201,
                            capability=CAP_AGENTS, replay_status=200, replay_check=replay_check)

    def _agent_items(self, principal, agents=None):
        items = []
        for agent in self.service.list_agents(principal) if agents is None else agents:
            # One read per project for all the agents listed, not one per agent.
            attention = self._agent_attention(principal, agent, many=True)
            items.append(dict(agent, attention=self._agent_attention_view(agent, attention),
                              resume_prompt=self._agent_resume_prompt(agent, attention)))
        return items

    @route('GET', r'/v1/agents')
    def agents_list(self, ctx):
        self.require(ctx, CAP_AGENTS)
        if any(key in ctx.query for key in ('limit', 'cursor', 'unconfirmed')):
            limit, state = self._page(ctx, ctx.query)
            unconfirmed = ctx.query.get('unconfirmed', 'false')
            if unconfirmed not in ('true', 'false'):
                raise invalid('unconfirmed must be true or false')
            agents, total = self.service.agent_page(ctx.principal, state['o'], limit,
                                                  unconfirmed=unconfirmed == 'true')
            return 200, {'items': self._agent_items(ctx.principal, agents), 'total': total,
                         'next_cursor': make_cursor(ctx.principal, None, ctx.query, state['o'] + limit,
                                                    key=self.service.cursor_key())
                         if state['o'] + limit < total else None,
                         'generated_at': now_iso(self.service._now())}
        items = self._agent_items(ctx.principal)
        return 200, {'items': items, 'total': len(items),
                     'generated_at': now_iso(self.service._now())}

    @route('GET', r'/v1/agents/(?P<aid>' + ID + r')/history')
    def agents_history(self, ctx):
        """The agent's history, its owner's view (kittrial-5bb.365): credentials issued,
        revoked, extended; disabled, enabled, changed; projects given and taken. Readable by
        the agent's owner or a superuser; anyone else gets the plain 404 an unknown id gets.
        A credential cannot read it (``agents.manage`` is never a credential's)."""
        self.require(ctx, CAP_AGENTS)
        return 200, dict(self.service.agent_history(ctx.principal, ctx.params['aid']),
                         generated_at=now_iso(self.service._now()))

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
        metadata = {}

        def update():
            record = self.service.state['agents'].get(ctx.params['aid'])
            if isinstance(record, dict):
                # An id with no agent record is the service's own 404: judging a grant
                # here would answer the usability refusal for an agent that is not there
                # (kittrial-5bb.90 item 1).
                self._require_grantable(ctx.principal, payload.get('projects'),
                                        record.get('projects') or (),
                                        owner_id=record.get('owner'))
            # The record's ownership is judged before the route's field list is answered
            # (kittrial-5bb.183 review item 1): a caller who may not change the agent gets
            # the service's own 404, exactly as the same body without the unknown field
            # does. Only then is a field the route does not take refused, not dropped
            # (kittrial-5bb.183 item 2). This is the same lookup update_agent() makes.
            self.service.get_agent(ctx.principal, ctx.params['aid'])
            if 'projects' in payload:
                # And so is the grant (kittrial-5bb.183 review, P3): a project the agent's owner
                # cannot see keeps the service's own 404 whether or not the body also carries a
                # field the route does not take. Nothing is written by the check.
                self.service.check_agent_grant(ctx.principal, payload.get('projects'), owner_id=record['owner'])
            refuse_unknown_fields(payload, self.AGENT_UPDATE_FIELDS, 'An agent change')
            if 'enabled' in payload and payload['enabled'] is not None and not bool(payload['enabled']):
                metadata.update(self._revocation_metadata(agent_id=ctx.params['aid']))
            # The event names its agent (kittrial-5bb.365), so the agent's history lists it.
            metadata.setdefault('agent_ids', [ctx.params['aid']])
            result = self.service.update_agent(ctx.principal, ctx.params['aid'], payload,
                                               request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.update', None, update, capability=CAP_AGENTS,
                            audit_metadata=metadata)

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
        metadata = {}
        def disable():
            metadata.update(self._revocation_metadata(agent_id=ctx.params['aid']))
            result = self.service.disable_agent(ctx.principal, ctx.params['aid'])
            return result, result
        return self._mutate(ctx, 'agents.disable', None, disable, capability=CAP_AGENTS,
                            audit_metadata=metadata)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/enable')
    def agents_enable(self, ctx):
        def enable():
            result = self.service.enable_agent(ctx.principal, ctx.params['aid'])
            return result, result
        return self._mutate(ctx, 'agents.enable', None, enable, capability=CAP_AGENTS)

    #: How many claims one bulk release takes off an agent in one request. A
    #: release is one canonical assignment each; the bound keeps one request from
    #: walking a very large held set, and the answer says when it was cut.
    CLAIM_RELEASE_LIMIT = 20

    @route('POST', r'/v1/agents/(?P<aid>[^/]+)/release-claims')
    def agents_release_claims(self, ctx):
        """Release an agent's claimed, undelivered tasks from the web (kittrial-5bb.276).

        The owner incident's second half: a claim could be released only on the host.
        A single claim is released through the ordinary assignment route
        (``POST /v1/projects/{pid}/tasks/{tid}/assign`` with ``assignee: null``); this
        route releases every held, undelivered claim of one agent across every project
        it is granted, so a person need not walk the queue. It uses the same canonical
        ``tasks.assign`` each single release uses, under the caller's own authority, so
        nothing here invents a second way to clear an assignee.

        The caller must hold the coordinate capability in every project it is about to
        release in (one check per project, before any write). A caller who may
        administer the agent but not one of its projects is refused with that project
        named, which is the move to ask its owner; no partial release is made under a
        caller who may release in only some of them.

        ``dry_run`` answers how many claims it would release, per project, and writes
        nothing.
        """
        aid = ctx.params['aid']
        payload = dict(ctx.payload or {})
        refuse_unknown_fields(payload, ('dry_run',), 'An agent claim release')
        dry_run = bool(payload.get('dry_run'))
        if ctx.idempotency_key is None and not dry_run:
            raise invalid('Send an Idempotency-Key: releasing claims writes')
        # The record's ownership is judged before what the server can do with it,
        # so a caller with no right to the agent keeps the service's own 404.
        agent = self.service.get_agent(ctx.principal, aid)
        if 'tasks.assign' not in self.backend.ROUTES:
            raise not_implemented('Canonical task assignment is unavailable on this server')
        actor = agent.get('actor') or agent.get('id')
        from claim_limit import held_undelivered
        projects = list(agent.get('projects') or [])
        # Authority over every project first, so a caller who may release in only
        # some of them cannot half-release the agent.
        for project_id in projects:
            try:
                self.service.check_authority(ctx.principal, project_id, CAP_COORDINATE)
            except HttpError as refusal:
                raise conflict(
                    'You cannot release claims in %s, so nothing was released: %s'
                    % (project_id, refusal.message),
                    {'project': project_id, 'released': []}) from None
        planned, released, failed = [], [], []
        truncated = False
        for project_id in projects:
            read = self.backend.agent_tasks(project_id, actor)
            held = held_undelivered(read.get('tasks') or [])
            if not read.get('complete'):
                raise conflict('The agent\'s held tasks could not be read completely, so no claim '
                               'was released.', {'project': project_id, 'held_readable': False})
            if len(held) > self.CLAIM_RELEASE_LIMIT:
                truncated = True
            counts = {'project': project_id, 'held': len(held),
                      'tasks': [row.get('id') for row in held]}
            planned.append(counts)
            for row in held[:self.CLAIM_RELEASE_LIMIT]:
                if dry_run:
                    continue
                key = '%s/%s' % (ctx.idempotency_key, row.get('id'))
                try:
                    self.backend.invoke('tasks.assign', ctx.principal, project_id,
                                        {'task_id': row.get('id'), 'expected_assignee': row.get('assignee') or actor,
                                         'assignee': None,
                                         # The full closed body the canonical assignment takes:
                                         # _command indexes these two directly, so a release
                                         # without them raised a KeyError and answered 500 on the
                                         # endpoint backend (revision, review item 3a). Both
                                         # false: a release clears an undelivered claim that is
                                         # already this agent's, so neither exception applies.
                                         'allow_pending_review': False, 'allow_held_slot': False,
                                         'reason': 'Owner released the agent\'s claims (%s)' % aid},
                                        key, target=row.get('id'), authorize=ctx.authorize,
                                        capability=CAP_COORDINATE)
                except HttpError as refusal:
                    # One row that cannot be released does not stop the rest; it is
                    # reported by name with the canonical sentence.
                    failed.append({'project': project_id, 'task': row.get('id'),
                                   'message': refusal.message})
                    continue
                released.append({'project': project_id, 'task': row.get('id')})
        return 200, {'operation': 'agents.claims.release', 'agent': aid,
                     'released': released, 'failed': failed, 'planned': planned,
                     'dry_run': dry_run, 'truncated': truncated,
                     'candidate_total': sum(entry['held'] for entry in planned)}

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
            # A field this route does not take is refused, not dropped, as when an agent is
            # made (kittrial-5bb.208 review). After the checks above: who may not see the
            # agent keeps the service's 404.
            self.service.get_agent(ctx.principal, ctx.params['aid'])
            refuse_unknown_fields(payload, self.AGENT_CREDENTIAL_FIELDS, 'A new agent credential')
            result = self.service.issue_agent_credential(
                ctx.principal, ctx.params['aid'], scopes=payload.get('scopes'),
                label=payload.get('label'), request_id=ctx.request_id)
            return result, _redact_agent_secret(result)
        return self._mutate_agent_credential(ctx, 'agents.credentials.issue', issue, status=201,
                                             replay_status=200)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/credentials/'
                  r'(?P<cid>' + ID + r')/revoke')
    def agents_credential_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_agent_credential(
                ctx.principal, ctx.params['aid'], ctx.params['cid'],
                request_id=ctx.request_id)
            return result, result
        return self._mutate_agent_credential(ctx, 'agents.credentials.revoke', revoke, status=204)

    @route('POST', r'/v1/agents/(?P<aid>' + ID + r')/credentials/'
                  r'(?P<cid>' + ID + r')/extend')
    def agents_credential_extend(self, ctx):
        return self._extend_agent_credential(ctx)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')/credentials/'
                  r'(?P<cid>' + ID + r')/extend')
    def project_agent_credential_extend(self, ctx):
        return self._extend_agent_credential(ctx, ctx.params['pid'])

    def _extend_agent_credential(self, ctx, project_id=None):
        def check():
            agent, _ = self.service.agent_extension_target(
                ctx.principal, ctx.params['aid'], ctx.params['cid'], project_id)
            for pid in ([project_id] if project_id is not None else agent.get('projects') or []):
                self._require_usable(pid)
            refuse_unknown_fields(dict(ctx.payload or {}), (), 'An agent credential extension')
        def extend():
            check()
            result = self.service.extend_agent_credential(
                ctx.principal, ctx.params['aid'], ctx.params['cid'], project_id=project_id,
                request_id=ctx.request_id)
            return result, result
        # The route's own capability is the coarse gate; the precise extension authority
        # is decided once, in Service.agent_extension_target: CAP_AGENTS (the agent's own
        # account or a superuser) for the agent route, CAP_PROJECT_ADMIN (that project's
        # owner or a superuser) for the project route, and any credential caller refused
        # first there. The project route's gate is therefore the authenticated project
        # READ the caller already needs to see the agent, never a second, shadowing copy
        # of the precise check: each guard is load-bearing and pinned by its own route
        # test (kittrial-5bb.294 item 3).
        return self._mutate_agent_credential(
            ctx, 'agents.credentials.extend', extend, project_id=project_id,
            capability=CAP_AGENTS if project_id is None else CAP_READ, replay_check=check)

    def _mutate_agent_credential(self, ctx, operation, fn, *, project_id=None,
                                 capability=CAP_AGENTS, **options):
        """Save the credential and its route audit in one atomic JSON replacement.

        Issuance rolls back before a successful state save. Revocation stays effective
        in memory even if that save fails. A receipt failure after saving keeps the
        durable effect and its in-progress reservation; it cannot issue a second secret.
        """
        with self.service.store.lock:
            agent = self.service.state['agents'].get(ctx.params['aid'])
            if not isinstance(agent, dict):
                return self._mutate(ctx, operation, project_id, fn, capability=capability, **options)
            saved = [False]
            with self.service._agent_credential_transaction(
                    agent, rollback=operation != 'agents.credentials.revoke') as mark_saved:
                def state_saved():
                    mark_saved()
                    saved[0] = True
                try:
                    return self._mutate(ctx, operation, project_id, fn, capability=capability,
                                        audit_success=False, state_saved=state_saved, **options)
                except HttpError:
                    raise  # _mutate already releases a refused reservation and audits it
                except Exception:
                    if not saved[0] and ctx.idempotency_key is not None:
                        digest = self.service._idempotency_key(ctx.principal, project_id,
                            '%s %s' % (operation, ctx.route_target), ctx.idempotency_key)
                        self.service.idempotency_release(digest)
                    raise

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

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')/history')
    def project_agent_history(self, ctx):
        """The agent's history as this project's owners see it (kittrial-5bb.365): no
        credential label, no scopes, nothing about another project, and nothing from before
        the agent was given this project. An agent not granted here is the same 404 as the
        project's agent read."""
        self._project(ctx, CAP_PROJECT_ADMIN)
        return 200, dict(self.service.agent_history(ctx.principal, ctx.params['aid'], project_id=ctx.params['pid']),
                         generated_at=now_iso(self.service._now()))

    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')/coordinator')
    def project_agent_coordinator_grant(self, ctx):
        """Let this agent act as coordinator in this project (kittrial-5bb.209). An owner of the
        project or a superuser; the body is empty. Answers the project's view of the agent."""
        refuse_unknown_fields(dict(ctx.payload or {}), (), 'A coordinator grant')

        def grant():
            result = self.service.grant_coordinator(ctx.principal, ctx.params['pid'], ctx.params['aid'],
                                                    request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.coordinator.grant', ctx.params['pid'], grant,
                            capability=CAP_PROJECT_ADMIN, reason='agent %s' % ctx.params['aid'])

    @route('DELETE', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')/coordinator')
    def project_agent_coordinator_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_coordinator(ctx.principal, ctx.params['pid'], ctx.params['aid'],
                                                     request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.coordinator.revoke', ctx.params['pid'], revoke,
                            capability=CAP_PROJECT_ADMIN, reason='agent %s' % ctx.params['aid'])

    # The project's release grant (kittrial-5bb.378): an owner gives and revokes one
    # agent's authority to RECORD releases here. It is stored beside the coordinator grant
    # and read live on every release write; the coordinator grant does not imply it.
    @route('PUT', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')/release')
    def project_agent_release_grant(self, ctx):
        refuse_unknown_fields(dict(ctx.payload or {}), (), 'A release grant')

        def grant():
            result = self.service.grant_release(ctx.principal, ctx.params['pid'], ctx.params['aid'],
                                                request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.release.grant', ctx.params['pid'], grant,
                            capability=CAP_PROJECT_ADMIN, reason='agent %s' % ctx.params['aid'])

    @route('DELETE', r'/v1/projects/(?P<pid>' + ID + r')/agents/(?P<aid>' + ID + r')/release')
    def project_agent_release_revoke(self, ctx):
        def revoke():
            result = self.service.revoke_release(ctx.principal, ctx.params['pid'], ctx.params['aid'],
                                                 request_id=ctx.request_id)
            return result, result
        return self._mutate(ctx, 'agents.release.revoke', ctx.params['pid'], revoke,
                            capability=CAP_PROJECT_ADMIN, reason='agent %s' % ctx.params['aid'])

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
                if isinstance(last, dict) and any(item.get('kind') in ('blocker','dependency')
                                                 for item in last.get('open_items') or [] if isinstance(item,dict)):
                    blocked.add(task_id)
        return blocked

    def _agent_own_tasks(self, project_id, actor, many=False):
        """Split this request's current review snapshot by the actual assignee.

        Complete snapshots supply both own attention and claimable suggestions.
        A single agent falls back to owner-filtered work when the snapshot is
        incomplete. Owner lists reuse the bounded snapshot and report truncation.
        Authority is checked per caller/project; nothing is cached across requests.
        """
        cache = getattr(self, '_agent_own_cache', None)
        if cache is None:
            cache = self._agent_own_cache = {}
        key = (project_id, None if many else actor)
        if key not in cache:
            queue=self._agent_project_tasks(project_id,many=many)['queue']
            if not many and not queue.get('complete'):
                queue=None
            read = self.backend.agent_tasks(project_id, None if many else actor,
                                            queue=queue)
            cache[key] = {'tasks': [task for task in read.get('tasks') or [] if isinstance(task, dict)],
                          'complete': bool(read.get('complete'))}
        read = cache[key]
        if not many:
            return read
        from claim_limit import actor_holds
        return {'tasks': [task for task in read['tasks'] if actor_holds(task.get('assignee'), actor)],
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
        approves = self.service.agent_approves(agent_id, project_id)
        if not may_review and not approves:
            return []
        actions = []
        for row in rows:
            if row.get('review_state') != 'awaiting-review' or row.get('status') == 'closed':
                continue
            if not self._independent([actor], [row.get('assignee'), row.get('contribution_author')], project_id):
                continue
            recommended_by = [name for name in row.get('recommended_by') or [] if isinstance(name, str)]
            if approves:
                # It holds the coordinator grant here (kittrial-5bb.209): the decision is its own. It is
                # never shown its own party's work for it: that approval would be refused.
                if not self._independent([actor], [row.get('assignee'), row.get('contribution_author')], project_id, True):
                    continue
                kind, count, who = 'to-review', 'to_review', 'agent'
                reason = (('A reviewer recommends approving this contribution. ' if recommended_by else
                           'A contribution by another account awaits review. ')
                          + 'You hold the coordinator grant in this project: review it, then approve it or request '
                            'changes.')
            elif recommended_by and owner_approves:
                kind, count, who = 'review-recommended', 'review_recommended', 'owner'
                reason = ('A reviewer recommends approving this contribution. Tell your owner it is ready to '
                          'approve; an agent cannot approve.')
            elif not self._own_party_recommended(row, actor, project_id):
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
    def _agent_review_summary(counts, snapshot_truncated=False):
        """What to add to an agent's summary when there is review work, whatever its state.

        The state values do not change for review work, so without this an agent with
        nothing of its own would read "idle, nothing in flight" while contributions wait
        for it or for its owner.
        """
        parts = []
        prefix = 'at least ' if snapshot_truncated else ''
        if counts.get('review_recommended'):
            parts.append('%s%d contribution(s) recommended for approval: tell the owner'
                         % (prefix, counts['review_recommended']))
        if counts.get('to_review'):
            parts.append('%s%d contribution(s) to review' % (prefix, counts['to_review']))
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
                  'links': {'task': base, 'brief': base + '/brief', 'history': base + '/history',
                            'reviews': base + '/reviews',
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

    def _agent_project_tasks(self, project_id, many=False):
        """Reuse the bounded current-work snapshot, including unclaimed open tasks.

        The endpoint costs one work command per page; the in-process backend reads
        its project once. There is no separate task-list read. Both the backend's
        page bound and the attention bound are preserved in ``complete``.
        """
        cache = getattr(self, '_agent_task_cache', None)
        if cache is None:
            cache = self._agent_task_cache = {}
        key=(project_id,many)
        if key in cache:
            return cache[key]
        snapshot = (self._independent_queue(self.backend.agent_snapshot(project_id), project_id)
                    if not many and hasattr(self.backend,'agent_snapshot') else self._review_queue(project_id))
        rows = [task for task in (snapshot.get('items') or []) if isinstance(task, dict)]
        bound = AGENT_MAX_PAGES * MAX_PAGE
        result = {'tasks': rows[:bound], 'complete': len(rows) <= bound and bool(snapshot.get('complete')),
                  'queue':snapshot}
        cache[key] = result
        return result

    def _refuse_claim_over_limit(self, ctx, task, claimant):
        """Refuse a claim that would take an agent past its own limit or hard cap (kittrial-5bb.276/.300).

        The limit is the claimed agent's own setting (``claim_limit`` on its record),
        default one undelivered claim; the hard cap over all undelivered claims
        (active plus blocked) is :func:`claim_limit.hard_cap` (``claim_limit + 3``).
        A person is not limited. Re-claiming the task the agent already holds changes
        nothing and is not counted again.

        The held set comes from the same bounded ``work`` snapshot the agent's own
        attention reads, across EVERY project the agent is granted, so the gate and
        ``GET /v1/agents/me/next`` count the same rows. It fails CLOSED: if any of
        those reads is incomplete, or the review state or blocking count of a held
        row is not known, the claim is refused with a distinct sentence instead of
        being let through, because an unreadable count must never read as "no
        limit". A row whose latest checkpoint left a blocker or a dependency does
        not count toward ``claim_limit`` itself, while still counting toward
        :func:`claim_limit.hard_cap`: see :func:`claim_limit.blocked`.
        """
        from claim_limit import actor_holds, check, hard_cap, held_against_limit, held_blocked
        if not isinstance(claimant, str) or not claimant:
            return
        agents = self.service.state.get('agents') or {}
        owner = None
        agent_id = getattr(getattr(ctx, 'principal', None), 'agent_id', None)
        if agent_id:
            record = agents.get(agent_id)
            if isinstance(record, dict) and actor_holds(claimant, self.service.agent_actor(record)):
                owner = record
        if owner is None:
            for record in agents.values():
                # The attribution label an agent's own writes carry is its id
                # (http_auth.Service.agent_actor), or a <agent>/<label> sub-label
                # under it (kittrial-5bb.300 r2).
                if isinstance(record, dict) and actor_holds(claimant, self.service.agent_actor(record)):
                    owner = record
                    break
        if owner is None:
            # A person's own session actor: not limited unless the owner set a limit
            # on their record, and no person record carries one.
            return
        limit = self._agent_claim_limit_view(owner, None)['limit']
        if limit is None:
            return
        cap = hard_cap(limit)
        # The task being claimed is already this agent's own row when the backend
        # would treat the claim as a no-op; only a NEW row counts.
        if isinstance(task, dict) and task.get('assignee') == claimant:
            return
        agent_actor = self.service.agent_actor(owner)
        project_id = ctx.params['pid']
        # Every project the agent is granted is read, not only the one being claimed
        # (kittrial-5bb.276 revision, review item 2b). Reading this project alone and
        # treating "the agent has another project" as an incomplete set made a phantom
        # held task, so an agent granted two projects could never claim at all while
        # /v1/agents/me/next said it held nothing. It is the same set the attention
        # view walks, so the gate and the read cannot disagree about held and
        # available.
        projects = []
        for pid in [project_id] + list(owner.get('projects') or []):
            if isinstance(pid, str) and pid not in projects:
                projects.append(pid)
        held, blocked_tasks, complete = [], [], True
        for pid in projects:
            try:
                read = self.backend.agent_tasks(pid, agent_actor)
            except Exception as error:
                # Any failure at all is an unknown held set, never "no limit": the claim
                # is refused with a sentence that says the read failed, rather than
                # turning a tracker error into an unlimited agent.
                raise conflict(
                    'This agent\'s held tasks could not be read, so the claim is refused: %s'
                    % error, {'claim_limit': limit, 'hard_cap': cap, 'held_readable': False}) from None
            if not read.get('complete'):
                complete = False
            rows = read.get('tasks') or []
            held.extend(held_against_limit(rows))
            blocked_tasks.extend(held_blocked(rows))
        sentence = check(limit, held, more=0 if complete else 1, blocked=blocked_tasks)
        if sentence is None:
            return
        raise conflict(sentence, {'claim_limit': limit,
                                  'hard_cap': cap,
                                  'held': [row.get('id') for row in held],
                                  'blocked': [row.get('id') for row in blocked_tasks],
                                  'held_readable': complete,
                                  # The claim route reads this to attach the same
                                  # bounded claimable-candidate fall-through an
                                  # ordinary held refusal carries, so an agent at
                                  # its limit is still handed somewhere to go.
                                  'claim_limit_refusal': True})

    def _agent_guidance_item(self, project_id, actor):
        """This actor's guidance move item for one project, or None (kittrial-5bb.325).

        Only when this service serves BOTH routes a web agent needs to clear it - reading
        the guidance and acknowledging a version over HTTP (:func:`serves_web_guidance`,
        kittrial-5bb.212). Until then the item is withheld: .325 made a web agent stop and
        ask, correctly, because over HTTP there is nothing it can run to clear the item
        (kittrial-5bb.334). The SSH/endpoint reads keep it unchanged.

        A backend with no guidance store (the in-process test backend) or a guidance
        read that fails answers None: a guidance read never turns another read into an
        error, exactly as an unreadable guidance record keeps its own repaired shape.
        The item carries the version and the command, never the guidance text.
        """
        if not all(serves_web_guidance()):
            return None
        reader = getattr(self.backend, 'guidance_state', None)
        if reader is None:
            return None
        try:
            block = reader(project_id, actor)
        except Exception:
            return None
        item = block.get('item') if isinstance(block, dict) else None
        return item if isinstance(item, dict) else None

    @staticmethod
    def _guidance_action(project_id, actor, item):
        """The one guidance acknowledgement as the agent's first next_action.

        Priority 0 puts it ahead of every task action: the coordinator cannot reach a run
        prompt that never calls `guidance get`, so a changed guidance version is the
        agent's own move on its queue (kittrial-5bb.325). It carries the version and the
        command the endpoint's own item names, and no task link, because it is not a task.
        """
        return {'priority': 0, 'kind': 'guidance', 'project': project_id,
                'task': item.get('task') or 'guidance',
                'title': item.get('title'), 'status': None,
                'review_state': item.get('review_state'),
                'assignee': actor, 'reason': item.get('text'),
                'command': item.get('command'),
                'guidance_version': item.get('guidance_version'),
                'links': {'project': '/v1/projects/%s' % project_id}}

    def _agent_attention(self, principal, agent, many=False, role=None):
        """Owner/agent-visible attention, computed at read time from bounded reads.

        Each granted project is re-authorized with the same live-authority check the
        task routes apply (:meth:`_agent_may_read`), so a project the principal can no
        longer read drops out of attention instead of leaking its tasks.

        A complete current-work snapshot supplies own states and unclaimed tasks.
        Single-agent reads recover own tasks with an owner-filtered read when it
        is incomplete. Owner lists reuse the bounded rows and report incomplete
        own counts. Nothing is cached across requests here.

        One action per own task, the most pressing reason first, then in this order:
        changes requested (1), blocked by its latest checkpoint (2), in progress with no
        contribution yet (3), then claimable work (4), then a contribution that waits
        for a reviewer or, once approved, for integration (5): the agent can do nothing
        about those, so they never sit ahead of work the agent can do. ``in_progress`` counts own open tasks with no
        contribution that are NOT blocked. A blocked action
        carries ``blocked_since`` (when the checkpoint was written). Blocked and
        in-progress actions carry ``newer_activity`` (whether another actor wrote
        activity the checkpoint did not incorporate), so an
        agent can leave a blocked task with nothing new alone instead of re-reading it
        and writing another checkpoint on every wake. Delivered work follows its
        review state and does not count as blocked, regardless of open items.
        """
        actor = agent.get('actor') or agent.get('id')
        effective_role = role if role is not None else agent.get('role')
        from work import review_move                                            # one move sentence for both answers (kittrial-5bb.249)
        from claim_limit import actor_holds, held_against_limit, held_blocked   # the one counted-held rule the claim gate uses
        counts = {'claimable': 0, 'claimed': 0, 'changes_requested': 0,
                  'awaiting_review': 0, 'blocked': 0, 'in_progress': 0, 'awaiting_integration': 0,
                  'review_errors': 0, 'checkpoint_errors': 0, 'read_errors': 0,
                  'review_recommended': 0, 'to_review': 0, 'newer_activity': 0, 'guidance': 0,
                  'comment_attention': 0}
        #: The rows that count against this agent's claim limit and hard cap, by the one
        #: rule the gate uses, so /v1/agents/me/next and the gate cannot disagree about
        #: held and available (kittrial-5bb.276 revision; hard cap in kittrial-5bb.300).
        claim_held = 0
        claim_blocked = 0
        own_actions = []
        guidance_actions = []
        claimable_actions = []
        review_actions = []
        truncated = False
        snapshot_truncated = own_tasks_truncated = actions_truncated = False
        claiming_modes = {}
        for project_id in agent.get('projects') or []:
            if not self._agent_may_read(principal, project_id):
                continue
            # The per-project claiming setting (kittrial-5bb.352), read FRESH here (it
            # lives outside the tracker, so the tracker read cache would not see it) and
            # used by every claimability answer and next-action sentence below.
            claiming_mode = self._claiming_mode(project_id)
            claiming_modes[project_id] = claiming_mode
            # The guidance acknowledgement (kittrial-5bb.325) is this agent's own move in
            # every project it may read: one row per project whose current version this
            # actor has not acknowledged. No guidance set means no row, and no row while
            # this service has no HTTP guidance read/ack route to clear it
            # (kittrial-5bb.334, one gate in _agent_guidance_item).
            #
            # Single-agent reads only: this item is the calling agent's own move, so the
            # owner's multi-agent listing (``many``) does not pay one extra host read per
            # agent per project for a row its reader cannot act on.
            if not many:
                item = self._agent_guidance_item(project_id, actor)
                if item is not None:
                    counts['guidance'] += 1
                    guidance_actions.append(self._guidance_action(project_id, actor, item))
            try:
                read = self._agent_project_tasks(project_id,many=many)
                own = self._agent_own_tasks(project_id, actor, many=many)
            except HttpError:
                # Authorization can change during the read. Still-authorized
                # failed reads are unknown work, never an empty project.
                if self._agent_may_read(principal, project_id):
                    counts['read_errors'] += 1
                    truncated = snapshot_truncated = own_tasks_truncated = True
                continue
            if not read['complete'] or not own['complete']:
                truncated = True
            snapshot_truncated |= not read['complete']
            own_tasks_truncated |= not own['complete']
            for task in own['tasks']:
                counts['claimed'] += 1
                claim_held += len(held_against_limit([task]))
                claim_blocked += len(held_blocked([task]))
                review = task.get('review_state')
                is_open = task.get('status') != 'closed'
                open_items = task.get('open_items',0)
                blocking_items = attention_blocking_items(task)
                comment_pending = is_open and task.get('assignee_comment_pending') is True and review != 'error'
                delivered=bool(task.get('contribution_id')) or review not in (None,'none')
                unreadable = is_open and (open_items is None or blocking_items is None)
                blocked = is_open and not delivered and type(blocking_items) is int and blocking_items > 0
                in_progress = (is_open and not blocked and not unreadable and not task.get('contribution_id')
                               and review in (None, 'none'))
                integrating = review in ('awaiting-integration', 'approved')
                counts['comment_attention'] += comment_pending and delivered
                counts['changes_requested'] += review == 'changes-requested'
                counts['awaiting_review'] += review == 'awaiting-review'
                counts['awaiting_integration'] += integrating
                counts['blocked'] += blocked
                counts['in_progress'] += in_progress
                counts['newer_activity'] += (blocked or in_progress or comment_pending) and task.get('newer_activity') is True
                counts['review_errors'] += review=='error'
                counts['checkpoint_errors'] += unreadable and review!='error' and not delivered
                details = {'requests': list(task.get('pending_change_requests') or [])[:20], 'open_items': open_items,
                           'blocking_items':blocking_items,
                           'blocked_since': task.get('checkpoint_at') if blocked else None,
                           'newer_activity': task.get('newer_activity') if blocked or in_progress or comment_pending else None,
                           # A stable marker of the newest other-party activity after the
                           # checkpoint, plus the counts it was read with (kittrial-5bb.351).
                           # Held actions only, exactly like the boolean it refines; a
                           # legacy checkpoint yields null and a reader falls back to it.
                           'newer_activity_marker': task.get('newer_activity_marker')
                           if blocked or in_progress or comment_pending else None,
                           # The current fields digest alone (kittrial-5bb.367 F2b).
                           'newer_activity_fields': task.get('newer_activity_fields')
                           if blocked or in_progress or comment_pending else None,
                           # The checkpoint-independent wait marker (kittrial-5bb.369),
                           # carried beside the marker on held actions so /next, /wait and
                           # the task read answer the same fields.
                           'wait_newer_activity_marker': task.get('wait_newer_activity_marker')
                           if blocked or in_progress or comment_pending else None,
                           'newer_activity_by_others': task.get('newer_activity_by_others')
                           if blocked or in_progress or comment_pending else None,
                           'newer_activity_own': task.get('newer_activity_own')
                           if blocked or in_progress or comment_pending else None,
                           'newer_activity_coverage': task.get('newer_activity_coverage')
                           if blocked or in_progress or comment_pending else None,
                           'unresolved_directions': task.get('unresolved_directions')
                           if blocked or in_progress or comment_pending else None}
                # Whose move it is, in the same words as the canonical `work --mine` row
                # (kittrial-5bb.249): one helper, so the two answers cannot disagree about the
                # move, the item count or the redelivery fact. The one deliberate difference is
                # the way back: a web agent cannot run the client's `review TASK`, so the
                # sentence names this task's brief route instead (kittrial-5bb.253 item 4). N is
                # the canonical open-item count where the backend carries it. The named-reviewer
                # case is not reachable here: these are the agent's own tasks.
                item_count = task.get('pending_review_items')
                if type(item_count) is not int or item_count < 0:
                    item_count = len(details['requests'])
                task_base = '/v1/projects/%s/tasks/%s' % (project_id, task.get('id'))
                move, move_text = review_move(review, task.get('id'),
                                              actor_holds(task.get('assignee'), actor),
                                              pending_items=item_count,
                                              redelivered=bool(task.get('redelivered')),
                                              way_back='`%s`' % (task_base + '/brief'),
                                              comment_pending=comment_pending, assignee=task.get('assignee'))
                details['whose_move'], details['next'] = move, move_text
                if review != 'changes-requested' and not in_progress and not comment_pending:
                    # The task asked for the two fields only where the kind is
                    # changes-requested (review item 2b): every other kind has a mover who is
                    # not this agent (an operator reconciles a review error or an unreadable
                    # checkpoint; a reviewer or the coordinator holds awaiting work), so it
                    # carries neither field instead of pairing `who: operator` with
                    # `whose_move: you` or contradicting the queue row's blocked/waiting
                    # sentence. `in-progress` is the agent's OWN undelivered, assigned task,
                    # so it carries its move too (kittrial-5bb.288): a lane assigned a task
                    # it never touched is told it is that lane's move, in the same words as
                    # the canonical `work --owner ACTOR` row.
                    details.pop('whose_move', None)
                    details.pop('next', None)
                if comment_pending and delivered and review != 'changes-requested':
                    own_actions.append(self._agent_action(
                        1, 'new-comments', project_id, task,
                        'New task comments need your attention; read the task history and record the next step.',
                        **details))
                elif review == 'changes-requested':
                    own_actions.append(self._agent_action(
                        1, 'changes-requested', project_id, task,
                        'A reviewer requested changes on this contribution.', **details))
                elif review == 'error':
                    own_actions.append(self._agent_action(
                        2, 'review-error', project_id, task,
                        'Review state error: an operator must reconcile the malformed review history.',
                        who='operator', **details))
                elif unreadable and not delivered:
                    own_actions.append(self._agent_action(
                        2,'checkpoint-error',project_id,task,
                        'Checkpoint history could not be read; an operator must reconcile it.',
                        who='operator',**details))
                elif blocked:
                    own_actions.append(self._agent_action(
                        2, 'blocked', project_id, task,
                        'The latest checkpoint left an unresolved blocker or dependency.', **details))
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
                    action = (next_action(task, claiming=claiming_mode)
                              if review in ('withdrawn', 'superseded', 'integrated',
                                            'legacy-review-ready') else None)
                    action = action or {'who': 'owner', 'text': 'Check the task and its review history'}
                    own_actions.append(self._agent_action(
                        5, 'review-state', project_id, task,
                        'Review state %s: %s.' % (review or 'unknown', action['text']),
                        who=action['who'], **details))
            for task in read['tasks']:
                # The one bounded interpretation (kittrial-5bb.6 deliverable b): a row the
                # shared predicate calls claimable, not "any open unassigned task". Epics,
                # decisions, gates, jobs, record anchors, delivered work and owned work are
                # therefore never offered as claimable suggestions.
                if task.get('claimable') is True:
                    counts['claimable'] += 1
                    if len(claimable_actions) < AGENT_CLAIMABLE_LIMIT:
                        claimable_actions.append(self._agent_action(
                            4, 'claimable-task', project_id, task,
                            'Open, unclaimed work the agent may take.'))
            # Review work for this agent, from the same snapshot (kittrial-5bb.115): one call.
            # When the agent is configured (or queried) as a worker, omit other people's
            # deliveries awaiting review while keeping CAP_REVIEWS for its own deliveries.
            if effective_role != 'worker':
                review_actions += self._agent_review_actions(principal, agent, project_id, read['tasks'], counts)
        # Counts cover the read pages; incomplete-source counts are lower bounds.
        # Only the collected suggestions are capped,
        # so a long claimable list can never hide the agent's own feedback.
        if counts['claimable'] > len(claimable_actions):
            truncated = True
            actions_truncated = True
        review_actions = self._capped_review_actions(review_actions)
        if counts['review_recommended'] + counts['to_review'] > len(review_actions):
            truncated = True
            actions_truncated = True
        actions = guidance_actions + own_actions + review_actions + claimable_actions
        actions.sort(key=lambda a: (a['priority'], self.AGENT_KIND_ORDER.get(a['kind'], 0),
                                   0 if a['kind'] == 'in-progress' and a.get('newer_activity') is True else 1,
                                   a['project'], a['task']))
        if len(actions) > AGENT_ACTION_LIMIT:
            actions = actions[:AGENT_ACTION_LIMIT]
            truncated = True
            actions_truncated = True
        if counts['read_errors']:
            state = 'error'
        elif counts['changes_requested']:
            state = 'changes-requested'
        elif counts['review_errors'] or counts['checkpoint_errors']:
            state = 'error'
        elif counts['comment_attention']:
            state = 'working'
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
        elif snapshot_truncated or own_tasks_truncated:
            state = 'unknown'
        else:
            state = 'idle'
        summary = (self._agent_summary(state, counts, snapshot_truncated, own_tasks_truncated)
                   + self._agent_review_summary(counts, snapshot_truncated)
                   + ((' %s%d %s task(s) have newer activity; read their linked briefs and history.'
                       % ('at least ' if own_tasks_truncated else '', counts['newer_activity'],
                          'assigned' if counts['comment_attention'] else 'owned undelivered'))
                      if counts['newer_activity'] else ''))
        if guidance_actions:
            # A changed guidance version leads, because it changes what the agent should do
            # next and no run prompt is guaranteed to run `guidance get` (kittrial-5bb.325).
            summary = ('The project guidance changed since this agent last acknowledged it; read it and '
                       'record the read before other work. ' + summary)
        empty_message = None
        if not actions and 'assigned' in claiming_modes.values():
            from claiming import EMPTY_SENTENCE
            empty_message = EMPTY_SENTENCE
        result = {'state': state, 'summary': summary,
                  'counts': counts, 'claim_limit_held': claim_held,
                  'claim_limit_blocked': claim_blocked, 'actions': actions,
                  'truncated': truncated,
                  'snapshot_truncated':snapshot_truncated,'own_tasks_truncated':own_tasks_truncated,
                  'actions_truncated':actions_truncated,
                  'computed_at': now_iso(self.service._now())}
        # The empty-list sentence (kittrial-5bb.352 adjustment 4): under 'assigned', an
        # empty answer says who hands work out, so the Bridge's empty-list message can
        # show it instead of "nothing to do".
        if empty_message:
            result['empty_message'] = empty_message
        return result

    @staticmethod
    def _agent_summary(state, counts, snapshot_truncated=False, own_tasks_truncated=False):
        claimable = ('at least ' if snapshot_truncated else '') + str(counts.get('claimable', 0))
        def own_count(name):
            return ('at least ' if own_tasks_truncated else '') + str(counts.get(name, 0))
        if state == 'error':
            if counts.get('read_errors'):
                return ('%d project work read(s) failed; counts are incomplete. Retry the work read.'
                        % counts['read_errors'])
            return ('%d review history error(s), %d unreadable checkpoint history error(s); an operator must reconcile them.'
                    % (counts.get('review_errors',0),counts.get('checkpoint_errors',0)))
        if state == 'changes-requested':
            return ('%s contribution(s) have changes requested; act on them first.'
                    % own_count('changes_requested'))
        if state == 'blocked':
            return ('%s task(s) have unresolved blockers or dependencies.'
                    % own_count('blocked'))
        if state == 'working' and counts.get('comment_attention'):
            return ('%s assigned task(s) have new comments; read their history and record the next step.'
                    % own_count('comment_attention'))
        if state == 'working':
            return ('%s claimed task(s) in flight, %s not delivered yet; %s claimable.'
                    % (own_count('claimed'), own_count('in_progress'), claimable))
        if state == 'waiting-review':
            return ('%s contribution(s) waiting for a human review decision.'
                    % own_count('awaiting_review'))
        if state == 'waiting-integration':
            return ('%s approved contribution(s) waiting for integration; nothing for the agent to do.'
                    % own_count('awaiting_integration'))
        if state == 'unknown':
            return ('Incomplete work read: %s claimable task(s) observed; check project work for more.' % claimable)
        return ('Idle: %s claimable task(s), nothing in flight.' % claimable)

    @staticmethod
    def _agent_claim_limit_record_view(agent):
        """The limit and who may raise it, from the agent record only (kittrial-5bb.303).

        No work read: ``claim_limit`` is on the record and ``raise_by`` is one constant.
        The counted ``held``/``available`` view needs the bounded current-work snapshot,
        so it is answered only where that snapshot is already read, on
        ``GET /v1/agents/me/next`` (:meth:`_agent_claim_limit_view`).
        """
        from claim_limit import RAISE_BY, default_limit
        return {'limit': default_limit(agent), 'raise_by': RAISE_BY}

    @staticmethod
    def _agent_claim_limit_view(agent, attention):
        """The per-agent claim limit and what holds it (kittrial-5bb.276/.300).

        Exactly the value the claim route enforces, from the same helper and the
        same held set the attention snapshot already counted, so /v1/agents/me/next
        can never report a different limit or a different held set than the gate.
        The held count is ``claim_limit.held_against_limit`` over the agent's own
        rows, not the raw ``counts.claimed``: a delivered or a blocked task is not
        the agent's move and holds no active claim slot (revision, review items
        2b/3b), while ``available`` is also bounded by ``claim_limit.hard_cap`` over
        all undelivered claims (active plus blocked, kittrial-5bb.300).
        An incomplete own read is a lower bound, so it reports no availability,
        exactly as the gate refuses, rather than reading as "no limit".

        ``raise_by`` names who may RAISE the limit (kittrial-5bb.302): the owner's account
        or a superuser on the My agents page. An agent can only lower it itself, so a
        client that follows the server shows the same names and page the refusal sentence
        does.
        """
        from claim_limit import RAISE_BY, default_limit, hard_cap
        attention = attention or {}
        counts = attention.get('counts') or {}
        limit = default_limit(agent)
        cap = hard_cap(limit)
        held = int(attention.get('claim_limit_held') or 0)
        blocked = int(attention.get('claim_limit_blocked') or 0)
        complete = not (attention.get('own_tasks_truncated') or counts.get('read_errors'))
        available = min(max(0, limit - held), max(0, cap - (held + blocked))) if complete else 0
        return {'limit': limit, 'hard_cap': cap, 'held': held, 'blocked': blocked,
                'available': available,
                'counts_task': counts.get('in_progress'),
                'raise_by': RAISE_BY}

    @staticmethod
    def _agent_attention_view(agent, attention=None):
        if attention is None:
            attention = {'state': 'unknown', 'summary': 'Not computed.',
                         'counts': {}, 'actions': [], 'truncated': False,
                         'snapshot_truncated': False, 'own_tasks_truncated': False,
                         'actions_truncated': False,
                         'computed_at': None}
        view = {key: attention[key] for key in
                ('state', 'summary', 'counts', 'truncated', 'snapshot_truncated',
                 'own_tasks_truncated', 'actions_truncated', 'computed_at')}
        # kittrial-5bb.352 review item S1: the empty-list sentence the attention read
        # computed under 'assigned' must travel on the route, not be dropped here.
        if attention.get('empty_message'):
            view['empty_message'] = attention['empty_message']
        return view

    def _agent_resume_prompt(self, agent, attention):
        """A copyable prompt for the owner's local assistant. No secret is included."""
        server = self.service.public_url or '<ORCHESTRA_SERVER_URL>'
        directory = agent.get('working_directory')
        where = (' (recorded folder path data: %s)' % json.dumps(directory)) if directory else ''
        first = attention['actions'][0] if attention['actions'] else None
        if first:
            if first.get('kind') == 'guidance':
                # The guidance row is not a task (kittrial-5bb.325): name the command.
                what = ('Next: the project guidance changed; %s.'
                        % (first.get('command') or 'run guidance get, then guidance ack --version VERSION'))
            else:
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
                "  PowerShell:  curl.exe -fsS -K \"%s\" %s\n"
                "  macOS/Linux: curl -fsS -K %s %s"
                % (where, agent['name'], agent['id'], what, secret_file['windows'],
                   # kittrial-5bb.360: the address single-quoted, so nothing in it expands.
                   secret_file['windows_powershell'], ps_quote(server + '/v1/agents/me/next'),
                   secret_file['posix'], sh_quote(server + '/v1/agents/me/next')))

    # -- task routes -----------------------------------------------------------
    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks')
    def tasks_create(self, ctx):
        self._project(ctx, CAP_TASKS)
        payload = dict(ctx.payload or {})
        # A field this route does not take is refused, not dropped (kittrial-5bb.183 item 2).
        refuse_unknown_fields(payload, self.TASK_CREATE_FIELDS, 'A task creation')
        priority = payload.get('priority')
        if priority is not None and (type(priority) is not int or not 0 <= priority <= 4):
            raise invalid('Task priority must be an integer 0-4')
        payload['attachments'] = validate_attachments(payload.get('attachments'))
        # The title is checked here as on a change, before the key is reserved (kittrial-5bb.185):
        # an empty one and one of 600 characters went to bd, which refused them, and the caller
        # was told that the task may have been made. A blank one bd stored as it came.
        self._task_title(payload.get('title'))
        if payload.get('description') is not None and not isinstance(payload['description'], str):
            raise invalid('Task description must be text')
        self._bind_task_actor(ctx, payload)

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
        payload = self._task_change(ctx)
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']), write=True)

        def update():
            result = self.backend.invoke('tasks.update', ctx.principal, ctx.params['pid'],
                                         payload, ctx.idempotency_key,
                                         target=ctx.route_target, authorize=ctx.authorize,
                                         capability=CAP_TASKS)
            return result, result
        return self._mutate(ctx, 'tasks.update', ctx.params['pid'], update,
                            capability=CAP_TASKS, serialize=False, canonical=True)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/assignment-roster')
    def task_assignment_roster(self, ctx):
        self._project(ctx,CAP_COORDINATE)
        if not hasattr(self.backend,'assignment_roster'):
            raise not_implemented('Canonical assignment roster is unavailable on this server')
        refuse_unknown_fields(ctx.query,('limit','cursor'),'Assignment roster query')
        limit,state=self._page(ctx,ctx.query)
        result=self.backend.assignment_roster(ctx.principal,ctx.params['pid'],state['o'],limit)
        result['next_cursor']=(make_cursor(ctx.principal,ctx.params['pid'],ctx.query,state['o']+limit,
                                           key=self.service.cursor_key())
                               if result['more'] else None)
        return 200,result

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/assign')
    def task_assign(self,ctx):
        self._project(ctx,CAP_COORDINATE)
        if 'tasks.assign' not in self.backend.ROUTES:
            raise not_implemented('Canonical task assignment is unavailable on this server')
        if ctx.idempotency_key is None:
            raise invalid('Send an Idempotency-Key and reuse it with the identical assignment after an uncertain outcome')
        payload=dict(ctx.payload or {})
        fields=('expected_assignee','assignee','reason','allow_pending_review','allow_held_slot')
        refuse_unknown_fields(payload,fields,'Task assignment')
        from task_assignment import validate_request
        try:
            validate_request(dict(payload,schema_version=1,operation_id='http-validation',task=ctx.params['tid']))
        except ValueError as error:
            raise invalid(str(error)) from None
        payload['task_id']=ctx.params['tid']
        # An AGENT assigning a task to ITSELF through tasks.assign is a claim and takes
        # the same per-agent limit as the claim route (revision, review item 5a): a
        # granted coordinator agent at its limit held a second task this way. A PERSON
        # assigning a task to an agent stays allowed even at the limit: that is the
        # owner's explicit choice, not the agent taking work. docs/HTTP_DEPLOYMENT.md
        # states both.
        claim_gate = (ctx.principal.actor if getattr(ctx.principal, 'via', None) == 'credential'
                      else None)
        def assign():
            def write():
                result=self.backend.invoke('tasks.assign',ctx.principal,ctx.params['pid'],payload,
                                           ctx.idempotency_key,target=ctx.route_target,
                                           authorize=ctx.authorize,capability=CAP_COORDINATE)
                return result,result
            if claim_gate is not None and claim_limit.actor_holds(payload.get('assignee'), claim_gate):
                with self.service.claim_gate(ctx.principal.agent_id or claim_gate):
                    self._refuse_claim_over_limit(
                        ctx, self.backend.get_task(ctx.params['pid'], ctx.params['tid']), payload.get('assignee'))
                    return write()
            return write()
        return self._mutate(ctx,'tasks.assign',ctx.params['pid'],assign,capability=CAP_COORDINATE,
                            serialize=False,canonical=True,audit_metadata={'task':ctx.params['tid']})

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/release')
    def task_release(self, ctx):
        """Release the caller's own claimed, undelivered task (kittrial-5bb.301).

        The owner's own worker held three tasks and could not take other work, because
        it could not release one of its own claims: only the owner's account or a
        superuser could clear an assignee (kittrial-5bb.276). This route lets the HOLDER
        release its own claim with a short reason. The holder is the authenticated
        caller, never a submitted field, so no caller can release another actor's claim
        by naming it.

        It builds the same canonical assignment with ``assignee: null`` that every other
        release uses, so it invents no second way to clear an assignee; the canonical
        ``task-release`` action permits the ordinary task authority for exactly this
        operation and nothing else (see endpoint.py). The release is recorded as the
        canonical assignment notice: who, when and the reason. A checkpoint and its open
        items stay on the task, and the claim stops counting against the holder's
        ``claim_limit`` at once.

        Refused: a task the caller does not hold (409); a record anchor (404) or the merge
        slot (409, as every task write route); a task with ANY current contribution (409,
        the canonical check: awaiting review, changes requested, approved or integrated);
        a body field other than the reason, a blank reason or one over
        :data:`CLAIM_RELEASE_REASON_MAX` (422); a missing Idempotency-Key (422). On a
        backend without canonical assignment: 501. An exact retry of a committed release is
        answered from the stored result, before the holder check, so a lost response
        reconciles instead of reading the now-unassigned task as somebody else's.
        """
        self._project(ctx, CAP_TASKS)
        payload = dict(ctx.payload or {})
        refuse_unknown_fields(payload, self.TASK_RELEASE_FIELDS, 'A claim release')
        reason = payload.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise invalid('A claim release needs a short reason')
        if len(reason) > self.CLAIM_RELEASE_REASON_MAX:
            raise invalid('A claim release reason is at most %d characters'
                          % self.CLAIM_RELEASE_REASON_MAX)
        if ctx.idempotency_key is None:
            raise invalid('Send an Idempotency-Key: releasing a claim writes')
        if 'tasks.release' not in self.backend.ROUTES:
            raise not_implemented('Canonical claim release is unavailable on this server')
        actor = self.service.bind_actor(ctx.principal, None)
        request = {'task_id': ctx.params['tid'], 'reason': reason.strip()}

        def release():
            # These checks run INSIDE the mutation, after _mutate has answered an exact retry
            # from the stored result (kittrial-5bb.301 review item 2). They used to run
            # before it, so a retry after a lost response read the now-unassigned task as
            # "not claimed by you" instead of replaying the committed release. A different
            # body under the same key is still refused by the wrapper's key-reuse check,
            # which runs before this.
            task = self.backend.get_task(ctx.params['pid'], ctx.params['tid'])
            self._refuse_record_anchor(task, write=True)
            assignee = (task.get('assignee') or None) if isinstance(task, dict) else None
            if not isinstance(task, dict) or not claim_limit.actor_holds(assignee, actor):
                raise conflict('That task is not claimed by you; only its holder can release it')
            result = self.backend.invoke('tasks.release', ctx.principal, ctx.params['pid'],
                                         dict(request, expected_assignee=assignee),
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=CAP_TASKS)
            return result, result
        return self._mutate(ctx, 'tasks.release', ctx.params['pid'], release, capability=CAP_TASKS,
                            serialize=False, canonical=True,
                            audit_metadata={'task': ctx.params['tid']})

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/comments')
    def tasks_comment(self, ctx):
        self._project(ctx, CAP_TASKS)
        payload = dict(ctx.payload or {})
        refuse_unknown_fields(payload, ('text',), 'A task comment')
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip():
            raise invalid('A task comment must be nonempty text of at most 65536 UTF-8 bytes')
        try:
            text_bytes = len(text.encode('utf-8'))
        except UnicodeEncodeError:
            raise invalid('A task comment must be valid UTF-8 text') from None
        if text_bytes > MAX_ATTACHMENT_BYTES:
            raise invalid('A task comment must be nonempty text of at most 65536 UTF-8 bytes')
        if '\x00' in text:
            raise invalid('A task comment cannot contain NUL')
        tid, pid = ctx.params['tid'], ctx.params['pid']
        self._refuse_record_anchor(self.backend.get_task(pid, tid), write=True)
        from reserved_comments import check_comment_body
        try:
            check_comment_body(text, 'attachment', actor=ctx.principal.actor, task=tid)
        except ValueError as error:
            raise invalid(str(error)) from None
        payload['task_id'] = tid

        def comment():
            result = self.backend.invoke('tasks.comment', ctx.principal, pid, payload,
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=CAP_TASKS)
            if isinstance(result, list) and len(result) == 1:
                result = result[0]
            # Do not invent identity or time, or echo an arbitrary backend object.
            if not isinstance(result, dict) or not result.get('id') or result.get('text') != text:
                raise UncertainOutcome()
            public = {'comment': {k: result[k] for k in ('id', 'issue_id', 'author', 'text', 'created_at') if k in result}}
            return public, public
        return self._mutate(ctx, 'tasks.comment', pid, comment, status=201,
                            capability=CAP_TASKS, serialize=False, canonical=True,
                            audit_metadata={'task': tid, 'text_bytes': text_bytes})

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
        # Rows the tracker returned and the service could not read (kittrial-5bb.169): the
        # ones with an id stay in the list, marked; all are named beside it, whatever the
        # page or the filter, so that one such row is seen and not a reason to show nothing.
        unreadable = [t['id'] for t in rows if isinstance(t, dict) and t.get('malformed') and t.get('id')]
        def review_states(of):
            # The review states come from another read of the endpoint. While a row cannot be
            # read that read may fail too (an endpoint from before it learned to name such a
            # row): the list is then answered without review states, and says so, instead of
            # failing with it. Without an unreadable row a failure is what it was.
            try:
                return self._with_review_states(ctx.params['pid'], of, shared=True)
            except HttpError:
                if not unreadable:
                    raise
                for row in of:
                    if isinstance(row, dict):
                        row.setdefault('review_state', None)
                unavailable.append(True)
                return False
        unavailable = []
        if filters:
            rows = [t for t in rows if isinstance(t, dict)]
            complete = review_states(rows)
            rows = [t for t in rows if task_matches(t, filters)]
            result = {'items': rows[state['o']:state['o'] + limit], 'total': len(rows)}
        else:
            result = {'items': rows[state['o']:state['o'] + limit], 'total': snapshot.get('total', len(rows))}
            complete = review_states(result['items'])
        result['items'] = self._task_views(result['items'], ctx.params['pid'])
        # False when some rows' review state is unknown (``review_state: null``): the
        # bounded canonical projection did not cover them, or they are closed tasks
        # whose finished review the queue no longer lists.
        result['review_states_complete'] = complete and all(
            t.get('review_state') is not None for t in result['items'])
        result['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                             state['o'] + limit, key=self.service.cursor_key())
                                 if state['o'] + limit < result['total'] else None)
        if unreadable:
            result['unreadable'] = unreadable
            if unavailable:
                result['review_states_unavailable'] = True
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
        return 200, self._task_views([row], ctx.params['pid'])[0]

    def _row_or_none(self, project_id, task_id):
        """The task's row for deciding why another read of it failed; None when this read fails too."""
        try:
            return self.backend.get_task(project_id, task_id)
        except HttpError:
            return None

    @staticmethod
    def _refuse_record_anchor(row, write=False):
        """A record anchor is not a task: the task, brief and history routes answer
        404 for it, never its row or raw record comments, and the task write routes
        (PATCH, claim, checkpoints, reviews) refuse it the same way (kittrial-5bb.64).

        The project's merge slot is not a task either (kittrial-5bb.113). The read
        routes answer 404 for it; a write route says what it is, so an agent that was
        once offered it learns why the claim is refused."""
        if isinstance(row, dict) and row.get('malformed'):
            # The row exists and cannot be read: say that, for a read and for a write
            # (kittrial-5bb.169). Nothing of an unreadable row is shown or changed here.
            raise unreadable_row(row.get('id'))
        if is_record_anchor(row):
            raise not_found('Task not found')
        if is_merge_slot(row):
            if write:
                raise conflict(merge_slot_sentence(row.get('id')))
            raise not_found('Task not found')

    @staticmethod
    def _refuse_open_dependency(row, snapshot):
        """A task blocked by an open dependency is not claimable (kittrial-5bb.293).

        The claim route refuses it with one sentence naming the open dependency, the
        same rule ``work.claimable()`` applies so ``work --claimable`` and
        ``/v1/agents/me/next`` never offer it. ``ready`` already excludes blocked
        tasks; this matches it. Only the blocking dependency types block (item 3b)."""
        from work import dependency_index, open_dependency_ids, open_dependency_sentence
        open_deps = open_dependency_ids(row, snapshot, index=dependency_index(snapshot))
        if open_deps:
            raise conflict(open_dependency_sentence(open_deps) or 'Task is blocked by an open dependency')

    def _refuse_open_dependency_closed(self, row, backend, project_id):
        """Like :meth:`_refuse_open_dependency` but fails CLOSED (kittrial-5bb.293 item 3c).

        The claim route must never offer a claim the queue would refuse. When the
        other ends' statuses cannot be read, the claim is refused with one sentence
        naming that, rather than being allowed through."""
        from work import BLOCKING_DEPENDENCY_TYPES, dependency_index, dependency_ref, open_dependency_ids, open_dependency_sentence
        deps = []
        ids = []
        for d in row.get('dependencies') or []:
            other_id, kind, _ = dependency_ref(d)
            if kind in BLOCKING_DEPENDENCY_TYPES and other_id:
                deps.append(d)
                ids.append(other_id)
        if not deps:
            return
        try:
            snapshot = backend.read_tasks(project_id)['items'] if hasattr(backend, 'read_tasks') else []
        except Exception:
            snapshot = []
        by_id = dependency_index(snapshot)
        # Fail CLOSED: a blocking dependency whose other end is not in the snapshot
        # cannot be shown to be closed, so the claim is refused.
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise conflict('Task is blocked by an open dependency: `%s`. Its status cannot be read, '
                           'so the claim is refused.' % '`, `'.join(missing[:5]))
        self._refuse_open_dependency(row, snapshot)

    @staticmethod
    def _refuse_requirement_record(row):
        """A requirement record is a readable, visible row, never claimable work
        (kittrial-5bb.277): it is NOT a record anchor and is NOT hidden (kittrial-5bb.276
        needs a claimed requirement visible and releasable), so the claim route says what
        it is and where it is edited and accepted rather than answering 404.

        What is refused here is this route's claim, and only that: an explicit host claim
        by a coordinator through the endpoint (`bd update ID --claim`) still works, and
        live projects rely on it (kittrial-5bb.92 review item 1). That fact is kept in
        ``docs/REQUIREMENTS.md`` and deliberately **not** in this sentence (kittrial-5bb.277
        review item 3): the caller who reads a refusal is usually an agent, and naming the
        host command here would hand it the exact way around the refusal. The sentence
        names the Requirements page instead.
        """
        if not isinstance(row, dict):
            return
        from work import REQUIREMENT_RECORD_LABELS, requirement_record
        if not requirement_record(row.get('labels')):
            return
        label = next((name for name in row.get('labels') or ()
                      if name in REQUIREMENT_RECORD_LABELS), 'requirement')
        raise conflict('This is a %s record, not a unit of work, so it cannot be claimed. '
                       'It is not offered as claimable work here: edit it on the project\'s Requirements '
                       'page (or requirement_records.py draft|revise), and an operator accepts it with '
                       'admin.py requirement-apply.' % label)

    # -- reference and capability records over the web (kittrial-5bb.213) -----
    REF_PROPOSE_FIELDS = ('key', 'revision', 'expected_sha256', 'title', 'statement', 'authority',
                          'owner', 'review_by', 'tags', 'decisions')
    REF_REVISE_FIELDS = ('key', 'revision', 'expected_revision', 'expected_sha256', 'title', 'statement',
                         'authority', 'owner', 'review_by', 'tags', 'decisions')
    REF_ACCEPT_FIELDS = ('key', 'revision', 'expected_revision', 'record_sha256', 'expected_sha256',
                         'acceptance_state', 'acceptance', 'decision', 'evidence', 'decision_id',
                         'owners', 'approvers', 'policy', 'unverified_pointer_reason')
    REF_RETIRE_FIELDS = ('key', 'revision', 'expected_revision', 'record_sha256', 'expected_sha256',
                         'successor', 'superseded_by', 'acceptance_state', 'acceptance', 'decision',
                         'evidence', 'reason', 'decision_id', 'owners', 'approvers', 'policy')

    CAP_PROPOSE_FIELDS = ('key', 'revision', 'expected_sha256', 'name', 'aliases', 'summary',
                          'requirements', 'anchors', 'code', 'tests', 'owner', 'tags')
    CAP_REVISE_FIELDS = ('key', 'revision', 'expected_revision', 'expected_sha256', 'name', 'aliases',
                         'summary', 'requirements', 'anchors', 'code', 'tests', 'owner', 'tags')
    CAP_ACCEPT_FIELDS = REF_ACCEPT_FIELDS
    CAP_RETIRE_FIELDS = REF_RETIRE_FIELDS
    CAP_VERIFY_FIELDS = ('key', 'revision', 'entry_revision', 'record_sha256', 'expected_sha256',
                         'entry_sha256', 'commit', 'checked_at', 'source', 'graph_built_at_commit',
                         'tool', 'results', 'passed', 'summary', 'method', 'command',
                         'unverified_pointer_reason')
    CAP_ALIAS_PROPOSE_FIELDS = ('key', 'alias', 'evidence')
    CAP_ALIAS_REJECT_FIELDS = ('key', 'alias', 'reason')

    def _records_backend(self):
        if not getattr(self.backend, 'RECORDS', False):
            raise not_implemented('Reference and capability mutations require the canonical endpoint backend')

    def _require_record_membership(self, ctx, coordinator=False):
        pid = ctx.params['pid']
        role = self.service.state.get('memberships', {}).get(pid, {}).get(ctx.principal.user_id)
        if coordinator:
            if role not in ('owner', 'coordinator'):
                raise forbidden('Record coordination requires project membership as an owner or coordinator; '
                                'a superuser without project membership must add themselves to the project first.')
        else:
            if role not in ('contributor', 'coordinator', 'owner'):
                raise forbidden('Record mutations require project membership; '
                                'a superuser without project membership must add themselves to the project first.')

    def _records_standing(self, ctx, pid, capabilities):
        role = self.service.state.get('memberships', {}).get(pid, {}).get(ctx.principal.user_id)

        def holds(cap):
            if cap in capabilities:
                return True
            try:
                self.service.check_authority(ctx.principal, pid, cap)
                return True
            except HttpError:
                return False
        can_propose = role in ('contributor', 'coordinator', 'owner') and holds(CAP_RECORDS)
        can_coordinate = role in ('coordinator', 'owner') and holds(CAP_COORDINATE)
        return can_propose, can_coordinate

    def _enrich_record_view(self, ctx, pid, capabilities, item):
        if not isinstance(item, dict):
            return item
        item = dict(item)
        can_propose, can_coordinate = self._records_standing(ctx, pid, capabilities)
        actors = [item.get('proposer'), item.get('acceptor'), item.get('proposed_by'),
                  item.get('accepted_by'), item.get('accepted_proposer'), item.get('verifier')]
        for block_name, field_names in (('draft', ('proposer',)),
                                        ('acceptance', ('operator', 'proposer')),
                                        ('retirement', ('operator',)),
                                        ('verification', ('verifier',))):
            block = item.get(block_name)
            if isinstance(block, dict):
                actors.extend(block.get(f) for f in field_names)
        for alias_item in item.get('aliases_pending') or []:
            if isinstance(alias_item, dict):
                actors.append(alias_item.get('actor'))
        names = self._actor_names(actors, pid)
        for field in ('proposer', 'acceptor', 'proposed_by', 'accepted_by', 'accepted_proposer', 'verifier'):
            val = item.get(field)
            if isinstance(val, str) and val:
                item[field + '_name'] = names.get(val, val)
        if isinstance(item.get('draft'), dict):
            draft = dict(item['draft'])
            if isinstance(draft.get('proposer'), str):
                draft['proposer_name'] = names.get(draft['proposer'], draft['proposer'])
            item['draft'] = draft
        if isinstance(item.get('acceptance'), dict):
            acc = dict(item['acceptance'])
            if isinstance(acc.get('operator'), str):
                acc['operator_name'] = names.get(acc['operator'], acc['operator'])
            if isinstance(acc.get('proposer'), str):
                acc['proposer_name'] = names.get(acc['proposer'], acc['proposer'])
            item['acceptance'] = acc
        if isinstance(item.get('retirement'), dict):
            ret = dict(item['retirement'])
            if isinstance(ret.get('operator'), str):
                ret['operator_name'] = names.get(ret['operator'], ret['operator'])
            item['retirement'] = ret
        if isinstance(item.get('verification'), dict):
            ver = dict(item['verification'])
            if isinstance(ver.get('verifier'), str):
                ver['verifier_name'] = names.get(ver['verifier'], ver['verifier'])
            item['verification'] = ver
        if isinstance(item.get('aliases_pending'), list):
            item['aliases_pending'] = [
                dict(entry, actor_name=names.get(entry.get('actor'), entry.get('actor')))
                if isinstance(entry, dict) else entry
                for entry in item['aliases_pending']
            ]
        by_agent = ctx.principal.via == 'credential'
        caller_names = [ctx.principal.actor or ctx.principal.user_id, ctx.principal.user_id]
        draft_proposer = (
            item.get('proposed_by')
            or (item.get('draft') or {}).get('proposer')
            or (item.get('proposer')
                if item.get('state') in ('draft-only', 'draft', 'proposed')
                or item.get('waiting_acceptance') or item.get('has_pending_draft')
                else None)
        )
        draft_authors = [a for a in (item.get('draft_authors') or []) if isinstance(a, str) and a]
        if not draft_authors and isinstance(draft_proposer, str) and draft_proposer:
            draft_authors = [draft_proposer]
        item['self_accept_blocked'] = bool(
            can_coordinate and draft_authors
            and (self.service.approval_by_another_party or by_agent)
            and not self._independent_all(caller_names, draft_authors, pid, by_agent))
        verify_proposer = (
            (item.get('accepted_proposer')
             or (item.get('acceptance') or {}).get('proposer')
             or item.get('proposed_by')
             or item.get('proposer'))
            if item.get('record') is not None
            else (item.get('proposed_by') or item.get('proposer'))
        )
        verify_authors_raw = (
            item.get('accepted_draft_authors')
            if item.get('record') is not None
            else item.get('draft_authors')
        )
        verify_authors = [a for a in (verify_authors_raw or []) if isinstance(a, str) and a]
        if not verify_authors and isinstance(verify_proposer, str) and verify_proposer:
            verify_authors = [verify_proposer]
        item['self_verify_blocked'] = bool(
            can_coordinate and verify_authors
            and (self.service.approval_by_another_party or by_agent)
            and not self._independent_all(caller_names, verify_authors, pid, by_agent))
        item['can_propose'] = can_propose
        item['can_coordinate'] = can_coordinate
        item['approval_by_another_party'] = bool(self.service.approval_by_another_party)
        return item

    def _enrich_records_list(self, ctx, pid, result):
        capabilities = self.service.capabilities_for(ctx.principal, pid)
        can_propose, can_coordinate = self._records_standing(ctx, pid, capabilities)
        result = dict(result)
        for list_key in ('items', 'records', 'candidates'):
            if isinstance(result.get(list_key), list):
                result[list_key] = [self._enrich_record_view(ctx, pid, capabilities, item)
                                    for item in result[list_key]]
        if 'items' not in result and isinstance(result.get('records'), list):
            seen = {r.get('key') for r in result['records'] if isinstance(r, dict)}
            extra = [c for c in (result.get('candidates') or [])
                     if isinstance(c, dict) and c.get('key') not in seen]
            result['items'] = list(result['records']) + extra
        result['can_propose'] = can_propose
        result['can_coordinate'] = can_coordinate
        result['approval_by_another_party'] = bool(self.service.approval_by_another_party)
        return result

    def _normalize_record_payload(self, ctx, command, body, operation_id):
        body = dict(body)
        if command == 'revise':
            if 'revision' not in body and 'expected_revision' in body:
                er = body.pop('expected_revision')
                body['revision'] = (er + 1) if (isinstance(er, int) and not isinstance(er, bool)) else er
            else:
                body.pop('expected_revision', None)
        elif command in ('accept', 'retire'):
            body.setdefault('acceptance_state', 'accepted' if command == 'accept' else 'superseded')
            if 'revision' not in body and 'expected_revision' in body:
                body['revision'] = body.pop('expected_revision')
            else:
                body.pop('expected_revision', None)
            if 'record_sha256' not in body and 'expected_sha256' in body:
                body['record_sha256'] = body.pop('expected_sha256')
            else:
                body.pop('expected_sha256', None)
            if command == 'retire':
                if 'successor' not in body and 'superseded_by' in body:
                    body['successor'] = body.pop('superseded_by')
                else:
                    body.pop('superseded_by', None)
            pid = ctx.params['pid']
            account_id = 'account:' + ctx.principal.user_id
            with self.service.store.lock:
                memberships = dict((self.service.state.get('memberships') or {}).get(pid) or {})
            member_owners = [
                'account:' + uid for uid, role in sorted(memberships.items())
                if role in ('owner', 'coordinator')
            ]
            if account_id not in member_owners:
                member_owners.append(account_id)
            if ctx.principal.via == 'credential' and ctx.principal.actor:
                agent_tag = 'agent:' + ctx.principal.actor
                actual_approvers = [account_id, agent_tag]
                actual_owners = member_owners + [agent_tag]
                allowed_approvers = {account_id, ctx.principal.user_id, agent_tag, ctx.principal.actor}
            else:
                actual_approvers = [account_id]
                actual_owners = member_owners
                allowed_approvers = {account_id, ctx.principal.user_id}
            allowed_owners = set(actual_owners) | set(memberships.keys()) | {'account:' + u for u in memberships}
            acc_in = body.get('acceptance') if isinstance(body.get('acceptance'), dict) else {}
            supplied_approvers = body.get('approvers') if 'approvers' in body else acc_in.get('approvers')
            if supplied_approvers is not None:
                if (not isinstance(supplied_approvers, list) or not supplied_approvers
                        or not all(isinstance(x, str) and x in allowed_approvers for x in supplied_approvers)):
                    raise invalid('approvers on the web may only name the authenticated principal performing this acceptance')
            supplied_owners = body.get('owners') if 'owners' in body else acc_in.get('owners')
            if supplied_owners is not None:
                if (not isinstance(supplied_owners, list) or not supplied_owners
                        or not all(isinstance(x, str) and x in allowed_owners for x in supplied_owners)):
                    raise invalid('owners on the web must come from project membership')
            if 'acceptance' not in body:
                ev = body.pop('evidence', None)
                if ev is None:
                    ev = body.pop('reason', None)
                else:
                    body.pop('reason', None)
                dec = body.pop('decision', None)
                if ev is None and isinstance(dec, dict):
                    ev = dec.get('why_satisfied') or dec.get('checked')
                body.pop('owners', None)
                body.pop('approvers', None)
                policy = body.pop('policy', None) or 'any-owner'
                decision_id = body.pop('decision_id', None) or operation_id
                body.pop('unverified_pointer_reason', None)
                if ev is not None or dec is not None:
                    body['acceptance'] = {
                        'decision_id': decision_id,
                        'owners': list(actual_owners),
                        'approvers': list(actual_approvers),
                        'policy': policy,
                        'evidence': ev if ev is not None else '',
                    }
            else:
                for extra in ('evidence', 'reason', 'decision', 'decision_id',
                              'owners', 'approvers', 'policy', 'unverified_pointer_reason'):
                    body.pop(extra, None)
                if isinstance(body['acceptance'], dict):
                    acc = dict(body['acceptance'])
                    if 'decision' in acc and 'evidence' not in acc:
                        dec = acc.pop('decision')
                        if isinstance(dec, dict):
                            acc['evidence'] = dec.get('why_satisfied') or dec.get('checked') or ''
                    acc['owners'] = list(actual_owners)
                    acc['approvers'] = list(actual_approvers)
                    acc.setdefault('policy', 'any-owner')
                    acc.setdefault('decision_id', operation_id)
                    body['acceptance'] = acc
        return body

    def _records_write(self, ctx, route_name, command, allowed_fields, where, *,
                       coordinator=False, key_param=True, status=200):
        self._records_backend()
        needed = CAP_COORDINATE if coordinator else CAP_RECORDS
        self._project(ctx, needed)
        self._require_record_membership(ctx, coordinator=coordinator)
        fields, operation_id = self._proposal_body(ctx, allowed_fields, where)
        pid = ctx.params['pid']
        if key_param and 'key' in fields and fields['key'] != ctx.params['key']:
            raise invalid('key in body must match the route key')
        body = dict(fields, schema_version=1, operation_id=operation_id)
        if key_param:
            body['key'] = ctx.params['key']
        body = self._normalize_record_payload(ctx, command, body, operation_id)

        def mutate_record():
            result = self.backend.invoke(route_name, ctx.principal, pid,
                                         {'command': command, 'body': body},
                                         ctx.idempotency_key,
                                         target=ctx.route_target,
                                         authorize=ctx.authorize,
                                         capability=needed)
            capabilities = self.service.capabilities_for(ctx.principal, pid)
            if isinstance(result, dict):
                result = self._enrich_record_view(ctx, pid, capabilities, result)
            return result, result
        return self._mutate(ctx, route_name, pid, mutate_record, status=status,
                            capability=needed, serialize=False, canonical=True,
                            reason='%s %s' % (command, body.get('key') or ''))

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/references')
    def references_list(self, ctx):
        """The reference catalog (.41 slice 1, .213 slice 5), read-only at CAP_READ.

        Query: `tag` (comma-separated, all must match), `owner`, `state`, `due`,
        `authority` (repository, url or attested), `waiting` (1/true), `limit` and the
        page `cursor`. Statements are untrusted text: they are returned as data
        and never placed in an error body or an audit record.
        """
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        limit, state = self._page(ctx, ctx.query)
        args = ['--limit', str(limit), '--offset', str(state['o'])]
        for tag in [t for t in (ctx.query.get('tag') or '').split(',') if t]:
            args += ['--tag', tag]
        for name in ('owner', 'state', 'due', 'authority'):
            if ctx.query.get(name):
                args += ['--' + name, ctx.query[name]]
        if 'waiting' in ctx.query:
            if ctx.query['waiting'] in ('1', 'true'):
                args.append('--waiting')
            elif ctx.query['waiting'] not in ('0', 'false', ''):
                raise invalid('waiting must be 1, 0, true or false')
        import reference_records
        try:
            options = reference_records.parse_list_options(args, web_service=True)
        except ValueError as error:
            raise invalid(str(error).replace('ref list: ', ''))
        result = self._enrich_records_list(ctx, pid, self.backend.references(pid, options))
        result['next_cursor'] = (make_cursor(ctx.principal, pid, ctx.query,
                                             state['o'] + limit, key=self.service.cursor_key())
                                 if result.get('next_offset') is not None else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/references/find')
    def references_find(self, ctx):
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        q = ctx.query.get('q')
        if not isinstance(q, str) or not q.strip():
            raise invalid('q is required')
        caller_arg(q, 'q')
        limit_raw = ctx.query.get('limit', '5')
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise invalid('limit must be an integer')
        import reference_records
        try:
            phrase, parsed_limit = reference_records.parse_find_options([q, '--limit', str(limit)])
        except ValueError as error:
            raise invalid(str(error).replace('ref find: ', ''))
        result = self.backend.references_find(pid, phrase, parsed_limit)
        return 200, self._enrich_records_list(ctx, pid, result)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/references/(?P<key>' + REFERENCE_KEY + r')')
    def references_get(self, ctx):
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        capabilities = self.service.capabilities_for(ctx.principal, pid)
        return 200, self._enrich_record_view(
            ctx, pid, capabilities, self.backend.reference(pid, ctx.params['key']))

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/references')
    def references_propose(self, ctx):
        return self._records_write(ctx, 'references.propose', 'propose',
                                   self.REF_PROPOSE_FIELDS, 'A reference proposal',
                                   coordinator=False, key_param=False, status=201)

    @route('PATCH', r'/v1/projects/(?P<pid>' + ID + r')/references/(?P<key>' + REFERENCE_KEY + r')')
    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/references/(?P<key>' + REFERENCE_KEY + r')/revisions')
    def references_revise(self, ctx):
        return self._records_write(ctx, 'references.revise', 'revise',
                                   self.REF_REVISE_FIELDS, 'A reference revision',
                                   coordinator=False, key_param=True, status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/references/(?P<key>' + REFERENCE_KEY + r')/accept')
    def references_accept(self, ctx):
        return self._records_write(ctx, 'references.accept', 'accept',
                                   self.REF_ACCEPT_FIELDS, 'A reference acceptance',
                                   coordinator=True, key_param=True, status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/references/(?P<key>' + REFERENCE_KEY + r')/retire')
    def references_retire(self, ctx):
        return self._records_write(ctx, 'references.retire', 'retire',
                                   self.REF_RETIRE_FIELDS, 'A reference retirement',
                                   coordinator=True, key_param=True, status=200)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/capabilities')
    def capabilities_list(self, ctx):
        """The capability catalog (.56, .213 slice 5), read-only at CAP_READ."""
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        limit, state = self._page(ctx, ctx.query)
        args = ['--limit', str(limit), '--offset', str(state['o'])]
        for tag in [t for t in (ctx.query.get('tag') or '').split(',') if t]:
            args += ['--tag', tag]
        for name in ('owner', 'state'):
            if ctx.query.get(name):
                args += ['--' + name, ctx.query[name]]
        if 'pointers' in ctx.query:
            if ctx.query['pointers'] in ('1', 'true'):
                args.append('--pointers')
            elif ctx.query['pointers'] not in ('0', 'false', ''):
                raise invalid('pointers must be 1, 0, true or false')
        if 'waiting' in ctx.query:
            if ctx.query['waiting'] in ('1', 'true'):
                args.append('--waiting')
            elif ctx.query['waiting'] not in ('0', 'false', ''):
                raise invalid('waiting must be 1, 0, true or false')
        import capability_records
        try:
            options = capability_records.parse_list_options(args, web_service=True)
        except ValueError as error:
            raise invalid(str(error).replace('capability list: ', ''))
        result = self._enrich_records_list(ctx, pid, self.backend.capabilities(pid, options))
        result['next_cursor'] = (make_cursor(ctx.principal, pid, ctx.query,
                                             state['o'] + limit, key=self.service.cursor_key())
                                 if result.get('next_offset') is not None else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/find')
    def capabilities_find(self, ctx):
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        q = ctx.query.get('q')
        if not isinstance(q, str) or not q.strip():
            raise invalid('q is required')
        caller_arg(q, 'q')
        limit_raw = ctx.query.get('limit', '5')
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise invalid('limit must be an integer')
        import capability_records
        try:
            phrase, parsed_limit = capability_records.parse_find_options([q, '--limit', str(limit)])
        except ValueError as error:
            raise invalid(str(error).replace('capability find: ', ''))
        result = self.backend.capabilities_find(pid, phrase, parsed_limit)
        return 200, self._enrich_records_list(ctx, pid, result)

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')')
    def capabilities_get(self, ctx):
        self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        capabilities = self.service.capabilities_for(ctx.principal, pid)
        return 200, self._enrich_record_view(
            ctx, pid, capabilities, self.backend.capability(pid, ctx.params['key']))

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities')
    def capabilities_propose(self, ctx):
        return self._records_write(ctx, 'capabilities.propose', 'propose',
                                   self.CAP_PROPOSE_FIELDS, 'A capability proposal',
                                   coordinator=False, key_param=False, status=201)

    @route('PATCH', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')')
    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')/revisions')
    def capabilities_revise(self, ctx):
        return self._records_write(ctx, 'capabilities.revise', 'revise',
                                   self.CAP_REVISE_FIELDS, 'A capability revision',
                                   coordinator=False, key_param=True, status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')/accept')
    def capabilities_accept(self, ctx):
        return self._records_write(ctx, 'capabilities.accept', 'accept',
                                   self.CAP_ACCEPT_FIELDS, 'A capability acceptance',
                                   coordinator=True, key_param=True, status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')/retire')
    def capabilities_retire(self, ctx):
        return self._records_write(ctx, 'capabilities.retire', 'retire',
                                   self.CAP_RETIRE_FIELDS, 'A capability retirement',
                                   coordinator=True, key_param=True, status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')/verify')
    def capabilities_verify(self, ctx):
        return self._records_write(ctx, 'capabilities.verify', 'web-verify',
                                   self.CAP_VERIFY_FIELDS, 'A capability verification',
                                   coordinator=True, key_param=True, status=200)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')/aliases')
    def capabilities_alias_propose(self, ctx):
        return self._records_write(ctx, 'capabilities.alias.propose', 'propose-alias',
                                   self.CAP_ALIAS_PROPOSE_FIELDS, 'A capability alias proposal',
                                   coordinator=False, key_param=True, status=201)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/capabilities/(?P<key>' + CAPABILITY_KEY + r')/aliases/reject')
    def capabilities_alias_reject(self, ctx):
        return self._records_write(ctx, 'capabilities.alias.reject', 'reject-alias',
                                   self.CAP_ALIAS_REJECT_FIELDS, 'A capability alias rejection',
                                   coordinator=True, key_param=True, status=200)

    # -- direct owner requirements, slice A -------------------------------------
    def _requirements_backend(self):
        if not hasattr(self.backend,'requirements_read') or not hasattr(self.backend,'owner_requirements'):
            raise not_implemented('Requirements editing needs the canonical tracker backend')

    def _requirement_editor_here(self, ctx, pid=None):
        """The requirement editor this caller is here, or None (kittrial-5bb.278).

        One rule with the canonical endpoint: `http_authority.requirement_editor` reads
        the live membership or the live coordinator grant, so a page render and the
        effect-time check cannot disagree about who may write.
        """
        import http_authority
        try:
            return http_authority.requirement_editor(
                self.service.state, pid or ctx.params['pid'],
                {'via': ctx.principal.via, 'user_id': ctx.principal.user_id,
                 'credential_id': ctx.principal.credential_id})
        except http_authority.AuthorityDenied:
            return None

    def _requirements_owner(self, ctx):
        """Authorize one requirements route: a project's owner OR coordinator may write.

        `coordinate` is the live capability (an owner holds it, a coordinator holds it by
        role, and an agent credential holds it only through the project's coordinator
        grant). The capability check runs first so a non-member keeps the service's own
        answer (404, never a hint that the project exists) and an expired or revoked
        credential keeps its own sentence; a member or credential that is merely not the
        project's editor gets one sentence naming the rule. The membership/grant rule
        itself is :func:`http_authority.requirement_editor`: the same function the
        canonical endpoint re-checks at effect time.
        """
        pid = ctx.params['pid']
        import http_authority
        try:
            self._project(ctx, CAP_COORDINATE)
        except HttpError as error:
            if error.status != 403:
                raise
            raise forbidden(http_authority.REQUIREMENT_EDITOR_REFUSED) from None
        editor = self._requirement_editor_here(ctx, pid)
        if editor is None:
            raise forbidden(http_authority.REQUIREMENT_EDITOR_REFUSED)
        return editor

    def _requirements_state_only(self, ctx, result):
        """A member who cannot finish the transition reads the state, not its handles.

        A current owner OR coordinator still receives the exact operation id and receipt
        hash its finish/abandon route needs (kittrial-5bb.278: a coordinator may finish
        what it may also start); contributors and viewers receive the state, reason and a
        sentence naming who can act.
        """
        pid=ctx.params['pid']
        owner=self._requirement_editor_here(ctx,pid) is not None
        if owner:
            return result
        def without_operation(record):
            return {name:value for name,value in record.items() if name!='operation_id'} \
                if isinstance(record,dict) else record
        items=result.get('items',[result]) if isinstance(result,dict) else []
        for item in items:
            if not isinstance(item,dict):
                continue
            for name in ('state','reason'):
                if isinstance(item.get(name),dict):
                    item[name]=without_operation(item[name])
            if isinstance(item.get('state_history'),list):
                item['state_history']=[without_operation(state) for state in item['state_history']]
            if isinstance(item.get('state_message'),str) and 'finish or abandon' in item['state_message']:
                item['state_message']='A requirement transition is pending; ask a project owner to finish or abandon it.'
            elif isinstance(item.get('state_message'),str) and 'admin.py requirement-reconcile' in item['state_message']:
                item['state_message']='Requirement status cannot be verified; ask a project owner to reconcile it.'
            pending=item.get('pending_transition')
            if isinstance(pending,dict):
                item['pending_transition']={'id':pending.get('id'),
                    'state':without_operation(pending.get('state')),
                    'reason':without_operation(pending.get('reason')),
                    'message':'A requirement transition is pending; ask a project owner to finish or abandon it.'}
            if isinstance(item.get('completion'),dict):
                item['completion']={name:value for name,value in item['completion'].items()
                                    if name not in ('operation_id','pending_receipt_sha256')}
        recoveries=result.get('terminal_recoveries') if isinstance(result,dict) else None
        if isinstance(recoveries,dict):
            recoveries['items']=[{'id':entry.get('id'),'state':without_operation(entry.get('state')),
                'reason':without_operation(entry.get('reason')),
                'message':'A requirement transition is pending; ask a project owner to finish or abandon it.'}
                for entry in recoveries.get('items',[]) if isinstance(entry,dict)]
            recoveries['damaged']=[{'id':entry.get('id'),'message':entry.get('message')}
                for entry in recoveries.get('damaged',[]) if isinstance(entry,dict)]
            recoveries.pop('completions',None)
        return result

    def _requirements_names(self, result):
        items=result.get('items', [result])
        accounts = [(item.get('state') or {}).get('account_id') for item in items]
        accounts += [(item.get('completion') or {}).get('actor') for item in items]
        accounts += [((item.get('pending_transition') or {}).get('state') or {}).get('account_id') for item in items]
        names=self.service.actor_names(accounts)
        # The shared resolver returns the identifier for unknown accounts.
        # Historical human attribution must use neutral text on this page.
        names={account:name for account,name in names.items() if name != account}
        for item in items:
            if item.get('state'):
                item['state_author_name']=names.get(item['state']['account_id']) or 'Former project owner'
            if item.get('completion'):
                item['completion_author_name']=names.get(item['completion']['actor']) or 'Host operator or former project owner'
            if item.get('pending_transition'):
                pending = item['pending_transition']
                pending['author_name']=names.get(pending['state']['account_id']) or 'Former project owner'

    def _requirements_write(self, ctx, action, status=200):
        self._requirements_backend()
        editor=self._requirements_owner(ctx)
        if action == 'baseline' and (editor.get('role') != 'owner' or editor.get('agent_id')
                or ctx.principal.via != 'session' or ctx.principal.credential_id):
            raise forbidden('Baseline cuts need the current signed-in project owner')
        if action=='governance' and editor.get('role')!='owner':
            # The coordinator's decision 11(i) of kittrial-5bb.278: the governance-mode
            # switch changes the rules, not requirement text, so it stays the owner's.
            raise forbidden('Switching requirements governance needs the signed-in project owner')
        fields,operation_id=self._proposal_body(ctx,{
            'baseline':('name',),
            'create':('kind','parent','title','description','key'),
            'revise':('expected_revision','expected_sha256','title','description'),
            'accept':('expected_revision','expected_sha256'),
            'withdraw':('expected_revision','expected_sha256','expected_state_sha256','reason'),
            'supersede':('expected_revision','expected_sha256','expected_state_sha256','reason','successor'),
            'release':('original_operation_id','expected_receipt_sha256','reason'),
            'finish':('original_operation_id','expected_receipt_sha256','confirm'),
            'abandon':('original_operation_id','expected_receipt_sha256','reason'),
            'governance':('mode','expected_revision','expected_sha256')}[action],'Requirements')
        import requirement_http
        try:
            if action == 'baseline':
                from requirement_baseline_http import checked_body
                checked_body(fields)
            else:
                requirement_http.checked_body(fields,action)
        except ValueError as error: raise invalid(str(error))
        if operation_id is not None:
            raise invalid('Use the Idempotency-Key header for requirements writes')
        pid=ctx.params['pid']
        if action in ('baseline','withdraw','supersede','finish','abandon'):
            # Give immediate mode advice; the canonical adapter repeats the
            # check under the project/authority locks, including exact replays.
            if self.backend.requirements_read(pid,['governance'])['mode']!='simple':
                raise invalid('This project uses governed requirements. Ask its owner to enable simple editing.')
        def write():
            result=self.backend.owner_requirements(ctx.principal,pid,action,fields,
                                                    ctx.idempotency_key or ctx.request_id,ctx.params.get('rid'))
            if action in ('withdraw','supersede','finish','abandon'):
                token=request_hash({'project':pid,'operation_id':result['operation_id'],
                                    'disposition':result.get('disposition','complete')})
                with self.service.store.lock:
                    recorded=self.service.state.setdefault('requirement_terminal_audits',{})
                    if token not in recorded:
                        self.service.audit(ctx.request_id,ctx.principal,'requirements.'+action,'committed',project_id=pid,
                                           requirement_id=result['id'], original_operation_id=result['operation_id'],
                                           recovery_operation_id=(result.get('completion') or {}).get('operation_id') or result.get('recovery_operation_id'))
                        recorded[token]=ctx.request_id
            return result,result
        return self._mutate(ctx,'requirements.'+action,pid,write,status=status,
                            capability=CAP_COORDINATE,serialize=False,canonical=True,
                            # Terminal replay authority belongs to the canonical
                            # owner adapter. Its durable identity still deduplicates
                            # effects; a service response must not bypass that check.
                            idempotent=action not in ('baseline','withdraw','supersede','finish','abandon'),
                            audit_success=action not in ('withdraw','supersede','finish','abandon'),
                            audit_metadata=self._requirement_agent_audit(ctx))

    def _requirement_agent_audit(self, ctx):
        """The agent that acted, and its grant, recorded beside the human account.

        A person's own write adds nothing: the audit entry already carries the account.
        For an agent credential the accepting party stays the human account (owner
        decision 11(ii) of kittrial-5bb.278), so the entry names the agent and the exact
        grant it acted on. Bounded, redacted values only.
        """
        editor = self._requirement_editor_here(ctx, ctx.params.get('pid'))
        if not editor or not editor.get('agent_id'):
            return None
        grant = editor.get('grant') or {}
        return {'acting_agent': editor['agent_id'],
                'coordinator_grant': {'granted_by': grant.get('granted_by'),
                                      'granted_at': grant.get('granted_at')}}

    # Named snapshots are human member reads; no new agent/worker authority.
    def _requirements_baseline_member(self, ctx):
        self._requirements_backend(); self._project(ctx, CAP_READ)
        pid = ctx.params['pid']
        if (ctx.principal.via != 'session' or ctx.principal.credential_id or ctx.principal.agent_id
                or self.service.state.get('memberships', {}).get(pid, {}).get(ctx.principal.user_id)
                not in ('owner', 'coordinator', 'contributor', 'viewer')):
            raise forbidden('Requirements baselines need a current signed-in project member')

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/requirements/baselines')
    def requirements_baselines(self, ctx):
        self._requirements_baseline_member(ctx)
        return 200, self.backend.requirements_read(ctx.params['pid'], ['baselines'])

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/baselines')
    def requirements_cut_baseline(self, ctx):
        return self._requirements_write(ctx, 'baseline', status=201)

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/requirements/baselines/(?P<baseline>[A-Za-z0-9_-]{1,64})')
    def requirements_baseline(self, ctx):
        self._requirements_baseline_member(ctx)
        result = self.backend.requirements_read(ctx.params['pid'], ['baseline', ctx.params['baseline']])
        if self._requirement_editor_here(ctx, ctx.params['pid']) is None:
            receipt = result.get('receipt')
            if isinstance(receipt, dict):
                names = self.service.actor_names([receipt.get('account_id')])
                account = receipt.get('account_id')
                name = names.get(account)
                result['author_name'] = name if name and name != account else 'Former project owner'
                result['receipt'] = {key: value for key, value in receipt.items()
                                     if key not in ('account_id', 'operation_id')}
        return 200, result

    def _requirements_document_args(self, ctx, action):
        if set(ctx.query) - {'baseline'}:
            raise invalid('Requirements documents take only the optional baseline query')
        args = [action]
        if 'baseline' in ctx.query:
            from requirement_baseline_records import baseline_name
            try:
                args.append(baseline_name(ctx.query['baseline']))
            except ValueError as error:
                raise invalid(str(error))
        return args

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/brd/export')
    def requirements_markdown_export(self, ctx):
        self._requirements_baseline_member(ctx)
        return 200, self.backend.requirements_read(ctx.params['pid'],
            self._requirements_document_args(ctx, 'export'))

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/brd/changes')
    def requirements_baseline_changes(self, ctx):
        self._requirements_baseline_member(ctx)
        result = self.backend.requirements_read(ctx.params['pid'],
            self._requirements_document_args(ctx, 'changes'))
        for change in result.get('changes', []):
            for side in ('before', 'after'):
                projected = {'state': change.get(side + '_state')}
                self._requirements_state_only(ctx, projected)
                self._requirements_names(projected)
                change[side + '_state'] = projected['state']
                if 'state_author_name' in projected:
                    change[side + '_state_author_name'] = projected['state_author_name']
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/requirements/governance')
    def requirements_governance_read(self,ctx):
        self._requirements_backend(); self._project(ctx,CAP_READ)
        return 200,self.backend.requirements_read(ctx.params['pid'],['governance'])

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/requirements/recoveries')
    def requirements_recoveries(self,ctx):
        self._requirements_backend(); self._requirements_owner(ctx)
        return 200,self.backend.owner_requirement_recoveries(ctx.principal,ctx.params['pid'])

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/recoveries/clear')
    def requirements_clear_creation(self,ctx):
        return self._requirements_write(ctx,'release')

    @route('PUT', r'/v1/projects/(?P<pid>'+ID+r')/requirements/governance')
    def requirements_governance_write(self,ctx):
        return self._requirements_write(ctx,'governance')

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/requirements')
    def requirements_list(self,ctx):
        self._requirements_backend(); self._project(ctx,CAP_READ)
        result=dict(self.backend.requirements_read(ctx.params['pid'],['list']))
        result['items']=[self._requirement_author(ctx,item) for item in result.get('items',[])]
        result=self._requirements_state_only(ctx,result)
        self._requirements_names(result)
        result['can_edit']=self._requirements_page_editor(ctx)
        result['can_cut_baseline']=(ctx.principal.via == 'session'
            and not ctx.principal.credential_id and not ctx.principal.agent_id
            and self.service.state.get('memberships', {}).get(ctx.params['pid'], {}).get(ctx.principal.user_id) == 'owner'
            and result.get('governance', {}).get('mode') == 'simple')
        return 200,result

    def _requirements_page_editor(self,ctx):
        """Whether this page is editable IN THE BROWSER for this caller.

        A person's page (owner or coordinator, kittrial-5bb.278); an agent credential uses
        the routes and http_client. Unchanged for an owner, so no owner sees a difference.
        """
        if ctx.principal.via!='session' or ctx.principal.agent_id:
            return False
        return self._requirement_editor_here(ctx,ctx.params['pid']) is not None

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements')
    def requirements_create(self,ctx):
        return self._requirements_write(ctx,'create',status=201)

    #: One batch acceptance is bounded (kittrial-5bb.278): one action, many requirements.
    REQUIREMENT_BATCH_MAX = 200
    #: Said of every item a stopped batch never reached, so none is silently dropped.
    REQUIREMENT_BATCH_NOT_ATTEMPTED = ('Not attempted: stop_on_error stopped this batch after an '
                                       'earlier item was refused.')

    def _requirements_batch_items(self, ctx):
        """Validate the whole batch before ANY effect; a bad item refuses the request."""
        payload = dict(ctx.payload or {})
        operation_id = payload.pop('operation_id', None)
        if operation_id is not None:
            raise invalid('Use the Idempotency-Key header for requirements writes')
        unknown = sorted(set(payload) - {'items', 'stop_on_error'})
        if unknown:
            raise invalid('A batch acceptance does not take: %s' % ', '.join(unknown)[:200])
        stop = payload.get('stop_on_error', False)
        if not isinstance(stop, bool):
            raise invalid('stop_on_error must be true or false')
        items = payload.get('items')
        if not isinstance(items, list) or not items:
            raise invalid('A batch acceptance needs 1 to %d items' % self.REQUIREMENT_BATCH_MAX)
        if len(items) > self.REQUIREMENT_BATCH_MAX:
            raise invalid('A batch acceptance takes at most %d items' % self.REQUIREMENT_BATCH_MAX)
        import requirement_governance as governance
        seen, checked = set(), []
        for item in items:
            if not isinstance(item, dict):
                raise invalid('Each batch item is an object with id, expected_revision and '
                              'expected_sha256')
            unknown = sorted(set(item) - {'id', 'expected_revision', 'expected_sha256'})
            if unknown:
                raise invalid('A batch item does not take: %s' % ', '.join(unknown)[:200])
            rid = item.get('id')
            if not isinstance(rid, str) or not SAFE_ID.match(rid):
                raise invalid('Each batch item needs a requirement id')
            if rid in seen:
                raise invalid('A batch acceptance names requirement %s twice' % rid)
            seen.add(rid)
            expected_revision = item.get('expected_revision')
            if type(expected_revision) is not int or expected_revision < 1:
                raise invalid('Invalid expected requirements revision for %s' % rid)
            try:
                governance.digest(item.get('expected_sha256'))
            except ValueError:
                raise invalid('Expected requirements hash is required for %s; reload and try again'
                              % rid) from None
            checked.append({'id': rid, 'expected_revision': expected_revision,
                            'expected_sha256': item['expected_sha256']})
        return checked, stop

    @staticmethod
    def _requirement_batch_reason(error):
        """The refused item's own sentence: the canonical detail when there is one.

        ``_checked`` keeps a canonical refusal's own words in ``detail`` and a generic
        sentence in ``message``. A batch answer must say WHY an item was not accepted, so
        the detail wins, bounded, with the endpoint's ``ValueError:`` prefix removed.
        """
        for text in (error.detail, error.message):
            if isinstance(text, str) and text.strip():
                said = text.strip()
                if said.startswith('ValueError: '):
                    said = said[len('ValueError: '):]
                return said[:300]
        return 'Not accepted'

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/accept-batch')
    def requirements_accept_batch(self,ctx):
        """Accept many requirements in one action (kittrial-5bb.278, slice 10).

        Per item, and no rollback (the coordinator's decision 11(iii)): a native acceptance
        cannot be undone, so a refused item changes nothing while the items around it stay
        accepted. Each item runs the SAME canonical adapter as a single acceptance, with its
        own operation id derived from this request's Idempotency-Key and the item id, so an
        exact replay of the batch accepts nothing twice. `stop_on_error` (optional, default
        false) stops after the first refusal, and every item it never reached is listed as
        refused with the reason it was not attempted: nothing is dropped silently.
        """
        self._requirements_backend()
        self._requirements_owner(ctx)
        items, stop = self._requirements_batch_items(ctx)
        pid = ctx.params['pid']
        if self.backend.requirements_read(pid,['governance'])['mode']!='simple':
            raise invalid('This project uses governed requirements. Ask its owner to enable simple editing.')
        key = ctx.idempotency_key or ctx.request_id

        def write():
            accepted, refused = [], []
            for position, item in enumerate(items):
                fields = {'expected_revision': item['expected_revision'],
                          'expected_sha256': item['expected_sha256']}
                try:
                    result = self.backend.owner_requirements(ctx.principal, pid, 'accept', fields,
                                                             key, item['id'])
                except HttpError as error:
                    # The words of a refusal are the answer (kittrial-5bb.156); an uncertain
                    # outcome is not, and must stop the batch instead of being reported as a
                    # per-item refusal.
                    if error.status >= 500:
                        raise
                    refused.append({'id': item['id'],
                                    'reason': self._requirement_batch_reason(error)})
                    if stop:
                        refused.extend({'id': rest['id'],
                                        'reason': self.REQUIREMENT_BATCH_NOT_ATTEMPTED}
                                       for rest in items[position + 1:])
                        break
                else:
                    accepted.append(result)
            answer = {'accepted': accepted, 'refused': refused,
                      'summary': {'accepted': len(accepted), 'refused': len(refused)}}
            return answer, answer
        return self._mutate(ctx,'requirements.accept-batch',pid,write,capability=CAP_COORDINATE,
                            serialize=False,audit_metadata=self._requirement_agent_audit(ctx))

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')')
    def requirements_get(self,ctx):
        self._requirements_backend(); self._project(ctx,CAP_READ)
        result=dict(self.backend.requirements_read(ctx.params['pid'],['get',ctx.params['rid']]))
        result=self._requirement_author(ctx,result)
        result=self._requirements_state_only(ctx,result)
        self._requirements_names(result)
        result['governance']=self.backend.requirements_read(ctx.params['pid'],['governance'])
        result['can_edit']=self._requirements_page_editor(ctx)
        return 200,result

    def _requirement_author(self,ctx,item):
        result=dict(item)
        if not result.get('pending_draft'):
            result['pending_author']=None
            return result
        author=result.get('pending_author')
        if not isinstance(author,dict) or author.get('kind')!='account':
            return result
        account=author.get('text')
        user=self.service.state.get('users',{}).get(account)
        role=self.service.state.get('memberships',{}).get(ctx.params['pid'],{}).get(account)
        if not isinstance(user,dict) or role not in ('owner','coordinator'):
            result['pending_author']={'kind':'unknown','text':'', 'omitted_chars':0,'at':author.get('at')}
            return result
        name=user.get('display_name') or user.get('username')
        if not isinstance(name,str) or not name.strip():
            result['pending_author']={'kind':'unknown','text':'','omitted_chars':0,'at':author.get('at')}
            return result
        result['pending_author']=dict(author,kind='owner-account',text=name[:200],
            omitted_chars=max(0,len(name)-200),is_self=account==ctx.principal.user_id)
        return result

    @route('PATCH', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')')
    def requirements_revise(self,ctx):
        return self._requirements_write(ctx,'revise')

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')/accept')
    def requirements_accept(self,ctx):
        return self._requirements_write(ctx,'accept')

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')/withdraw')
    def requirements_withdraw(self,ctx):
        return self._requirements_write(ctx,'withdraw')

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')/supersede')
    def requirements_supersede(self,ctx):
        return self._requirements_write(ctx,'supersede')

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')/transition/finish')
    def requirements_finish_transition(self,ctx):
        return self._requirements_write(ctx,'finish')

    @route('POST', r'/v1/projects/(?P<pid>'+ID+r')/requirements/(?P<rid>'+ID+r')/transition/abandon')
    def requirements_abandon_transition(self,ctx):
        return self._requirements_write(ctx,'abandon')

    @route('GET', r'/v1/projects/(?P<pid>'+ID+r')/brd')
    def requirements_brd(self,ctx):
        self._requirements_backend(); self._project(ctx,CAP_READ)
        result=self.backend.requirements_read(ctx.params['pid'],['brd'])
        result['items']=[self._requirement_author(ctx,item) for item in result.get('items',[])]
        result=self._requirements_state_only(ctx,result)
        self._requirements_names(result)
        return 200,result

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
        if mine or self._triages(ctx, capabilities):
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
        result['next_cursor'] = (make_cursor(ctx.principal, pid, ctx.query, result['next_offset'],
                                             key=self.service.cursor_key())
                                 if result.get('next_offset') is not None else None)
        result['can_triage'] = self._triages(ctx, capabilities)
        result['can_decide'] = self._decides(ctx, capabilities)
        result['can_propose'] = CAP_PROPOSALS in capabilities
        return 200, result

    @staticmethod
    def _triages(ctx, capabilities):
        """Whether the caller triages proposals: a signed-in member with `reviews.approve`. Never
        a credential, as the write below says; an agent's coordinator grant (kittrial-5bb.209)
        gives it the approval of contributions and changes nothing here."""
        return CAP_APPROVE in capabilities and ctx.principal.via != 'credential'

    @staticmethod
    def _decides(ctx, capabilities):
        """Whether the caller records the owner decision on an escalated proposal: a signed-in
        member with `project.admin` (an owner, or a superuser), the check the write below makes.
        A coordinator triages and does not decide; never a credential (kittrial-5bb.261)."""
        return CAP_PROJECT_ADMIN in capabilities and ctx.principal.via != 'credential'

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
        result['can_triage'] = self._triages(ctx, capabilities)
        result['can_decide'] = self._decides(ctx, capabilities)
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
        if command == 'decide' and CAP_PROJECT_ADMIN not in self.service.capabilities_for(principal, ctx.params['pid']):
            # A coordinator triages; the decision an escalated proposal waits for is the
            # owner's (kittrial-5bb.209 review): `reviews.approve` alone used to carry both.
            raise forbidden('The decision on a proposal is an owner\'s. A coordinator triages a proposal and may '
                            'escalate it to an owner; it does not record the owner\'s decision')
        ctx.payload = payload
        fields, operation_id = self._proposal_body(ctx, self.PROPOSAL_DISPOSE_FIELDS, 'A proposal disposition')
        body = dict(fields, schema_version=1, operation=command, key=ctx.params['key'], operation_id=operation_id)
        pid = ctx.params['pid']
        # The endpoint is handed the capability this route checked for THIS operation, and
        # authorizes that one again from the live state (kittrial-5bb.261): `project.admin` for
        # the owner decision, `reviews.approve` for triage. It used to be handed
        # `reviews.approve` for both, so its second authorization of a decide asked less than
        # the first.
        needed = CAP_PROJECT_ADMIN if command == 'decide' else CAP_APPROVE

        def dispose():
            result = self.backend.invoke('proposals.dispose', principal, pid, {'command': command, 'body': body},
                                         ctx.idempotency_key, target=ctx.route_target,
                                         authorize=ctx.authorize, capability=needed)
            return result, result
        return self._mutate(ctx, 'proposals.dispose', pid, dispose, status=201, capability=needed,
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
                     'next_cursor': make_cursor(principal, None, ctx.query, offset + limit,
                                                key=self.service.cursor_key())
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
        try:
            brief = self.backend.task_brief(pid, tid)
        except HttpError:
            # The brief of a row that cannot be read fails in the endpoint; say what is the
            # matter with the row instead of the endpoint's failure (kittrial-5bb.169). Only
            # on this path is the row read again, so a healthy brief costs what it did.
            self._refuse_record_anchor(self._row_or_none(pid, tid))
            raise
        self._refuse_record_anchor(brief.get('task'))
        review = brief['review']
        checkpoint = brief.get('checkpoint')
        contribution = review.get('contribution')
        # The backend reads as the service; explain the wait to THIS caller.
        from work import approval_wait, APPROVAL_WAIT
        caller=ctx.principal.actor or ctx.principal.user_id
        from task_assignment import holder_message
        records=brief.pop('assignment_records',None)
        if records:
            items=[]
            for entry in records.get('items',[]):
                message=holder_message(entry['notice'],caller)
                if message:items.append(dict(entry,message=message))
            if items:brief['assignment_notices']={'items':items,'omitted':records.get('omitted',0),
                                                'complete':records.get('omitted',0)==0}
        assignee=brief['task'].get('assignee')
        author=(contribution or {}).get('author')
        required=bool(review.get('approval_requires_other_party') or self.service.approval_by_another_party)
        named=any(r.get('reviewer')==caller for r in review.get('pending_review_requests') or [])
        text=approval_wait(review.get('state'),tid,caller,assignee,author,required,named_reviewer=named)
        if (required and review.get('state')=='awaiting-review' and author
                and not self._independent_all([caller],[author,assignee],pid)):
            text=APPROVAL_WAIT
        review['waiting_for_approval']=text
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
        kept = set(self._independent([entry.get('author') for entry in standing], parties, pid)) if parties else set()
        shown = [entry for entry in standing if entry.get('author') in kept]
        newest = review.get('recommendation')
        if not (newest and newest.get('author') in kept):
            newest = next((dict(entry) for entry in shown if 'summary' in entry), None)
        review['recommendation'] = newest
        review['recommendations'] = [{'id': entry.get('id'), 'author': entry.get('author'), 'at': entry.get('at')}
                                     for entry in shown]
        # The uncapped (author, operation id) list is for THIS SERVICE's write check
        # (kittrial-5bb.164), not for the page: it names records by the author's own person,
        # which the two lines above leave out of every read. It never leaves the service.
        review.pop('recommendation_keys', None)
        advice = [review.get('recommendation')] if review.get('recommendation') else []
        advice += review.get('recommendations') or []
        actors += [entry.get('author') for entry in advice]
        names = self._actor_names(actors, pid)
        task = dict(brief['task'], review_state=review.get('state') or 'none')
        brief['task'] = self._task_view(task, names, project_id=ctx.params['pid'])
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
        if isinstance(brief.get('newer'), dict):
            # Canonical counts/refs/coverage stay intact; SSH helper commands are
            # not actionable for a web credential. History uses the same authority.
            newer = dict(brief['newer'])
            newer.pop('history_new', None)
            newer.pop('verify', None)
            newer['history'] = base + '/history'
            newer['note'] = ('Read history for full entries and reconcile any unknown or bounded coverage. '
                             'Reading clears nothing; a checkpoint records what was incorporated.')
            brief['newer'] = newer
        # Where the project's repository is (kittrial-5bb.118): a label the project's
        # owner recorded. Data for the reader, never an instruction; null when not set.
        brief['project_repository'] = self.service.project_view(ctx.principal, pid).get('repository')
        # The same note wherever the value reaches an agent: it is a label, not an instruction.
        brief['project_repository_note'] = self.service.REPOSITORY_NOTE if brief['project_repository'] else None
        # The page needs the complete list, not the first ten: keep carry_open_items in
        # the answer and build the template from it (kittrial-5bb.356).
        brief['checkpoint_template'] = self._checkpoint_template(brief, base, brief.get('carry_open_items', []))
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
                     'intent': current.get('intent') or '', 'acceptance': current.get('acceptance') or '',
                     'summary': '', 'next_action': '',
                     'source_commit': current.get('source_commit') or '',
                     'branch': current.get('branch') or '', 'open_items': carried, 'resolved': []},
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
        payload = dict(ctx.payload or {})
        # A field the route does not take is refused, not dropped (kittrial-5bb.183 item 2).
        refuse_unknown_fields(payload, self.TASK_CLAIM_FIELDS, 'A task claim')
        task = self.backend.get_task(ctx.params['pid'], ctx.params['tid'])
        self._refuse_record_anchor(task, write=True)
        # A requirement record is readable and visible, but never claimable work
        # (kittrial-5bb.277). Refused here so no agent takes one as a task.
        self._refuse_requirement_record(task)
        # A task blocked by an open dependency is not claimable (kittrial-5bb.293).
        # The claim route fails CLOSED when the dependencies cannot be read (item 3c):
        # it never offers a claim the queue would refuse. No extra read_tasks (item 2b):
        # the task row already carries its dependencies, and the check needs only the
        # other ends' statuses, which one bounded read of those ids provides.
        self._refuse_open_dependency_closed(task, self.backend, ctx.params['pid'])
        payload['task_id'] = ctx.params['tid']
        actor = self.service.bind_actor(ctx.principal, payload.pop('actor', None))
        payload['actor'] = actor

        def claim():
            # The per-project claiming setting (kittrial-5bb.352): under 'assigned' a
            # claim of an UNASSIGNED task is refused with one sentence naming the
            # setting. A task already assigned to the caller is not refused here: its
            # holder may still start it, as before. Read FRESH: the setting lives
            # outside the tracker, so the tracker cache must not answer for it. This is
            # inside the mutation, after the idempotency reserve, so an exact retry
            # replays its stored answer instead of being refused again.
            if not (isinstance(task, dict) and task.get('assignee')):
                if self._claiming_mode(ctx.params['pid']) == 'assigned':
                    from claiming import CLAIM_REFUSAL
                    raise conflict(CLAIM_REFUSAL)
            # The per-agent limit on claimed, undelivered tasks (kittrial-5bb.276)
            # is judged here, before the write is sent, so one gate covers the web
            # page, orchestra-client.py and any other client of this route. It is
            # inside the mutation, after the idempotency reserve, so an exact retry
            # still replays its stored answer instead of being refused again.
            #
            # ONE per-agent lock spans the held-set read and the claim write (kittrial-5bb.276
            # revision; per-agent in kittrial-5bb.300). The held set is a separate endpoint
            # read, so without a lock two concurrent claims by one agent both saw the set
            # before either wrote and both passed a limit of one. Keying the lock by the
            # agent id serializes one agent's claims while unrelated agents' claims do not
            # wait for each other. One service process is the supported setup; the refusal
            # handling below runs after the lock is released, so the bounded candidate read
            # it may make cannot hold the claim path.
            try:
                with self.service.claim_gate(ctx.principal.agent_id or actor):
                    self._refuse_claim_over_limit(ctx, task, actor)
                    result = self.backend.invoke('tasks.claim', ctx.principal, ctx.params['pid'],
                                                 payload, ctx.idempotency_key,
                                                 target=ctx.route_target, authorize=ctx.authorize,
                                                 capability=CAP_TASKS)
            except HttpError as refusal:
                held_by = getattr(refusal, 'held_by', None)
                if held_by is None and refusal.code != 'conflict':
                    raise
                # Candidate fall-through (kittrial-5bb.6 deliverable b, review item 4;
                # round-3 finding F1): a refused claim on a task that is NOT CLOSED -- OPEN,
                # the `in_progress` a real claim leaves, or `blocked` -- answers with the next
                # BOUNDED claimable rows so an agent need not re-read the queue. A held task
                # is `in_progress` on a real tracker, so reading only while the task was OPEN
                # made the fall-through vanish in the ordinary case. It is not attached to a
                # refusal about a task nobody could take anyway: a CLOSED task, even one that
                # still has an assignee and therefore reports `held_by`, carries no candidates
                # and costs no read. The refused task itself is never among the candidates.
                # Advisory: claimability is not a reservation and the next claim is still
                # atomic. When there is nothing to fall through to, the refusal is
                # re-raised byte for byte, so every existing sentence and detail is kept.
                limit_refusal = (isinstance(refusal.detail, dict)
                                 and refusal.detail.get('claim_limit_refusal'))
                candidates, truncated = [], False
                if str((task or {}).get('status')) != 'closed':
                    candidates, truncated = self._claim_candidates(ctx.params['pid'], ctx.params['tid'])
                if not candidates and held_by is None and not limit_refusal:
                    raise
                detail = dict(refusal.detail) if isinstance(refusal.detail, dict) else {}
                if limit_refusal:
                    # An agent at its limit keeps its own sentence (which names the
                    # limit and the held tasks) and gains the candidates: the move
                    # is to finish or release one of those, or to take another row
                    # once it has.
                    if candidates:
                        detail['candidates'] = candidates
                        if truncated:
                            detail['candidates_truncated'] = True
                            detail['candidates_notice'] = ('More claimable tasks remain; at most %d '
                                                           'candidates are listed.' % CLAIM_CANDIDATES_LIMIT)
                    raise conflict(refusal.message, detail) from None
                if held_by is not None:
                    detail['held_by'] = held_by
                if candidates:
                    detail['candidates'] = candidates
                    if truncated:
                        # Say when the list was cut instead of dropping rows silently.
                        detail['candidates_truncated'] = True
                        detail['candidates_notice'] = ('More claimable tasks remain; at most %d '
                                                       'candidates are listed.' % CLAIM_CANDIDATES_LIMIT)
                if held_by is None:
                    raise conflict(refusal.message, detail) from None
                # Somebody else has it. Nobody takes a task over through a claim, an owner
                # included (kittrial-5bb.187); said with the holder's name as people know it.
                shown = self._actor_names([held_by], ctx.params['pid']).get(held_by) or held_by
                raise conflict('Task is already claimed by %s' % shown, detail) from None
            return result, result
        return self._mutate(ctx, 'tasks.claim', ctx.params['pid'], claim,
                            capability=CAP_TASKS, serialize=False, canonical=True)

    @route('POST', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/checkpoints')
    def checkpoints_add(self, ctx):
        self._project(ctx, CAP_CHECKPOINTS)
        payload = dict(ctx.payload or {})
        # A field the record does not carry is refused, not dropped (kittrial-5bb.183 item 2).
        refuse_unknown_fields(payload, self.CHECKPOINT_BODY_FIELDS, 'A checkpoint')
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']), write=True)
        payload['task_id'] = ctx.params['tid']
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
        # Approval is an owner's or a coordinator's action; the contributor who delivered the
        # work can contribute or request changes, never approve. A worker credential can never
        # approve at all; a personal agent can where it holds the project's coordinator grant
        # (kittrial-5bb.209), and is then judged by party below whatever the setting says.
        capability = CAP_APPROVE if payload.get('operation') == 'approve' else CAP_REVIEWS
        self._project(ctx, capability)
        recommending = payload.get('operation') == 'recommend'
        # With the installation's setting on, an approval is judged by whose work it is, which
        # needs the same read a recommendation makes (kittrial-5bb.199). Off: no read, no rule.
        by_agent = ctx.principal.via == 'credential'
        judged = payload.get('operation') == 'approve' and (self.service.approval_by_another_party or by_agent)
        current = None
        if recommending or judged:
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
            delivered = (current.get('review') or {}).get('contribution') or {} if current else {}
            pid = ctx.params['pid']
            if judged:
                # Nobody approves work of their own party: the account that approves, against the
                # account behind the contribution's author and behind the task's assignee. A
                # superuser is bound like anybody else for their own party's work.
                actor = payload.get('actor') or ctx.principal.actor or ctx.principal.user_id
                refusal = self._own_party_refusal([actor, ctx.principal.user_id], (current.get('task') or {}).get('assignee'),
                                                  delivered.get('author'), pid, by_agent)
                if refusal:
                    raise forbidden(refusal)
            if recommending:
                # Independence by PERSON, which only the web service can know; the
                # canonical write then applies the name rule and every other rule.
                parties = [(current.get('task') or {}).get('assignee'), delivered.get('author')]
                actor = payload.get('actor') or ctx.principal.actor or ctx.principal.user_id
                if not self._independent_all([actor, ctx.principal.user_id], parties, pid):
                    raise forbidden(self.NOT_INDEPENDENT_PARTY if self.service.approval_by_another_party
                                    else self.NOT_INDEPENDENT)
                # One standing recommendation for a contribution from each PERSON
                # (kittrial-5bb.154): a second one, by the same actor or by another agent of
                # the same person, is refused and nothing is stored. Read from EVERY standing
                # one, not only the newest ones a read shows (kittrial-5bb.164 item 3): past
                # that cut a person's record was invisible here and a second one was stored.
                review_block = current.get('review') or {}
                keys = review_block.get('recommendation_keys')
                if not isinstance(keys, list):
                    # An endpoint from a kit before kittrial-5bb.164: only the capped names are
                    # readable, and nothing here can tell an exact retry by operation id.
                    keys = [{'author': entry.get('author')}
                            for entry in review_block.get('recommendations') or []
                            if isinstance(entry, dict)]
                standing = [entry.get('author') for entry in keys
                            if isinstance(entry, dict) and isinstance(entry.get('author'), str)]
                own = [name for name in standing if not self._independent([name], [actor, ctx.principal.user_id], pid)]
                # The record this request names by operation id is an exact retry (or the same
                # id carrying other content) when it is one of the caller's own party's; the
                # canonical action then answers it as recorded, or refuses it as an operation id
                # already used, exactly as it did before the person rule (kittrial-5bb.164 item 1).
                named = payload.get('operation_id')
                retrying = isinstance(named, str) and named and any(
                    entry.get('operation_id') == named for entry in keys
                    if isinstance(entry, dict) and entry.get('author') in own)
                if own and not retrying:
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

    def _project_delivery(self, ctx):
        """Authorize a delivery read: ``CAP_REVIEWS``, or ``CAP_APPROVE`` on the same project.

        The ``CAP_APPROVE`` fallback runs ONLY after a ``403`` from the ``CAP_REVIEWS``
        check for the SAME route project (kittrial-5bb.373 precision 2); any other
        refusal (``404`` unknown project, ``401``) is returned as is, and a principal
        with neither capability gets the original ``403``.
        """
        try:
            self._project(ctx, CAP_REVIEWS)
            return
        except HttpError as first_error:
            if first_error.status != 403:
                raise
            try:
                self._project(ctx, CAP_APPROVE)
                return
            except HttpError:
                raise first_error from None

    @staticmethod
    def _delivery_contribution_query(query):
        unknown = sorted(set(query) - {'contribution'})
        if unknown:
            raise invalid('Unsupported query parameter(s): %s' % ', '.join(unknown))
        if 'contribution' not in query:
            return None
        contribution_id = query.get('contribution')
        if not isinstance(contribution_id, str) or not SAFE_ID.fullmatch(contribution_id):
            raise not_found('Contribution not found')
        return contribution_id

    def _audit_bundle_read(
        self,
        ctx,
        pid,
        record,
        outcome,
        *,
        reason=None,
        sha256=None,
        size_bytes=None,
        task=None,
        contribution=None,
    ):
        capped_contribution = str(contribution)[:200] if contribution else None
        with self.service.store.lock:
            event = self.service.audit(
                ctx.request_id,
                ctx.principal,
                'delivery.bundle.read',
                outcome,
                project_id=pid,
                reason=reason,
            )
            if isinstance(record, dict):
                event['task'] = record.get('task') or task
                raw_contribution = record.get('contribution') or capped_contribution
                event['contribution'] = str(raw_contribution)[:200] if raw_contribution else None
                event['commit'] = record.get('commit')
                if sha256 is None and isinstance(record.get('delivery'), dict):
                    sha256 = record['delivery'].get('sha256')
            else:
                if task is not None:
                    event['task'] = task
                if capped_contribution is not None:
                    event['contribution'] = capped_contribution
            if sha256 is not None:
                event['sha256'] = sha256
            if size_bytes is not None:
                event['bytes'] = size_bytes
            self.service.store.save_soon('the audit entry of a bundle read')

    def _send_bundle_stream(self, handle_fd, record, verified, ctx=None, pid=None):
        from review_workflow import BUNDLE_BLOCK_BYTES
        headers_sent = False
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(verified['bytes']))
            self.send_header('ETag', str(verified['sha256']))
            self.send_header('X-Contribution', str(record['contribution']))
            self.send_header('X-Orchestra-Contribution', str(record['contribution']))
            self.send_header('X-Orchestra-Commit', str(record['commit']))
            self.send_header('X-Orchestra-Base-Commit', str(record['base_commit']))
            self.send_header('X-Orchestra-Bundle-Sha256', str(verified['sha256']))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self._send_hsts()
            self.send_header('X-Request-Id', getattr(self, '_current_request_id', '') or '')
            if getattr(self, '_say_close', False):
                self.send_header('Connection', 'close')
                self._say_close = False
            if getattr(self, '_set_cookie_token', None):
                self._send_cookie(self._set_cookie_token)
            self.end_headers()
            headers_sent = True
            if self.command == 'HEAD':
                return 'ok', None
            os.lseek(handle_fd, 0, os.SEEK_SET)
            expected_bytes = int(verified['bytes'])
            expected_sha = str(verified['sha256']).lower()
            expected_stat = verified.get('stat_snapshot')
            remaining = expected_bytes
            stream_digest = hashlib.sha256()
            held_block = None
            while remaining > 0:
                chunk = os.read(handle_fd, min(BUNDLE_BLOCK_BYTES, remaining))
                if not chunk:
                    self.close_connection = True
                    try:
                        self.wfile.flush()
                    except OSError:
                        pass
                    return 'interrupted', 'short_read'
                stream_digest.update(chunk)
                remaining -= len(chunk)
                if held_block is not None:
                    self.wfile.write(held_block)
                held_block = chunk
            extra = os.read(handle_fd, 1)
            info_after = os.fstat(handle_fd)
            stat_after = (
                info_after.st_dev,
                info_after.st_ino,
                info_after.st_size,
                info_after.st_mtime_ns,
                info_after.st_ctime_ns,
            )
            if (
                extra
                or stream_digest.hexdigest().lower() != expected_sha
                or (expected_stat is not None and stat_after != expected_stat)
            ):
                self.close_connection = True
                try:
                    self.wfile.flush()
                except OSError:
                    pass
                return 'interrupted', 'bundle_checksum_mismatch'
            if held_block is not None:
                self.wfile.write(held_block)
            self.wfile.flush()
            return 'ok', None
        except Exception:
            self.close_connection = True
            if not headers_sent:
                audit_ctx = ctx
                if audit_ctx is None and getattr(self, 'service', None) is not None:
                    from types import SimpleNamespace
                    audit_ctx = SimpleNamespace(
                        request_id=getattr(self, '_current_request_id', None) or '',
                        principal=getattr(self, '_principal', None) or '',
                    )
                if audit_ctx is not None:
                    self._audit_bundle_read(
                        audit_ctx,
                        pid if pid is not None else (record.get('project') if isinstance(record, dict) else None),
                        record,
                        'interrupted',
                        reason='stream_error',
                        sha256=verified.get('sha256') if isinstance(verified, dict) else None,
                        size_bytes=verified.get('bytes') if isinstance(verified, dict) else None,
                    )
                raise
            try:
                self.wfile.flush()
            except OSError:
                pass
            return 'interrupted', 'stream_error'
        finally:
            try:
                os.close(handle_fd)
            except OSError:
                pass

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/delivery')
    def tasks_delivery(self, ctx):
        """Return the contribution's recorded delivery metadata (kittrial-5bb.373)."""
        from review_workflow import (
            DeliveryCorruptError,
            DeliveryTooLargeError,
            DeliveryUnavailableError,
            resolve_delivery_bundle_path,
            verify_served_bundle,
        )
        self._project_delivery(ctx)
        pid = ctx.params['pid']
        tid = ctx.params['tid']
        contribution_id = ctx.query.get('contribution') if isinstance(ctx.query.get('contribution'), str) else None
        record = None
        try:
            contribution_id = self._delivery_contribution_query(ctx.query)
            candidate = self.backend.task_delivery_record(
                pid,
                tid,
                contribution_id=contribution_id,
            )
            if candidate.get('task') != tid or candidate.get('project') not in (None, pid):
                raise not_found('Contribution not found')
            record = candidate
            delivery = record.get('delivery') if isinstance(record.get('delivery'), dict) else {}
            kind = delivery.get('kind')
            roots = self.backend.get_delivery_roots() if hasattr(self.backend, 'get_delivery_roots') else ()
            max_bytes = (
                self.backend.get_delivery_serve_max_bytes()
                if hasattr(self.backend, 'get_delivery_serve_max_bytes')
                else 256 * 1024 * 1024
            )
        except HttpError as error:
            self._audit_bundle_read(
                ctx,
                pid,
                record,
                'refused',
                reason=error.code,
                task=tid,
                contribution=contribution_id,
            )
            raise
        sha256 = delivery.get('sha256') if kind == 'bundle' else None
        path = delivery.get('path') if kind == 'bundle' else None
        bundle_bytes = None
        if kind == 'bundle':
            try:
                resolved_path, handle_fd, raw_size = resolve_delivery_bundle_path(path, roots)
            except DeliveryUnavailableError:
                bundle_bytes = None
            else:
                try:
                    verified = verify_served_bundle(
                        resolved_path,
                        handle_fd,
                        raw_size,
                        sha256,
                        record.get('commit'),
                        max_bytes=max_bytes,
                        git=getattr(self.backend, 'bundle_git', None),
                    )
                except (DeliveryUnavailableError, DeliveryTooLargeError, DeliveryCorruptError):
                    bundle_bytes = None
                else:
                    bundle_bytes = verified['bytes']
                    try:
                        os.close(handle_fd)
                    except OSError:
                        pass
        cid = record.get('contribution')
        qs = ('?' + urlencode({'contribution': cid})) if cid else ''
        base_url = '/v1/projects/%s/tasks/%s/delivery' % (quote(pid, safe=''), quote(tid, safe=''))
        links = {
            'self': base_url + qs,
            'bundle': (base_url + '/bundle' + qs) if kind == 'bundle' else None,
        }
        return 200, {
            'task': tid,
            'project': pid,
            'contribution': cid,
            'revision': record.get('revision'),
            'kind': kind,
            'commit': record.get('commit'),
            'base_commit': record.get('base_commit'),
            'sha256': sha256,
            'bytes': bundle_bytes,
            'path': path,
            'serve_limit': max_bytes,
            'repository': record.get('repository'),
            'author': record.get('author'),
            'at': record.get('at'),
            'delivery': delivery,
            'links': links,
        }

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/delivery/bundle')
    def tasks_delivery_bundle(self, ctx):
        """Verify and stream the contribution's delivered git bundle (kittrial-5bb.373)."""
        from review_workflow import (
            DELIVERY_CHECKSUM_MISMATCH,
            DELIVERY_NOT_AVAILABLE,
            DELIVERY_REMOTE_NO_BUNDLE,
            DeliveryCorruptError,
            DeliveryTooLargeError,
            DeliveryUnavailableError,
            resolve_delivery_bundle_path,
            verify_served_bundle,
        )
        self._project_delivery(ctx)
        pid = ctx.params['pid']
        tid = ctx.params['tid']
        contribution_id = ctx.query.get('contribution') if isinstance(ctx.query.get('contribution'), str) else None
        record = None
        size = None
        try:
            contribution_id = self._delivery_contribution_query(ctx.query)
            candidate = self.backend.task_delivery_record(pid, tid, contribution_id=contribution_id)
            if candidate.get('task') != tid or candidate.get('project') not in (None, pid):
                raise not_found('Contribution not found')
            record = candidate
            delivery = record.get('delivery') if isinstance(record.get('delivery'), dict) else {}
            if delivery.get('kind') != 'bundle':
                raise invalid(DELIVERY_REMOTE_NO_BUNDLE)
            roots = self.backend.get_delivery_roots() if hasattr(self.backend, 'get_delivery_roots') else ()
            max_bytes = (
                self.backend.get_delivery_serve_max_bytes()
                if hasattr(self.backend, 'get_delivery_serve_max_bytes')
                else 256 * 1024 * 1024
            )
            try:
                resolved_path, handle_fd, size = resolve_delivery_bundle_path(delivery.get('path'), roots)
                verified = verify_served_bundle(
                    resolved_path,
                    handle_fd,
                    size,
                    delivery.get('sha256'),
                    record.get('commit'),
                    max_bytes=max_bytes,
                    git=getattr(self.backend, 'bundle_git', None),
                )
            except DeliveryUnavailableError:
                raise HttpError(410, 'bundle_unavailable', DELIVERY_NOT_AVAILABLE) from None
            except DeliveryTooLargeError as error:
                raise HttpError(413, 'payload_too_large', str(error)) from None
            except DeliveryCorruptError:
                raise HttpError(503, 'bundle_checksum_mismatch', DELIVERY_CHECKSUM_MISMATCH) from None
        except HttpError as error:
            self._audit_bundle_read(
                ctx,
                pid,
                record,
                'refused',
                reason=error.code,
                size_bytes=size,
                task=tid,
                contribution=contribution_id,
            )
            raise
        outcome, interrupt_reason = self._send_bundle_stream(handle_fd, record, verified, ctx=ctx, pid=pid)
        self._audit_bundle_read(
            ctx,
            pid,
            record,
            outcome,
            reason=interrupt_reason,
            sha256=verified['sha256'],
            size_bytes=verified['bytes'],
        )
        return None, None

    #: Every field a recommendation may carry over HTTP (``task_id`` is set by the route).
    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/tasks/(?P<tid>' + ID + r')/history')
    def tasks_history(self, ctx):
        """The task's history in exact cursor-bound pages, oldest first or newest first.

        Every page is exact for any ``limit``: a page continued from ``next_cursor``
        resumes at the first entry the previous page did not return, and ``total`` counts
        the logical entries. A long comment arrives as SEVERAL consecutive entries: they
        share ``entry_id`` and carry ``body_offset`` (where the fragment starts in the
        whole body) and ``continued`` (true when more of the same body follows).
        Concatenating them in page order reconstructs the body, so a page boundary may
        split a comment without losing text (kittrial-5bb.273).

        ``recent=N`` (1..100, kittrial-5bb.353; the CLI's ``history --recent``) is the
        page size of a NEWEST-FIRST read of the same snapshot: the newest N logical
        entries first, and ``next_cursor`` continuing toward older ones. A long comment's
        fragments still arrive together and in body order. It replaces ``limit`` (giving
        both is refused). ``since_checkpoint=true`` selects the entries the task's newest
        valid checkpoint did not incorporate, in either order. The cursor is bound to the
        query, so it continues only the same direction and selection. ``order`` says
        which direction a page is in, and ``truncated`` is true when entries exist that
        this answer neither returned nor can continue to (a backend that cannot page).
        """
        self._project(ctx, CAP_READ)
        self._refuse_record_anchor(self.backend.get_task(ctx.params['pid'], ctx.params['tid']))
        scope_id = ctx.params['pid'] + '/' + ctx.params['tid']
        recent, since_checkpoint = self._history_selection(ctx.query)
        if recent is not None:
            limit = recent
            state = read_cursor(ctx.principal, scope_id, ctx.query, ctx.query.get('cursor'),
                                key=self.service.cursor_key())
        else:
            limit, state = self._page(ctx, ctx.query, project_id=scope_id)
        result = self.backend.task_history(ctx.params['pid'], ctx.params['tid'], limit,
                                           state['o'], canonical=state['x'],
                                           recent=recent is not None, since_checkpoint=since_checkpoint)
        who = lambda e: (e.get('user_id') or e.get('author')) if isinstance(e, dict) else None
        # (A canonical entry's author may be structured; ActorNames.get ignores it.)
        names = self._actor_names([who(e) for e in result['items']], ctx.params['pid'])
        body = {'items': [dict(e, user_name=names.get(who(e))) if isinstance(e, dict) else e
                          for e in result['items']], 'total': result['total']}
        if result.get('activity_cursor'):
            body['activity_cursor'] = result['activity_cursor']
        continuation = result.get('canonical_cursor')
        returned = state['o'] + len(_logical_entries(result['items']))
        body['next_cursor'] = (
            make_cursor(ctx.principal, scope_id, ctx.query,
                        returned, continuation, key=self.service.cursor_key())
            if continuation else None)
        body['order'] = 'newest-first' if recent is not None else 'oldest-first'
        # Entries this answer neither returned nor can continue to: a backend without a
        # continuation cursor (the in-process one) cannot page past its first page, so the
        # answer says so instead of looking complete (kittrial-5bb.353).
        body['truncated'] = body['next_cursor'] is None and returned < result['total']
        if body['truncated']:
            body['truncated_note'] = (
                '%d of %d entries are not in this answer and this server cannot page to them: '
                'read them with the client\'s history command.' % (result['total'] - returned, result['total']))
        if since_checkpoint:
            for key in ('since_checkpoint', 'checkpoint_author', 'checkpoint_timestamp',
                        'checkpoint_coverage', 'unverified_entries'):
                body[key] = result.get(key)
        return 200, body

    @staticmethod
    def _history_selection(query):
        """``recent`` (1..MAX_PAGE, or None) and ``since_checkpoint`` (a bool) of a history read."""
        recent = query.get('recent')
        if recent is not None:
            if 'limit' in query:
                raise invalid('Give limit or recent, not both: recent is the page size of a newest-first read')
            try:
                recent = int(recent)
            except (TypeError, ValueError):
                raise invalid('recent must be an integer')
            if not 1 <= recent <= MAX_PAGE:
                raise invalid('recent must be between 1 and %d' % MAX_PAGE)
        since = query.get('since_checkpoint', 'false')
        if since not in ('true', 'false'):
            raise invalid('since_checkpoint must be true or false')
        return recent, since == 'true'

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
        body['items'] = self._task_views(body['items'], ctx.params['pid'])
        return 200, body

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/lanes')
    def project_lanes(self, ctx):
        """Who holds what, who is free, and the three flags of the lane board.

        Readable by any project member (``CAP_READ``); a non-member or a member of another
        project is refused by ``_project`` before anything is read, exactly as the queue
        route refuses them. The aging and the flags are computed in ONE place, the
        canonical ``work lanes`` read (``lanes.py``), which this route only pages and
        scopes: the threshold (``idle_minutes``, 1..10080, default 15) is an argument of the
        page and reaches that read unchanged, and it is never an installation setting.

        ``offset`` is taken from the page's cursor, as the neighbouring reads take the
        cursor's offset, but the page itself is cut by the read (its own ``next_offset``),
        so the cursor the answer carries is minted from that, never from a second cut here.
        """
        self._project(ctx, CAP_READ)
        refuse_ambiguous_query(urlsplit(self.path).query, ('idle_minutes',))
        limit, state = self._page(ctx, ctx.query)
        idle_minutes = self._lanes_idle_minutes(ctx.query.get('idle_minutes'))
        scope, basis, role = self._lanes_scope(ctx.principal, ctx.params['pid'])
        read = self.backend.lanes_read(
            ctx.params['pid'], ctx.principal,
            ['--idle-minutes', str(idle_minutes), '--limit', str(limit), '--offset', str(state['o'])],
            self._lanes_actor(ctx.principal), scope)
        body = dict(read)
        # Why this viewer got this scope, so the page states the rule in force from the answer.
        body['scope_basis'] = basis
        body['scope_role'] = role
        continuation = body.get('next_offset')
        body['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query, continuation,
                                           key=self.service.cursor_key())
                               if type(continuation) is int and continuation > state['o'] else None)
        return 200, body

    def _lanes_idle_minutes(self, raw):
        """The page's threshold argument, or the refusal that names the read's own range.

        A bad threshold is a malformed request (400), not an unprocessable payload: it names
        the ONE read's own range, so the page cannot ask for a value the read would refuse.
        """
        from lanes import DEFAULT_IDLE_MINUTES, IDLE_MINUTES_MIN, IDLE_MINUTES_MAX
        if raw is None:
            return DEFAULT_IDLE_MINUTES
        # Plain ASCII digits only (kittrial-5bb.389): ``int()`` would also take ``1_5``,
        # ``+15`` and surrounding whitespace and quietly repair the caller's value.
        if not re.fullmatch(r'[0-9]+', raw):
            raise HttpError(400, 'invalid_request',
                            'idle_minutes must be a whole number of minutes, %d to %d'
                            % (IDLE_MINUTES_MIN, IDLE_MINUTES_MAX))
        minutes = int(raw)
        if not IDLE_MINUTES_MIN <= minutes <= IDLE_MINUTES_MAX:
            raise HttpError(400, 'invalid_request',
                            'idle_minutes must be %d to %d (one minute to seven days)'
                            % (IDLE_MINUTES_MIN, IDLE_MINUTES_MAX))
        return minutes

    @staticmethod
    def _lanes_actor(principal):
        """The actor the board is read as: the caller's own canonical attribution label.

        The same value ``Service.bind_actor`` accepts on a claim, so a task a person claimed
        from this page is in the lane ``scope='own'`` finds for them.
        """
        return principal.actor or principal.user_id

    def _lanes_scope(self, principal, project_id):
        """The lanes panel's own isolation of the read: ``(scope, basis, role)``.

        This service calls the endpoint WITHOUT a bound key, so the endpoint's own
        ``lanes_scope_for`` answers ``'all'`` for it. That is a property of the transport,
        not an authority the page may inherit, so the route decides for its own viewer. The
        viewer reads every lane (``'all'``) in exactly the cases below and everybody else is
        scoped to their own agents' lanes (``'own'``, the contributor route's rule):

        * ``operator``: an installation operator, the kit's ONE operator definition
          ``review_workflow.listed_operator_actor`` given the same inputs the endpoint uses
          (the operator allowlist and the project's actor -> principal map, read from the
          host through the read-only ``host-coordination`` action). A host that cannot
          answer is not an operator list, so this case then narrows, never widens;
        * ``project-role``: a person whose role IN THIS PROJECT holds ``CAP_COORDINATE``
          (coordinator or owner);
        * ``superuser``: a superuser with no coordinating membership here, who is an owner
          everywhere as on every other coordinator route (kittrial-5bb.410: said as what it
          is, not as an ``owner`` role they do not hold);
        * ``coordinator-grant``: an agent with an EFFECTIVE coordinator grant in this
          project (the grant exists and its issuer still may give it, the agent is enabled and in
          the project, its account is still a coordinator or owner here, and the credential
          is live).

        The second and third are ONE live :meth:`Service.check_authority` decision per read,
        the same one every coordinator route takes; nothing is cached, so a demotion, a
        revoked grant or an expired credential narrows the very next read. Any refusal of
        that check is ``'own'``: this falls back to the narrower view, never the wider.
        ``role`` is the decided role for ``project-role`` and ``None`` otherwise.
        """
        from review_workflow import listed_operator_actor
        actor = self._lanes_actor(principal)
        read = getattr(self.backend, 'host_coordination', None)
        document = {}
        if read is not None:
            try:
                document = read(project_id) or {}
            except HttpError:
                document = {}
        operators = [name for name in (document.get('operators') or []) if isinstance(name, str)]
        owners = {item.get('actor'): item.get('principal')
                  for item in (document.get('sessions') or [])
                  if isinstance(item, dict) and isinstance(item.get('principal'), str)}
        try:
            allowed = listed_operator_actor(actor, operators, owners)
        except (ValueError, OSError):
            allowed = actor in set(operators)
        if allowed:
            return 'all', 'operator', None
        try:
            _project, role = self.service.check_authority(principal, project_id, CAP_COORDINATE)
        except HttpError:
            return 'own', 'own', None
        if principal.via == 'credential':
            return 'all', 'coordinator-grant', None
        if principal.superuser:
            # decide() reads a superuser as owner everywhere, so ``role`` is not their role
            # here: report the membership if it coordinates, else say it is the superuser.
            with self.service.store.lock:
                held = (self.service.state.get('memberships', {}).get(project_id) or {}).get(principal.user_id)
            if held not in ('owner', 'coordinator'):
                return 'all', 'superuser', None
            role = held
        return 'all', 'project-role', role if role in ('owner', 'coordinator') else None

    @route('GET', r'/v1/me/work')
    def me_work(self, ctx):
        """The signed-in person's own work and review attention across their projects.

        Session only (an agent reads ``/v1/agents/me/next``). Walks the caller's own
        memberships, re-authorizing each project with the live ``CAP_READ`` check,
        one queue read per project and at most :data:`ME_WORK_MAX_PROJECTS` projects.
        ``to_review`` lists contributions only in projects where the caller holds the
        approval capability. With the party rule on it holds only work the caller may
        actually approve: a row of the caller's own party is left out, since approving it
        would be refused (kittrial-5bb.199 review, N4). Computed at read time; nothing is
        scheduled or marked.
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
            for item in items:
                if item.get('review_state') not in (None,'none') or item.get('contribution'):
                    blocked.discard(item.get('id'))
            names = self.service.actor_names([i.get('assignee') for i in items]
                                             + [i.get('contribution_author') for i in items])
            # What this person could recommend: someone else's contribution, by person, that
            # nobody of theirs has recommended yet (the rule of _agent_review_actions). "Yet" is
            # read from every name on the row, counted or not (kittrial-5bb.147).
            reviewable = {i.get('id') for i in items if CAP_REVIEWS in capabilities
                          and self._independent([actor], [i.get('assignee'), i.get('contribution_author')], project['id'])
                          and not self._own_party_recommended(i, actor, project['id'])}
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
                    # The party rule's read (kittrial-5bb.199 review, N4): a row whose author or
                    # assignee is the caller's own party would be refused at approval, so My work
                    # does not offer it. Off, `_independent_all` is by person, as it always was.
                    if not self.service.approval_by_another_party or self._independent_all(
                            [actor, principal.user_id],
                            [item.get('assignee'), item.get('contribution_author')],
                            project['id']):
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
        attention_rows={(project['id'],item.get('id')):item for project,_,read in reads for item in read['items']}
        for _, _, task_id, project_id in ranked[:ME_WORK_DETAIL_MAX]:
            try:
                detail = self._cached_read(
                    'detail:' + task_id, project_id,
                    lambda p=project_id, t=task_id: self.backend.task_detail(p, t),
                    shared=True)
            except HttpError:
                continue
            if isinstance(detail, dict):
                row=attention_rows[(project_id,task_id)]
                blocking=attention_blocking_items(row.get('attention') or {})
                detail=dict(detail,blocked=row.get('review_state') in (None,'none')
                            and not row.get('contribution') and type(blocking) is int and blocking>0)
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
                                             state['o'] + limit, key=self.service.cursor_key())
                                 if state['o'] + limit < result['total'] else None)
        return 200, result

    @route('GET', r'/v1/projects/(?P<pid>' + ID + r')/audit')
    def audit_list(self, ctx):
        self._project(ctx, CAP_PROJECT_ADMIN)
        limit, state = self._page(ctx, ctx.query)
        events = [e for e in self.service.state['audit'] if e.get('project_id') == ctx.params['pid']]
        # The events of agents granted this project, cut to the project's view and only since
        # the agent was given this project (kittrial-5bb.365). Stored events are not changed.
        agent_rows = self.service.project_agent_audit(ctx.params['pid'])
        if agent_rows:
            events = sorted(events + agent_rows, key=lambda event: event['time'])
        if hasattr(self.backend, 'requirement_operator_audits'):
            operator_events = self.backend.requirement_operator_audits(ctx.params['pid'])
            if operator_events:
                events += operator_events
                # Same-time service events retain their append order. Sorting
                # their random request IDs can move an earlier event across a
                # page boundary and make a later event disappear from a slice.
                events.sort(key=lambda event:event['time'])
        page = events[state['o']:state['o'] + limit]
        names = self.service.actor_names([e.get('user_id') for e in page])
        body = {'items': [dict(e, user_name=names.get(e.get('user_id')), detail=e.get('reason'))
                          for e in page], 'total': len(events)}
        body['next_cursor'] = (make_cursor(ctx.principal, ctx.params['pid'], ctx.query,
                                           state['o'] + limit, key=self.service.cursor_key())
                               if state['o'] + limit < len(events) else None)
        return 200, body

    @route('GET', r'/healthz', anonymous=True, csrf=False)
    def healthz(self, ctx):
        # The mark is what the office service's health reads to tell this service from another
        # server answering 200 on the port (kittrial-5bb.168 item 8).
        return 200, dict(service_identity(), status='ok')

    @route('GET', r'/v1/service/certificate', anonymous=True, csrf=False)
    def service_certificate(self, ctx):
        """The service's own public leaf certificate, for the person setting up an agent (kittrial-5bb.203).

        Only what the TLS handshake already gives every client: the certificate the loaded context
        presents first, taken by an in-memory handshake against that context and re-encoded as the
        kit's own PEM by ``service_certificate_pem`` -- never the private key and never the ``--cert``
        file itself, which is read only once, by the TLS loader. No log-in, because the point is that
        the certificate can be fetched before the browser or the agent has been made to trust it (the
        My agents set-up dialog shows its SHA-256 fingerprint and saves the file). A service without
        a certificate of its own -- plain http, or the first-install tunnel -- and a context against
        which the in-memory handshake cannot be made serve nothing here: the ordinary not-found
        answer, as if the route did not exist. The route serves what a client receives first only
        while the context holds one certificate and has no ``sni_callback``; with a second RSA or
        ECDSA certificate, or an SNI switch, the client can receive a different one and the route
        must follow what the handshake presents.
        """
        certificate = getattr(self.server, 'tls_certificate_pem', None)
        if not certificate:
            raise not_found('No such operation')
        return 200, {'certificate': certificate,
                     'sha256': certificate_fingerprint(certificate)}

    def _page(self, ctx, query, project_id=None):
        limit_raw = query.get('limit', str(DEFAULT_PAGE))
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise invalid('limit must be an integer')
        if not 1 <= limit <= MAX_PAGE:
            raise invalid('limit must be between 1 and %d' % MAX_PAGE)
        scope = ctx.params.get('pid') if project_id is None else project_id
        state = read_cursor(ctx.principal, scope, query, query.get('cursor'),
                            key=self.service.cursor_key())
        return limit, state


def build_handler(service, backend, *, trusted_proxies=(), max_body=MAX_BODY_BYTES,
                  web_root=DEFAULT_WEB_ROOT, hsts=False):
    return type('ConfiguredApiHandler', (ApiHandler,), {
        'service': service, 'backend': backend,
        'web_root': str(web_root) if web_root is not None else None,
        'read_cache': {}, 'read_cache_lock': threading.Lock(),
        'trusted_proxies': tuple(trusted_proxies or ()),
        'max_body': max_body,
        'hsts': bool(hsts),
        'transfers': TransferGate(),
    })


#: What a socket that is already gone raises when it is wrapped or given its timeout: closed
#: (EBADF; on Windows WSAENOTSOCK, which is ENOTSOCK there), not connected any more, reset,
#: aborted, a broken pipe. Nobody is there to be served; nothing is said.
SOCKET_GONE = frozenset({errno.EBADF, errno.ENOTSOCK, errno.ENOTCONN, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE})
#: How the error of a connection whose set-up failed for a reason of the server's own begins.
SETUP_FAILED = 'connection set-up failed: '
#: The exit statuses of ``main`` for a start that failed, one reason each. The office supervisor
#: reads them to say each in one line (``office_service.WEB_*`` are the same numbers), instead of
#: "A supervised child exited" (kittrial-5bb.168 follow-up).
EXIT_PORT_TAKEN = 4
EXIT_ADDRESS_UNAVAILABLE = 5
EXIT_PORT_NOT_PERMITTED = 6
EXIT_CERTIFICATE_UNUSABLE = 7


def failure_said(error):
    """The OS's own words for a failed start, one line and bounded, never a traceback."""
    return (getattr(error, 'strerror', None) or str(error) or type(error).__name__)[:200]


def key_needs_a_passphrase(keyfile):
    """Whether the key file holds an ENCRYPTED PEM block, so the refusal can say so.

    Read only to WORD the one-line refusal: an encrypted PKCS#8 key carries ``BEGIN ENCRYPTED
    PRIVATE KEY`` and a legacy one ``Proc-Type: 4,ENCRYPTED``, both in the first block. Nothing
    from the file is printed, and a file that cannot be read is not this case - the loader's own
    error is what the refusal reports (kittrial-5bb.168 review item 3).
    """
    try:
        with open(keyfile, 'rb') as stream:
            return b'ENCRYPTED' in stream.read(4096)
    except OSError:
        return False

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
        self._setup_said, self._setup_unsaid = None, 0
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
            elif str(error).startswith(SETUP_FAILED):
                # A fault of the server itself in a connection's set-up: said, and like the
                # line above at most once every TLS_LINE_EVERY seconds, because it comes with
                # every connection while it lasts.
                now = time.monotonic()
                with self._guard:
                    quiet = self._setup_said is not None and now - self._setup_said < self.TLS_LINE_EVERY
                    if quiet:
                        self._setup_unsaid += 1
                    else:
                        unsaid, self._setup_unsaid, self._setup_said = self._setup_unsaid, 0, now
                if not quiet:
                    print('setup: %s: %s; the connection was closed%s'
                          % (ascii(str(client_address[0]))[:60], ascii(str(error)[len(SETUP_FAILED):])[:200],
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


def certificate_fingerprint(certificate):
    """The SHA-256 fingerprint of a PEM certificate, in the form the dialog and the extension show it.

    The same string ``openssl x509 -noout -fingerprint -sha256`` prints and the VS Code extension
    shows for the certificate the TLS handshake presented: the SHA-256 of the certificate's DER,
    as uppercase hex pairs joined by colons. The dialog shows it so a person can compare it with
    what the extension shows; that comparison only says both reached the same endpoint, which is
    why the docs also name the independent check on the server itself (see
    ``docs/HTTP_DEPLOYMENT.md``, "The service's own certificate ...").

    Only the FIRST certificate of the text is used. The route's own PEM (``service_certificate_pem``)
    holds exactly one certificate -- the one the handshake presents -- so this is that one.
    Anything that is not a certificate block is refused.
    """
    match = re.search(r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----',
                      str(certificate or ''), re.S)
    if not match:
        raise ValueError('not a PEM certificate')
    try:
        der = base64.b64decode(''.join(match.group(1).split()), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('not a PEM certificate: its body is not base64') from None
    return ':'.join('%02X' % byte for byte in hashlib.sha256(der).digest())


def service_certificate_pem(context):
    """The certificate the given TLS context presents first, as PEM, or ``None``.

    One in-memory handshake against the very ``ssl.SSLContext`` ``load_cert_chain`` filled: the
    client's ``getpeercert(binary_form=True)`` is the DER of exactly the certificate a real client
    receives first, so the route can serve nothing the handshake would not present (kittrial-5bb.203
    review 01a12488, items 1 and 2). ``ssl.MemoryBIO`` keeps it off the network: no socket is opened,
    and the ``--cert`` file is NOT read again -- the TLS loader read it once, exactly as main does.
    Whatever the loader itself ignores is therefore irrelevant here: armour that does not start a
    line (indented, or after a line that merely ends with it), a ``TRUSTED CERTIFICATE`` or
    ``X509 CERTIFICATE`` label, an older certificate kept above the real one and a comment that
    merely names the armour lines cannot make the route answer anything else. There is no size cap
    and no per-block cap: this decodes nothing, so the answer is one leaf, about 1.2 kB, whatever the
    file's size.

    ``None`` when the handshake cannot be made at all (a context with no certificate of its own, a
    pair that cannot complete): the service still starts and the route answers the ordinary 404,
    never a 500 and never the raw file.
    """
    try:
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        c_in, c_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client_side = client.wrap_bio(c_in, c_out, server_side=False)
        server_side = context.wrap_bio(s_in, s_out, server_side=True)
        for _ in range(20):
            done = 0
            for side in (client_side, server_side):
                try:
                    side.do_handshake()
                    done += 1
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
            s_in.write(c_out.read())
            c_in.write(s_out.read())
            if done == 2:
                der = client_side.getpeercert(binary_form=True)
                break
        else:
            return None
    except (ssl.SSLError, OSError, ValueError):
        return None
    if not der:
        return None
    encoded = base64.b64encode(der).decode('ascii')
    lines = [encoded[at:at + 64] for at in range(0, len(encoded), 64)]
    return ('-----BEGIN CERTIFICATE-----\n' + '\n'.join(lines)
            + '\n-----END CERTIFICATE-----\n')


def create_server(service, backend, *, host='127.0.0.1', port=0, trusted_proxies=(),
                  max_body=MAX_BODY_BYTES, certfile=None, keyfile=None,
                  allow_plaintext_non_loopback=False, web_root=DEFAULT_WEB_ROOT,
                  client_seconds=None, connection_limit=None, address_limit=None, hsts=False):
    """Bind the service. Refuse a non-loopback plaintext listener unless explicitly allowed."""
    loopback = host in LOOPBACK
    if not loopback and certfile is None and not allow_plaintext_non_loopback:
        raise ValueError('Refusing plaintext on a non-loopback interface; supply TLS or '
                         'explicitly allow disposable plaintext')
    context = None
    certificate = None
    if certfile:
        # Before anything is bound: a certificate or key that cannot be used stops the start.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # An empty password, never None: with an encrypted key and no password OpenSSL asks the
        # TERMINAL for a passphrase ("Enter PEM pass phrase:"), which a service started by
        # systemd or the supervisor must never do. With '' it fails at once, and the one-line
        # arm below names it (kittrial-5bb.168 review item 2.1).
        context.load_cert_chain(certfile, keyfile, password='')
        # What the handshake actually presents, for GET /v1/service/certificate
        # (kittrial-5bb.203 review 01a12488, items 1 and 2): one in-memory handshake against this
        # very context, so the route can never answer with anything but the certificate a client
        # receives first. The file is not read again and no armour is pattern-matched; a context the
        # handshake cannot be made against leaves the route answering 404, not the start failing.
        certificate = service_certificate_pem(context)
    if ':' in host:
        server_class = type('GuardedServer6', (GuardedServer,), {'address_family': __import__('socket').AF_INET6})
    else:
        server_class = GuardedServer
    httpd = server_class((host, port), build_handler(service, backend,
                                                     trusted_proxies=trusted_proxies,
                                                     max_body=max_body,
                                                     web_root=web_root,
                                                     hsts=hsts))
    # The TLS context is the connection's (ApiHandler.setup): the listening socket stays plain.
    httpd.tls_context = context
    # The public certificate this listener serves, for GET /v1/service/certificate: None when the
    # service has no certificate of its own (plain http, or the first-install tunnel).
    httpd.tls_certificate_pem = certificate
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


def bootstrap_refusal_advice(error, password_min_length=1):
    """What the person at the terminal has to do about a refused ``--bootstrap-user``.

    Only "a superuser already exists" can be answered with "add this person as an account
    inside the service": on a first bootstrap there is no superuser who could, so a refused
    password or user name says to run the command again with a good one (kittrial-5bb.176
    item 3).
    """
    if getattr(error, 'status', None) == 409:
        return 'Add this person as an account inside the service instead.'
    if 'only spaces' in getattr(error, 'message', ''):
        # kittrial-5bb.363: a whitespace-only password is refused with its own sentence.
        return 'Run the command again with a password that is not only spaces.'
    if 'Password' in getattr(error, 'message', ''):
        return ('Run the command again with a password of %d to %d characters.'
                % (password_min_length, MAX_PASSWORD))
    return 'Run the command again with a name of 2 to 64 characters of letters, digits, . _ @ -.'


def build_backend(service, args):
    """Select the canonical backend. ``endpoint`` is the documented Linux service."""
    if args.backend == 'endpoint':
        return EndpointBackend(args.endpoint_python, args.endpoint, args.root,
                               service=service, actor_namespace=args.actor_namespace,
                               timeout=args.endpoint_timeout, create_timeout=getattr(args, 'create_timeout', 900))
    return InProcessBackend(service, getattr(args, 'actor_namespace', None))


def settings_lines(service):
    """What the service says about its rule settings when it starts: one line each, and the
    change is in the audit when it differs from the last start (``Service.note_settings``).
    A last start that cannot be read is named as unknown, not shown as ``off`` (N5)."""
    was, now = service.note_settings()
    word = {True: 'on', False: 'off'}
    line = 'approval by another party (--approval-by-another-party): %s' % word[now]
    if was is None:
        line += ' (the last start was not recorded; that is written to the audit)'
    elif was != now:
        line += ' (it was %s at the last start; recorded in the audit)' % word[was]
    return [line]


def main(argv=None):
    # No abbreviations: a prefix of a flag that changes a rule of the installation
    # (--approval-by-another) must not turn the rule on (kittrial-5bb.199 review).
    parser = argparse.ArgumentParser(description='Orchestra authenticated HTTP service', allow_abbrev=False)
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
    parser.add_argument('--agent-credential-days', type=int, default=30, metavar='DAYS',
                        help='future agent credential lifetime: 1 to 366 whole days (default 30; office may choose 90)')
    parser.add_argument('--session-idle-minutes', type=int, default=None, metavar='MINUTES',
                        help='session idle limit: 5 to 1440 whole minutes (default 30). A session unused for '
                             'this long signs out')
    parser.add_argument('--session-absolute-hours', type=int, default=None, metavar='HOURS',
                        help='session absolute limit: 1 to 168 whole hours (default 12), never shorter than '
                             '--session-idle-minutes. No use extends it')
    parser.add_argument('--approval-by-another-party', action='store_true',
                        help='nobody approves (or recommends) work of their own party: an account, its agents '
                             'and the worker credentials it issued. Off unless given')
    parser.add_argument('--strict-transport-security', action='store_true',
                        help='send Strict-Transport-Security on HTTPS answers only. Off unless given: a '
                             'self-signed first install must not pin a browser to HTTPS for the host')
    parser.add_argument('--public-url',
                        help='canonical base URL of this service, used only to render '
                             'copyable agent setup/resume snippets (e.g. https://host)')
    parser.add_argument('--password-min-length', type=int, default=1, metavar='N',
                        help='the installation password minimum: 1 to 1024 characters (default 1, '
                             'any non-empty password). The 1024 maximum is a bound on the work one '
                             'request may ask for, not a policy')
    parser.add_argument('--bootstrap-user', help='one-time operator bootstrap superuser')
    parser.add_argument('--list-unconfirmed-projects', action='store_true',
                        help='print the project records the endpoint backend will not serve (an upgrade '
                             'check: a superuser confirms or archives each one), then exit; read-only')
    parser.add_argument('--list-dormant-grants', action='store_true',
                        help='print the coordinator and release grants whose issuer may no longer give '
                             'them, so they have no effect (an upgrade check, kittrial-5bb.410), then exit; '
                             'read-only')
    web = parser.add_mutually_exclusive_group()
    web.add_argument('--web-root', default=str(DEFAULT_WEB_ROOT),
                     help='directory holding the browser interface served at / '
                          '(default: the kit web/ directory)')
    web.add_argument('--no-web', action='store_true',
                     help='serve only the JSON API; do not serve the browser interface')
    args = parser.parse_args(argv)
    try:
        validate_agent_credential_days(args.agent_credential_days)
        # The installation's password minimum (kittrial-5bb.363) is refused the same way:
        # one sentence, a non-zero exit, before the state store is opened or a port is bound.
        password_min_length = validate_password_min_length(args.password_min_length)
        # Refused here, before the state store is opened: a limit that is out of bounds, not
        # a whole number, or shorter than the idle window is one sentence and a non-zero exit,
        # never a clamped value (kittrial-5bb.330). An omitted flag keeps 30 min / 12 h.
        session_idle, session_absolute = session_limit_seconds(args.session_idle_minutes,
                                                               args.session_absolute_hours)
    except ValueError as error:
        parser.error(str(error))

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
    if args.list_dormant_grants:
        # Read-only, like --list-unconfirmed-projects: the document is read as JSON, no store is opened.
        if not Path(args.state).is_file():
            parser.error('--list-dormant-grants needs an existing --state file')
        document = record_json.loads(Path(args.state).read_text(encoding='utf-8'))
        if not isinstance(document, dict) or not isinstance(document.get('users'), dict):
            parser.error('--state is not a service state document')
        from http_authority import dormant_grants
        items = dormant_grants(document)
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
        try:
            lock_fd = runtime_service_lock(args.root)
        except ValueError as error:
            # A host without fcntl (Windows): one sentence instead of a traceback, before the
            # state store is opened (kittrial-5bb.176 item 2).
            print('Refusing to bootstrap %s: %s. Run this command on the POSIX host that runs the service.'
                  % (args.bootstrap_user, error), file=sys.stderr)
            return 1
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
            confirmation = getpass.getpass('Repeat new superuser password: ')
            if password != confirmation:
                # The host bootstrap is one of the places a password is set (kittrial-5bb.363):
                # ask twice and refuse a mismatch before the store is written.
                print('Refusing to bootstrap %s: the two passwords do not match.'
                      % args.bootstrap_user, file=sys.stderr)
                return 1
            Service.bootstrap_superuser(store, args.bootstrap_user, password,
                                        password_min_length=password_min_length)
        except HttpError as error:
            # A second bootstrap, a refused user name or a password the kit refuses: one
            # sentence and the one thing to do about it, never a traceback
            # (kittrial-5bb.176 items 2 and 3).
            print('Refusing to bootstrap %s: %s. %s'
                  % (args.bootstrap_user, error.message,
                     bootstrap_refusal_advice(error, password_min_length)), file=sys.stderr)
            return 1
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
    if args.public_url:
        # kittrial-5bb.360: one sentence, before the state is opened or a port is bound.
        from server_address import public_url_problem
        problem = public_url_problem(args.public_url, 'the service start (--public-url)')
        if problem:
            parser.error(problem)
    store = Store(args.state)
    if args.backend == 'endpoint' and (not args.endpoint or not args.root):
        parser.error('--backend endpoint requires --endpoint and --root '
                     '(use --backend inprocess only for a disposable local check)')
    trusted = list(args.trusted_proxy)
    if args.trust_proxy and 'localhost' not in trusted:
        trusted.append('localhost')
    service = Service(store, public_url=args.public_url, approval_by_another_party=args.approval_by_another_party,
                      agent_credential_days=args.agent_credential_days,
                      password_min_length=password_min_length,
                      session_idle=session_idle, session_absolute=session_absolute)
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
                              address_limit=args.connections_per_address,
                              hsts=args.strict_transport_security)
    except OSError as failed:
        # On Windows a port another program holds for itself alone is refused as "access", not "in use".
        if failed.errno == errno.EADDRINUSE or getattr(failed, 'winerror', None) == 10013:
            # One line and a status of its own, not a traceback: the commonest reason a service
            # does not start, and the operator must be able to read it. A socket bound but not
            # listening holds the port too, so the line says "holds", not "is listening".
            print('orchestra-http: port %d on %s is taken: something else holds it, so the web service did '
                  'not start. Stop that, or start this service on another port.' % (args.port, args.host),
                  file=sys.stderr, flush=True)
            return EXIT_PORT_TAKEN
        if isinstance(failed, ssl.SSLError) or (args.cert and isinstance(failed, FileNotFoundError)):
            # Only when the key really is an encrypted one: the hint read as the cause for every
            # certificate failure, a missing file and a mismatched pair included (kittrial-5bb.168
            # review item 3).
            hint = (' A key with a passphrase cannot be used here, because this service never asks for one.'
                    if key_needs_a_passphrase(args.key) else '')
            print('orchestra-http: the certificate or key cannot be used: %s, so the web service did not start.%s '
                  'The office supervisor names the file and what is wrong before it starts anything.'
                  % (failure_said(failed), hint), file=sys.stderr, flush=True)
            return EXIT_CERTIFICATE_UNUSABLE
        if args.port < 1024 and failed.errno in (errno.EACCES, errno.EPERM):
            print('orchestra-http: port %d cannot be bound: %s. A port below 1024 needs privilege; use a port '
                  'from 1024 to 65535.' % (args.port, failure_said(failed)), file=sys.stderr, flush=True)
            return EXIT_PORT_NOT_PERMITTED
        if args.cert and isinstance(failed, PermissionError):
            print('orchestra-http: the certificate or key cannot be read: %s, so the web service did not start.'
                  % failure_said(failed), file=sys.stderr, flush=True)
            return EXIT_CERTIFICATE_UNUSABLE
        if isinstance(failed, socket.gaierror) or failed.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
            print('orchestra-http: the address %s cannot be used: %s, so the web service did not start. Give an '
                  'address of this host, or a name that resolves to one.' % (args.host, failure_said(failed)),
                  file=sys.stderr, flush=True)
            return EXIT_ADDRESS_UNAVAILABLE
        if failed.errno in (errno.EACCES, errno.EPERM):
            print('orchestra-http: port %d cannot be bound: %s, so the web service did not start.'
                  % (args.port, failure_said(failed)), file=sys.stderr, flush=True)
            return EXIT_PORT_NOT_PERMITTED
        raise
    # Said once the service has its port: a start that failed says its one line and has
    # started nothing, so it has changed no setting either.
    for line in settings_lines(service):
        print(line, file=sys.stderr, flush=True)
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
