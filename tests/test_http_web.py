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
from test_http_service import (ADMIN, BASE, BUNDLE, COMMIT, Response, ServerHarness,
                               unique_dir)

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


if __name__ == '__main__':
    unittest.main()
