"""The office service on an open port of the host (kittrial-5bb.163).

Three shapes: plain HTTP on loopback behind a proxy or a tunnel (the default, unchanged),
HTTPS on any address with a certificate and key (a self-signed one must work), and plain
HTTP on the network only with the operator's explicit setting and a warning at every start.
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
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

    def test_a_bad_configuration_file_is_named_in_the_refusal(self):
        """kittrial-5bb.168 item 3: service_config names the file for every way reading it fails."""
        missing = Path(self.tmp.name)/'missing.json'
        with self.assertRaises(ValueError) as caught:
            office.service_config(missing)
        self.assertIn(str(missing), str(caught.exception))
        self.file.write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            office.service_config(self.file)
        self.assertIn(str(self.file), str(caught.exception))
        self.assertIn('is not JSON', str(caught.exception))
        directory = Path(self.tmp.name)/'a-directory'
        directory.mkdir()
        with self.assertRaises(ValueError) as caught:
            office.service_config(directory)
        self.assertIn(str(directory), str(caught.exception))
        dangling = Path(self.tmp.name)/'dangling.json'
        dangling.symlink_to(Path(self.tmp.name)/'gone.json')
        with self.assertRaises(ValueError) as caught:
            office.service_config(dangling)
        self.assertIn(str(dangling), str(caught.exception))
        unreadable = Path(self.tmp.name)/'unreadable.json'
        unreadable.write_text('{}', encoding='utf-8')
        os.chmod(unreadable, 0)
        self.addCleanup(os.chmod, unreadable, 0o600)
        with self.assertRaises(ValueError) as caught:
            office.service_config(unreadable)
        self.assertIn(str(unreadable), str(caught.exception))

    def test_a_host_with_a_port_is_refused_and_ipv6_literals_are_not(self):
        for host in ('127.0.0.1:8443', 'office.example.invalid:443', '0.0.0.0:10000'):
            with self.subTest(host=host):
                self.assertIn('must not carry a port', self.refused(http_host=host, cert='/c', key='/k'))
        # A bare IPv6 literal has colons but no port, and stays accepted.
        self.assertEqual(self.config(http_host='fd00::5', cert='/c', key='/k')['http_host'], 'fd00::5')
        self.assertEqual(self.config(http_host='::', cert='/c', key='/k', public_url='https://o:10000')['http_host'], '::')
        self.assertEqual(self.config(http_host='::1')['http_host'], '::1')

    def test_cert_key_and_public_url_types_are_refused(self):
        for setting, value in (('cert', 7), ('cert', ['/c']), ('cert', True), ('key', 7), ('key', {'path': '/k'})):
            with self.subTest(setting=setting, value=value):
                settings = {'http_host': '10.1.2.3', 'cert': '/c', 'key': '/k'}
                settings[setting] = value
                self.assertIn('%s must be the path of a file' % setting, self.refused(**settings))
        for value in ('ftp://x', 'https:///no-host', 'office.example.invalid', '/just/a/path', '', 'http://',
                      'https://office:notaport', 7, ['u']):
            with self.subTest(public_url=value):
                self.assertIn('public_url must be an http or https URL with a host',
                              self.refused(cert='/c', key='/k', public_url=value))
        for value in ('https://office.example.invalid:8443', 'http://[::1]:8000', 'http://localhost'):
            with self.subTest(public_url=value):
                self.assertEqual(self.config(cert='/c', key='/k', public_url=value)['public_url'], value)

    def test_a_lone_zero_is_the_wildcard(self):
        """kittrial-5bb.168 item 5: 0 is the system's 0.0.0.0, so it needs a public_url too."""
        self.assertIn('listens on every address', self.refused(http_host='0', cert='/c', key='/k'))
        settings = self.config(http_host='0', cert='/c', key='/k', public_url='https://office:10000')
        self.assertEqual(settings['http_host'], '0')
        self.assertEqual(office.listener(settings, 10000)['probe'], 'https://127.0.0.1:10000/healthz')
        self.assertEqual(office.listener({'http_host': '0', 'allow_plaintext_on_network': True,
                                          'public_url': 'http://office:10000'}, 10000)['shape'], 'plain-http-on-network')

    def test_strict_transport_security_is_off_unless_the_setting_says_so(self):
        """kittrial-5bb.168 item 6: the header has a setting; nothing is sent by default."""
        script, root = Path('/release/kit/office_service.py'), Path('/runtime')
        self.assertNotIn('--strict-transport-security',
                         office.web_command({}, root, 10000, '/release/python', script))
        settings = self.config(strict_transport_security=True)
        self.assertTrue(settings['strict_transport_security'])
        self.assertIn('--strict-transport-security',
                      office.web_command(settings, root, 10000, '/release/python', script))
        self.assertEqual(self.refused(strict_transport_security='yes'), 'strict_transport_security must be true or false')

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


@unittest.skipUnless(LINUX and socket.has_ipv6, 'an IPv6 listener needs Linux and this host IPv6')
class IPv6Tests(unittest.TestCase):
    """kittrial-5bb.168 item 2: an IPv6 host starts and answers; the docs offer only what is served."""

    def serve(self, host):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        service = Service(Store(Path(tmp)/'state.json'))
        httpd = http_service.create_server(service, http_service.InProcessBackend(service), host=host, port=0,
                                           web_root=None, allow_plaintext_non_loopback=(host == '::'))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))
        return httpd

    def test_a_loopback_and_a_wildcard_ipv6_listener_start_and_answer(self):
        for host, asked in (('::1', '::1'), ('::', '::1')):
            with self.subTest(host=host):
                httpd = self.serve(host)
                self.assertEqual(httpd.socket.family, socket.AF_INET6)
                port = httpd.server_address[1]
                connection = http.client.HTTPConnection(asked, port, timeout=5)
                connection.request('GET', '/healthz')
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200, body)
                self.assertEqual(body['service'], 'orchestra-office')

    def test_the_listener_probe_brackets_an_ipv6_address(self):
        self.assertEqual(office.listener({'http_host': '::1'}, 10000)['probe'], 'http://[::1]:10000/healthz')
        self.assertEqual(office.listener({'http_host': '::', 'cert': '/c', 'key': '/k',
                                          'public_url': 'https://o:1'}, 10000)['probe'],
                         'https://[::1]:10000/healthz')


@unittest.skipUnless(LINUX, 'the office supervisor is for Linux')
class CertificateTests(unittest.TestCase):
    """kittrial-5bb.168 item 1: an unusable pair is one sentence naming the file, before anything starts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def preflight(self, cert, key):
        with self.assertRaises(ValueError) as caught:
            office.certificate_preflight({'cert': cert, 'key': key})
        return str(caught.exception)

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the pairs are made with it')
    def test_each_way_the_pair_can_be_unusable_names_the_file(self):
        first, second = self.base/'first', self.base/'second'
        first.mkdir()
        second.mkdir()
        cert, key = self_signed(str(first))
        other_cert, other_key = self_signed(str(second), 'other.example.invalid')
        garbage = self.base/'garbage.txt'
        garbage.write_text('this is not a PEM file\n', encoding='utf-8')
        encrypted = self.base/'encrypted.key'
        subprocess.check_call([OPENSSL, 'rsa', '-in', key, '-aes256', '-passout', 'pass:secret', '-out', str(encrypted)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        unreadable = self.base/'unreadable.key'
        unreadable.write_text(Path(key).read_text(encoding='utf-8'), encoding='utf-8')
        os.chmod(unreadable, 0)
        self.addCleanup(os.chmod, unreadable, 0o600)

        said = self.preflight(str(self.base/'missing.crt'), key)
        self.assertIn(str(self.base/'missing.crt'), said)
        self.assertIn('does not exist', said)
        said = self.preflight(cert, str(self.base/'missing.key'))
        self.assertIn(str(self.base/'missing.key'), said)
        said = self.preflight(str(garbage), key)
        self.assertIn(str(garbage), said)
        self.assertIn('not a PEM certificate', said)
        said = self.preflight(cert, str(garbage))
        self.assertIn(str(garbage), said)
        self.assertIn('not a PEM private key', said)
        said = self.preflight(cert, str(unreadable))
        self.assertIn(str(unreadable), said)
        self.assertIn('cannot be read', said)
        said = self.preflight(cert, str(encrypted))
        self.assertIn(str(encrypted), said)
        self.assertIn('needs a passphrase', said)
        said = self.preflight(other_cert, key)
        self.assertIn(str(key), said)
        self.assertIn(str(other_cert), said)
        self.assertIn('does not match', said)
        said = self.preflight(key, cert)
        self.assertIn(str(key), said)
        self.assertIn(str(cert), said)
        self.assertIn('look swapped', said)

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the pairs are made with it')
    def test_no_sentence_carries_key_text(self):
        cert, key = self_signed(self.tmp.name)
        body = [line for line in Path(key).read_text(encoding='utf-8').splitlines()
                if line and not line.startswith('-----')][0]
        said = self.preflight(cert, str(self.base/'missing.key'))
        self.assertNotIn(body, said)
        said = self.preflight(key, cert)
        self.assertNotIn(body, said)

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the pairs are made with it')
    def test_a_good_pair_passes_and_leaves_nothing_behind(self):
        cert, key = self_signed(self.tmp.name)
        office.certificate_preflight({'cert': cert, 'key': key})     # no exception

    def test_a_directory_or_a_fifo_is_refused_without_being_opened(self):
        """A FIFO would block ``open`` forever: it is refused as not a regular file."""
        directory = self.base/'a-directory'
        directory.mkdir()
        said = self.preflight(str(directory), str(self.base/'k'))
        self.assertIn(str(directory), said)
        self.assertIn('directory', said)
        fifo = self.base/'a-fifo'
        os.mkfifo(fifo)
        said = self.preflight(str(fifo), str(self.base/'k'))
        self.assertIn(str(fifo), said)
        self.assertIn('not a regular file', said)

    def test_the_key_mode_and_the_certificate_window_are_warnings_not_refusals(self):
        key = self.base/'shared.key'
        key.write_text('not a key at all\n', encoding='utf-8')
        os.chmod(key, 0o640)
        cert = self.base/'not-a-cert.pem'
        cert.write_text('not a certificate\n', encoding='utf-8')
        lines = office.start_warnings({'cert': str(cert), 'key': str(key)})
        self.assertTrue(any('readable by the group' in line for line in lines), lines)
        self.assertTrue(any('cannot be read on this interpreter' in line for line in lines), lines)
        os.chmod(key, 0o600)
        os.chmod(key, 0o604)
        self.assertTrue(any('readable by everyone' in line
                            for line in office.start_warnings({'key': str(key)})), lines)

    def test_an_expired_and_a_not_yet_valid_certificate_each_get_a_warning(self):
        cert = self.base/'cert.pem'
        cert.write_text('placeholder\n', encoding='utf-8')
        now = 1_800_000_000
        with mock.patch.object(office, 'certificate_window', return_value=(now - 100, now - 1)), \
                mock.patch.object(office.time, 'time', return_value=float(now)):
            lines = office.start_warnings({'cert': str(cert)})
        self.assertTrue(any('expired on' in line for line in lines), lines)
        with mock.patch.object(office, 'certificate_window', return_value=(now + 1, now + 100)), \
                mock.patch.object(office.time, 'time', return_value=float(now)):
            lines = office.start_warnings({'cert': str(cert)})
        self.assertTrue(any('is not valid until' in line for line in lines), lines)
        with mock.patch.object(office, 'certificate_window', return_value=None):
            lines = office.start_warnings({'cert': str(cert)})
        self.assertTrue(any('no expiry warning is given' in line for line in lines), lines)

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the certificate is made with it')
    def test_a_real_certificate_window_is_read(self):
        cert, _ = self_signed(self.tmp.name)
        window = office.certificate_window(cert)
        self.assertIsNotNone(window)
        self.assertLess(window[0], window[1])


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

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the self-signed certificate is made with it')
    def test_strict_transport_security_is_on_https_answers_only_and_only_when_asked(self):
        """kittrial-5bb.168 item 6: off by default, sent only where the operator asked and only over HTTPS."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        cert, key = self_signed(directory)
        trusting = ssl.create_default_context(cafile=cert)

        def header(port, secure):
            connection = (http.client.HTTPSConnection('127.0.0.1', port, timeout=15, context=trusting) if secure
                          else http.client.HTTPConnection('127.0.0.1', port, timeout=15))
            status, headers, body = self.call(connection, 'GET', '/healthz')
            self.assertEqual(status, 200, body)
            return headers.get('Strict-Transport-Security')

        asked = self.serve(public_url='https://office.example.invalid:10000', certfile=cert, keyfile=key, hsts=True)
        self.assertEqual(header(asked, True), http_service.HSTS_HEADER)
        default = self.serve(public_url='https://office.example.invalid:10000', certfile=cert, keyfile=key)
        self.assertIsNone(header(default, True))
        plain = self.serve(public_url='http://office.example.invalid:10000',
                           allow_plaintext_non_loopback=True, hsts=True)
        self.assertIsNone(header(plain, False))


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

    def runtime(self):
        """A bare runtime directory, for the tests that do not need a serving listener."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.root = Path(tmp)/'runtime'
        self.root.mkdir()
        return self.root

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

    def install(self):
        """A fake installed release whose provenance matches a mocked ``report``."""
        install = self.root/'install'
        (install/'releases'/'r1'/'kit').mkdir(parents=True)
        (install/'current').symlink_to('releases/r1')
        (install/'releases'/'r1'/'manifest.json').write_text(
            json.dumps({'build_id': 'r1', 'source_commit': 'c' * 40}))
        return install

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    @unittest.skipUnless(OPENSSL, 'openssl is not installed; the self-signed certificate is made with it')
    def test_health_in_main_obeys_the_config(self):
        """kittrial-5bb.168 item 7: main() asks the listener --config describes, not loopback."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        cert, key = self_signed(directory)
        port = self.serve(host='0.0.0.0', certfile=cert, keyfile=key)
        config = self.root/'office.json'
        config.write_text(json.dumps({'schema_version': 1, 'http_host': '0.0.0.0', 'cert': cert, 'key': key,
                                      'public_url': 'https://office.example.invalid:10000'}), encoding='utf-8')
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = office.main(['health', '--root', str(self.root), '--port', str(port), '--config', str(config)])
        self.assertEqual(code, 1)                                     # no database and no backup in this test
        self.assertIn('web=up', printed.getvalue())
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            office.main(['health', '--root', str(self.root), '--port', str(port)])
        self.assertIn('web=down', printed.getvalue())

    def test_a_bad_config_in_both_tools_is_one_line_naming_the_file(self):
        """kittrial-5bb.168 item 3: health and office_verify name the file and exit 1, never a traceback."""
        bad = self.runtime()/'bad.json'
        bad.write_text('{not json', encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = office.main(['health', '--root', str(self.root), '--port', '1', '--config', str(bad)])
        self.assertEqual(code, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
        self.assertIn(str(bad), err.getvalue())

        import office_verify
        install = self.install()
        err = io.StringIO()
        with mock.patch.object(office_verify, 'report', return_value={'source_commit': 'c' * 40, 'build_id': 'r1',
                                                                      'version': '0'}), \
                contextlib.redirect_stderr(err):
            code = office_verify.main(['--install-root', str(install), '--root', str(self.root), '--port', '1',
                                       '--config', str(bad)])
        self.assertEqual(code, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
        self.assertIn(str(bad), err.getvalue())
        for missing in (self.root/'gone.json', self.root):
            with self.subTest(path=missing):
                err = io.StringIO()
                with mock.patch.object(office_verify, 'report', return_value={'source_commit': 'c' * 40,
                                                                              'build_id': 'r1', 'version': '0'}), \
                        contextlib.redirect_stderr(err):
                    code = office_verify.main(['--install-root', str(install), '--root', str(self.root), '--port',
                                               '1', '--config', str(missing)])
                self.assertEqual(code, 1)
                self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
                self.assertIn(str(missing), err.getvalue())

    def test_health_reads_another_server_answering_two_hundred_as_down(self):
        """kittrial-5bb.168 item 8: /healthz carries this service's mark, so another answer is down."""
        import http.server

        class Plain(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}\n')

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Plain)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=5)))
        line, code = office.health(self.runtime(), server.server_address[1], None)
        self.assertEqual(code, 1)
        self.assertIn('web=down', line)
        self.assertIn("another program holds the port", line)

    def test_health_reads_another_orchestra_service_as_down(self):
        port = self.serve()
        other = {'service': 'orchestra-office', 'version': '9.9.9', 'build_id': 'not-this-one'}
        with mock.patch.object(http_service, '_SERVICE_IDENTITY', other):
            line, code = office.health(self.root, port, None)
        self.assertEqual(code, 1)
        self.assertIn('web=down', line)
        self.assertIn('another Orchestra service holds the port', line)
        self.assertIn('not-this-one', line)
        self.assertIn('reason=web:', line)

    def test_health_gives_a_bounded_reason_for_web_down_and_backup_unknown(self):
        """kittrial-5bb.168 item 8: the health line says why, in one bounded line."""
        line, code = office.health(self.runtime(), self.free_port(), None)
        self.assertEqual(code, 1)
        self.assertIn('web=down', line)
        self.assertIn('backup=unknown', line)
        self.assertIn('reason=web:', line)
        self.assertIn('backup:', line.split('reason=', 1)[1])
        self.assertEqual(len(line.splitlines()), 1)
        self.assertLess(len(line), 700)
        # A fresh runtime's exit 1 while the first backup is unknown is documented (docs/OFFICE_SERVICE.md).
        self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
