#!/usr/bin/env python3
"""Standard-library client for the authenticated office HTTP transport.

This is the command-line/worker counterpart of ``http_service.py``. It speaks only
JSON over HTTP(S), never touches the runtime filesystem, and refuses plaintext to a
non-loopback host so a bearer token cannot be sent in the clear by accident.

Design points (see ``docs/HTTP_TRANSPORT_DESIGN.md`` sections 6 and 7):

* bearer sessions/credentials travel only in ``Authorization: Bearer``; a cookie
  mode is available for browser-like callers and then sends the CSRF token,
* every retryable mutation can carry an ``Idempotency-Key``; an uncertain ``503``
  raises :class:`UncertainOutcome` carrying the same key instead of reporting a
  fabricated success, so the caller reconciles with an exact retry,
* **retry contract:** an exact retry of a canonical mutation sent no more than 29
  days after the original attempt (``http_authority.JOURNAL_RETRY_HORIZON_SECONDS``)
  replays, reports uncertainty or is refused as expired - it is never re-executed -
  as long as the server's total uncredited forward clock error stays below 24 h (the
  operator recovers with ``--reset-high-water`` after correcting the clock). A
  service-local route (credential issue, account/project create, membership change)
  can be retried exactly within its 24 h idempotency window. An older retry is
  unsupported and may run the effect again, so reconcile and use a new key instead,
* errors are surfaced as :class:`HttpApiError` with the server's stable code,
  message, detail and request id.

No third-party packages. ``client.py`` (the SSH/local transport) is untouched and
remains the operator path.
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('http_client.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import http.client
import json
import os
import secrets
import ssl
import sys
from urllib.parse import quote, urlencode, urlsplit


def _lifecycle_body_file(path):
    """Bound caller text before decoding, while keeping the copied client standalone."""
    with open(path, 'rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Lifecycle request file exceeds 1 MiB')
    text = raw.decode('utf-8-sig')
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > 64:
                raise ValueError('JSON nested too deeply (more than 64 levels)')
        elif char in ']}':
            depth -= 1
    def fields(pairs):
        body = {}
        for name, value in pairs:
            if name in body:
                raise ValueError('Duplicate lifecycle request field: ' + name)
            body[name] = value
        return body
    try:
        body = json.loads(text, object_pairs_hook=fields)
    except RecursionError:
        raise ValueError('JSON nested too deeply (more than 64 levels)') from None
    if not isinstance(body, dict):
        raise ValueError('The lifecycle request body must be a JSON object')
    return body

MUTATING = ('POST', 'PUT', 'PATCH', 'DELETE')
LOOPBACK = ('127.0.0.1', '::1', 'localhost')


class HttpApiError(Exception):
    """A clean server error: stable code, message, optional detail and request id."""

    def __init__(self, status, code, message, detail=None, request_id=None, key=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail
        self.request_id = request_id
        self.key = key

    def __str__(self):
        # The message is the SERVER's own text: a kit program that prints this exception (for
        # example tools/demonstrate_review_targeting.py prints `{error}`) must not let a server
        # rewrite the terminal, so it goes through the client's one escape for server text
        # (kittrial-5bb.241). Imported here, not at the top: this module prints nothing itself, and
        # a copy shipped without client.py beside it keeps printing the message as it always did.
        try:
            from client import server_text
        except ImportError:
            server_text = str
        text = '%s (%s)' % (server_text(self.message), self.code)
        if self.request_id:
            text += ' [%s]' % self.request_id
        return text


class UncertainOutcome(HttpApiError):
    """A mutation may have committed. Retry the identical request with ``key``."""


class Client:
    def __init__(self, base_url, *, timeout=30, ca_file=None, insecure=False,
                 allow_plaintext=False, use_cookie=False, request_id=None):
        parts = urlsplit(base_url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('base_url must be an absolute http(s) URL')
        loopback = parts.hostname in LOOPBACK
        if parts.scheme != 'https' and not (loopback or allow_plaintext):
            raise ValueError('Refusing plaintext HTTP to a non-loopback host; use https '
                             'or pass allow_plaintext for a disposable loopback test')
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == 'https' else 80)
        self.prefix = parts.path.rstrip('/')
        self.timeout = timeout
        self.ca_file = ca_file
        self.insecure = insecure
        self.use_cookie = use_cookie
        self.request_id = request_id
        self.token = None
        self.csrf = None
        self.cookie = None

    # -- credentials -----------------------------------------------------------
    def use_credential(self, secret):
        """Switch to a project-scoped worker credential (bearer)."""
        self.token = secret
        self.csrf = None
        self.cookie = None
        return self

    def clear(self):
        self.token = self.csrf = self.cookie = None
        return self

    def _connection(self):
        if self.scheme == 'https':
            context = ssl.create_default_context(cafile=self.ca_file)
            if self.insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=context)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    # -- wire ------------------------------------------------------------------
    def request(self, method, path, body=None, *, key=None, anonymous=False, headers=None):
        headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False)
            headers['Content-Type'] = 'application/json; charset=utf-8'
        if not anonymous:
            if self.use_cookie and self.cookie:
                # Browser-like mode: cookie-only auth, so the server requires CSRF.
                headers['Cookie'] = self.cookie
                if method in MUTATING and self.csrf:
                    headers['X-CSRF-Token'] = self.csrf
            elif self.token:
                headers['Authorization'] = 'Bearer ' + self.token
        if key:
            headers['Idempotency-Key'] = key
        if self.request_id:
            headers['X-Request-Id'] = self.request_id
        connection = self._connection()
        try:
            connection.request(method, self.prefix + path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            header_list = response.getheaders()
        finally:
            connection.close()
        data = None
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw.decode('utf-8', 'replace')
        if status >= 400:
            error = data.get('error', {}) if isinstance(data, dict) else {}
            request_id = data.get('request_id') if isinstance(data, dict) else None
            if status == 503 and error.get('code') == 'uncertain':
                raise UncertainOutcome(status, 'uncertain', error.get('message', 'Uncertain outcome'),
                                       error.get('detail'), request_id, key)
            raise HttpApiError(status, error.get('code', 'http_error'),
                               error.get('message', 'Request failed'), error.get('detail'),
                               request_id, key)
        if method == 'POST' and path == '/v1/sessions' and isinstance(data, dict):
            session = data.get('session', {})
            self.token = session.get('token')
            self.csrf = session.get('csrf_token')
            for name, value in header_list:
                if name.lower() == 'set-cookie':
                    self.cookie = value.split(';')[0]
                    break
        return data

    # -- sessions and accounts -------------------------------------------------
    def login(self, username, password):
        return self.request('POST', '/v1/sessions', {'username': username, 'password': password},
                            anonymous=True)

    def logout(self):
        try:
            return self.request('DELETE', '/v1/sessions/current')
        finally:
            self.clear()

    def whoami(self):
        return self.request('GET', '/v1/sessions/current')

    def create_account(self, username, display_name=None, key=None):
        return self.request('POST', '/v1/accounts', {'username': username,
                                                     'display_name': display_name}, key=key)

    def list_accounts(self):
        return self.request('GET', '/v1/accounts')['items']

    def change_password(self, user_id, new_password, current_password=None, key=None):
        return self.request('POST', '/v1/accounts/%s/password' % quote(user_id, safe=''),
                            {'new_password': new_password,
                             'current_password': current_password}, key=key)

    def issue_reset(self, user_id, key=None):
        return self.request('POST', '/v1/accounts/%s/reset' % quote(user_id, safe=''), key=key)

    def redeem_reset(self, user_id, reset_value, new_password):
        return self.request('POST', '/v1/accounts/%s/reset/redeem' % quote(user_id, safe=''),
                            {'reset_value': reset_value, 'new_password': new_password},
                            anonymous=True)

    def disable_account(self, user_id, key=None):
        return self.request('POST', '/v1/accounts/%s/disable' % quote(user_id, safe=''), key=key)

    # -- projects and membership ----------------------------------------------
    def create_project(self, name, project_id=None, key=None):
        return self.request('POST', '/v1/projects', {'name': name, 'project_id': project_id},
                            key=key)

    def list_projects(self):
        return self.request('GET', '/v1/projects')['items']

    def get_project(self, project):
        return self.request('GET', '/v1/projects/%s' % quote(project, safe=''))

    def assignment_roster(self, project, limit=100, cursor=None):
        query={'limit':limit}
        if cursor is not None:query['cursor']=cursor
        return self.request('GET','/v1/projects/%s/assignment-roster' % quote(project,safe='') + '?' + urlencode(query))

    def assign_task(self, project, task, expected_assignee, assignee, reason,
                    *, allow_pending_review=False, allow_held_slot=False, key=None):
        identity=key or new_idempotency_key()
        payload=dict(expected_assignee=expected_assignee,assignee=assignee,reason=reason,
                     allow_pending_review=allow_pending_review,allow_held_slot=allow_held_slot)
        try:
            return self.request('POST','/v1/projects/%s/tasks/%s/assign' %
                                (quote(project,safe=''),quote(task,safe='')),payload,key=identity)
        except OSError:
            raise UncertainOutcome(0,'uncertain','Connection lost; reconcile the identical assignment',
                                   None,None,identity) from None

    def guidance(self, project):
        return self.request('GET', '/v1/projects/%s/guidance' % quote(project, safe=''))

    def guidance_status(self, project, offset=0, limit=50):
        return self.request('GET', '/v1/projects/%s/guidance/status?%s'
                            % (quote(project, safe=''), urlencode(dict(offset=offset, limit=limit))))

    def member_onboarding(self, project):
        return self.request('GET', '/v1/projects/%s/onboarding/read' % quote(project, safe=''))

    def _write_guidance(self, project, method, suffix, body, key):
        identity = key or new_idempotency_key()
        try:
            return self.request(method, '/v1/projects/%s/guidance%s' % (quote(project, safe=''), suffix),
                                body, key=identity)
        except OSError:
            raise UncertainOutcome(0, 'uncertain', 'Connection lost; reconcile the identical guidance request',
                                   None, None, identity) from None

    def acknowledge_guidance(self, project, version, key=None):
        return self._write_guidance(project, 'POST', '/ack', {'version': version}, key)

    def set_guidance(self, project, text, key=None):
        return self._write_guidance(project, 'PUT', '', {'text': text}, key)

    def clear_guidance(self, project, key=None):
        return self._write_guidance(project, 'DELETE', '', {}, key)

    def merge_slot(self, project):
        return self.request('GET', '/v1/projects/%s/merge-slot' % quote(project, safe=''))

    def lifecycle(self, project, task):
        return self.request('GET', '/v1/projects/%s/tasks/%s/lifecycle'
                            % (quote(project, safe=''), quote(task, safe='')))

    def _write_lifecycle(self, project, task, operation, body, key=None, dimension=None):
        identity = key or new_idempotency_key()
        suffix = 'scope' if operation == 'scope' else 'facts/' + quote(dimension, safe='')
        path = '/v1/projects/%s/tasks/%s/lifecycle/%s' % (quote(project, safe=''), quote(task, safe=''), suffix)
        try:
            return self.request('POST', path, body, key=identity)
        except OSError:
            raise UncertainOutcome(0, 'uncertain', 'Connection lost; reconcile the identical lifecycle request',
                                   None, None, identity) from None

    def set_lifecycle_scope(self, project, task, scope, expected_scope_token, evidence,
                            provenance='performed', note=None, key=None):
        body = dict(scope=scope, expected_scope_token=expected_scope_token, evidence=evidence, provenance=provenance)
        if note is not None:body['note'] = note
        return self._write_lifecycle(project, task, 'scope', body, key)

    def record_lifecycle_fact(self, project, task, dimension, value, scope, evidence,
                              provenance='performed', note=None, trigger=None, key=None):
        body = dict(scope=scope, value=value, evidence=evidence, provenance=provenance)
        if note is not None:body['note'] = note
        if trigger is not None:body['trigger'] = trigger
        return self._write_lifecycle(project, task, 'fact', body, key, dimension)

    def _write_merge_slot(self, project, operation, body, key):
        identity = key or new_idempotency_key()
        try:
            return self.request('POST', '/v1/projects/%s/merge-slot/%s'
                                % (quote(project, safe=''), operation), body, key=identity)
        except OSError:
            raise UncertainOutcome(0, 'uncertain', 'Connection lost; reconcile the identical merge-slot request',
                                   None, None, identity) from None

    def acquire_merge_slot(self, project, task, target, key=None):
        return self._write_merge_slot(project, 'acquire', {'task_id': task, 'target': target}, key)

    def release_merge_slot(self, project, key=None):
        return self._write_merge_slot(project, 'release', {}, key)

    def force_release_merge_slot(self, project, expected_holder, reason, key=None, expected_task=None):
        body = {'expected_holder': expected_holder, 'reason': reason}
        if expected_task is not None:
            body['expected_task'] = expected_task   # the task the caller saw held (kittrial-5bb.268)
        return self._write_merge_slot(project, 'force-release', body, key)

    def create_merge_slot(self, project, key=None):
        return self._write_merge_slot(project, 'create', {}, key)

    def archive_project(self, project, key=None):
        return self.request('POST', '/v1/projects/%s/archive' % quote(project, safe=''), key=key)

    def set_member(self, project, user_id, role, key=None):
        return self.request('PUT', '/v1/projects/%s/members/%s'
                            % (quote(project, safe=''), quote(user_id, safe='')),
                            {'role': role}, key=key)

    def remove_member(self, project, user_id, key=None):
        return self.request('DELETE', '/v1/projects/%s/members/%s'
                            % (quote(project, safe=''), quote(user_id, safe='')), key=key)

    # -- personal agents -------------------------------------------------------
    def create_agent(self, name, *, owner=None, key=None, **fields):
        body = dict(fields, name=name)
        if owner is not None:
            body['owner'] = owner
        return self.request('POST', '/v1/agents', body, key=key)

    def extend_agent_credential(self, agent_id, credential_id, *, project=None, key=None):
        stem = '/v1/agents' if project is None else '/v1/projects/%s/agents' % quote(project, safe='')
        return self.request('POST', '%s/%s/credentials/%s/extend' %
                            (stem, quote(agent_id, safe=''), quote(credential_id, safe='')), {}, key=key)

    def set_my_claim_limit(self, value, key=None):
        """Lower the CALLING agent's own claim limit (``PATCH /v1/agents/me``).

        The caller must authenticate as the agent itself; ``value`` is a whole number
        from 1 up to the limit that agent holds now. Raising it is the owner's move.
        """
        return self.request('PATCH', '/v1/agents/me', {'claim_limit': value}, key=key)

    # -- worker credentials ----------------------------------------------------
    def issue_credential(self, project, label=None, scopes=None, actor=None, key=None):
        return self.request('POST', '/v1/projects/%s/worker-credentials' % quote(project, safe=''),
                            {'label': label, 'scopes': scopes, 'actor': actor}, key=key)

    def revoke_credential(self, project, credential_id, key=None):
        return self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                            % (quote(project, safe=''), quote(credential_id, safe='')),
                            key=key)

    # -- tasks, checkpoints, reviews, history ---------------------------------
    def create_task(self, project, title, description=None, attachments=None, key=None):
        return self.request('POST', '/v1/projects/%s/tasks' % quote(project, safe=''),
                            {'title': title, 'description': description,
                             'attachments': attachments}, key=key)

    def update_task(self, project, task, version, **fields):
        body = dict(fields)
        body['version'] = version
        return self.request('PATCH', '/v1/projects/%s/tasks/%s'
                            % (quote(project, safe=''), quote(task, safe='')), body)

    def list_tasks(self, project, limit=None, cursor=None):
        return self.request('GET', '/v1/projects/%s/tasks%s'
                            % (quote(project, safe=''), _query(limit, cursor)))

    def get_task(self, project, task):
        return self.request('GET', '/v1/projects/%s/tasks/%s'
                            % (quote(project, safe=''), quote(task, safe='')))

    def claim_task(self, project, task, actor=None, key=None):
        return self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                            % (quote(project, safe=''), quote(task, safe='')),
                            {'actor': actor}, key=key)

    def add_comment(self, project, task, text, key=None):
        """Append literal text under the authenticated actor; no author override."""
        return self.request('POST', '/v1/projects/%s/tasks/%s/comments'
                            % (quote(project, safe=''), quote(task, safe='')),
                            {'text': text}, key=key)

    def add_checkpoint(self, project, task, *, previous, summary, open_items=None,
                       activity_cursor=None, actor=None, key=None, schema_version=1,
                       source_commit='', branch='', intent=None, acceptance=None,
                       next_action=None, resolved=None, lane_state=None):
        """Append one canonical checkpoint; ``activity_cursor`` comes from history/brief."""
        body = {'schema_version': schema_version, 'previous': previous,
                'activity_cursor': activity_cursor, 'source_commit': source_commit,
                'branch': branch, 'intent': intent, 'acceptance': acceptance,
                'summary': summary, 'next_action': next_action,
                'open_items': open_items or [], 'resolved': resolved or [], 'actor': actor}
        if lane_state is not None:
            body['lane_state'] = lane_state
        return self.request('POST', '/v1/projects/%s/tasks/%s/checkpoints'
                            % (quote(project, safe=''), quote(task, safe='')),
                            body, key=key)

    def add_review(self, project, task, operation, *, operation_id=None, previous=None,
                   schema_version=1, commit=None, base_commit=None, bundle_sha256=None,
                   summary=None, actor=None, key=None, repository=None, delivery=None,
                   supersedes=None, follows=None, contribution=None, items=None, resolutions=None,
                   reason=None, reviewer=None, request=None, item=None, disposition=None,
                   verdict=None, method=None, sha256=None, bytes_=None, hold=None):
        """Record one contribution/review operation.

        The canonical backend forwards exactly the fields the operation needs, so the
        caller supplies the canonical payload (``previous``/``operation_id`` and the
        operation-specific evidence). ``follows`` is the optional additive follow-on
        relation (mutually exclusive with ``supersedes``); it must survive the HTTP hop
        or a follow-on silently becomes a first contribution or a supersede.
        ``reason``/``reviewer``/``request``/``item`` and ``disposition`` carry the
        additive operations (withdraw, request-review, resolve-item, decline-review) and
        the request-changes ``summary`` over HTTP (kittrial-5bb.110 item 9).
        ``method``/``sha256``/``bytes_``/``hold`` carry the verifier attestation and the
        integration hold operations (``attest``/``hold``/``release-hold``,
        kittrial-5bb.10); ``bytes_`` is sent as the canonical field name ``bytes``.

        A null OPTIONAL field is not sent at all: sending ``"summary": null`` for an
        unset request-changes summary made the canonical writer refuse every legacy
        request-changes the kit's own client sent (kittrial-5bb.110 item 1). Only the
        canonical required fields that are legitimately null for THIS operation are
        kept, because their ABSENCE -- unlike their null -- is an invalid field set:
        ``previous`` on everything but a recommendation (which carries none) and
        ``supersedes`` on a contribution. Every other null is an absent field and is
        dropped, so a null can never arrive on the wrong operation.
        """
        body = {'schema_version': schema_version, 'operation': operation,
                'operation_id': operation_id, 'previous': previous, 'actor': actor,
                'commit': commit, 'base_commit': base_commit, 'bundle_sha256': bundle_sha256,
                'summary': summary, 'repository': repository, 'delivery': delivery,
                'supersedes': supersedes, 'follows': follows, 'contribution': contribution,
                'items': items, 'resolutions': resolutions, 'reason': reason,
                'reviewer': reviewer, 'request': request, 'item': item,
                'disposition': disposition, 'verdict': verdict, 'method': method,
                'sha256': sha256, 'bytes': bytes_, 'hold': hold}
        kept_nullable = {'operation_id'}
        if operation != 'recommend':
            kept_nullable.add('previous')
        if operation == 'contribute':
            kept_nullable.add('supersedes')
        for field in [name for name, value in body.items()
                      if value is None and name not in kept_nullable]:
            del body[field]
        return self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                            % (quote(project, safe=''), quote(task, safe='')), body, key=key)

    def history(self, project, task, limit=None, cursor=None):
        return self.request('GET', '/v1/projects/%s/tasks/%s/history%s'
                            % (quote(project, safe=''), quote(task, safe=''),
                               _query(limit, cursor)))

    def add_feedback(self, project, text, source_task=None, evidence=None, actor=None, key=None):
        return self.request('POST', '/v1/projects/%s/feedback' % quote(project, safe=''),
                            {'text': text, 'source_task': source_task, 'evidence': evidence,
                             'actor': actor}, key=key)

    def list_feedback(self, project, limit=None, cursor=None):
        return self.request('GET', '/v1/projects/%s/feedback%s'
                            % (quote(project, safe=''), _query(limit, cursor)))

    def audit(self, project, limit=None, cursor=None):
        return self.request('GET', '/v1/projects/%s/audit%s'
                            % (quote(project, safe=''), _query(limit, cursor)))


def new_idempotency_key():
    """A fresh key; keep it to retry an uncertain mutation exactly."""
    return 'cli_' + secrets.token_urlsafe(18)


def _query(limit, cursor):
    params = {}
    if limit is not None:
        params['limit'] = str(limit)
    if cursor is not None:
        params['cursor'] = cursor
    return ('?' + urlencode(params)) if params else ''


# --------------------------------------------------------------------------- CLI
def _build_parser():
    parser = argparse.ArgumentParser(description='Orchestra office HTTP client')
    parser.add_argument('--base-url', default=os.environ.get('ORCHESTRA_HTTP_URL'),
                        help='service base URL (or ORCHESTRA_HTTP_URL)')
    parser.add_argument('--token',
                        help='bearer session or worker credential; pass it as --token=VALUE '
                             'so a secret beginning with "-" is not read as an option')
    parser.add_argument('--ca-file', help='CA bundle for TLS verification')
    parser.add_argument('--insecure', action='store_true', help='skip TLS verification (tests only)')
    parser.add_argument('--use-cookie', action='store_true',
                        help='send the session cookie and CSRF token instead of a bearer header')
    sub = parser.add_subparsers(dest='command', required=True)
    login = sub.add_parser('login', help='create a browser/worker session')
    login.add_argument('--username', required=True)
    login.add_argument('--password-env', default='ORCHESTRA_PASSWORD',
                       help='environment variable holding the password (default ORCHESTRA_PASSWORD)')
    sub.add_parser('logout', help='revoke the current session')
    sub.add_parser('whoami', help='show the authenticated principal')
    mine = sub.add_parser('my-claim-limit',
                          help="lower the calling agent's own claim limit")
    mine.add_argument('--claim-limit', type=int, required=True,
                      help='the new limit: a whole number from 1 up to the current one')
    mine.add_argument('--idempotency-key',
                      help='reuse exactly this key and request after an uncertain result')
    slot = sub.add_parser('merge-slot', help='read or coordinate the project merge slot')
    slot.add_argument('project')
    slot.add_argument('operation', choices=('read', 'acquire', 'release', 'force-release', 'create'))
    slot.add_argument('--task')
    slot.add_argument('--target')
    slot.add_argument('--expected-holder')
    slot.add_argument('--reason')
    slot.add_argument('--idempotency-key', help='reuse exactly this key and request after an uncertain result')
    roster = sub.add_parser('assignment-roster', help='page existing eligible project identities')
    roster.add_argument('project')
    roster.add_argument('--limit', type=int, default=100)
    roster.add_argument('--cursor')
    assignment = sub.add_parser('assign-task', help='coordinate an assignee-only change with a durable task notice')
    assignment.add_argument('project')
    assignment.add_argument('task')
    expected = assignment.add_mutually_exclusive_group(required=True)
    expected.add_argument('--expected-assignee')
    expected.add_argument('--expected-unassigned', action='store_true')
    target = assignment.add_mutually_exclusive_group(required=True)
    target.add_argument('--assignee')
    target.add_argument('--unassign', action='store_true')
    assignment.add_argument('--reason', required=True)
    assignment.add_argument('--allow-pending-review', action='store_true')
    assignment.add_argument('--allow-held-slot', action='store_true')
    assignment.add_argument('--idempotency-key', help='reuse exactly this key and request after an uncertain result')
    life = sub.add_parser('lifecycle', help='read or record task scope and lifecycle facts')
    life.add_argument('project');life.add_argument('task')
    life.add_argument('operation', choices=('read','scope','fact'))
    life.add_argument('--dimension', choices=('implemented','tested','reviewed','integrated','deployed','live-verified'))
    life.add_argument('--file', help='UTF-8 JSON HTTP request body for scope or fact')
    life.add_argument('--idempotency-key', help='reuse exactly this key and body after an uncertain result')
    call = sub.add_parser('call', help='issue one raw JSON request')
    call.add_argument('method')
    call.add_argument('path')
    call.add_argument('--body', help='JSON request body')
    call.add_argument('--idempotency-key', help='reuse a key to reconcile an uncertain write')
    call.add_argument('--credential',
                      help='use this worker credential for the call; pass it as '
                           '--credential=VALUE so a secret beginning with "-" is not read '
                           'as an option')
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error('--base-url or ORCHESTRA_HTTP_URL is required')
    client = Client(args.base_url, ca_file=args.ca_file, insecure=args.insecure,
                    use_cookie=args.use_cookie)
    if args.token:
        client.token = args.token
    try:
        if args.command == 'login':
            password = os.environ.get(args.password_env)
            if password is None:
                parser.error('environment variable %s is not set' % args.password_env)
            result = client.login(args.username, password)
            print(json.dumps(result, indent=2))
            print('session token issued; export it into the environment, do not log it',
                  file=sys.stderr)
            return 0
        if args.command == 'logout':
            client.logout()
            return 0
        if args.command == 'whoami':
            print(json.dumps(client.whoami(), indent=2))
            return 0
        if args.command == 'my-claim-limit':
            print(json.dumps(client.set_my_claim_limit(args.claim_limit,
                                                       key=args.idempotency_key), indent=2))
            return 0
        if args.command == 'assignment-roster':
            print(json.dumps(client.assignment_roster(args.project,args.limit,args.cursor),indent=2))
            return 0
        if args.command == 'assign-task':
            result=client.assign_task(args.project,args.task,args.expected_assignee,args.assignee,args.reason,
                allow_pending_review=args.allow_pending_review,allow_held_slot=args.allow_held_slot,
                key=args.idempotency_key)
            print(json.dumps(result,indent=2))
            return 0
        if args.command == 'merge-slot':
            if args.operation == 'read':result = client.merge_slot(args.project)
            elif args.operation == 'acquire':
                if not args.task or not args.target:parser.error('acquire requires --task and --target')
                result = client.acquire_merge_slot(args.project, args.task, args.target, args.idempotency_key)
            elif args.operation == 'release':result = client.release_merge_slot(args.project, args.idempotency_key)
            elif args.operation == 'create':result = client.create_merge_slot(args.project, args.idempotency_key)
            else:
                if not args.expected_holder or not args.reason:parser.error('force-release requires --expected-holder and --reason')
                result = client.force_release_merge_slot(args.project, args.expected_holder, args.reason, args.idempotency_key)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'lifecycle':
            if args.operation == 'read':result = client.lifecycle(args.project, args.task)
            else:
                if not args.file:parser.error('scope and fact require --file')
                if args.operation == 'fact' and not args.dimension:parser.error('fact requires --dimension')
                body = _lifecycle_body_file(args.file)
                result = client._write_lifecycle(args.project, args.task, args.operation, body,
                                                 args.idempotency_key, args.dimension)
            print(json.dumps(result, indent=2));return 0
        if args.credential:
            client.use_credential(args.credential)
        body = json.loads(args.body) if args.body else None
        result = client.request(args.method.upper(), args.path, body,
                                key=args.idempotency_key)
        print(json.dumps(result, indent=2))
        return 0
    except UncertainOutcome as error:
        print('UNCERTAIN: %s' % error, file=sys.stderr)
        print('Reconcile with the identical request and Idempotency-Key %s' % error.key,
              file=sys.stderr)
        return 2
    except HttpApiError as error:
        print('ERROR: %s' % error, file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print('ERROR: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
