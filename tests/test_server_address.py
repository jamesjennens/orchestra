"""The server address never becomes a command (kittrial-5bb.360).

From the independent review of kittrial-5bb.348: the operator's ``public_url`` went unquoted
into every command the set-up wizard, AGENT.md and the server's own setup text hand a person,
and the office configuration checked only its scheme and host, so ``https://h/$(touch X)``
passed and ran in the person's shell (the reviewer's probe, Git bash and PowerShell 5.1).

Pinned here: the one rule (``server_address.public_url_problem``) accepts every real address
form and refuses the rest in ONE sentence naming the setting, its source and the character; it
is applied where the address is set (the office configuration, the service start, ``Service``
itself); and the server's own command texts single-quote the address anyway, which a hostile
value run in real bash and PowerShell proves. The wizard's side is in test_vscode_setup.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from server_address import ps_quote, public_url_problem, sh_quote  # noqa: E402
from test_http_service import unique_dir  # noqa: E402

BASH = shutil.which('bash')
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')

#: Every real address form: the two the coordinator named; the three shapes the office's orc.sh
#: writes into office.json (http://localhost:PORT, https://ADDRESS:PORT, http://ADDRESS:PORT, with
#: ADDRESS a multi-label DNS name or an IPv4 address; the coordinator's description, 2026-10-21);
#: an IPv4 literal with a port over https (the shape of the jjbp web service's address, as the
#: coordinator reported it); and other ordinary forms. No live file or process was read for these.
ACCEPTED = ['https://[::1]:8791', 'http://host:8791/orc', 'http://localhost:7443', 'https://srv-01.corp.example:7443',
            'http://srv-01.corp.example:7443', 'https://10.20.30.40:7443', 'http://10.20.30.40:7443',
            'http://localhost:8790', 'https://192.168.0.132:8791',
            'https://orchestra.office.example', 'https://orchestra.office.example/', 'HTTPS://Host.Example:443/',
            'http://intra_host:80', 'https://h.example/a%2Fb/x_y~z-', 'https://[fd00::5]', 'http://10.0.0.1:65535/a/b']
#: (value, the character or escape the sentence must name)
REFUSED = [('https://h/$(touch X)', "'$'"), ('https://h/`id`', "'`'"), ("https://h/a'b", '"\'"'),
           ('https://h/a"b', "'\"'"), ('https://h/a;b', "';'"), ('https://h/a|b', "'|'"), ('https://h/a&b', "'&'"),
           ('https://h/a(b', "'('"), ('https://h/a)b', "')'"), ('https://h/a<b', "'<'"), ('https://h/a>b', "'>'"),
           ('https://h/a b', "' '"), ('https://h/a\tb', "'\\t'"), ('https://h/a%24b', '%24'), ('https://h/a%20b', '%20'),
           ('https://h/a%60b', '%60'), ('https://h/a%27b', '%27'), ('https://u@h/', "'@'"), ('https://h/?q=1', "'?'"),
           ('https://h/#x', "'#'")]


class TheRuleCase(unittest.TestCase):
    def test_every_real_address_form_is_accepted(self):
        for value in ACCEPTED:
            with self.subTest(value=value):
                self.assertIsNone(public_url_problem(value, 'office.json'))

    def test_a_refusal_is_one_sentence_naming_the_setting_its_source_and_the_character(self):
        for value, named in REFUSED:
            with self.subTest(value=value):
                said = public_url_problem(value, 'the office configuration (office.json)')
                self.assertTrue(said.startswith('public_url from the office configuration (office.json) has the '), said)
                self.assertIn(named, said)
                self.assertTrue(said.endswith(').'), said)
                self.assertNotIn('\n', said)

    def test_the_shape_is_checked_too(self):
        for value in ('ftp://h', 'https://', 'https:///x', 'https://[zz]/', 'https://h:0', 'https://h:99999',
                      'https://h/%2', 'https://h/%zz', '', None, 7):
            with self.subTest(value=value):
                self.assertIsNotNone(public_url_problem(value, '--public-url'))

    def test_the_quotes_expand_nothing(self):
        self.assertEqual("'a'\\''b'", sh_quote("a'b"))
        self.assertEqual("'a''b'", ps_quote("a'b"))


class WhereTheAddressIsSetCase(unittest.TestCase):
    def test_the_service_refuses_it(self):
        from http_auth import Service, Store
        home = unique_dir('server-address-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        store = Store(home / 'state.json')
        with self.assertRaises(ValueError) as refused:
            Service(store, public_url='https://h/$(touch X)')
        self.assertEqual("public_url from the service start (--public-url) has the character '$', which a server "
                         "address may not hold: give an http or https address with a host, an optional port and a "
                         "path of letters, digits, . _ ~ - / and %XX escapes (got https://h/$(touch X)).",
                         str(refused.exception))
        for value in ACCEPTED:
            self.assertEqual(value.rstrip('/'), Service(store, public_url=value).public_url)

    def test_an_unset_address_still_works(self):
        # The webtrial service sets none (the coordinator, 2026-10-21): nothing to check, nothing refused.
        from http_auth import Service, Store
        home = unique_dir('server-address-unset-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        for value in (None, ''):
            self.assertIsNone(Service(Store(home / 'state.json'), public_url=value).public_url)

    @unittest.skipIf(os.name == 'nt', 'office_service.py is the POSIX office supervisor')
    def test_the_derived_default_always_passes(self):
        # With no public_url, office_service names the service from http_host and the port; that
        # address (or none, on loopback) must pass the same rule, so an installation that sets none
        # keeps working.
        import office_service
        for settings in ({}, {'http_host': '127.0.0.1'}, {'http_host': '192.168.0.132', 'cert': 'c', 'key': 'k'},
                         {'http_host': 'srv-01.corp.example', 'allow_plaintext_on_network': True},
                         {'http_host': 'fd00::5', 'cert': 'c', 'key': 'k'}):
            with self.subTest(settings=settings):
                derived = office_service.listener(settings, 7443).get('public_url')
                self.assertTrue(derived is None or public_url_problem(derived, 'the derived default') is None, derived)

    def test_the_service_start_refuses_it_in_one_line_before_anything_is_opened(self):
        home = unique_dir('server-address-start-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        state = home / 'state.json'
        done = subprocess.run([sys.executable, str(ROOT / 'http_service.py'), '--state', str(state), '--backend', 'inprocess',
                               '--port', '0', '--public-url', 'https://h/$(touch X)'],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(2, done.returncode, done.stderr)
        self.assertIn("public_url from the service start (--public-url) has the character '$'", done.stderr)
        self.assertNotIn('Traceback', done.stderr)
        self.assertFalse(state.exists(), 'the state was opened before the address was checked')

    @unittest.skipIf(os.name == 'nt', 'office_service.py is the POSIX office supervisor')
    def test_the_office_configuration_refuses_it_naming_the_file(self):
        import office_service
        home = unique_dir('server-address-office-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        config = home / 'office.json'
        base = {'schema_version': 1, 'http_host': '127.0.0.1', 'http_state': str(home / 'http-state.json')}
        config.write_text(json.dumps(dict(base, public_url='https://h/$(touch X)')), encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            office_service.service_config(config)
        self.assertTrue(str(refused.exception).startswith(
            "public_url from the office configuration (office.json) has the character '$'"), str(refused.exception))
        for value in ACCEPTED:
            config.write_text(json.dumps(dict(base, public_url=value)), encoding='utf-8')
            office_service.service_config(config)        # accepted: a working installation keeps working


class TheServersOwnCommandsCase(unittest.TestCase):
    """The setup snippet and the resume sentence quote the address: run with a hostile value."""

    HOSTILE_SH = "http://127.0.0.1:1/x$(touch PWNED1)`touch PWNED2`';touch PWNED3;'"
    HOSTILE_PS = "http://127.0.0.1:1/x$(New-Item -ItemType File PWNED4)';New-Item -ItemType File PWNED5;'"

    def service(self, hostile):
        from http_auth import Service, Store
        home = unique_dir('server-address-cmd-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        service = Service(Store(home / 'state.json'), public_url='https://ok.example')
        service.public_url = hostile        # bypass the check: the quoting itself is under test
        return service, home

    def lines(self, text, prefix):
        return [line.split(prefix, 1)[1].strip() for line in text.splitlines() if prefix in line]

    def run_shell(self, shell, command, cwd):
        if shell == 'bash':
            # echo stands in for curl: what reaches the program is printed, nothing is fetched.
            return subprocess.run([BASH, '-c', command.replace('curl ', 'echo ', 1)], cwd=str(cwd),
                                  capture_output=True, text=True, timeout=60)
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command',
                               command.replace('curl.exe ', 'Write-Output ', 1)], cwd=str(cwd),
                              capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, LOCALAPPDATA=str(cwd)))

    def test_the_setup_snippet_and_the_resume_sentence_run_nothing_from_the_address(self):
        if not BASH and not POWERSHELL:
            self.skipTest('neither bash nor PowerShell is installed')
        checked = 0
        for shell, hostile, prefix in (('bash', self.HOSTILE_SH, 'macOS/Linux: '), ('ps', self.HOSTILE_PS, 'PowerShell:  ')):
            if (shell == 'bash' and not BASH) or (shell == 'ps' and not (POWERSHELL and os.name == 'nt')):
                continue
            service, home = self.service(hostile)
            snippet = service.agent_setup({'id': 'agent_1', 'name': 'Kestrel', 'projects': []}, 'S')['setup_snippet']
            # The resume sentence of GET /v1/me/agents (ApiHandler._agent_resume_prompt) for the same service.
            import http_service
            handler = type('Handler', (), {'service': service})()
            resume = http_service.ApiHandler._agent_resume_prompt(
                handler, {'id': 'agent_1', 'name': 'Kestrel'}, {'actions': []})
            commands = self.lines(snippet, prefix) + self.lines(resume, prefix)
            if shell == 'bash':
                commands += [line for line in snippet.splitlines() if line.startswith('curl ')]
            self.assertTrue(commands, snippet)
            for command in commands:
                with self.subTest(shell=shell, command=command):
                    self.run_shell(shell, command, home)
                    checked += 1
            self.assertEqual([], sorted(p.name for p in home.iterdir() if p.name.startswith('PWNED')))
        self.assertGreater(checked, 0)


if __name__ == '__main__':
    unittest.main()
