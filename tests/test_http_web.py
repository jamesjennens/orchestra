"""Contract tests for the web interface slice (kittrial-5bb.20 slice 1).

Two surfaces, both over a real loopback ``ThreadingHTTPServer`` like
``test_http_service``:

* the anonymous static route that serves the browser interface from ``web/``:
  allowlist, security headers, HEAD, traversal and encoded traversal, excluded
  development files and symlink escapes;
* the read routes the interface needs (members, worker-credential metadata, task
  brief, review queue, my work and exact account lookup), each checked for the
  authorization rule of its neighbours and uniform not-found answers.
"""
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_service import (CONTENT_SECURITY_POLICY, DEFAULT_WEB_ROOT, InProcessBackend,
                          create_server, static_file)
from test_http_review_fixes import CONTRIBUTION, EndpointCase
from test_http_service import (BASE, BUNDLE, COMMIT, Response, ServerHarness, unique_dir)

WEB = DEFAULT_WEB_ROOT


class StaticHarness(ServerHarness):
    def raw(self, method, path, headers=None):
        """Send ``path`` byte-for-byte, so encoded and dot segments reach the server."""
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        try:
            client.putrequest(method, path, skip_accept_encoding=True)
            for key, value in (headers or {}).items():
                client.putheader(key, value)
            client.endheaders()
            response = client.getresponse()
            return Response(response.status, response.getheaders(), response.read())
        finally:
            client.close()


class StaticServingCase(StaticHarness):
    def assert_page_headers(self, response):
        self.assertEqual(CONTENT_SECURITY_POLICY, response.headers.get('content-security-policy'))
        self.assertEqual('nosniff', response.headers.get('x-content-type-options'))
        self.assertEqual('no-referrer', response.headers.get('referrer-policy'))
        self.assertEqual('no-cache', response.headers.get('cache-control'))
        self.assertTrue(response.headers.get('etag'))

    def test_index_is_served_anonymously_with_security_headers(self):
        response = self.raw('GET', '/')
        self.assertEqual(200, response.status)
        self.assertEqual('text/html; charset=utf-8', response.headers.get('content-type'))
        self.assertEqual((WEB / 'index.html').read_bytes(), response.body)
        self.assert_page_headers(response)
        self.assertNotIn('set-cookie', response.headers)
        same = self.raw('GET', '/index.html')
        self.assertEqual(response.body, same.body)

    def test_csp_is_the_strict_policy(self):
        policy = self.raw('GET', '/').headers['content-security-policy']
        for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'",
                          "frame-ancestors 'none'", "base-uri 'none'", "connect-src 'self'"):
            self.assertIn(directive, policy)
        self.assertNotIn('unsafe-inline', policy)
        self.assertNotIn('unsafe-eval', policy)

    def test_scripts_styles_and_views_have_exact_media_types(self):
        cases = {'/js/main.js': 'text/javascript; charset=utf-8',
                 '/js/api.js': 'text/javascript; charset=utf-8',
                 '/js/views/task.js': 'text/javascript; charset=utf-8',
                 '/css/app.css': 'text/css; charset=utf-8'}
        for path, media in cases.items():
            response = self.raw('GET', path)
            self.assertEqual(200, response.status, path)
            self.assertEqual(media, response.headers.get('content-type'), path)
            self.assertEqual((WEB / path[1:]).read_bytes(), response.body, path)
            self.assert_page_headers(response)

    def test_head_returns_headers_without_a_body(self):
        get = self.raw('GET', '/js/app.js')
        head = self.raw('HEAD', '/js/app.js')
        self.assertEqual(200, head.status)
        self.assertEqual(b'', head.body)
        self.assertEqual(get.headers['content-length'], head.headers['content-length'])
        self.assertEqual(get.headers['etag'], head.headers['etag'])
        self.assertEqual(404, self.raw('HEAD', '/prototype.html').status)

    def test_conditional_get_revalidates(self):
        etag = self.raw('GET', '/js/app.js').headers['etag']
        again = self.raw('GET', '/js/app.js', headers={'If-None-Match': etag})
        self.assertEqual(304, again.status)
        self.assertEqual(b'', again.body)

    def test_development_files_are_never_served(self):
        for path in ('/prototype.html', '/js/prototype.js', '/js/mock.js', '/data/harness.json',
                     '/data/', '/data', '/js/Mock.js', '/JS/mock.js', '/Prototype.html'):
            response = self.raw('GET', path)
            self.assertEqual(404, response.status, path)
            self.assertEqual('not_found', response.data['error']['code'], path)

    def test_no_directory_listing(self):
        # (``//`` is normalized to ``/`` by the standard library handler, so it is the
        # entry page, not a listing.)
        for path in ('/js', '/js/', '/js/views/', '/css/', '/web/', '/js//app.js'):
            self.assertEqual(404, self.raw('GET', path).status, path)

    def test_traversal_and_encoded_traversal_are_refused(self):
        for path in ('/../http_service.py', '/js/../../http_service.py', '/js/..%2f..%2fREADME.md',
                     '/%2e%2e/http_service.py', '/js/%2e%2e/index.html', '/js/%252e%252e/x.js',
                     '/js%2fapp.js', '/js/app.js%00.html', '/js\\app.js', '/./index.html',
                     '/js/./app.js', '/.git/config', '/js/.hidden.js', '/index.html/'):
            response = self.raw('GET', path)
            self.assertEqual(404, response.status, path)
            self.assertNotIn(b'import', response.body, path)

    def test_unknown_extensions_and_unlisted_files_are_404(self):
        for path in ('/README.md', '/http_service.py', '/js/app.js.map', '/js/app.mjs',
                     '/css/app.css.bak', '/index.htm', '/AGENTS.md', '/js/views/nope.js'):
            self.assertEqual(404, self.raw('GET', path).status, path)

    def test_only_get_and_head_are_static(self):
        response = self.request('POST', '/index.html', {'x': 1})
        self.assertEqual(404, response.status)
        self.assertEqual(404, self.request('DELETE', '/js/app.js').status)

    def test_api_routes_are_unchanged(self):
        response = self.raw('GET', '/healthz')
        self.assertEqual(200, response.status)
        self.assertEqual('no-store', response.headers.get('cache-control'))
        self.assertNotIn('content-security-policy', response.headers)
        self.assertEqual(401, self.raw('GET', '/v1/projects').status)
        self.assertEqual(404, self.raw('GET', '/v1/index.html').status)

    def test_ui_needs_no_inline_script_or_style(self):
        # The strict CSP forbids inline code; the shipped entry page must not rely on it.
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>', html)
        self.assertNotIn(' style=', html)
        self.assertNotIn(' onclick=', html)
        for script in (WEB / 'js').rglob('*.js'):
            text = script.read_text(encoding='utf-8')
            self.assertNotIn("setAttribute('style'", text, script)
            self.assertIsNone(re.search(r'\.innerHTML\s*=|insertAdjacentHTML|eval\(|new Function',
                                        text), script)


class StaticConfigurationCase(StaticHarness):
    def setUp(self):
        super().setUp()
        self.web = unique_dir('web-')
        self.addCleanup(shutil.rmtree, self.web, ignore_errors=True)
        (self.web / 'js').mkdir()
        (self.web / 'index.html').write_text('<!doctype html><title>relocated</title>',
                                             encoding='utf-8')
        (self.web / 'js' / 'main.js').write_text('export {};', encoding='utf-8')
        (self.web / 'js' / 'mock.js').write_text('secret sample', encoding='utf-8')
        self.outside = unique_dir('outside-')
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        (self.outside / 'leak.js').write_text('outside the root', encoding='utf-8')

    def serve(self, web_root):
        httpd = create_server(self.service, InProcessBackend(self.service), host='127.0.0.1',
                              port=0, web_root=web_root)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        self.port = httpd.server_address[1]

    def test_relocated_web_root(self):
        self.serve(self.web)
        self.assertIn(b'relocated', self.raw('GET', '/').body)
        self.assertEqual(404, self.raw('GET', '/js/mock.js').status)

    def test_web_serving_can_be_disabled(self):
        self.serve(None)
        self.assertEqual(404, self.raw('GET', '/').status)
        self.assertEqual(404, self.raw('GET', '/js/main.js').status)
        self.assertEqual(200, self.raw('GET', '/healthz').status)

    def _symlink(self, link, target):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available to this user on this platform')

    def test_symlink_outside_the_root_is_refused(self):
        self._symlink(self.web / 'js' / 'leak.js', self.outside / 'leak.js')
        self.serve(self.web)
        self.assertEqual(404, self.raw('GET', '/js/leak.js').status)
        self.assertIsNone(static_file(self.web, '/js/leak.js'))

    def test_symlink_to_an_excluded_file_is_refused(self):
        self._symlink(self.web / 'js' / 'alias.js', self.web / 'js' / 'mock.js')
        self.serve(self.web)
        self.assertEqual(404, self.raw('GET', '/js/alias.js').status)

    def test_cli_options(self):
        from http_service import main
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(['--state', str(self.tmp_path / 's.json'), '--no-web', '--web-root', 'x'])


# ---------------------------------------------------------------- read routes
def without_request_id(body):
    return {key: value for key, value in (body or {}).items() if key != 'request_id'}


class TeamHarness(ServerHarness):
    """An owner, a contributor, a viewer, an outsider and a project with one task."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.ids = {}
        for name in ('olive', 'carl', 'vera', 'otto'):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
        self.olive = self.login('olive', 'olive-password-1')[0]
        self.carl = self.login('carl', 'carl-password-1')[0]
        self.vera = self.login('vera', 'vera-password-1')[0]
        self.otto = self.login('otto', 'otto-password-1')[0]
        self.project = self.create_project(self.olive, 'Alpha')
        self.other = self.create_project(self.otto, 'Elsewhere')
        for name, role in (('carl', 'contributor'), ('vera', 'viewer')):
            response = self.request('PUT', '/v1/projects/%s/members/%s'
                                    % (self.project, self.ids[name]), {'role': role},
                                    token=self.olive)
            self.assertEqual(200, response.status, response.data)
        self.task = self.create_task(self.olive, self.project, 'Paginate history').data['id']

    def path(self, suffix='', project=None):
        return '/v1/projects/%s%s' % (project or self.project, suffix)

    def claim(self, token, task=None):
        response = self.request('POST', self.path('/tasks/%s/claim' % (task or self.task)), {},
                                token=token)
        self.assertEqual(200, response.status, response.data)
        return response.data

    def contribute(self, token, task=None, commit=COMMIT, **extra):
        body = {'operation': 'contribute', 'commit': commit, 'base_commit': BASE,
                'bundle_sha256': BUNDLE, 'summary': 'delivered'}
        body.update(extra)
        return self.request('POST', self.path('/tasks/%s/reviews' % (task or self.task)), body,
                            token=token)

    def review(self, token, operation, task=None, **extra):
        body = {'operation': operation}
        body.update(extra)
        return self.request('POST', self.path('/tasks/%s/reviews' % (task or self.task)), body,
                            token=token)

    def assert_uniform_not_found(self, suffix, token=None):
        """A non-member sees exactly what a caller asking for a missing project sees."""
        token = token or self.otto
        hidden = self.request('GET', self.path(suffix), token=token)
        missing = self.request('GET', self.path(suffix, project='proj_missing0000'), token=token)
        self.assertEqual(404, hidden.status, hidden.data)
        self.assertEqual(404, missing.status, missing.data)
        self.assertEqual(without_request_id(hidden.data), without_request_id(missing.data))
        self.assertEqual(401, self.request('GET', self.path(suffix)).status)


class MembersRouteCase(TeamHarness):
    def test_every_member_reads_the_member_list(self):
        for token in (self.olive, self.carl, self.vera, self.admin):
            response = self.request('GET', self.path('/members'), token=token)
            self.assertEqual(200, response.status, response.data)
            roles = {m['username']: m['role'] for m in response.data['items']}
            self.assertEqual({'olive': 'owner', 'carl': 'contributor', 'vera': 'viewer'}, roles)
            self.assertEqual(3, response.data['total'])
            self.assertIsNone(response.data['next_cursor'])
        member = response.data['items'][0]
        self.assertEqual({'user_id', 'username', 'display_name', 'role', 'disabled',
                          'superuser'}, set(member))

    def test_account_flags_only_reach_project_administrators(self):
        secret = self.request('POST', self.path('/worker-credentials'), {'label': 'w'},
                              token=self.olive).data['credential']['secret']
        public = {'user_id', 'username', 'display_name', 'role'}
        for token in (self.carl, self.vera, secret):
            items = self.request('GET', self.path('/members'), token=token).data['items']
            self.assertEqual([public] * 3, [set(m) for m in items])
        for token in (self.olive, self.admin):
            items = self.request('GET', self.path('/members'), token=token).data['items']
            self.assertEqual([public | {'disabled', 'superuser'}] * 3, [set(m) for m in items])

    def test_non_member_and_missing_project_are_uniform_404(self):
        self.assert_uniform_not_found('/members')

    def test_member_list_pages_with_a_bound_cursor(self):
        first = self.request('GET', self.path('/members?limit=2'), token=self.olive).data
        self.assertEqual(2, len(first['items']))
        second = self.request('GET', self.path('/members?limit=2&cursor=%s'
                                               % first['next_cursor']), token=self.olive).data
        self.assertEqual(1, len(second['items']))
        seen = [m['user_id'] for m in first['items'] + second['items']]
        self.assertEqual(3, len(set(seen)))
        stolen = self.request('GET', self.path('/members?limit=2&cursor=%s'
                                               % first['next_cursor']), token=self.carl)
        self.assertEqual(409, stolen.status)
        self.assertEqual(422, self.request('GET', self.path('/members?limit=101'),
                                           token=self.olive).status)

    def test_worker_credential_reads_members_of_its_own_project_only(self):
        secret = self.request('POST', self.path('/worker-credentials'), {'label': 'w'},
                              token=self.olive).data['credential']['secret']
        self.assertEqual(200, self.request('GET', self.path('/members'), token=secret).status)
        self.assertEqual(404, self.request('GET', self.path('/members', project=self.other),
                                           token=secret).status)


class WorkerCredentialListCase(TeamHarness):
    def test_owner_sees_metadata_and_never_a_secret(self):
        issued = self.request('POST', self.path('/worker-credentials'),
                              {'label': 'build runner', 'scopes': ['tasks', 'reviews']},
                              token=self.olive)
        self.assertEqual(201, issued.status, issued.data)
        secret = issued.data['credential']['secret']
        response = self.request('GET', self.path('/worker-credentials'), token=self.olive)
        self.assertEqual(200, response.status, response.data)
        self.assertEqual(1, response.data['total'])
        item = response.data['items'][0]
        self.assertEqual('build runner', item['label'])
        self.assertEqual(['tasks', 'reviews'], item['scopes'])
        self.assertEqual(self.ids['olive'], item['user_id'])
        self.assertEqual('olive', item['user_name'])
        self.assertFalse(item['revoked'])
        self.assertNotIn(secret.encode(), response.body)
        for forbidden_key in (b'"secret"', b'token_hash', b'"hash"'):
            self.assertNotIn(forbidden_key, response.body)
        # Revocation shows as metadata, and the superuser sees it too.
        self.request('POST', self.path('/worker-credentials/%s/revoke' % item['id']),
                     token=self.olive)
        again = self.request('GET', self.path('/worker-credentials'), token=self.admin).data
        self.assertTrue(again['items'][0]['revoked'])

    def test_contributors_viewers_and_credentials_are_refused(self):
        secret = self.request('POST', self.path('/worker-credentials'), {'label': 'w'},
                              token=self.olive).data['credential']['secret']
        for token in (self.carl, self.vera, secret):
            response = self.request('GET', self.path('/worker-credentials'), token=token)
            self.assertEqual(403, response.status, response.data)

    def test_non_member_and_missing_project_are_uniform_404(self):
        self.assert_uniform_not_found('/worker-credentials')

    def test_other_projects_and_agent_credentials_are_not_listed(self):
        self.request('POST', self.path('/worker-credentials', project=self.other),
                     {'label': 'elsewhere'}, token=self.otto)
        agent = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'projects': [self.project]},
                             token=self.olive)
        self.assertEqual(201, agent.status, agent.data)
        listed = self.request('GET', self.path('/worker-credentials'), token=self.olive).data
        self.assertEqual([], listed['items'])


class BriefRouteCase(TeamHarness):
    def test_brief_follows_claim_contribution_request_changes_and_revision(self):
        brief = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.vera)
        self.assertEqual(200, brief.status, brief.data)
        self.assertEqual('none', brief.data['review']['state'])
        self.assertIsNone(brief.data['review']['contribution'])
        self.assertEqual({'who': 'anyone', 'text': 'Claim this task'},
                         brief.data['task']['next_action'])
        self.claim(self.carl)
        first = self.contribute(self.carl)
        self.assertEqual(201, first.status, first.data)
        brief = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.olive).data
        self.assertEqual('awaiting-review', brief['review']['state'])
        contribution = brief['review']['contribution']
        self.assertEqual(1, contribution['revision'])
        self.assertEqual(COMMIT, contribution['commit'])
        self.assertEqual('carl', contribution['author_name'])
        self.assertEqual('carl', brief['task']['assignee_name'])
        self.assertEqual('owner', brief['task']['next_action']['who'])
        self.assertEqual(contribution['id'], brief['review']['latest_id'])

        asked = self.review(self.olive, 'request-changes', contribution=contribution['id'],
                            previous=brief['review']['latest_id'],
                            items=[{'id': 'item-1', 'text': 'Handle an empty page'}, 'Add a test'])
        self.assertEqual(201, asked.status, asked.data)
        brief = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.carl).data
        self.assertEqual('changes-requested', brief['review']['state'])
        self.assertEqual(['Handle an empty page', 'Add a test'],
                         [r['text'] for r in brief['review']['requests']])
        self.assertEqual({'open'}, {r['status'] for r in brief['review']['requests']})
        self.assertEqual(2, brief['review']['open_requests'])
        self.assertEqual('assignee', brief['task']['next_action']['who'])

        revised = self.contribute(self.carl, commit='d' * 40,
                                  delivery={'kind': 'remote', 'remote': 'origin',
                                            'branch': 'contrib/paginate'},
                                  bundle_sha256=None)
        self.assertEqual(201, revised.status, revised.data)
        brief = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.olive).data
        self.assertEqual('awaiting-review', brief['review']['state'])
        self.assertEqual(2, brief['review']['contribution']['revision'])
        self.assertEqual('contrib/paginate', brief['review']['contribution']['branch'])
        self.assertEqual({'resolved'}, {r['status'] for r in brief['review']['requests']})
        self.assertEqual(0, brief['review']['open_requests'])

        # A reviewer holding the first revision is refused, not silently applied.
        stale = self.review(self.olive, 'approve', contribution=contribution['id'])
        self.assertEqual(409, stale.status, stale.data)
        fresh = self.review(self.olive, 'approve',
                            contribution=brief['review']['contribution']['id'])
        self.assertEqual(201, fresh.status, fresh.data)

    def test_checkpoint_appears_in_the_brief(self):
        self.claim(self.carl)
        added = self.request('POST', self.path('/tasks/%s/checkpoints' % self.task),
                             {'previous': None, 'summary': 'Half done',
                              'open_items': [{'text': 'Decide page size'}]}, token=self.carl)
        self.assertEqual(201, added.status, added.data)
        brief = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.vera).data
        self.assertEqual('Half done', brief['checkpoint']['summary'])
        self.assertEqual('carl', brief['checkpoint']['author_name'])
        self.assertEqual(['Decide page size'],
                         [item['text'] for item in brief['checkpoint']['open_items']])

    def test_not_found_is_uniform(self):
        self.assert_uniform_not_found('/tasks/%s/brief' % self.task)
        missing = self.request('GET', self.path('/tasks/task_missing/brief'), token=self.olive)
        self.assertEqual(404, missing.status)
        other_task = self.create_task(self.otto, self.other, 'hidden').data['id']
        crossed = self.request('GET', self.path('/tasks/%s/brief' % other_task), token=self.olive)
        self.assertEqual(404, crossed.status)
        self.assertEqual(without_request_id(missing.data), without_request_id(crossed.data))

    def test_viewer_reads_but_cannot_review(self):
        self.claim(self.carl)
        self.contribute(self.carl)
        self.assertEqual(200, self.request('GET', self.path('/tasks/%s/brief' % self.task),
                                           token=self.vera).status)
        self.assertEqual(403, self.review(self.vera, 'request-changes', items=['x']).status)
        self.assertEqual(403, self.review(self.carl, 'approve').status)


class QueueRouteCase(TeamHarness):
    def setUp(self):
        super().setUp()
        self.second = self.create_task(self.olive, self.project, 'Second').data['id']
        self.third = self.create_task(self.olive, self.project, 'Third').data['id']
        self.claim(self.carl)
        self.claim(self.carl, self.second)
        self.contribute(self.carl)
        self.contribute(self.carl, task=self.second)
        self.review(self.olive, 'request-changes', task=self.second, items=['Rename it'])

    def test_queue_lists_work_in_flight_by_attention(self):
        response = self.request('GET', self.path('/queue'), token=self.vera)
        self.assertEqual(200, response.status, response.data)
        states = [(item['id'], item['review_state']) for item in response.data['items']]
        self.assertEqual([(self.second, 'changes-requested'), (self.task, 'awaiting-review')],
                         states)
        first = response.data['items'][0]
        self.assertEqual(1, first['open_requests'])
        self.assertEqual(COMMIT, first['contribution']['commit'])
        self.assertEqual('carl', first['assignee_name'])
        self.assertTrue(response.data['complete'])

    def test_state_filter_and_pagination(self):
        only = self.request('GET', self.path('/queue?state=awaiting-review'), token=self.olive)
        self.assertEqual([self.task], [item['id'] for item in only.data['items']])
        self.assertEqual(422, self.request('GET', self.path('/queue?state=none'),
                                           token=self.olive).status)
        page = self.request('GET', self.path('/queue?limit=1'), token=self.olive).data
        self.assertEqual(2, page['total'])
        rest = self.request('GET', self.path('/queue?limit=1&cursor=' + page['next_cursor']),
                            token=self.olive).data
        self.assertEqual(self.task, rest['items'][0]['id'])

    def test_non_member_and_missing_project_are_uniform_404(self):
        self.assert_uniform_not_found('/queue')


class MyWorkRouteCase(TeamHarness):
    def test_assigned_and_review_attention(self):
        self.claim(self.carl)
        self.contribute(self.carl)
        second = self.create_task(self.olive, self.project, 'Second').data['id']
        self.claim(self.carl, second)
        carl = self.request('GET', '/v1/me/work', token=self.carl)
        self.assertEqual(200, carl.status, carl.data)
        self.assertEqual({self.task, second}, {t['id'] for t in carl.data['assigned']})
        self.assertEqual([], carl.data['to_review'])
        self.assertEqual('Alpha', carl.data['assigned'][0]['project_name'])
        olive = self.request('GET', '/v1/me/work', token=self.olive).data
        self.assertEqual([self.task], [t['id'] for t in olive['to_review']])
        self.assertEqual([], olive['assigned'])
        self.assertFalse(olive['truncated'])
        vera = self.request('GET', '/v1/me/work', token=self.vera).data
        self.assertEqual(([], []), (vera['assigned'], vera['to_review']))

    def test_other_projects_do_not_leak(self):
        other_task = self.create_task(self.otto, self.other, 'hidden').data['id']
        claimed = self.request('POST', self.path('/tasks/%s/claim' % other_task,
                                                 project=self.other), {}, token=self.otto)
        self.assertEqual(200, claimed.status)
        self.request('POST', self.path('/tasks/%s/reviews' % other_task, project=self.other),
                     {'operation': 'contribute', 'commit': COMMIT, 'base_commit': BASE,
                      'bundle_sha256': BUNDLE, 'summary': 'x'}, token=self.otto)
        for token in (self.olive, self.carl, self.admin):
            body = self.request('GET', '/v1/me/work', token=token).body
            self.assertNotIn(other_task.encode(), body)
            self.assertNotIn(b'Elsewhere', body)

    def test_membership_removal_drops_the_project(self):
        self.claim(self.carl)
        self.assertTrue(self.request('GET', '/v1/me/work', token=self.carl).data['assigned'])
        self.request('DELETE', self.path('/members/%s' % self.ids['carl']), token=self.olive)
        self.assertEqual([], self.request('GET', '/v1/me/work', token=self.carl).data['assigned'])

    def test_credentials_and_anonymous_are_refused(self):
        secret = self.request('POST', self.path('/worker-credentials'), {'label': 'w'},
                              token=self.olive).data['credential']['secret']
        self.assertEqual(403, self.request('GET', '/v1/me/work', token=secret).status)
        self.assertEqual(401, self.request('GET', '/v1/me/work').status)


class AccountLookupCase(TeamHarness):
    def lookup(self, token, username, project=None):
        return self.request('GET', '/v1/accounts/lookup?username=%s&project=%s'
                            % (username, project or self.project), token=token)

    def test_owner_finds_an_exact_active_username(self):
        found = self.lookup(self.olive, 'otto')
        self.assertEqual(200, found.status, found.data)
        self.assertEqual({'id': self.ids['otto'], 'username': 'otto', 'display_name': 'otto'},
                         found.data)
        self.assertEqual(self.ids['otto'], self.lookup(self.olive, 'OTTO').data['id'])
        self.assertEqual(200, self.lookup(self.admin, 'otto').status)

    def test_missing_partial_and_disabled_are_the_same_404(self):
        self.request('POST', '/v1/accounts/%s/disable' % self.ids['vera'], token=self.admin)
        missing = self.lookup(self.olive, 'nobody')
        partial = self.lookup(self.olive, 'ott')
        disabled = self.lookup(self.olive, 'vera')
        for response in (missing, partial, disabled):
            self.assertEqual(404, response.status, response.data)
        self.assertEqual(without_request_id(missing.data), without_request_id(partial.data))
        self.assertEqual(without_request_id(missing.data), without_request_id(disabled.data))

    def test_only_project_administrators_may_look_up(self):
        self.assertEqual(403, self.lookup(self.carl, 'otto').status)
        self.assertEqual(403, self.lookup(self.vera, 'otto').status)
        self.assertEqual(404, self.lookup(self.otto, 'olive').status)  # not otto's project
        self.assertEqual(404, self.lookup(self.olive, 'otto', project='proj_missing0000').status)
        secret = self.request('POST', self.path('/worker-credentials'), {'label': 'w'},
                              token=self.olive).data['credential']['secret']
        self.assertEqual(403, self.lookup(secret, 'otto').status)
        self.assertEqual(401, self.request('GET', '/v1/accounts/lookup?username=otto&project=%s'
                                           % self.project).status)

    def test_malformed_requests(self):
        self.assertEqual(422, self.request('GET', '/v1/accounts/lookup?username=otto',
                                           token=self.olive).status)
        self.assertEqual(422, self.lookup(self.olive, 'a%20b').status)


class SessionAndTaskListCase(TeamHarness):
    def test_current_session_returns_username_and_csrf_to_the_cookie_only(self):
        login = self.request('POST', '/v1/sessions',
                             {'username': 'carl', 'password': 'carl-password-1'})
        cookie = login.set_cookies()[0].split(';')[0]
        csrf = login.data['session']['csrf_token']
        current = self.request('GET', '/v1/sessions/current', cookie=cookie).data
        self.assertEqual('carl', current['user']['username'])
        self.assertEqual(csrf, current['csrf_token'])
        bearer = self.request('GET', '/v1/sessions/current', token=self.carl).data
        self.assertNotIn('csrf_token', bearer)
        # The returned token is the one the cookie session accepts.
        created = self.request('POST', self.path('/tasks'), {'title': 'from the page'},
                               cookie=cookie, csrf=current['csrf_token'])
        self.assertEqual(201, created.status, created.data)

    def test_task_list_filters_and_presentation_fields(self):
        second = self.create_task(self.olive, self.project, 'Close me').data['id']
        version = self.request('GET', self.path('/tasks/%s' % second), token=self.olive).data
        self.request('PATCH', self.path('/tasks/%s' % second),
                     {'status': 'closed', 'version': version['version']}, token=self.olive)
        self.claim(self.carl)
        active = self.request('GET', self.path('/tasks?status=active'), token=self.vera).data
        self.assertEqual([self.task], [t['id'] for t in active['items']])
        self.assertEqual('carl', active['items'][0]['assignee_name'])
        self.assertEqual('assignee', active['items'][0]['next_action']['who'])
        closed = self.request('GET', self.path('/tasks?status=closed'), token=self.vera).data
        self.assertEqual([second], [t['id'] for t in closed['items']])
        self.assertIsNone(closed['items'][0]['next_action'])
        text = self.request('GET', self.path('/tasks?q=PAGINATE'), token=self.vera).data
        self.assertEqual([self.task], [t['id'] for t in text['items']])
        mine = self.request('GET', self.path('/tasks?assignee=%s' % self.ids['carl']),
                            token=self.vera).data
        self.assertEqual(1, mine['total'])
        self.assertEqual(422, self.request('GET', self.path('/tasks?status=bogus'),
                                           token=self.vera).status)
        everything = self.request('GET', self.path('/tasks'), token=self.vera).data
        self.assertEqual(2, everything['total'])

    def test_approved_filter_and_named_history(self):
        self.claim(self.carl)
        self.contribute(self.carl)
        self.review(self.olive, 'approve')
        for state in ('approved', 'awaiting-integration'):
            found = self.request('GET', self.path('/tasks?review_state=' + state),
                                 token=self.vera).data
            self.assertEqual([self.task], [t['id'] for t in found['items']], state)
        history = self.request('GET', self.path('/tasks/%s/history' % self.task),
                               token=self.vera).data
        self.assertEqual(['olive', 'carl', 'carl', 'olive'],
                         [e['user_name'] for e in history['items']])


class CanonicalBindingReadCase(EndpointCase):
    """The same reads over ``EndpointBackend`` and the strict canonical stub."""

    def test_brief_and_queue_follow_the_canonical_review_chain(self):
        alex, project = self.setup_project()
        task = self.create_task(alex, project, 'canonical task').data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                                           % (project, task), {}, token=alex).status)
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (project, task),
                             token=alex)
        self.assertEqual(200, brief.status, brief.data)
        self.assertEqual('none', brief.data['review']['state'])
        self.assertIsNone(brief.data['review']['latest_id'])
        self.assertEqual('canonical task', brief.data['task']['title'])
        delivered = self.contribute(alex, project, task)
        self.assertEqual(201, delivered.status, delivered.data)
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (project, task),
                             token=alex).data
        review = brief['review']
        self.assertEqual('awaiting-review', review['state'])
        self.assertEqual(1, review['contribution']['revision'])
        self.assertEqual(COMMIT, review['contribution']['commit'])
        self.assertEqual(review['contribution']['id'], review['latest_id'])
        self.assertEqual(set(brief['lifecycle']), {'implemented', 'tested', 'reviewed',
                                                   'integrated', 'deployed', 'live-verified'})
        queue = self.request('GET', '/v1/projects/%s/queue' % project, token=alex)
        self.assertEqual(200, queue.status, queue.data)
        self.assertEqual([(task, 'awaiting-review')],
                         [(i['id'], i['review_state']) for i in queue.data['items']])
        self.assertTrue(queue.data['complete'])
        asked = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task),
            {'operation': 'request-changes', 'schema_version': 1,
             'contribution': review['contribution']['id'], 'previous': review['latest_id'],
             'items': [{'id': 'item-1', 'text': 'Handle an empty page'}]}, token=alex,
            key='web-review-0001')
        self.assertEqual(201, asked.status, asked.data)
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (project, task),
                             token=alex).data
        self.assertEqual('changes-requested', brief['review']['state'])
        self.assertEqual(['Handle an empty page'],
                         [r['text'] for r in brief['review']['requests']])
        self.assertEqual(1, brief['review']['open_requests'])
        mine = self.request('GET', '/v1/me/work', token=alex).data
        self.assertEqual([task], [t['id'] for t in mine['assigned']])
        self.assertEqual('changes-requested', mine['assigned'][0]['review_state'])

        # A revision does not resolve a canonical request: that needs the respond step
        # (slice 2). The brief must keep it open and name the older revision, which is
        # what the page uses to say so instead of implying it was addressed.
        body = dict(CONTRIBUTION, operation='contribute', schema_version=1,
                    operation_id='op-revision-2', commit='d' * 40,
                    supersedes=review['contribution']['id'],
                    previous=brief['review']['latest_id'])
        revised = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task),
                               body, token=alex)
        self.assertEqual(201, revised.status, revised.data)
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (project, task),
                             token=alex).data
        self.assertEqual('changes-requested', brief['review']['state'])
        self.assertEqual(2, brief['review']['contribution']['revision'])
        self.assertEqual(['open'], [r['status'] for r in brief['review']['requests']])
        self.assertEqual(review['contribution']['id'],
                         brief['review']['requests'][0]['contribution'])
        self.assertNotEqual(brief['review']['contribution']['id'],
                            brief['review']['requests'][0]['contribution'])

    def test_task_list_carries_the_canonical_review_state(self):
        alex, project = self.setup_project()
        idle = self.create_task(alex, project, 'idle task').data['id']
        busy = self.create_task(alex, project, 'busy task').data['id']
        done = self.create_task(alex, project, 'done task').data['id']
        self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, busy), {}, token=alex)
        self.assertEqual(201, self.contribute(alex, project, busy).status)
        closed = self.request('PATCH', '/v1/projects/%s/tasks/%s' % (project, done),
                              {'status': 'closed'}, token=alex)
        self.assertEqual(200, closed.status, closed.data)
        listing = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex)
        self.assertEqual(200, listing.status, listing.data)
        rows = {t['id']: t for t in listing.data['items']}
        self.assertEqual('awaiting-review', rows[busy]['review_state'])
        # Never "Deliver a contribution" for work that is already awaiting review.
        self.assertEqual('owner', rows[busy]['next_action']['who'])
        self.assertEqual('none', rows[idle]['review_state'])
        self.assertEqual('anyone', rows[idle]['next_action']['who'])
        # A closed task the work projection no longer lists is unknown, not guessed.
        self.assertIsNone(rows[done]['review_state'])
        self.assertIsNone(rows[done]['next_action'])
        self.assertFalse(listing.data['review_states_complete'])
        found = self.request('GET', '/v1/projects/%s/tasks?review_state=awaiting-review'
                             % project, token=alex).data
        self.assertEqual([busy], [t['id'] for t in found['items']])
        active = self.request('GET', '/v1/projects/%s/tasks?status=active' % project,
                              token=alex).data
        self.assertTrue(active['review_states_complete'])

    def test_brief_of_a_hidden_project_is_404_before_any_canonical_read(self):
        alex, project = self.setup_project()
        admin = self.admin_token()
        self.create_account(admin, 'blair', 'blair-password-1')
        blair = self.login('blair', 'blair-password-1')[0]
        for suffix in ('/tasks/x-1/brief', '/queue', '/members'):
            self.assertEqual(404, self.request('GET', '/v1/projects/%s%s' % (project, suffix),
                                               token=blair).status)
        self.assertFalse((self.canonical_root / 'canonical.json').exists() and
                         self.canonical_rows())



# ---------------------------------------------------------------- slice-1 review fixes
class RouteTableCase(unittest.TestCase):
    """The browser's hash routes accept every id the server accepts (P2-1).

    ``web/js/routes.js`` holds the route table and the id pattern. This compiles its
    templates exactly as ``compile()`` does and checks canonical dotted ids route to
    the right view; it also runs the real module under node when node is installed.
    """
    ROUTES_JS = WEB / 'js' / 'routes.js'

    def table(self):
        text = self.ROUTES_JS.read_text(encoding='utf-8')
        id_pattern = re.search(r"export const ID = '([^']+)';", text).group(1)
        templates = re.findall(r"\['(\w+)', '(/[^']*)'\]", text)
        self.assertGreaterEqual(len(templates), 17)
        return id_pattern, templates

    @staticmethod
    def compile(id_pattern, template):
        parts = re.split(r'(\{[a-z]+\})', template)
        source = ''.join('(?P<%s>%s)' % (part[1:-1], id_pattern)
                         if re.fullmatch(r'\{[a-z]+\}', part) else re.escape(part)
                         for part in parts)
        return re.compile('^' + source + '$')

    def match(self, route):
        id_pattern, templates = self.table()
        for name, template in templates:
            found = self.compile(id_pattern, template).match(route)
            if found:
                return name, found.groupdict()
        return None

    def test_id_pattern_is_the_server_pattern(self):
        from http_service import ID
        self.assertEqual(ID, self.table()[0])

    def test_dotted_canonical_ids_reach_their_views(self):
        self.assertEqual(('task', {'pid': 'kittrial', 'tid': 'kittrial-5bb.20'}),
                         self.match('/p/kittrial/t/kittrial-5bb.20'))
        self.assertEqual(('reviews', {'pid': 'proj.a-b_c'}), self.match('/p/proj.a-b_c/reviews'))
        self.assertEqual(('project', {'pid': 'jjbp'}), self.match('/p/jjbp'))
        self.assertEqual(('record', {'pid': 'p', 'id': 'kittrial-5bb.20.1'}),
                         self.match('/p/p/records/kittrial-5bb.20.1'))
        for bad in ('/p/../t/x', '/p/x/t/.hidden', '/p/x/t/', '/p/x/t/a/b', '/p/x y'):
            self.assertIsNone(self.match(bad), bad)

    def test_app_uses_the_shared_table(self):
        app = (WEB / 'js' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("from './routes.js'", app)
        self.assertNotIn('[\\w-]', app)  # no private, narrower id pattern

    def test_the_real_module_under_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; the Python checks above still apply')
        import subprocess
        script = ("import(process.argv[1]).then((r) => console.log(JSON.stringify(["
                  "r.matchRoute('/p/kittrial/t/kittrial-5bb.20'), r.projectOf('/p/a.b/reviews'),"
                  "r.matchRoute('/p/x/t/../y')])))")
        out = subprocess.run([node, '--input-type=module', '-e', script,
                              self.ROUTES_JS.as_uri()], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(0, out.returncode, out.stderr)
        task, project, bad = json.loads(out.stdout)
        self.assertEqual({'name': 'task', 'params': {'pid': 'kittrial',
                                                      'tid': 'kittrial-5bb.20'}}, task)
        self.assertEqual('a.b', project)
        self.assertIsNone(bad)


class LookupThrottleCase(TeamHarness):
    def lookup(self, token, username):
        return self.request('GET', '/v1/accounts/lookup?username=%s&project=%s'
                            % (username, self.project), token=token)

    def lookups(self):
        return [e for e in self.store.state['audit'] if e['action'] == 'accounts.lookup']

    def test_lookups_are_throttled_per_principal(self):
        self.service.lookup_max = 3
        for name in ('otto', 'nobody', 'carl'):
            self.assertIn(self.lookup(self.olive, name).status, (200, 404))
        limited = self.lookup(self.olive, 'vera')
        self.assertEqual(429, limited.status, limited.data)
        self.assertEqual('rate_limited', limited.data['error']['code'])
        self.assertEqual(limited.headers.get('x-request-id'), limited.data['request_id'])
        # Another principal has its own budget.
        self.assertEqual(200, self.lookup(self.admin, 'vera').status)
        # The window slides: old lookups age out.
        self.service._lookups[self.ids['olive']] = [0.0] * 3
        self.assertEqual(200, self.lookup(self.olive, 'vera').status)

    def test_every_lookup_is_audited_without_the_name(self):
        self.lookup(self.olive, 'otto')
        self.lookup(self.olive, 'Nobody')
        self.service.lookup_max = 2
        self.lookup(self.olive, 'carl')
        events = self.lookups()
        self.assertEqual(['found', 'not_found', 'throttled'], [e['outcome'] for e in events])
        for event in events:
            self.assertEqual(self.project, event['project_id'])
            self.assertEqual(self.ids['olive'], event['user_id'])
            self.assertTrue(event['request_id'])
            self.assertRegex(event['reason'], r'^username_sha256=[0-9a-f]{16}$')
        self.assertNotIn('otto', json.dumps(events))
        self.assertNotIn('nobody', json.dumps(events).lower())
        # Same name in any case gives the same digest, so repeated probing is visible.
        self.lookup(self.admin, 'NOBODY')
        self.assertEqual(self.lookups()[1]['reason'], self.lookups()[-1]['reason'])
        # A refused caller (not an administrator) is neither charged nor recorded.
        self.assertEqual(403, self.lookup(self.carl, 'otto').status)
        self.assertEqual(4, len(self.lookups()))
        # The project owner reads them in the project audit.
        audit = self.request('GET', self.path('/audit?limit=100'), token=self.olive).data
        self.assertEqual(4, len([e for e in audit['items'] if e['action'] == 'accounts.lookup']))


class NextActionCase(unittest.TestCase):
    def test_unknown_review_state_invites_nothing(self):
        from http_service import next_action
        self.assertIsNone(next_action({'status': 'open', 'assignee': 'u1', 'review_state': None}))
        self.assertIsNone(next_action({'status': 'open', 'assignee': None}))
        self.assertEqual('anyone', next_action({'status': 'open', 'review_state': 'none'})['who'])
        self.assertEqual('assignee', next_action({'status': 'open', 'assignee': 'u1',
                                                  'review_state': 'none'})['who'])
        self.assertNotIn('Deliver', next_action({'status': 'open', 'assignee': 'u1',
                                                 'review_state': 'integrated'})['text'])


class CountingQueueBackend(InProcessBackend):
    READ_CACHE_SECONDS = 20

    def __init__(self, service):
        super().__init__(service)
        self.queue_reads = 0

    def review_queue(self, project_id):
        self.queue_reads += 1
        return super().review_queue(project_id)


class MyWorkCacheCase(TeamHarness):
    def setUp(self):
        super().setUp()
        self._stop_server()
        self.backend = CountingQueueBackend(self.service)
        self.httpd = create_server(self.service, self.backend, host='127.0.0.1', port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def test_repeat_reads_reuse_the_queue_but_not_authority(self):
        self.claim(self.carl)
        first = self.request('GET', '/v1/me/work', token=self.carl).data
        self.assertEqual([self.task], [t['id'] for t in first['assigned']])
        reads = self.backend.queue_reads
        again = self.request('GET', '/v1/me/work', token=self.carl).data
        self.assertEqual(reads, self.backend.queue_reads)
        self.assertEqual(first['assigned'], again['assigned'])
        # Per principal: olive's read is her own.
        self.request('GET', '/v1/me/work', token=self.olive)
        self.assertEqual(reads + 1, self.backend.queue_reads)
        # The queue route itself is never served from the shared cache.
        self.request('GET', self.path('/queue'), token=self.carl)
        self.assertEqual(reads + 2, self.backend.queue_reads)
        # Losing membership takes effect at once, cache or not.
        self.request('DELETE', self.path('/members/%s' % self.ids['carl']), token=self.olive)
        self.assertEqual([], self.request('GET', '/v1/me/work', token=self.carl).data['assigned'])
        # Once the entry expires the next read is fresh.
        cache = self.httpd.RequestHandlerClass.read_cache
        for key in list(cache):
            cache[key] = (0, cache[key][1])
        self.request('GET', '/v1/me/work', token=self.olive)
        self.assertEqual(reads + 3, self.backend.queue_reads)


if __name__ == '__main__':
    unittest.main()
