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
import hashlib
import http.client
import json
import os
import secrets
import ssl
import sys
import tempfile
from urllib.parse import quote, urlencode, urlsplit


DESIGN_DOCUMENT_MAX_BYTES = 1024 * 1024


def _document_from_git(entry, repo):
    """kittrial-5bb.418: (entry with authority.sha256, document text) for --document-from-git.

    The entry's repo-document authority names the path and commit. The document is git's own object at
    that commit (never the working tree), refused unless it is a regular file (no symlink, submodule or
    tree), at most 1 MiB and UTF-8; its bytes are kept exactly (line endings, a byte-order mark). This
    client stays standalone, so it repeats the kit's design_documents.document_from_git rather than
    importing it."""
    import subprocess
    authority = entry.get('authority') if isinstance(entry, dict) else None
    if not isinstance(authority, dict) or authority.get('type') != 'repo-document' \
            or not isinstance(authority.get('path'), str) or not isinstance(authority.get('commit'), str):
        raise ValueError('--document-from-git needs an entry whose authority is a repo-document with a path and commit')
    path, commit = authority['path'], authority['commit']

    def git(*args):
        done = subprocess.run(['git', '--no-optional-locks', '-C', repo, *args], capture_output=True, timeout=30,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if done.returncode:
            raise ValueError('git cannot read %s at %s' % (path, commit[:12]))
        return done.stdout
    found = []
    for item in git('ls-tree', '-z', '--full-tree', commit, '--', path).split(b'\0'):
        meta, _, name = item.partition(b'\t')
        if item and name.decode('utf-8', 'replace') == path and len(meta.split()) == 3:
            found.append(meta.split())
    if len(found) != 1:
        raise ValueError('commit %s holds no file %s' % (commit[:12], path))
    mode, kind, obj = (part.decode('ascii', 'replace') for part in found[0])
    if kind != 'blob' or mode not in ('100644', '100755'):
        raise ValueError('%s at %s is not a regular file (git mode %s, %s)' % (path, commit[:12], mode, kind))
    data = git('cat-file', 'blob', obj)
    if len(data) > DESIGN_DOCUMENT_MAX_BYTES:
        raise ValueError('the design document is %d bytes; the limit is 1 MiB' % len(data))
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('%s at %s is not UTF-8 text' % (path, commit[:12])) from None
    sha256 = hashlib.sha256(data).hexdigest()
    if authority.get('sha256') not in (None, sha256):
        raise ValueError('authority.sha256 is not the sha256 of %s at %s (%s)' % (path, commit[:12], sha256))
    return dict(entry, authority=dict(authority, sha256=sha256)), text


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

    @staticmethod
    def _decode_body(raw):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode('utf-8', 'replace')

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
        data = self._decode_body(raw)
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

    def decision_attestation(self, project, decision):
        """The exact text of one decision, its digest and how it reads now (kittrial-5bb.454 r2)."""
        return self.request('GET','/v1/projects/%s/decisions/%s/attestation' %
                            (quote(project,safe=''),quote(decision,safe='')))

    def attest_decision(self, project, decision, digest, key=None):
        """Attest the exact text you read, named by its digest: a text that changed since is refused with 409."""
        identity=key or new_idempotency_key()
        try:
            return self.request('POST','/v1/projects/%s/decisions/%s/attest' %
                                (quote(project,safe=''),quote(decision,safe='')),{'digest':digest},key=identity)
        except OSError:
            raise UncertainOutcome(0,'uncertain','Connection lost; reconcile the identical attestation',
                                   None,None,identity) from None

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

    def integration_check(self, project, task, target=None):
        """The read-only merge check and its step ledger (kittrial-5bb.383)."""
        path = '/v1/projects/%s/tasks/%s/integration-check' % (
            quote(project, safe=''), quote(task, safe=''))
        if target is not None:
            path += '?' + urlencode({'target': target})
        return self.request('GET', path)

    def ledger(self, project, task, target=None):
        """The writes still owed, from the same read-only integration check.

        Returns the task, target, `merge_ready` and the `next` rows only: a
        coordinator reads the check before merging and this ledger to resume from the
        first row that is not `committed`.
        """
        state = self.integration_check(project, task, target)
        return {'task': state.get('task'), 'target': state.get('target'),
                'merge_ready': state.get('merge_ready'),
                'next': state.get('next') or []}

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

    # -- the project's own requirements (kittrial-5bb.278; WEB_COORDINATOR_DESIGN slice 10)
    #
    # An agent holding the project's coordinator grant does everything its owner does to
    # requirements through these: create, revise, accept, withdraw, supersede and batch
    # accept, and read the BRD. The accepting party stays the agent's own human account;
    # the agent and its grant are recorded in the service audit. Every write takes an
    # ``expected_revision`` + ``expected_sha256`` pair from the read that produced it.
    def list_requirements(self, project):
        return self.request('GET', '/v1/projects/%s/requirements' % quote(project, safe=''))

    def get_requirement(self, project, requirement):
        return self.request('GET', '/v1/projects/%s/requirements/%s'
                            % (quote(project, safe=''), quote(requirement, safe='')))

    def brd(self, project):
        """The generated BRD: the project's narrative sections and accepted requirements."""
        return self.request('GET', '/v1/projects/%s/brd' % quote(project, safe=''))

    def create_requirement(self, project, body, key=None):
        return self.request('POST', '/v1/projects/%s/requirements' % quote(project, safe=''),
                            body, key=key)

    def revise_requirement(self, project, requirement, body, key=None):
        return self.request('PATCH', '/v1/projects/%s/requirements/%s'
                            % (quote(project, safe=''), quote(requirement, safe='')), body, key=key)

    def accept_requirement(self, project, requirement, body, key=None):
        return self.request('POST', '/v1/projects/%s/requirements/%s/accept'
                            % (quote(project, safe=''), quote(requirement, safe='')), body, key=key)

    def withdraw_requirement(self, project, requirement, body, key=None):
        return self.request('POST', '/v1/projects/%s/requirements/%s/withdraw'
                            % (quote(project, safe=''), quote(requirement, safe='')), body, key=key)

    def supersede_requirement(self, project, requirement, body, key=None):
        return self.request('POST', '/v1/projects/%s/requirements/%s/supersede'
                            % (quote(project, safe=''), quote(requirement, safe='')), body, key=key)

    def accept_requirements(self, project, items, stop_on_error=False, key=None):
        """Accept many requirements in one action (the plan's ``accept_batch``).

        ``items`` is a list of ``{'id', 'expected_revision', 'expected_sha256'}``. The
        answer lists ``accepted`` and ``refused`` (a reason each) plus a ``summary``; one
        refused item changes nothing and does not roll back the rest. ``key`` matters: an
        exact replay accepts nothing twice.
        """
        return self.request('POST', '/v1/projects/%s/requirements/accept-batch'
                            % quote(project, safe=''),
                            {'items': items, 'stop_on_error': bool(stop_on_error)}, key=key)

    def requirements_recoveries(self, project):
        """Failed creations this caller may clear; editing is a coordinator's, so is this."""
        return self.request('GET', '/v1/projects/%s/requirements/recoveries'
                            % quote(project, safe=''))

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

    def task_summaries(self, project, tasks):
        """One bounded read of checkpoints and latest ordinary posts."""
        return self.request('POST', '/v1/projects/%s/tasks/summary' % quote(project, safe=''),
                            {'tasks': tasks})

    def get_task(self, project, task):
        return self.request('GET', '/v1/projects/%s/tasks/%s'
                            % (quote(project, safe=''), quote(task, safe='')))

    def claim_task(self, project, task, actor=None, key=None):
        return self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                            % (quote(project, safe=''), quote(task, safe='')),
                            {'actor': actor}, key=key)

    def release_task(self, project, task, reason, key=None):
        """Release the caller's own claimed, undelivered task, with a short reason.

        The holder is the authenticated caller, so the client never sends an actor:
        the server refuses a task the caller does not hold, a contribution awaiting
        review or changes requested, and the merge slot (kittrial-5bb.301). A
        checkpoint and its open items stay on the task, and the claim stops counting
        against the caller's ``claim_limit`` at once.
        """
        return self.request('POST', '/v1/projects/%s/tasks/%s/release'
                            % (quote(project, safe=''), quote(task, safe='')),
                            {'reason': reason}, key=key)

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

    def delivery(self, project, task, *, contribution=None):
        qs = ('?' + urlencode({'contribution': contribution})) if contribution is not None else ''
        return self.request('GET', '/v1/projects/%s/tasks/%s/delivery%s'
                            % (quote(project, safe=''), quote(task, safe=''), qs))

    def delivery_bundle(self, project, task, out_path, *, contribution=None, max_bytes=256 * 1024 * 1024):
        out = str(out_path)
        if os.path.islink(out):
            raise ValueError('Refusing to write delivered bundle through symlink: %s' % out)
        if os.path.isdir(out):
            raise ValueError('Refusing to overwrite directory with delivered bundle: %s' % out)
        parent = os.path.dirname(os.path.abspath(out)) or '.'
        if os.path.islink(parent) or not os.path.isdir(parent):
            raise ValueError('Output parent directory does not exist: %s' % parent)
        meta = self.delivery(project, task, contribution=contribution)
        if not isinstance(meta, dict):
            raise ValueError('Invalid delivery metadata response')
        delivery_block = meta.get('delivery') if isinstance(meta.get('delivery'), dict) else {}
        expected_sha = meta.get('sha256') or delivery_block.get('sha256')
        expected_bytes = meta.get('bytes')
        serve_limit = meta.get('serve_limit')
        limit = int(max_bytes)
        if isinstance(serve_limit, int) and serve_limit > 0:
            limit = min(limit, serve_limit)
        qs = ('?' + urlencode({'contribution': contribution})) if contribution is not None else ''
        path = '/v1/projects/%s/tasks/%s/delivery/bundle%s' % (
            quote(project, safe=''),
            quote(task, safe=''),
            qs,
        )
        headers = {}
        if self.use_cookie and self.cookie:
            headers['Cookie'] = self.cookie
        elif self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if self.request_id:
            headers['X-Request-Id'] = self.request_id
        connection = self._connection()
        tmp_path = None
        try:
            connection.request('GET', self.prefix + path, headers=headers)
            response = connection.getresponse()
            if response.status >= 400:
                raw = response.read()
                data = self._decode_body(raw)
                error = data.get('error', {}) if isinstance(data, dict) else {}
                request_id = data.get('request_id') if isinstance(data, dict) else None
                raise HttpApiError(
                    response.status,
                    error.get('code', 'http_error'),
                    error.get('message', 'Request failed'),
                    error.get('detail'),
                    request_id,
                )
            if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                raise ValueError('The delivered bundle does not match its recorded checksum.')
            expected_sha_lower = expected_sha.lower()
            etag = (response.getheader('ETag') or '').strip().strip('"').lower()
            header_sha = (response.getheader('X-Orchestra-Bundle-Sha256') or etag).strip().lower()
            if not etag or etag != expected_sha_lower or header_sha != expected_sha_lower:
                raise ValueError('The delivered bundle does not match its recorded checksum.')
            content_length_raw = response.getheader('Content-Length')
            expected_length = None
            if content_length_raw is not None:
                try:
                    expected_length = int(content_length_raw)
                except ValueError:
                    raise ValueError('The delivered bundle does not match its recorded checksum.') from None
                if expected_length < 0 or expected_length > limit:
                    raise ValueError('The delivered bundle exceeds the %d-byte serve limit.' % limit)
                if isinstance(expected_bytes, int) and expected_length != expected_bytes:
                    raise ValueError('The delivered bundle does not match its recorded checksum.')
            fd, tmp_path = tempfile.mkstemp(prefix='.delivery-', suffix='.bundle.tmp', dir=parent)
            digest = hashlib.sha256()
            total = 0
            try:
                with os.fdopen(fd, 'wb') as tmp_file:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise ValueError('The delivered bundle exceeds the %d-byte serve limit.' % limit)
                        digest.update(chunk)
                        tmp_file.write(chunk)
                    tmp_file.flush()
            except BaseException:
                raise
            if expected_length is not None and total != expected_length:
                raise ValueError('The delivered bundle does not match its recorded checksum.')
            if isinstance(expected_bytes, int) and total != expected_bytes:
                raise ValueError('The delivered bundle does not match its recorded checksum.')
            actual_sha = digest.hexdigest().lower()
            if actual_sha != expected_sha_lower:
                raise ValueError('The delivered bundle does not match its recorded checksum.')
            if os.path.islink(out) or os.path.isdir(out):
                raise ValueError('Refusing to overwrite symlink or directory: %s' % out)
            os.replace(tmp_path, out)
            tmp_path = None
            return {
                'task': meta.get('task') or task,
                'project': meta.get('project') or project,
                'contribution': meta.get('contribution'),
                'revision': meta.get('revision'),
                'kind': meta.get('kind') or 'bundle',
                'commit': meta.get('commit'),
                'base_commit': meta.get('base_commit'),
                'sha256': actual_sha,
                'bytes': total,
                'path': meta.get('path') or delivery_block.get('path'),
                'out': out,
                'verified': True,
            }
        finally:
            connection.close()
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

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

    # -- reference and capability records -------------------------------------
    def _write_record(self, method, path, body, key=None):
        identity = key or new_idempotency_key()
        try:
            return self.request(method, path, body, key=identity)
        except OSError:
            raise UncertainOutcome(0, 'uncertain', 'Connection lost; reconcile the identical record request',
                                   None, None, identity) from None

    def list_references(self, project, *, tag=None, owner=None, state=None, due=None,
                        authority=None, waiting=False, limit=None, cursor=None):
        params = {}
        if tag:
            params['tag'] = tag if isinstance(tag, str) else ','.join(tag)
        for name, val in (('owner', owner), ('state', state), ('due', due), ('authority', authority)):
            if val:
                params[name] = val
        if waiting:
            params['waiting'] = '1'
        if limit is not None:
            params['limit'] = str(limit)
        if cursor is not None:
            params['cursor'] = cursor
        qs = ('?' + urlencode(params)) if params else ''
        return self.request('GET', '/v1/projects/%s/references%s' % (quote(project, safe=''), qs))

    def find_references(self, project, q, limit=5):
        qs = '?' + urlencode({'q': q, 'limit': str(limit)})
        return self.request('GET', '/v1/projects/%s/references/find%s' % (quote(project, safe=''), qs))

    def get_reference(self, project, ref_key):
        return self.request('GET', '/v1/projects/%s/references/%s'
                            % (quote(project, safe=''), quote(ref_key, safe='')))

    def propose_reference(self, project, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/references' % quote(project, safe=''), body, key)

    def revise_reference(self, project, ref_key, body, key=None):
        return self._write_record('PATCH', '/v1/projects/%s/references/%s'
                                  % (quote(project, safe=''), quote(ref_key, safe='')), body, key)

    def accept_reference(self, project, ref_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/references/%s/accept'
                                  % (quote(project, safe=''), quote(ref_key, safe='')), body, key)

    def retire_reference(self, project, ref_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/references/%s/retire'
                                  % (quote(project, safe=''), quote(ref_key, safe='')), body, key)

    def list_capabilities(self, project, *, tag=None, owner=None, state=None,
                          pointers=False, waiting=False, limit=None, cursor=None):
        params = {}
        if tag:
            params['tag'] = ','.join(tag) if isinstance(tag, (list, tuple)) else str(tag)
        for name, val in (('owner', owner), ('state', state)):
            if val:
                params[name] = val
        if pointers:
            params['pointers'] = '1'
        if waiting:
            params['waiting'] = '1'
        if limit is not None:
            params['limit'] = str(limit)
        if cursor is not None:
            params['cursor'] = cursor
        qs = ('?' + urlencode(params)) if params else ''
        return self.request('GET', '/v1/projects/%s/capabilities%s' % (quote(project, safe=''), qs))

    def find_capabilities(self, project, q, limit=5):
        qs = '?' + urlencode({'q': q, 'limit': str(limit)})
        return self.request('GET', '/v1/projects/%s/capabilities/find%s' % (quote(project, safe=''), qs))

    def list_interfaces(self,project,*,limit=None,cursor=None):
        query={k:v for k,v in {'limit':limit,'cursor':cursor}.items() if v is not None}
        return self.request('GET','/v1/projects/%s/interfaces%s'%(quote(project,safe=''),('?' + urlencode(query)) if query else ''))

    def get_interface(self,project,interface_key):
        return self.request('GET','/v1/projects/%s/interfaces/%s'%(quote(project,safe=''),quote(interface_key,safe='')))

    def propose_interface(self,project,body,key=None):
        return self._write_record('POST','/v1/projects/%s/interfaces'%quote(project,safe=''),body,key)

    def revise_interface(self,project,interface_key,body,key=None):
        return self._write_record('PATCH','/v1/projects/%s/interfaces/%s'%(quote(project,safe=''),quote(interface_key,safe='')),body,key)

    def accept_interface(self,project,interface_key,body,key=None):
        return self._write_record('POST','/v1/projects/%s/interfaces/%s/accept'%(quote(project,safe=''),quote(interface_key,safe='')),body,key)

    def get_capability(self, project, cap_key):
        return self.request('GET', '/v1/projects/%s/capabilities/%s'
                            % (quote(project, safe=''), quote(cap_key, safe='')))

    def propose_capability(self, project, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/capabilities' % quote(project, safe=''), body, key)

    def revise_capability(self, project, cap_key, body, key=None):
        return self._write_record('PATCH', '/v1/projects/%s/capabilities/%s'
                                  % (quote(project, safe=''), quote(cap_key, safe='')), body, key)

    def accept_capability(self, project, cap_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/capabilities/%s/accept'
                                  % (quote(project, safe=''), quote(cap_key, safe='')), body, key)

    def retire_capability(self, project, cap_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/capabilities/%s/retire'
                                  % (quote(project, safe=''), quote(cap_key, safe='')), body, key)

    def verify_capability(self, project, cap_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/capabilities/%s/verify'
                                  % (quote(project, safe=''), quote(cap_key, safe='')), body, key)

    def propose_capability_alias(self, project, cap_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/capabilities/%s/aliases'
                                  % (quote(project, safe=''), quote(cap_key, safe='')), body, key)

    def reject_capability_alias(self, project, cap_key, body, key=None):
        return self._write_record('POST', '/v1/projects/%s/capabilities/%s/aliases/reject'
                                  % (quote(project, safe=''), quote(cap_key, safe='')), body, key)


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
    work_read = sub.add_parser('work', help='bounded task checkpoint/post summaries')
    work_read.add_argument('project')
    work_read.add_argument('operation', choices=('summary',))
    work_read.add_argument('tasks', nargs='+')
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
    attestation = sub.add_parser('decision-attestation', help="a decision's exact text, its digest and how it reads now")
    attestation.add_argument('project')
    attestation.add_argument('decision')
    attest = sub.add_parser('decision-attest', help="attest a decision's exact text, as your own session or agent")
    attest.add_argument('project')
    attest.add_argument('decision')
    attest.add_argument('--digest', required=True, help='what decision-attestation gave for the text you read')
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
    release = sub.add_parser('release-task', help='release your own claimed, undelivered task with a reason')
    release.add_argument('project')
    release.add_argument('task')
    release.add_argument('--reason', required=True)
    release.add_argument('--idempotency-key', help='reuse exactly this key and request after an uncertain result')
    life = sub.add_parser('lifecycle', help='read or record task scope and lifecycle facts')
    life.add_argument('project');life.add_argument('task')
    life.add_argument('operation', choices=('read','scope','fact'))
    life.add_argument('--dimension', choices=('implemented','tested','reviewed','integrated','deployed','live-verified'))
    life.add_argument('--file', help='UTF-8 JSON HTTP request body for scope or fact')
    life.add_argument('--idempotency-key', help='reuse exactly this key and body after an uncertain result')
    check = sub.add_parser('integration-check',
                           help='read the merge check: checks, unchecked rows and the step ledger')
    check.add_argument('project');check.add_argument('task')
    check.add_argument('--target', help='target branch to compare with the held merge slot')
    ledger = sub.add_parser('ledger',
                            help='read only the step ledger: the writes still owed, in order')
    ledger.add_argument('project');ledger.add_argument('task')
    ledger.add_argument('--target', help='target branch to compare with the held merge slot')
    ref = sub.add_parser('ref', help='read or mutate project reference records')
    ref.add_argument('project')
    ref.add_argument('operation', choices=('list', 'find', 'get', 'propose', 'revise', 'accept', 'retire'))
    ref.add_argument('--key', dest='record_key', help='reference key for get/revise/accept/retire')
    ref.add_argument('--q', help='search phrase for find')
    ref.add_argument('--tag', action='append', default=[], help='filter by tag (repeatable)')
    ref.add_argument('--owner')
    ref.add_argument('--state')
    ref.add_argument('--due')
    ref.add_argument('--authority')
    ref.add_argument('--waiting', action='store_true', help='only records waiting to be accepted')
    ref.add_argument('--limit', type=int)
    ref.add_argument('--cursor')
    ref.add_argument('--file', help='UTF-8 JSON HTTP request body for mutations')
    ref.add_argument('--document-from-git', action='store_true',
                     help='propose/revise of a design.<scope> entry: attach the document its repo-document '
                          'authority names, read from git at the pinned commit')
    ref.add_argument('--repo', default='.', help='the checkout --document-from-git reads (default: .)')
    ref.add_argument('--idempotency-key', help='reuse exactly this key and body after an uncertain result')
    cap = sub.add_parser('capability', help='read or mutate project capability records')
    cap.add_argument('project')
    cap.add_argument('operation', choices=(
        'list', 'find', 'get', 'propose', 'revise', 'accept', 'retire', 'verify',
        'propose-alias', 'reject-alias'))
    cap.add_argument('--key', dest='record_key', help='capability key')
    cap.add_argument('--q', help='search phrase for find')
    cap.add_argument('--tag', action='append', default=[], help='filter by tag (repeatable)')
    cap.add_argument('--owner')
    cap.add_argument('--state')
    cap.add_argument('--pointers', action='store_true', help='include pointers in list output')
    cap.add_argument('--waiting', action='store_true', help='only records waiting to be accepted')
    cap.add_argument('--limit', type=int)
    cap.add_argument('--cursor')
    cap.add_argument('--file', help='UTF-8 JSON HTTP request body for mutations')
    cap.add_argument('--idempotency-key', help='reuse exactly this key and body after an uncertain result')
    dlv = sub.add_parser('delivery', help='read delivery metadata or fetch and verify a delivered bundle')
    dlv.add_argument('project')
    dlv.add_argument('task')
    dlv.add_argument('--contribution', help='contribution record id (default: current contribution)')
    dlv.add_argument('--verify', action='store_true', help='verify bundle digest when fetching')
    dlv.add_argument('--out', help='write verified bundle bytes to this path')
    dlv.add_argument('--credential',
                     help='use this worker credential for the call; pass it as '
                          '--credential=VALUE so a secret beginning with "-" is not read '
                          'as an option')
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
    if getattr(args, 'credential', None):
        client.use_credential(args.credential)
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
        if args.command == 'work':
            print(json.dumps(client.task_summaries(args.project, args.tasks), indent=2))
            return 0
        if args.command == 'my-claim-limit':
            print(json.dumps(client.set_my_claim_limit(args.claim_limit,
                                                       key=args.idempotency_key), indent=2))
            return 0
        if args.command == 'assignment-roster':
            print(json.dumps(client.assignment_roster(args.project,args.limit,args.cursor),indent=2))
            return 0
        if args.command == 'decision-attestation':
            print(json.dumps(client.decision_attestation(args.project,args.decision),indent=2))
            return 0
        if args.command == 'decision-attest':
            print(json.dumps(client.attest_decision(args.project,args.decision,args.digest,key=args.idempotency_key),indent=2))
            return 0
        if args.command == 'assign-task':
            result=client.assign_task(args.project,args.task,args.expected_assignee,args.assignee,args.reason,
                allow_pending_review=args.allow_pending_review,allow_held_slot=args.allow_held_slot,
                key=args.idempotency_key)
            print(json.dumps(result,indent=2))
            return 0
        if args.command == 'release-task':
            result=client.release_task(args.project,args.task,args.reason,key=args.idempotency_key)
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
        if args.command == 'integration-check':
            print(json.dumps(client.integration_check(args.project, args.task, args.target), indent=2))
            return 0
        if args.command == 'ledger':
            print(json.dumps(client.ledger(args.project, args.task, args.target), indent=2))
            return 0
        if args.command == 'ref':
            op = args.operation
            if op == 'list':
                result = client.list_references(
                    args.project, tag=args.tag, owner=args.owner, state=args.state,
                    due=args.due, authority=args.authority, waiting=args.waiting,
                    limit=args.limit, cursor=args.cursor)
            elif op == 'find':
                if not args.q:
                    parser.error('find requires --q')
                result = client.find_references(args.project, args.q, args.limit or 5)
            elif op == 'get':
                if not args.record_key:
                    parser.error('get requires --key')
                result = client.get_reference(args.project, args.record_key)
            else:
                if not args.file:
                    parser.error('%s requires --file' % op)
                body = _lifecycle_body_file(args.file)
                if args.document_from_git:
                    if op not in ('propose', 'revise'):
                        parser.error('--document-from-git goes with propose or revise')
                    body, document = _document_from_git(body, args.repo)
                    body['document'] = document
                if op == 'propose':
                    result = client.propose_reference(args.project, body, args.idempotency_key)
                else:
                    rk = args.record_key or body.pop('key', None)
                    if not rk:
                        parser.error('%s requires --key or key in body' % op)
                    fn = getattr(client, op + '_reference')
                    result = fn(args.project, rk, body, args.idempotency_key)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'capability':
            op = args.operation
            if op == 'list':
                result = client.list_capabilities(
                    args.project, tag=args.tag, owner=args.owner, state=args.state,
                    pointers=args.pointers, waiting=args.waiting,
                    limit=args.limit, cursor=args.cursor)
            elif op == 'find':
                if not args.q:
                    parser.error('find requires --q')
                result = client.find_capabilities(args.project, args.q, args.limit or 5)
            elif op == 'get':
                if not args.record_key:
                    parser.error('get requires --key')
                result = client.get_capability(args.project, args.record_key)
            else:
                if not args.file:
                    parser.error('%s requires --file' % op)
                body = _lifecycle_body_file(args.file)
                if op == 'propose':
                    result = client.propose_capability(args.project, body, args.idempotency_key)
                else:
                    rk = args.record_key or body.pop('key', None)
                    if not rk:
                        parser.error('%s requires --key or key in body' % op)
                    method_map = {
                        'revise': client.revise_capability,
                        'accept': client.accept_capability,
                        'retire': client.retire_capability,
                        'verify': client.verify_capability,
                        'propose-alias': client.propose_capability_alias,
                        'reject-alias': client.reject_capability_alias,
                    }
                    result = method_map[op](args.project, rk, body, args.idempotency_key)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'delivery':
            if args.out:
                result = client.delivery_bundle(args.project, args.task, args.out,
                                                contribution=args.contribution)
            else:
                if args.verify:
                    parser.error('delivery --verify requires --out FILE')
                result = client.delivery(args.project, args.task,
                                         contribution=args.contribution)
            print(json.dumps(result, indent=2))
            return 0
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
