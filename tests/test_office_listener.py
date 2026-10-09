"""The office service on an open port of the host (kittrial-5bb.163).

Three shapes: plain HTTP on loopback behind a proxy or a tunnel (the default, unchanged),
HTTPS on any address with a certificate and key (a self-signed one must work), and plain
HTTP on the network only with the operator's explicit setting and a warning at every start.
"""
import contextlib
import http.client
import io
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tools'))
LINUX = sys.platform.startswith('linux')
OPENSSL = shutil.which('openssl')
if LINUX:
    import office_service as office
import http_service
from http_auth import Service, Store

NOT_LOOPBACK = ('http_host %s is not loopback and no cert and key are given. Give both for HTTPS, or set '
                'allow_plaintext_on_network to true to serve plain HTTP on the network (passwords and session cookies '
                'then cross it unencrypted)')
WARNING = ('WARNING: serving plain HTTP on %s:%d. Passwords and session cookies cross the network unencrypted. '
           'Use cert and key for HTTPS.')


def self_signed(directory, name='office.example.invalid'):
    """A self-signed certificate and key made as the docs tell the operator to make one."""
    cert, key = Path(directory)/'office.crt', Path(directory)/'office.key'
    subprocess.check_call([OPENSSL, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-keyout', str(key),
                           '-out', str(cert), '-subj', '/CN=' + name,
                           '-addext', 'subjectAltName=DNS:%s,IP:127.0.0.1' % name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(cert), str(key)


@unittest.skipUnless(LINUX, 'Linux supervisor only')
class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name)/'office.json'

    def config(self, **settings):
        self.file.write_text(json.dumps(dict({'schema_version': 1}, **settings)), encoding='utf-8')
        return office.service_config(self.file)

    def refused(self, **settings):
        with self.assertRaises(ValueError) as caught:
            self.config(**settings)
        return str(caught.exception)

    def test_loopback_stays_the_default_and_nothing_changes_for_it(self):
        for settings in ({}, {'http_host': '127.0.0.1'}, {'http_host': 'localhost'}, {'http_host': '::1'},
                         {'cert': '/c', 'key': '/k'}, {'public_url': 'https://orchestra.example.invalid'}):
            with self.subTest(settings=settings):
                self.assertEqual(self.config(**settings), dict({'schema_version': 1}, **settings))
        self.assertEqual(office.listener({}, 10000), {'shape': 'loopback', 'host': '127.0.0.1', 'port': 10000,
                                                      'probe': 'http://127.0.0.1:10000/healthz', 'public_url': None})
        self.assertEqual(office.listener(None, 10000)['shape'], 'loopback')

    def test_another_address_needs_a_certificate_or_the_explicit_setting(self):
        for host in ('0.0.0.0', '10.1.2.3', 'office.example.invalid', '::'):
            with self.subTest(host=host):
                self.assertEqual(self.refused(http_host=host, public_url='http://x'), NOT_LOOPBACK % host)
        self.assertEqual(self.config(http_host='10.1.2.3', cert='/c', key='/k')['http_host'], '10.1.2.3')
        self.assertEqual(self.config(http_host='10.1.2.3', allow_plaintext_on_network=True)['allow_plaintext_on_network'], True)
        # False is the same as leaving it out.
        self.assertEqual(self.refused(http_host='10.1.2.3', allow_plaintext_on_network=False), NOT_LOOPBACK % '10.1.2.3')

    def test_the_setting_is_refused_where_it_would_do_nothing(self):
        self.assertEqual(self.refused(allow_plaintext_on_network=True),
                         'allow_plaintext_on_network is set but http_host is loopback: remove the setting, or name the '
                         'address to serve on')
        self.assertEqual(self.refused(http_host='10.1.2.3', cert='/c', key='/k', allow_plaintext_on_network=True),
                         'allow_plaintext_on_network is set together with cert and key: remove one of the two; with a '
                         'certificate the service serves HTTPS')
        for value in ('yes', 1, 'true', None):
            with self.subTest(value=value):
                self.assertEqual(self.refused(http_host='10.1.2.3', allow_plaintext_on_network=value),
                                 'allow_plaintext_on_network must be true or false')

    def test_a_wildcard_needs_a_public_url_and_a_bad_host_is_refused(self):
        for host in ('0.0.0.0', '::'):
            self.assertEqual(self.refused(http_host=host, cert='/c', key='/k'),
                             'http_host %s listens on every address, so the service cannot name itself: set public_url '
                             'to the address people use' % host)
            self.assertEqual(self.config(http_host=host, cert='/c', key='/k', public_url='https://office:10000')['http_host'], host)
        for host in ('', ' 10.1.2.3', 'a b', 'host/path', 'http://host', 7, None, '-x', 'x' * 300):
            with self.subTest(host=host):
                self.assertEqual(self.refused(http_host=host, cert='/c', key='/k'), 'http_host must be a host name or an address')
        self.assertIn('cert and key must be supplied together', self.refused(http_host='10.1.2.3', cert='/c'))
        self.assertIn('Unknown office service setting: allow_plaintext', self.refused(allow_plaintext=True))

    def test_the_three_shapes(self):
        tls = {'http_host': '10.1.2.3', 'cert': '/c', 'key': '/k'}
        self.assertEqual(office.listener(tls, 10000), {'shape': 'https', 'host': '10.1.2.3', 'port': 10000,
                                                       'probe': 'https://10.1.2.3:10000/healthz',
                                                       'public_url': 'https://10.1.2.3:10000'})
        plain = {'http_host': 'office.example.invalid', 'allow_plaintext_on_network': True}
        self.assertEqual(office.listener(plain, 10001), {'shape': 'plain-http-on-network', 'host': 'office.example.invalid',
                                                         'port': 10001, 'probe': 'http://office.example.invalid:10001/healthz',
                                                         'public_url': 'http://office.example.invalid:10001'})
        # A wildcard is asked on loopback and named by the public_url the file gives.
        everywhere = office.listener({'http_host': '0.0.0.0', 'cert': '/c', 'key': '/k', 'public_url': 'https://office:10000'}, 10000)
        self.assertEqual((everywhere['probe'], everywhere['public_url']), ('https://127.0.0.1:10000/healthz', 'https://office:10000'))
        six = office.listener({'http_host': '::', 'allow_plaintext_on_network': True, 'public_url': 'http://office:1'}, 10000)
        self.assertEqual(six['probe'], 'http://[::1]:10000/healthz')
        literal = office.listener({'http_host': 'fd00::5', 'cert': '/c', 'key': '/k'}, 10000)
        self.assertEqual((literal['probe'], literal['public_url']), ('https://[fd00::5]:10000/healthz', 'https://[fd00::5]:10000'))
        # A public_url in the file always wins; HTTPS on loopback is still HTTPS.
        self.assertEqual(office.listener(dict(tls, public_url='https://orchestra.example.invalid'), 10000)['public_url'],
                         'https://orchestra.example.invalid')
        local = office.listener({'cert': '/c', 'key': '/k'}, 10000)
        self.assertEqual((local['shape'], local['probe'], local['public_url']), ('https', 'https://127.0.0.1:10000/healthz', None))

    def test_the_command_the_supervisor_starts(self):
        script, root = Path('/release/kit/office_service.py'), Path('/runtime')

        def command(**settings):
            return [str(part) for part in office.web_command(settings, root, 10000, '/release/python', script)]

        def value(found, flag):
            return found[found.index(flag) + 1] if flag in found else None
        default = command()
        self.assertEqual((value(default, '--host'), value(default, '--port'), value(default, '--public-url'), value(default, '--cert')),
                         ('127.0.0.1', '10000', None, None))
        self.assertNotIn('--allow-plaintext-on-network', default)
        tls = command(http_host='10.1.2.3', cert='/c.pem', key='/k.pem')
        self.assertEqual((value(tls, '--host'), value(tls, '--cert'), value(tls, '--key'), value(tls, '--public-url')),
                         ('10.1.2.3', '/c.pem', '/k.pem', 'https://10.1.2.3:10000'))
        self.assertNotIn('--allow-plaintext-on-network', tls)
        plain = command(http_host='10.1.2.3', allow_plaintext_on_network=True)
        self.assertEqual((value(plain, '--host'), value(plain, '--public-url'), value(plain, '--cert')),
                         ('10.1.2.3', 'http://10.1.2.3:10000', None))
        self.assertIn('--allow-plaintext-on-network', plain)
        self.assertEqual(value(command(public_url='https://orchestra.example.invalid'), '--public-url'),
                         'https://orchestra.example.invalid')


class WebServiceFlagTests(unittest.TestCase):
    """`http_service.py --allow-plaintext-on-network`: the word the supervisor passes on."""

    def start(self, *more):
        made = {}

        class Stopped:
            server_address = ('0.0.0.0', 4321)

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        def create(service, backend, **settings):
            if settings['host'] not in http_service.LOOPBACK and settings['certfile'] is None \
                    and not settings['allow_plaintext_non_loopback']:
                raise ValueError('Refusing plaintext on a non-loopback interface; supply TLS or explicitly allow '
                                 'disposable plaintext')
            made.update(settings)
            return Stopped()
        tmp = tempfile.mkdtemp()                                      # the store keeps its record file open
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(http_service, 'create_server', create), \
                contextlib.redirect_stderr(io.StringIO()) as said, contextlib.redirect_stdout(io.StringIO()):
            try:
                http_service.main(['--state', str(Path(tmp)/'state.json'), '--backend', 'inprocess', '--no-web', '--port', '4321',
                                   *more])
            except (ValueError, SystemExit) as refusal:
                return None, str(refusal), said.getvalue()
        return made, None, said.getvalue()

    def test_plain_http_off_loopback_is_refused_without_the_flag_and_warned_about_with_it(self):
        made, refusal, said = self.start('--host', '0.0.0.0')
        self.assertIsNone(made)
        made, refusal, said = self.start('--host', '0.0.0.0', '--allow-plaintext-on-network')
        self.assertTrue(made['allow_plaintext_non_loopback'])
        self.assertIn('WARNING: serving plain HTTP on 0.0.0.0:4321. Passwords and session cookies cross the network '
                      'unencrypted. Use --cert and --key for HTTPS.', said)

    def test_loopback_and_https_say_nothing(self):
        for arguments in ((), ('--host', '127.0.0.1'), ('--host', '0.0.0.0', '--cert', '/c', '--key', '/k')):
            with self.subTest(arguments=arguments):
                made, refusal, said = self.start(*arguments)
                self.assertFalse(made['allow_plaintext_non_loopback'])
                self.assertNotIn('WARNING', said)


class NetworkShapeTests(unittest.TestCase):
    """The real web service on an address that is not loopback: log-in, the cookie, CSRF, a credential."""

    ADMIN, PASSWORD = 'root-admin', 'correct horse battery staple 1'

    def serve(self, **listener):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(Path(tmp)/'state.json')
        Service.bootstrap_superuser(store, self.ADMIN, self.PASSWORD)
        service = Service(store, public_url=listener.pop('public_url'))
        httpd = http_service.create_server(service, http_service.InProcessBackend(service), host='0.0.0.0', port=0,
                                           web_root=None, **listener)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))
        return httpd.server_address[1]

    def call(self, connection, method, path, body=None, headers=None):
        sent = dict(headers or {})
        if body is not None:
            sent['Content-Type'] = 'application/json'
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=sent)
        try:
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    def exercise(self, connect, secure):
        status, headers, body = self.call(connect(), 'POST', '/v1/sessions', {'username': self.ADMIN, 'password': self.PASSWORD})
        self.assertEqual(status, 201, body)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertEqual('; Secure' in cookie, secure, cookie)        # marked Secure only where the channel is
        session = cookie.split(';')[0]
        token, csrf = body['session']['token'], body['session']['csrf_token']
        # The session cookie authenticates a read.
        status, _, me = self.call(connect(), 'GET', '/v1/sessions/current', headers={'Cookie': session})
        self.assertEqual((status, me['user']['username']), (200, self.ADMIN))
        # A cookie write without the CSRF token is refused; with it, accepted.
        status, _, _ = self.call(connect(), 'POST', '/v1/accounts', {'username': 'zoe'}, {'Cookie': session})
        self.assertEqual(status, 403)
        status, _, made = self.call(connect(), 'POST', '/v1/accounts', {'username': 'zoe'},
                                    {'Cookie': session, 'X-CSRF-Token': csrf})
        self.assertEqual(status, 201, made)
        # A project, an agent and its credential: the secret is issued once, over this channel.
        status, _, project = self.call(connect(), 'POST', '/v1/projects', {'name': 'Alpha'}, {'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 201, project)
        status, _, agent = self.call(connect(), 'POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/x/k',
                                                                      'projects': [project['id']]},
                                     {'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 201, agent)
        status, _, seen = self.call(connect(), 'GET', '/v1/agents/me', headers={'Authorization': 'Bearer ' + agent['credential']['secret']})
        self.assertEqual(status, 200, seen)

    def test_plain_http_on_the_network_works_and_does_not_mark_the_cookie_secure(self):
        with self.assertRaises(ValueError):                           # refused without the explicit word
            http_service.create_server(None, None, host='0.0.0.0', port=0, web_root=None)
        port = self.serve(public_url='http://office.example.invalid:10000', allow_plaintext_non_loopback=True)
        self.exercise(lambda: http.client.HTTPConnection('127.0.0.1', port, timeout=15), secure=False)

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the self-signed certificate is made with it')
    def test_https_with_a_self_signed_certificate_works_and_marks_the_cookie_secure(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        cert, key = self_signed(directory)
        port = self.serve(public_url='https://office.example.invalid:10000', certfile=cert, keyfile=key)
        trusting = ssl.create_default_context(cafile=cert)            # a client that was given the certificate
        self.exercise(lambda: http.client.HTTPSConnection('127.0.0.1', port, timeout=15, context=trusting), secure=True)
        # A client that was not given it refuses the connection: that is what the browser warns about.
        with self.assertRaises(ssl.SSLError):
            self.call(http.client.HTTPSConnection('127.0.0.1', port, timeout=15, context=ssl.create_default_context()),
                      'GET', '/healthz')
        # Plain HTTP to the HTTPS port gets nothing.
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.call(http.client.HTTPConnection('127.0.0.1', port, timeout=5), 'GET', '/healthz')


@unittest.skipUnless(LINUX, 'Linux supervisor only')
class HealthTests(unittest.TestCase):
    """health asks the listener the configuration describes."""

    def serve(self, host='127.0.0.1', **listener):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(Path(tmp)/'state.json')
        service = Service(store)
        httpd = http_service.create_server(service, http_service.InProcessBackend(service), host=host, port=0,
                                           web_root=None, **listener)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))
        self.root = Path(tmp)/'runtime'
        self.root.mkdir()
        return httpd.server_address[1]

    def web(self, port, settings):
        line, code = office.health(self.root, port, settings)
        return 'web=up' in line, code

    def test_loopback_as_before(self):
        port = self.serve()
        self.assertEqual(self.web(port, None), (True, 1))             # the database and the backup are not up in this test
        self.assertEqual(self.web(port, {}), (True, 1))
        self.assertEqual(self.web(port + 1 if port < 65000 else port - 1, None)[0], False)

    def test_plain_http_on_the_network(self):
        port = self.serve(host='0.0.0.0', allow_plaintext_non_loopback=True)
        self.assertTrue(self.web(port, {'http_host': '0.0.0.0', 'allow_plaintext_on_network': True, 'public_url': 'http://o:1'})[0])

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the self-signed certificate is made with it')
    def test_https_with_a_self_signed_certificate(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        cert, key = self_signed(directory)
        port = self.serve(host='0.0.0.0', certfile=cert, keyfile=key)
        settings = {'http_host': '0.0.0.0', 'cert': cert, 'key': key, 'public_url': 'https://office:10000'}
        self.assertTrue(self.web(port, settings)[0])
        # Asked without the configuration (plain HTTP on loopback) an HTTPS listener reads as down: pass --config.
        self.assertFalse(self.web(port, None)[0])

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the self-signed certificate is made with it')
    def test_the_verification_record_asks_an_https_listener_over_https(self):
        import office_verify
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        cert, key = self_signed(directory)
        port = self.serve(host='0.0.0.0', certfile=cert, keyfile=key)
        config = self.root/'office.json'
        config.write_text(json.dumps({'schema_version': 1, 'http_host': '0.0.0.0', 'cert': cert, 'key': key,
                                      'public_url': 'https://office.example.invalid:10000'}), encoding='utf-8')
        install = self.root/'install'
        (install/'releases'/'r1'/'kit').mkdir(parents=True)
        (install/'current').symlink_to('releases/r1')
        (install/'releases'/'r1'/'manifest.json').write_text(json.dumps({'build_id': 'r1', 'source_commit': 'c' * 40}))
        printed = io.StringIO()
        with mock.patch.object(office_verify, 'report', return_value={'source_commit': 'c' * 40, 'build_id': 'r1', 'version': '0'}), \
                contextlib.redirect_stdout(printed):
            office_verify.main(['--install-root', str(install), '--root', str(self.root), '--port', str(port),
                                '--config', str(config)])
        record = json.loads(printed.getvalue())
        self.assertEqual(record['listener']['shape'], 'https')
        self.assertIn('web=up', record['health'])

    def test_the_verification_record_names_the_shape(self):
        import office_verify
        port = self.serve(host='0.0.0.0', allow_plaintext_non_loopback=True)
        config = self.root/'office.json'
        config.write_text(json.dumps({'schema_version': 1, 'http_host': '0.0.0.0', 'allow_plaintext_on_network': True,
                                      'public_url': 'http://office.example.invalid:10000'}), encoding='utf-8')
        install = self.root/'install'
        (install/'releases'/'r1'/'kit').mkdir(parents=True)
        (install/'current').symlink_to('releases/r1')
        (install/'releases'/'r1'/'manifest.json').write_text(json.dumps({'build_id': 'r1', 'source_commit': 'c' * 40}))
        printed = io.StringIO()
        with mock.patch.object(office_verify, 'report', return_value={'source_commit': 'c' * 40, 'build_id': 'r1', 'version': '0'}), \
                contextlib.redirect_stdout(printed):
            office_verify.main(['--install-root', str(install), '--root', str(self.root), '--port', str(port),
                                '--config', str(config)])
        record = json.loads(printed.getvalue())
        self.assertEqual(record['listener'], {'shape': 'plain-http-on-network', 'host': '0.0.0.0', 'port': port,
                                              'public_url': 'http://office.example.invalid:10000'})
        self.assertIn('web=up', record['health'])
        printed = io.StringIO()
        with mock.patch.object(office_verify, 'report', return_value={'source_commit': 'c' * 40, 'build_id': 'r1', 'version': '0'}), \
                contextlib.redirect_stdout(printed):
            office_verify.main(['--install-root', str(install), '--root', str(self.root), '--port', str(port)])
        self.assertEqual(json.loads(printed.getvalue())['listener']['shape'], 'loopback')


if __name__ == '__main__':
    unittest.main()
