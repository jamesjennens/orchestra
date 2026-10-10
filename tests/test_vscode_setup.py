"""kittrial-5bb.203 review 01a12408: the certificate route, the fingerprint and the set-up prompt.

Three surfaces:

* the service's own public leaf certificate at ``GET /v1/service/certificate``, served without a
  log-in by an https service that was given a certificate, with nothing served by a plain-http
  one, and never anything but the leaf (item 1: a combined key-and-certificate PEM, a chain, a
  64 MB file, an ``openssl x509 -text`` dump, a ``TRUSTED CERTIFICATE`` block and a block-like
  comment must not change what the route answers, and a Latin-1 comment must not stop the
  service from starting -- item 3b);
* the My agents dialog's set-up prompt (``web/js/agentSetup.js``): the two files in
  ``.orchestra/``, the ``# server = ADDRESS`` (and fingerprint) lines written into the credential
  file's own content, idempotently, and NO ``.vscode/settings.json`` and no certificate file
  (item 2: the extension's own "Orchestra Bridge: Set up" writes the settings from that address);
* the fingerprint in the dialog and the docs: what comparing it proves and what it does not.

The fingerprint is checked against ``openssl x509 -noout -fingerprint -sha256`` where openssl
exists, because the point of the string is that a person compares it with what the extension
shows. The JavaScript is run under node where node is installed; the static checks carry the same
rules where it is not.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http_service  # noqa: E402
from http_auth import Service, Store  # noqa: E402
from http_service import (MAX_CERTIFICATE_FILE_BYTES, certificate_fingerprint,  # noqa: E402
                          create_server, service_certificate_pem)
from test_office_listener import OPENSSL, self_signed  # noqa: E402
from test_http_service import unique_dir  # noqa: E402
from test_http_web import WEB, run_node_module  # noqa: E402

SETUP_JS = WEB / 'js' / 'agentSetup.js'
AGENTS_JS = WEB / 'js' / 'views' / 'agents.js'
API_JS = WEB / 'js' / 'api.js'
ADMIN, PASSWORD = 'root-admin', 'correct horse battery staple 1'
BASH = shutil.which('bash')
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')
ONE_MIB = 1 << 20

#: A throwaway pair of blocks, for the tests that never reach a socket. Not a certificate.
SAMPLE_PEM = ('-----BEGIN CERTIFICATE-----\n'
              + base64.b64encode(b'not a real certificate').decode()
              + '\n-----END CERTIFICATE-----\n')
#: A comment whose own text looks like the start of a block, before the real one: the reviewer's
#: variant (k). The first match is not base64, so no leaf can be produced from it.
BLOCK_LIKE_COMMENT = ('# example: -----BEGIN CERTIFICATE-----\n'
                      'not base64 !!\n'
                      '-----END CERTIFICATE-----\n')


def pem_blocks(text):
    """Every ``-----BEGIN CERTIFICATE-----`` body in a text, whitespace removed."""
    return [''.join(match.split())
            for match in re.findall(r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----', text, re.S)]


def der_of(text, which=0):
    """The DER of one certificate block of a PEM text."""
    return base64.b64decode(pem_blocks(text)[which])


def colon_fingerprint(certificate, which=0):
    """The SHA-256 fingerprint computed here, independently of the service's own code."""
    blocks = re.findall(r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----', certificate, re.S)
    der = base64.b64decode(''.join(blocks[which].split()))
    return ':'.join('%02X' % byte for byte in hashlib.sha256(der).digest())


def colon_of_der(der):
    """The same string, from DER bytes (what the handshake and openssl give)."""
    return ':'.join('%02X' % byte for byte in hashlib.sha256(der).digest())


def make_cert(directory, stem, name):
    """A self-signed certificate and key with an explicit file stem, so a chain is possible."""
    cert, key = Path(directory) / (stem + '.crt'), Path(directory) / (stem + '.key')
    subprocess.check_call([OPENSSL, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                           '-keyout', str(key), '-out', str(cert), '-subj', '/CN=' + name,
                           '-addext', 'subjectAltName=DNS:%s,IP:127.0.0.1' % name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


class CertificateFingerprintCase(unittest.TestCase):
    """The helper itself: the shape the extension shows, and a refusal that is not a certificate."""

    def test_the_shape_is_what_the_dialog_and_the_extension_compare(self):
        fingerprint = certificate_fingerprint(SAMPLE_PEM)
        self.assertRegex(fingerprint, r'^[0-9A-F]{2}(:[0-9A-F]{2}){31}$')
        self.assertEqual(colon_fingerprint(SAMPLE_PEM), fingerprint)
        self.assertEqual(fingerprint, certificate_fingerprint(SAMPLE_PEM))

    def test_the_leaf_is_used_when_a_chain_follows_and_only_pem_is_accepted(self):
        self.assertEqual(certificate_fingerprint(SAMPLE_PEM), certificate_fingerprint(SAMPLE_PEM + SAMPLE_PEM))
        for bad in ('', None, 'no certificate here',
                    '-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                certificate_fingerprint(bad)


class CertificateFileShapesCase(unittest.TestCase):
    """``service_certificate_pem``: every shape of file ``--cert`` may name (item 1, no socket)."""

    def setUp(self):
        if not OPENSSL:
            self.skipTest('openssl is not installed; the certificate fixtures are made with it')
        self.tmp = unique_dir('vscode-shapes-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.leaf, self.leaf_key = make_cert(self.tmp, 'leaf', 'leaf.example.invalid')
        self.middle, _ = make_cert(self.tmp, 'middle', 'middle.example.invalid')
        self.root_cert, _ = make_cert(self.tmp, 'root', 'root.example.invalid')
        self.leaf_pem = self.leaf.read_text(encoding='ascii')

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        return path

    def test_separate_files_serve_the_leaf_re_encoded_by_the_kit(self):
        served = service_certificate_pem(str(self.leaf))
        self.assertEqual(der_of(self.leaf_pem), der_of(served))
        self.assertEqual(1, len(pem_blocks(served)))
        self.assertNotIn('PRIVATE KEY', served)

    def test_a_combined_pem_serves_only_the_certificate_key_first_and_certificate_first(self):
        key = self.leaf_key.read_text(encoding='ascii')
        for name, combined in (('keyfirst.pem', key + self.leaf_pem),
                               ('certfirst.pem', self.leaf_pem + key)):
            with self.subTest(name=name):
                served = service_certificate_pem(str(self.write(name, combined)))
                self.assertEqual(der_of(self.leaf_pem), der_of(served))
                self.assertNotIn('PRIVATE KEY', served)
                self.assertNotIn(base64.b64encode(b'PRIVATE KEY').decode(), served)

    def test_the_same_combined_file_as_certificate_and_key_serves_no_key(self):
        key = self.leaf_key.read_text(encoding='ascii')
        combined = self.write('both.pem', key + self.leaf_pem)
        served = service_certificate_pem(str(combined))     # create_server passes it as both
        self.assertEqual(der_of(self.leaf_pem), der_of(served))
        self.assertNotIn('PRIVATE KEY', served)

    def test_a_real_chain_serves_the_first_certificate_not_the_last(self):
        # Three DISTINCT blocks (the old test used the same block twice, so a mutant that took the
        # last one of a chain survived: review item 4).
        chain = self.write('chain.pem', self.leaf_pem
                           + self.middle.read_text(encoding='ascii')
                           + self.root_cert.read_text(encoding='ascii'))
        served = service_certificate_pem(str(chain))
        self.assertEqual(der_of(self.leaf_pem), der_of(served))
        self.assertNotEqual(der_of(served, 0), der_of(self.root_cert.read_text(encoding='ascii')))
        self.assertEqual(colon_fingerprint(self.leaf_pem), certificate_fingerprint(served))

    def test_a_huge_file_is_read_only_up_to_the_cap_and_serves_one_leaf(self):
        huge = self.write('huge.pem', self.leaf_pem + '# filler\n' * (64 * ONE_MIB // 9))
        self.assertGreater(huge.stat().st_size, 64 * ONE_MIB)
        served = service_certificate_pem(str(huge))
        self.assertEqual(der_of(self.leaf_pem), der_of(served))
        self.assertLess(len(served), 8192)                 # never as large as the file

    def test_a_certificate_beyond_the_read_cap_cannot_be_produced(self):
        beyond = self.write('beyond.pem', '# filler\n' * ((MAX_CERTIFICATE_FILE_BYTES + ONE_MIB) // 9)
                            + self.leaf_pem)
        self.assertGreater(beyond.stat().st_size, MAX_CERTIFICATE_FILE_BYTES)
        self.assertIsNone(service_certificate_pem(str(beyond)))

    def test_an_openssl_text_dump_is_never_echoed(self):
        text = subprocess.run([OPENSSL, 'x509', '-in', str(self.leaf), '-noout', '-text'],
                              capture_output=True, text=True, check=True).stdout
        dumped = self.write('text.pem', text + self.leaf_pem)
        served = service_certificate_pem(str(dumped))
        self.assertEqual(der_of(self.leaf_pem), der_of(served))
        self.assertNotIn('Certificate:', served)
        self.assertNotIn('Signature Algorithm', served)
        self.assertNotIn(text.strip()[:40], served)

    def test_a_trusted_certificate_block_cannot_produce_a_leaf(self):
        trusted = self.write('trusted.pem', self.leaf_pem.replace('BEGIN CERTIFICATE', 'BEGIN TRUSTED CERTIFICATE')
                             .replace('END CERTIFICATE', 'END TRUSTED CERTIFICATE'))
        self.assertIsNone(service_certificate_pem(str(trusted)))

    def test_a_block_like_comment_first_cannot_produce_a_leaf(self):
        commented = self.write('comment.pem', BLOCK_LIKE_COMMENT + self.leaf_pem)
        self.assertIsNone(service_certificate_pem(str(commented)))

    def test_a_latin1_comment_is_read_without_failing(self):
        latin = self.write('latin1.pem', b'# Zertifikat f\xfcr B\xfcro\n' + self.leaf_pem.encode('ascii'))
        served = service_certificate_pem(str(latin))
        self.assertEqual(der_of(self.leaf_pem), der_of(served))

    def test_crlf_line_ends_are_fine(self):
        crlf = self.write('crlf.pem', self.leaf_pem.replace('\n', '\r\n'))
        self.assertEqual(der_of(self.leaf_pem), der_of(service_certificate_pem(str(crlf))))

    def test_a_file_that_is_not_there_yields_nothing(self):
        self.assertIsNone(service_certificate_pem(str(self.tmp / 'not-there.pem')))

    @unittest.skipUnless(os.name == 'posix', 'file modes are a POSIX property')
    def test_an_unreadable_file_yields_nothing_instead_of_raising(self):
        path = self.write('unreadable.pem', self.leaf_pem)
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        self.assertIsNone(service_certificate_pem(str(path)))


class CertificateRouteCase(unittest.TestCase):
    """A real TLS listener: the route answers anonymously, and only ever with the leaf."""

    def setUp(self):
        if not OPENSSL:
            self.skipTest('openssl is not installed; the self-signed certificate is made with it')
        self.tmp = unique_dir('vscode-route-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.leaf, self.leaf_key = make_cert(self.tmp, 'leaf', 'leaf.example.invalid')
        self.middle, _ = make_cert(self.tmp, 'middle', 'middle.example.invalid')
        self.root_cert, _ = make_cert(self.tmp, 'root', 'root.example.invalid')
        self.leaf_pem = self.leaf.read_text(encoding='ascii')

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        return path

    def serve(self, certfile, keyfile=None, tls=True):
        """A real listener on 127.0.0.1, https with the given files or plain http when not tls."""
        state = unique_dir('vscode-state-')
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        store = Store(state / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, PASSWORD)
        service = Service(store)
        options = {'certfile': str(certfile)} if tls else {}
        if tls and keyfile is not None:
            options['keyfile'] = str(keyfile)
        httpd = create_server(service, http_service.InProcessBackend(service), host='127.0.0.1', port=0, **options)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))
        return httpd.server_address[1]

    def request(self, tls, port, path, cert=None, method='GET', body=None, headers=None):
        """Return (status, body, peer DER): the peer certificate is the handshake's own leaf."""
        if tls:
            context = (ssl.create_default_context(cafile=str(cert)) if cert
                       else ssl._create_unverified_context())
            connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=15, context=context)
        else:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            # The peer leaf is read before the body, while the connection is certainly open.
            peer = connection.sock.getpeercert(binary_form=True) if (tls and connection.sock is not None) else None
            response = connection.getresponse()
            data = response.read()
            return response.status, data, peer
        finally:
            connection.close()

    def answer(self, status, body):
        self.assertEqual(200, status, body)
        return json.loads(body)

    def test_an_https_service_serves_only_the_leaf_without_a_log_in(self):
        port = self.serve(self.leaf, self.leaf_key)
        status, body, peer = self.request(True, port, '/v1/service/certificate', self.leaf)
        answer = self.answer(status, body)
        self.assertEqual(der_of(self.leaf_pem), der_of(answer['certificate']))
        self.assertNotIn('PRIVATE KEY', answer['certificate'])
        self.assertEqual(colon_of_der(peer), answer['sha256'])              # the handshake's own leaf
        self.assertEqual(self.openssl_fingerprint(self.leaf_pem), answer['sha256'])
        key = self.leaf_key.read_text(encoding='ascii')
        self.assertNotIn(key.strip(), body.decode())
        self.assertNotIn('PRIVATE KEY', body.decode())

    def openssl_fingerprint(self, certificate):
        """What openssl prints for the same certificate: the string a person compares."""
        path = self.write('openssl-fingerprint.pem', certificate)
        out = subprocess.run([OPENSSL, 'x509', '-in', str(path), '-noout', '-fingerprint', '-sha256'],
                             capture_output=True, text=True, check=True).stdout
        return out.split('=', 1)[1].strip()

    def test_a_combined_certificate_file_starts_and_serves_no_key(self):
        key = self.leaf_key.read_text(encoding='ascii')
        for name, combined in (('keyfirst.pem', key + self.leaf_pem),
                               ('certfirst.pem', self.leaf_pem + key)):
            with self.subTest(name=name):
                path = self.write(name, combined)
                port = self.serve(path)                     # --cert alone, as office_service does
                status, body, _ = self.request(True, port, '/v1/service/certificate')
                answer = self.answer(status, body)
                self.assertEqual(der_of(self.leaf_pem), der_of(answer['certificate']))
                self.assertNotIn('PRIVATE KEY', body.decode())
                self.assertEqual(colon_fingerprint(self.leaf_pem), answer['sha256'])

    def test_the_same_combined_file_as_certificate_and_key_serves_no_key(self):
        key = self.leaf_key.read_text(encoding='ascii')
        path = self.write('both.pem', key + self.leaf_pem)
        port = self.serve(path, path)
        status, body, _ = self.request(True, port, '/v1/service/certificate')
        answer = self.answer(status, body)
        self.assertEqual(der_of(self.leaf_pem), der_of(answer['certificate']))
        self.assertNotIn('PRIVATE KEY', body.decode())

    def test_a_chain_answers_the_leaf_fingerprint(self):
        chain = self.write('chain.pem', self.leaf_pem
                           + self.middle.read_text(encoding='ascii')
                           + self.root_cert.read_text(encoding='ascii'))
        port = self.serve(chain, self.leaf_key)
        status, body, peer = self.request(True, port, '/v1/service/certificate')
        answer = self.answer(status, body)
        self.assertEqual(der_of(self.leaf_pem), der_of(answer['certificate']))
        self.assertEqual(colon_of_der(peer), answer['sha256'])
        self.assertEqual(colon_fingerprint(self.leaf_pem), answer['sha256'])

    def test_a_huge_file_starts_and_answers_one_small_leaf(self):
        huge = self.write('huge.pem', self.leaf_pem + '# filler\n' * (64 * ONE_MIB // 9))
        port = self.serve(huge, self.leaf_key)
        status, body, _ = self.request(True, port, '/v1/service/certificate')
        answer = self.answer(status, body)
        self.assertLess(len(body), 8192)
        self.assertEqual(der_of(self.leaf_pem), der_of(answer['certificate']))

    def test_a_latin1_comment_file_starts_and_serves_the_leaf(self):
        latin = self.write('latin1.pem', b'# Zertifikat f\xfcr B\xfcro\n' + self.leaf_pem.encode('ascii'))
        port = self.serve(latin, self.leaf_key)             # main starts on this file; the tip did not
        status, body, _ = self.request(True, port, '/v1/service/certificate')
        self.assertEqual(der_of(self.leaf_pem), der_of(self.answer(status, body)['certificate']))

    def test_a_file_that_cannot_produce_a_leaf_answers_404_not_500(self):
        trusted = self.write('trusted.pem', self.leaf_pem.replace('BEGIN CERTIFICATE', 'BEGIN TRUSTED CERTIFICATE')
                             .replace('END CERTIFICATE', 'END TRUSTED CERTIFICATE'))
        commented = self.write('comment.pem', BLOCK_LIKE_COMMENT + self.leaf_pem)
        for name, path in (('trusted', trusted), ('comment-like-block', commented)):
            with self.subTest(name=name):
                port = self.serve(path, self.leaf_key)
                status, body, _ = self.request(True, port, '/v1/service/certificate')
                self.assertEqual(404, status, body)         # never 500
                status, body, _ = self.request(True, port, '/healthz')
                self.assertEqual(200, status, body)         # the service is up

    def test_a_plain_http_service_serves_nothing_there(self):
        port = self.serve(self.leaf, self.leaf_key, tls=False)
        status, body, _ = self.request(False, port, '/v1/service/certificate')
        self.assertEqual(404, status, body)
        status, body, _ = self.request(False, port, '/healthz')
        self.assertEqual(200, status, body)                 # the route is absent, not the service

    def test_the_route_is_a_read_and_still_refuses_anything_else(self):
        port = self.serve(self.leaf, self.leaf_key)
        connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=15,
                                                 context=ssl._create_unverified_context())
        try:
            connection.request('POST', '/v1/service/certificate', body='{}',
                               headers={'Content-Type': 'application/json'})
            self.assertEqual(404, connection.getresponse().status)
        finally:
            connection.close()


class SetupPromptCase(unittest.TestCase):
    """The prompt's own text, run under node against the real module (item 2 and item 3a)."""

    SERVER = 'https://office.example.invalid:7443'
    SCRIPT = r"""
const m = await import(process.argv[1]);
const payload = m.secretlessPayload({ agent: { id: 'agent_1', name: 'Kestrel', owner_display_name: 'Olive',
  working_directory: 'C:\\Users\\olive\\kestrel', projects: ['proj_a', 'proj_b'] } }, 'https://ignored.example.invalid');
const dove = m.secretlessPayload({ agent: { id: 'agent_3', name: 'Dove', owner_display_name: 'Olive',
  working_directory: null, projects: [] } }, 'https://ignored.example.invalid');
const certificate = { certificate: '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n',
  sha256: 'AB:CD:EF' };
const server = 'https://office.example.invalid:7443';
const worker = m.setupPrompt(payload, { bridgeServer: server, certificate });
const reviewer = m.setupPrompt(payload, { role: 'reviewer', bridgeServer: server, certificate });
const plain = m.setupPrompt(dove, { bridgeServer: server });
const line = (text, label) => (text.match(new RegExp('^   - ' + label + ': (.*)$', 'm')) || [])[1] || null;
console.log(JSON.stringify({
  workerText: worker, reviewerText: reviewer, plainText: plain,
  addressComment: worker.includes('# server = ' + server),
  fingerprintComment: worker.includes('# server certificate sha256 = AB:CD:EF'),
  noFingerprintWithoutCertificate: !plain.includes('# server certificate'),
  writesSettings: /settings\.json|orchestraBridge|caFile/.test(worker),
  carriesPem: worker.includes('MIIB'),
  roleWorker: worker.includes('role is worker'),
  roleReviewer: reviewer.includes('role is reviewer'),
  posixCommand: line(worker, 'macOS/Linux'),
  powershellCommand: line(worker, 'PowerShell'),
  commentLines: m.credentialCommentLines(server, 'AB:CD:EF'),
  certFile: m.certificateFile('Kestrel (agent of Olive)'),
}));
"""

    def run_script(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; the static checks below still apply')
        out = run_node_module(self, node, self.SCRIPT, SETUP_JS.as_uri())
        self.assertEqual(0, out.returncode, out.stderr)
        return json.loads(out.stdout)

    def test_the_prompt_writes_no_settings_and_carries_no_certificate(self):
        result = self.run_script()
        self.assertFalse(result['writesSettings'],
                         'the prompt must not mention .vscode/settings.json, orchestraBridge or caFile')
        self.assertFalse(result['carriesPem'], 'the certificate PEM must not ride in the prompt')

    def test_the_credential_file_gets_the_address_and_the_fingerprint_lines(self):
        result = self.run_script()
        self.assertTrue(result['addressComment'])
        self.assertTrue(result['fingerprintComment'])
        self.assertTrue(result['noFingerprintWithoutCertificate'])
        self.assertEqual(['# server = ' + self.SERVER, '# server certificate sha256 = AB:CD:EF'],
                         result['commentLines'])

    def test_the_prompt_names_the_window_role(self):
        result = self.run_script()
        self.assertTrue(result['roleWorker'])
        self.assertTrue(result['roleReviewer'])

    def test_the_rewrite_command_replaces_instead_of_appending(self):
        result = self.run_script()
        posix, powershell = result['posixCommand'], result['powershellCommand']
        self.assertIsNotNone(posix)
        self.assertTrue(posix.startswith("printf '%s\\n'"), posix)
        # A rewrite: remove any earlier comment line, keep the header line, and re-enter the file
        # through a mode-600 temporary file (item 3a). A plain `>>` append is what M8 was.
        self.assertIn('grep -vE', posix)
        self.assertIn('chmod 600', posix)
        self.assertIn('mv ', posix)
        self.assertIn('.orchestra-agent-kestrel.curlrc.new', posix)   # only the temp file is appended to
        self.assertIsNotNone(powershell)
        self.assertIn('Regex]::Replace', powershell)
        self.assertIn('WriteAllText', powershell)
        self.assertNotIn('Add-Content', powershell)          # the old append-every-time command
        self.assertTrue(result['certFile']['name'].endswith('.crt'))


class CredentialFileRewriteCase(unittest.TestCase):
    """The prompt's own POSIX command, run twice against a Notepad-style file (item 3a)."""

    HEADER = 'header = "Authorization: Bearer SECRET-XYZ-123"'

    def prompt_commands(self):
        """The prompt's own two rewrite commands, read out of the real module under node."""
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; SetupPromptCase carries the static rule')
        script = ("const m = await import(process.argv[1]);"
                  "const p = m.secretlessPayload({agent:{id:'a',name:'Kestrel',projects:['p1']}},"
                  "'https://ignored.example.invalid');"
                  "const c = {certificate:'-----BEGIN CERTIFICATE-----\\nMIIB\\n-----END CERTIFICATE-----\\n',"
                  "sha256:'AB:CD:EF'};"
                  "const text = m.setupPrompt(p,{bridgeServer:'https://office.example.invalid:7443',certificate:c});"
                  "const line = (l) => (text.match(new RegExp('^   - ' + l + ': (.*)$','m'))||[])[1]||'';"
                  "console.log(JSON.stringify({posix: line('macOS/Linux'), powershell: line('PowerShell')}))")
        out = run_node_module(self, node, script, SETUP_JS.as_uri())
        self.assertEqual(0, out.returncode, out.stderr)
        return json.loads(out.stdout)

    def prompt_posix_command(self):
        commands = self.prompt_commands()
        if os.name != 'posix' or not BASH:
            # On Windows `bash` is the WSL stub, whose HOME and filesystem are not this test's;
            # the authoritative run of the prompt's own line is the Linux one.
            self.skipTest('the POSIX command is run where a POSIX shell and HOME are the real ones')
        command = commands['posix']
        self.assertTrue(command.startswith("printf '%s\\n'"), command)
        return command

    def prompt_powershell_command(self):
        commands = self.prompt_commands()
        if os.name != 'nt' or not POWERSHELL:
            self.skipTest('the PowerShell command is run where PowerShell is the real one')
        command = commands['powershell']
        self.assertTrue(command.startswith('$p='), command)
        return command

    def run_command(self, command, home):
        env = dict(os.environ, HOME=str(home))
        subprocess.run([BASH, '-c', command], check=True, env=env, capture_output=True, text=True)

    def assert_file_shape(self, path):
        text = path.read_text(encoding='utf-8')
        lines = text.split('\n')
        address = [line for line in lines if re.fullmatch(r'#\s*server\s*=\s*https?://\S+', line)]
        fingerprint = [line for line in lines if re.fullmatch(r'#\s*server certificate sha256 = \S+', line)]
        self.assertEqual(1, len(address), text)
        self.assertEqual(1, len(fingerprint), text)
        # The header line is a complete line of its own: the Notepad file has no final newline, and
        # an append onto it produced `header = "..."# server = ...` on one line (review item 3a).
        self.assertIn(self.HEADER, lines, text)
        self.assertEqual([], [line for line in lines if self.HEADER in line and line != self.HEADER], text)

    def test_the_command_is_idempotent_and_survives_a_missing_final_newline(self):
        command = self.prompt_posix_command()
        home = unique_dir('vscode-home-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = home / '.orchestra-agent-kestrel.curlrc'
        for name, first in (('no-final-newline', self.HEADER), ('with-final-newline', self.HEADER + '\n')):
            with self.subTest(name=name):
                path.write_text(first, encoding='utf-8')
                self.run_command(command, home)
                self.assert_file_shape(path)
                after_first = path.read_text(encoding='utf-8')
                self.run_command(command, home)
                self.assert_file_shape(path)
                self.assertEqual(after_first, path.read_text(encoding='utf-8'))

    def test_the_command_leaves_a_missing_credential_file_alone(self):
        command = self.prompt_posix_command()
        home = unique_dir('vscode-home-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = home / '.orchestra-agent-kestrel.curlrc'
        self.run_command(command, home)
        self.assertFalse(path.exists())                     # nothing half-written

    def test_the_powershell_command_is_idempotent_too(self):
        command = self.prompt_powershell_command()
        home = unique_dir('vscode-pshome-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = home / '.orchestra-agent-kestrel.curlrc'
        path.write_text(self.HEADER, encoding='utf-8')      # Notepad: no final newline
        env = dict(os.environ, USERPROFILE=str(home))
        for run in range(2):
            with self.subTest(run=run):
                out = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', command],
                                     env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(0, out.returncode, out.stderr)
                self.assert_file_shape(path)

    def test_curl_reads_the_file_the_command_wrote(self):
        if not shutil.which('curl'):
            self.skipTest('curl is not installed')
        command = self.prompt_posix_command()
        home = unique_dir('vscode-home-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = home / '.orchestra-agent-kestrel.curlrc'
        path.write_text(self.HEADER, encoding='utf-8')      # Notepad: no final newline
        self.run_command(command, home)
        out = subprocess.run([BASH, '-c', 'curl -K "$HOME/.orchestra-agent-kestrel.curlrc" --version'],
                             env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)     # curl parsed the config file


class TheDialogAndThePageCase(unittest.TestCase):
    """Static checks of what the page offers, where node may not be installed at all."""

    def test_the_dialog_says_what_is_written_and_what_is_left_to_the_person(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        self.assertIn("export const SETUP_WRITES_LINE = 'The prompt writes .orchestra/agent.json", agents)
        self.assertIn("export const SETUP_ONCE_LINE = 'What is still yours, once: install the Orchestra "
                      "Bridge extension in VS Code, run “Orchestra Bridge: Set up”", agents)
        self.assertIn("h('p', { class: 'small', 'data-writes': 'setup' }, SETUP_WRITES_LINE)", agents)
        self.assertIn("h('p', { class: 'small', 'data-once': 'true' }, SETUP_ONCE_LINE)", agents)
        setup = SETUP_JS.read_text(encoding='utf-8')
        self.assertIn("export const BRIDGE_ROLES = Object.freeze(['worker', 'reviewer']);", setup)

    def test_the_page_writes_no_settings_and_no_ca_file(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        setup = SETUP_JS.read_text(encoding='utf-8')
        for source, name in ((agents, 'agents.js'), (setup, 'agentSetup.js')):
            for gone in ('orchestraBridge', 'caFile', 'VSCODE_SETTINGS_PATH', 'mergeSettings',
                         'SETTINGS_REFUSED_SENTENCE', 'bridgeSettings'):
                self.assertNotIn(gone, source, '%s still mentions %s' % (name, gone))
        # The save path writes the two .orchestra files and the .gitignore line, and nothing else.
        save = re.search(r'async function saveSetupFiles\(files\) \{.*?\n\}', agents, re.S).group(0)
        self.assertNotIn('.vscode', save)
        self.assertNotIn('settings.json', save)
        self.assertIn("getFileHandle('.gitignore')", save)
        self.assertIn('.orchestra/', save)

    def test_the_page_asks_for_the_certificate_and_offers_the_save(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        self.assertIn("const data = await ctx.api.certificate(); return data && data.certificate ? data : null;", agents)
        self.assertIn("'data-fingerprint': certificate.sha256", agents)
        self.assertIn("onclick: () => saveCertificate(certificate, payload.certificateFile)", agents)
        self.assertIn("const blob = new Blob([certificate.certificate], { type: 'application/x-pem-file' });", agents)
        self.assertIn("download: file.name", agents)
        # The certificate block is only built when the service has one.
        self.assertIn("const certificateBlock = certificate ? h('section'", agents)
        # The role is still chosen here, one window is one role.
        self.assertIn("What this agent does in this window: ", agents)
        self.assertIn("h('option', { value: 'reviewer' }, 'reviews other agents’ deliveries')", agents)
        api = API_JS.read_text(encoding='utf-8')
        self.assertIn("certificate: () => call('GET', '/v1/service/certificate')", api)

    def test_the_dialog_says_what_the_fingerprint_comparison_proves(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        # The one line the person acts on (review item 2) ...
        self.assertIn('In VS Code run “Orchestra Bridge: Set up”: it will show this fingerprint', agents)
        # ... and what comparing it does and does not prove (review item 4).
        self.assertIn('What that comparison proves: the extension reached the same endpoint this page did', agents)
        self.assertIn('anyone who can serve this page can serve his own certificate and his own fingerprint', agents)
        self.assertIn('openssl x509 -in <the certificate file> -noout -fingerprint -sha256', agents)
        docs = (ROOT / 'docs' / 'HTTP_DEPLOYMENT.md').read_text(encoding='utf-8')
        self.assertIn('openssl x509 -in <the certificate file> -noout -fingerprint -sha256', docs)
        self.assertIn('a third party who can serve the page', docs)
        self.assertIn('serves his own certificate and his own fingerprint', docs)

    def test_the_settings_never_hold_the_secret_and_the_rewrite_never_prints_it(self):
        setup = SETUP_JS.read_text(encoding='utf-8')
        rewrite = re.search(r'export function credentialFileCommand\(file, comments\) \{.*?\n\}', setup, re.S).group(0)
        self.assertIn('grep -vE', rewrite)
        self.assertIn('chmod 600', rewrite)
        self.assertIn('Regex]::Replace', rewrite)
        self.assertIsNone(re.search(r'\bsecret\b|\bcredential\b', re.sub(r"'[^']*'", "''", rewrite)))
        prompt = re.search(r'export function setupPrompt\(payload, \{.*?\n\}', setup, re.S).group(0)
        self.assertIn('The first line is the address the extension reads, so nobody types it', prompt)
        self.assertIn('it never prints or copies what the file holds', prompt)
        self.assertIn('the kit writes no settings file', prompt)
        self.assertNotIn('settings.json', prompt)
        self.assertNotIn('caFile', prompt)

    def test_the_route_table_lists_the_certificate_route(self):
        design = (ROOT / 'docs' / 'HTTP_TRANSPORT_DESIGN.md').read_text(encoding='utf-8')
        self.assertIn('| `GET /v1/service/certificate` |', design)


if __name__ == '__main__':
    unittest.main()
