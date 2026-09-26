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
import argparse
import http.client
import json
import os
import secrets
import ssl
import sys
from urllib.parse import quote, urlencode, urlsplit

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
        text = '%s (%s)' % (self.message, self.code)
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

    def archive_project(self, project, key=None):
        return self.request('POST', '/v1/projects/%s/archive' % quote(project, safe=''), key=key)

    def set_member(self, project, user_id, role, key=None):
        return self.request('PUT', '/v1/projects/%s/members/%s'
                            % (quote(project, safe=''), quote(user_id, safe='')),
                            {'role': role}, key=key)

    def remove_member(self, project, user_id, key=None):
        return self.request('DELETE', '/v1/projects/%s/members/%s'
                            % (quote(project, safe=''), quote(user_id, safe='')), key=key)

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

    def add_checkpoint(self, project, task, *, previous, summary, open_items=None,
                       activity_cursor=None, actor=None, key=None, schema_version=1,
                       source_commit='', branch='', intent=None, acceptance=None,
                       next_action=None, resolved=None):
        """Append one canonical checkpoint; ``activity_cursor`` comes from history/brief."""
        body = {'schema_version': schema_version, 'previous': previous,
                'activity_cursor': activity_cursor, 'source_commit': source_commit,
                'branch': branch, 'intent': intent, 'acceptance': acceptance,
                'summary': summary, 'next_action': next_action,
                'open_items': open_items or [], 'resolved': resolved or [], 'actor': actor}
        return self.request('POST', '/v1/projects/%s/tasks/%s/checkpoints'
                            % (quote(project, safe=''), quote(task, safe='')),
                            body, key=key)

    def add_review(self, project, task, operation, *, operation_id=None, previous=None,
                   schema_version=1, commit=None, base_commit=None, bundle_sha256=None,
                   summary=None, actor=None, key=None, repository=None, delivery=None,
                   supersedes=None, contribution=None, items=None, resolutions=None):
        """Record one contribution/review operation.

        The canonical backend forwards exactly the fields the operation needs, so the
        caller supplies the canonical payload (``previous``/``operation_id`` and the
        operation-specific evidence) rather than a fixed union with nulls.
        """
        body = {'schema_version': schema_version, 'operation': operation,
                'operation_id': operation_id, 'previous': previous, 'actor': actor,
                'commit': commit, 'base_commit': base_commit, 'bundle_sha256': bundle_sha256,
                'summary': summary, 'repository': repository, 'delivery': delivery,
                'supersedes': supersedes, 'contribution': contribution,
                'items': items, 'resolutions': resolutions}
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
    parser.add_argument('--token', help='bearer session or worker credential')
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
    call = sub.add_parser('call', help='issue one raw JSON request')
    call.add_argument('method')
    call.add_argument('path')
    call.add_argument('--body', help='JSON request body')
    call.add_argument('--idempotency-key', help='reuse a key to reconcile an uncertain write')
    call.add_argument('--credential', help='use this worker credential for the call')
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
