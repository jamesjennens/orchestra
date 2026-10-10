"""kittrial-5bb.203: setting up an agent for VS Code needs no hand-made settings.

Two surfaces:

* the service's own public certificate at ``GET /v1/service/certificate``, served without a
  log-in by an https service that was given a certificate, with nothing served by a
  plain-http one, and never the key;
* the set-up prompt of the My agents dialog (``web/js/agentSetup.js``) writing the VS Code
  workspace settings the Orchestra Bridge extension reads, merging with what is already
  there, leaving a file that is not valid JSON alone, and never carrying the credential.

The fingerprint is checked against ``openssl x509 -noout -fingerprint -sha256`` where openssl
exists, because the point of the string is that a person compares it with what the extension
shows. The JavaScript is run under node where node is installed; the static checks carry the
same rules where it is not (the authoritative Linux host has no node).
"""
import base64
import hashlib
import http.client
import json
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http_service  # noqa: E402
from http_auth import Service, Store  # noqa: E402
from http_service import certificate_fingerprint, create_server  # noqa: E402
from test_office_listener import OPENSSL, self_signed  # noqa: E402
from test_http_service import unique_dir  # noqa: E402
from test_http_web import WEB, run_node_module  # noqa: E402

SETUP_JS = WEB / 'js' / 'agentSetup.js'
AGENTS_JS = WEB / 'js' / 'views' / 'agents.js'
API_JS = WEB / 'js' / 'api.js'
ADMIN, PASSWORD = 'root-admin', 'correct horse battery staple 1'

#: A throwaway pair of blocks, for the tests that never reach a socket. Not a certificate.
SAMPLE_PEM = ('-----BEGIN CERTIFICATE-----\n'
              + base64.b64encode(b'not a real certificate').decode()
              + '\n-----END CERTIFICATE-----\n')


def colon_fingerprint(certificate, which=0):
    """The SHA-256 fingerprint computed here, independently of the service's own code."""
    blocks = re.findall(r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----', certificate, re.S)
    der = base64.b64decode(''.join(blocks[which].split()))
    return ':'.join('%02X' % byte for byte in hashlib.sha256(der).digest())


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


class CertificateRouteCase(unittest.TestCase):
    """A real TLS listener: the route answers anonymously, and a plain one serves nothing."""

    def serve(self, tls):
        tmp = unique_dir('vscode-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, PASSWORD)
        service = Service(store)
        certificate = None
        options = {}
        if tls:
            cert, key = self_signed(str(tmp))
            certificate = Path(cert).read_text(encoding='utf-8')
            options = {'certfile': cert, 'keyfile': key}
        httpd = create_server(service, http_service.InProcessBackend(service), host='127.0.0.1', port=0, **options)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))
        return tmp, certificate, httpd.server_address[1]

    def client(self, tls, port, cert=None):
        if tls:
            return http.client.HTTPSConnection('127.0.0.1', port, timeout=15,
                                               context=ssl.create_default_context(cafile=cert))
        return http.client.HTTPConnection('127.0.0.1', port, timeout=15)

    def get(self, tls, port, path, cert=None):
        connection = self.client(tls, port, cert)
        try:
            connection.request('GET', path)          # deliberately no Authorization header
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_an_https_service_serves_its_own_certificate_without_a_log_in(self):
        tmp, certificate, port = self.serve(tls=True)
        status, body = self.get(True, port, '/v1/service/certificate', str(tmp / 'office.crt'))
        self.assertEqual(200, status, body)
        answer = json.loads(body)
        self.assertEqual(certificate, answer['certificate'])
        self.assertEqual(colon_fingerprint(certificate), answer['sha256'])
        self.assertEqual(self.openssl_fingerprint(certificate), answer['sha256'])
        # Only the public half: the key never appears, and neither does the state.
        key = (tmp / 'office.key').read_text(encoding='utf-8')
        self.assertNotIn(key.strip(), body.decode())
        self.assertNotIn('PRIVATE KEY', body.decode())

    def openssl_fingerprint(self, certificate):
        """What openssl prints for the same certificate: the string a person compares."""
        if not OPENSSL:
            self.skipTest('openssl is not installed: the fingerprint is compared with the kit only')
        tmp = unique_dir('vscode-ossl-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / 'cert.pem'
        path.write_text(certificate, encoding='utf-8')
        out = subprocess.run([OPENSSL, 'x509', '-in', str(path), '-noout', '-fingerprint', '-sha256'],
                             capture_output=True, text=True, check=True).stdout
        return out.split('=', 1)[1].strip()

    def test_a_plain_http_service_serves_nothing_there(self):
        _, _, port = self.serve(tls=False)
        status, body = self.get(False, port, '/v1/service/certificate')
        self.assertEqual(404, status, body)
        status, body = self.get(False, port, '/healthz')
        self.assertEqual(200, status, body)          # the route is absent, not the service

    def test_the_route_is_a_read_and_still_refuses_anything_else(self):
        tmp, _, port = self.serve(tls=True)
        connection = self.client(True, port, str(tmp / 'office.crt'))
        try:
            connection.request('POST', '/v1/service/certificate', body='{}',
                               headers={'Content-Type': 'application/json'})
            self.assertEqual(404, connection.getresponse().status)
        finally:
            connection.close()


class PromptWritesTheWorkspaceSettingsCase(unittest.TestCase):
    """The prompt's own settings block, run under node against the real module."""

    SCRIPT = r"""
const m = await import(process.argv[1]);
const payload = m.secretlessPayload({ agent: { id: 'agent_1', name: 'Kestrel', owner_display_name: 'Olive',
  working_directory: 'C:\\Users\\olive\\kestrel', projects: ['proj_a'] } }, 'https://ignored.example.invalid');
const many = m.secretlessPayload({ agent: { id: 'agent_2', name: 'Wren', owner_display_name: 'Olive',
  working_directory: '/home/olive/wren', projects: ['proj_a', 'proj_b'] } }, 'https://ignored.example.invalid');
const none = m.secretlessPayload({ agent: { id: 'agent_3', name: 'Dove', owner_display_name: 'Olive',
  working_directory: null, projects: [] } }, 'https://ignored.example.invalid');
const certificate = { certificate: '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n',
  sha256: 'AB:CD:EF' };
const blockOf = (text) => JSON.parse(text.split('~~~json\n').slice(-1)[0].split('\n~~~')[0]);
const server = 'https://office.example.invalid:7443';
const plain = m.setupPrompt(payload, { bridgeServer: server });
const reviewer = m.setupPrompt(payload, { role: 'reviewer', bridgeServer: server, certificate });
const several = m.setupPrompt(many, { bridgeServer: server });
const nothing = m.setupPrompt(none, { bridgeServer: server });
console.log(JSON.stringify({
  settings: blockOf(plain), reviewer: blockOf(reviewer), several: blockOf(several), nothing: blockOf(nothing),
  plainText: plain, reviewerText: reviewer, severalText: several, nothingText: nothing,
  serverUrlFromPage: blockOf(plain)['orchestraBridge.serverUrl'],
  agentNameIsTheSlug: blockOf(plain)['orchestraBridge.agentName'],
  fingerprintInComment: reviewer.includes('# server certificate sha256 = AB:CD:EF'),
  addressComment: reviewer.includes('# server = ' + server),
  noFingerprintWithoutCertificate: !plain.includes('sha256'),
  certificateSaved: reviewer.includes('%USERPROFILE%\\.orchestra-agent-kestrel.crt')
    && reviewer.includes('~/.orchestra-agent-kestrel.crt'),
  certificateInPrompt: reviewer.includes('MIIB'),
}));
"""

    def run_script(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; the static checks below still apply')
        out = run_node_module(self, node, self.SCRIPT, SETUP_JS.as_uri())
        self.assertEqual(0, out.returncode, out.stderr)
        return json.loads(out.stdout)

    def test_the_settings_are_the_ones_the_extension_reads(self):
        result = self.run_script()
        self.assertEqual({
            'orchestraBridge.serverUrl': 'https://office.example.invalid:7443',
            'orchestraBridge.agentName': 'kestrel',
            'orchestraBridge.enabled': True,
            'orchestraBridge.onWork': 'prefill',
            'orchestraBridge.role': 'worker',
            'orchestraBridge.projects': ['proj_a']}, result['settings'])
        # The caller decides the address, so the dialog can pass the page's own origin rather than
        # the configured public address (coordinator note of 2026-10-08).
        self.assertEqual('https://office.example.invalid:7443', result['serverUrlFromPage'])
        self.assertEqual('kestrel', result['agentNameIsTheSlug'])

    def test_the_reviewer_role_gets_the_same_writing_plus_its_role_and_the_certificate(self):
        result = self.run_script()
        self.assertEqual(dict(result['settings'], **{
            'orchestraBridge.role': 'reviewer',
            'orchestraBridge.caFile': '~/.orchestra-agent-kestrel.crt'}), result['reviewer'])
        self.assertTrue(result['addressComment'])
        self.assertTrue(result['fingerprintInComment'])
        self.assertTrue(result['certificateSaved'])
        self.assertTrue(result['certificateInPrompt'])
        # Nothing about a certificate without one, and the fingerprint never invents itself.
        self.assertTrue(result['noFingerprintWithoutCertificate'])

    def test_several_projects_leave_the_kit_placeholder_and_no_project_writes_an_empty_list(self):
        result = self.run_script()
        self.assertEqual(['REPLACE_PROJECT_ID'], result['several']['orchestraBridge.projects'])
        self.assertIn('REPLACE_PROJECT_ID', result['severalText'])
        self.assertEqual([], result['nothing']['orchestraBridge.projects'])
        self.assertIn('no project yet', result['nothingText'])


class TheDialogAndThePageCase(unittest.TestCase):
    """Static checks of what the page offers, where node may not be installed at all."""

    def test_the_dialog_says_what_is_written_and_what_is_left_to_the_person(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        self.assertIn("export const SETUP_WRITES_LINE = 'The prompt writes .orchestra/agent.json", agents)
        self.assertIn('.vscode/settings.json (this page’s address', agents)
        self.assertIn("export const SETUP_ONCE_LINE = 'What is still yours, once: install the Orchestra "
                      "Bridge extension in VS Code, reload the window, and click the first wake.", agents)
        self.assertIn("h('p', { class: 'small', 'data-writes': setupText.VSCODE_SETTINGS_PATH }, SETUP_WRITES_LINE)", agents)
        self.assertIn("h('p', { class: 'small', 'data-once': 'true' }, SETUP_ONCE_LINE)", agents)
        setup = SETUP_JS.read_text(encoding='utf-8')
        self.assertIn("export const VSCODE_SETTINGS_PATH = '.vscode/settings.json';", setup)
        self.assertIn("export const BRIDGE_ON_WORK = 'prefill';", setup)

    def test_the_page_asks_for_the_certificate_and_offers_the_save(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        self.assertIn("const data = await ctx.api.certificate(); return data && data.certificate ? data : null;", agents)
        self.assertIn("'data-fingerprint': certificate.sha256", agents)
        self.assertIn("onclick: () => saveCertificate(certificate, payload.certificateFile)", agents)
        self.assertIn("const blob = new Blob([certificate.certificate], { type: 'application/x-pem-file' });", agents)
        self.assertIn("download: file.name", agents)
        # The certificate block is only built when the service has one.
        self.assertIn("const certificateBlock = certificate ? h('section'", agents)
        # The role is chosen here, one window is one role, and the settings follow that choice.
        self.assertIn("What this agent does in this window: ", agents)
        self.assertIn("h('option', { value: 'reviewer' }, 'reviews other agents’ deliveries')", agents)
        self.assertIn('settingsFor(role)', agents)
        api = API_JS.read_text(encoding='utf-8')
        self.assertIn("certificate: () => call('GET', '/v1/service/certificate')", api)

    def test_the_save_to_folder_path_merges_the_settings_too(self):
        agents = AGENTS_JS.read_text(encoding='utf-8')
        save = re.search(r'async function saveSetupFiles\(files, addition = null\) \{.*?\n\}', agents, re.S).group(0)
        self.assertIn('setupText.mergeSettings(current, addition)', save)
        self.assertIn('notes.push(setupText.SETTINGS_REFUSED_SENTENCE)', save)
        self.assertIn("getDirectoryHandle('.vscode', { create: true })", save)
        self.assertIn("getFileHandle('settings.json', { create: true })", save)
        self.assertIn("(result.notes || []).map((note) => ' ' + note).join('')", agents)
        # The one place the .orchestra/ files are written still never mentions a credential.
        self.assertIsNone(re.search(r'secret|credential', save, re.I))

    def test_the_settings_never_hold_the_secret_and_a_bad_file_is_left_alone(self):
        setup = SETUP_JS.read_text(encoding='utf-8')
        # The settings builder knows nothing about the credential: it is handed a role, an address,
        # a project list and a certificate path, and nothing else.
        body = re.search(r'export function bridgeSettings\(payload, \{[^}]*\} = \{\}\) \{.*?\n\}', setup, re.S).group(0)
        self.assertIsNone(re.search(r'secret|credential', body, re.I))
        self.assertIn("'orchestraBridge.projects': granted.length === 1", body)
        self.assertIn('REPLACE_PROJECT_ID', body)
        merge = re.search(r'export function mergeSettings\(existingText, addition\) \{.*?\n\}', setup, re.S).group(0)
        self.assertIn('JSON.parse(body)', merge)
        self.assertIn('return { text: null, refused: true, created: false };', merge)
        self.assertIn("export const SETTINGS_REFUSED_SENTENCE = 'The existing .vscode/settings.json is not "
                      "valid JSON", setup)
        self.assertIn("export function credentialCommentLines(server, fingerprint) {", setup)
        self.assertIn("lines.push(`# server certificate sha256 = ${fingerprint}`)", setup)
        # The prompt tells the agent to merge, to stop on a file that is not valid JSON, and that
        # no settings file ever holds the credential.
        prompt = re.search(r'export function setupPrompt\(payload, \{.*?\n\}', setup, re.S).group(0)
        self.assertIn('merge these settings into ${VSCODE_SETTINGS_PATH}', prompt)
        self.assertIn('Keep every setting already in it exactly as it is and change nothing else', prompt)
        self.assertIn('If the file exists and is not valid JSON, leave it exactly as it is and say so', prompt)
        self.assertIn('the secret never goes into any settings file', prompt)


if __name__ == '__main__':
    unittest.main()
