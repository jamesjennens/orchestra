"""Generated setup instructions are executable contracts, including the no-Bridge watch."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import web_lifecycle
import web_merge_slot

ROOT = Path(__file__).resolve().parents[1]
SENTENCE = " When you finish work that has no git delivery (a document, an analysis, a checked answer), or you need the coordinator's answer, record a checkpoint with lane_state waiting_on coordinator (the Bridge tool orchestra_checkpoint takes waiting_on: coordinator), then stop; a task with a git delivery is still delivered with a contribution."


class SetupFollowupsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which('node')
        if not node:
            raise unittest.SkipTest('node unavailable')
        script = r'''
const s = await import(process.argv[1]);
const payload = {name:'worker',owner:'person',server:'https://example.org',projects:['p'],secretFile:{windows:'%USERPROFILE%\\a',powershell:'$env:USERPROFILE\\a',posix:'~/a'},certificateFile:s.certificateFile('worker')};
const comments=s.credentialCommentLines(payload.server,null);
const command=(noproxy,server='https://example.org')=>s.credentialFileCommand(payload.secretFile, comments,{noproxy,server});
console.log(JSON.stringify({worker:s.agentGuide(payload,{bridge:false}),proxyGuide:s.agentGuide(payload,{noproxy:true}),bridge:s.agentGuide(payload,{bridge:true}),coordinator:s.agentGuide({...payload,coordinates:['p']},{bridge:false}),sentence:s.WAITING_ON_SENTENCE,watch:s.workerWaitLoop(payload.server,payload.secretFile),on:command(true),off:command(false),ipv6:command(true,'https://[::1]:443'),body:s.credentialFileBody('SECRET',null,{server:payload.server,noproxy:true}),bodyOff:s.credentialFileBody('SECRET',null),lines:s.credentialFileLines(payload.server,null,'SECRET',{noproxy:true}),example:s.integrationExample(payload.server),invalid:['https://*.example.org','https://example.org\nheader = bad','https://u:p@example.org'].map(server=>{try{s.noproxyLine(server);return false}catch{return true}})}));
'''
        done = subprocess.run([node, '--input-type=module', '-e', script,
                               (ROOT/'web/js/agentSetup.js').as_uri()],
                              capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.value = json.loads(done.stdout)

    def test_no_bridge_wait_and_bridge_exclusion(self):
        v = self.value
        self.assertIn('## Waiting for work (without Orchestra Bridge)', v['worker'])
        self.assertIn('Never end the turn idle without a watch running', v['worker'])
        self.assertIn('BACKGROUND task tracked by your agent tool', v['worker'])
        self.assertIn('Start-Sleep -Seconds 60', v['worker'])
        self.assertIn('ConvertFrom-Json -ErrorAction Stop', v['worker'])
        self.assertNotIn('## Waiting for work (without Orchestra Bridge)', v['bridge'])
        self.assertNotIn('## Waiting for work (without Orchestra Bridge)', v['coordinator'])
        self.assertIn('form=coordinator&comments=others&timeout=50', v['coordinator'])

    def test_bridge_waiting_on_sentence_exact_and_worker_only(self):
        v = self.value
        self.assertEqual(SENTENCE, v['sentence'])
        for guide in ('worker', 'bridge'):
            self.assertEqual(1, v[guide].count(SENTENCE))
            self.assertIn('delivered work follows its review state.' + SENTENCE, v[guide])
        self.assertNotIn(SENTENCE, v['coordinator'])

    def watch_code(self):
        line = next(x for x in self.value['watch'] if x.startswith('- macOS/Linux'))
        command = line[line.index('python3 -c '):]
        args = shlex.split(command)
        self.assertEqual(['python3', '-c'], args[:2])
        return args[2]

    def test_watch_keeps_since_and_returns_only_on_change(self):
        fp = 'wait1:'+'a'*64
        replies = [json.dumps(dict(form='worker', fingerprint=fp, changed=False)),
                   json.dumps(dict(form='worker', fingerprint='wait1:'+'b'*64, changed=True))]
        output = io.StringIO()
        with mock.patch('subprocess.check_output', side_effect=replies) as curl, mock.patch('time.sleep') as sleep, contextlib.redirect_stdout(output):
            exec(self.watch_code(), {})
        self.assertEqual([mock.call(60), mock.call(60)], sleep.call_args_list)
        self.assertIn('since=FINGERPRINT', curl.call_args_list[0].args[0])
        self.assertIn('since='+fp, curl.call_args_list[1].args[0])
        self.assertEqual(json.loads(replies[-1]), json.loads(output.getvalue()))
        self.assertIn('-K', curl.call_args.args[0])
        self.assertNotIn('SECRET', str(curl.call_args))

    def test_watch_refuses_bad_json_and_auth_error_instead_of_sleeping_forever(self):
        for reply in ('<html>proxy</html>', '{}', '{"form":"worker","fingerprint":"bad","changed":false}'):
            with self.subTest(reply=reply), mock.patch('subprocess.check_output', return_value=reply) as curl, mock.patch('time.sleep'):
                with self.assertRaises((ValueError, TypeError)):
                    exec(self.watch_code(), {})
                self.assertEqual(1, curl.call_count)
        with mock.patch('subprocess.check_output', side_effect=subprocess.CalledProcessError(22, 'curl')) as curl, mock.patch('time.sleep'):
            with self.assertRaises(subprocess.CalledProcessError):
                exec(self.watch_code(), {})
            self.assertEqual(1, curl.call_count)

    @unittest.skipUnless(os.name == 'nt', 'PowerShell watch executes on Windows CI')
    def test_powershell_watch_parses_multiline_replies_and_exits_on_change(self):
        shell = shutil.which('powershell') or shutil.which('pwsh')
        self.assertIsNotNone(shell, 'Windows qualification requires PowerShell')
        command = next(x for x in self.value['watch'] if x.startswith('- Windows'))
        command = command.split(': ', 1)[1]
        # A PowerShell function shadows curl.exe: no real credential or connection is used.
        first = json.dumps(dict(form='worker', fingerprint='wait1:'+'a'*64, changed=False), indent=2)
        second = json.dumps(dict(form='worker', fingerprint='wait1:'+'b'*64, changed=True), indent=2)
        prelude = "$global:watchCalls=0; function Start-Sleep { param($Seconds) if ($Seconds -ne 60) { throw 'wrong cadence' } }; "
        prelude += "function curl.exe { $global:LASTEXITCODE=0; $global:watchCalls++; if ($global:watchCalls -gt 2) { throw 'watch failed to exit' }; if ($global:watchCalls -eq 1) { ('" + first + "') -split \"`n\" } else { ('" + second + "') -split \"`n\" } }; "
        done = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command',
                               prelude + command + "; if ($global:watchCalls -ne 2) { throw 'wrong calls' }"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual('wait1:'+'b'*64, json.loads(done.stdout)['fingerprint'])

    def test_worked_bodies_match_closed_adapters(self):
        example = self.value['example']
        bodies = [json.loads(example[i+1]) for i, line in enumerate(example) if line == '~~~json']
        self.assertEqual(5, len(bodies))
        web_merge_slot.validate(dict(operation='acquire', task=bodies[0]['task_id'], target=bodies[0]['target']))
        self.assertEqual({'task_id', 'target'}, set(bodies[0]))
        self.assertEqual({}, bodies[-1])
        web_merge_slot.validate(dict(operation='release', **bodies[-1]))
        scope = bodies[1]['scope']
        self.assertEqual({'source_commit', 'integration_commit', 'release_id', 'environment'}, set(scope))
        self.assertEqual('', scope['release_id'])
        self.assertEqual('', scope['environment'])
        scope_body = dict(bodies[1], expected_scope_token=None, task='TASK', operation='scope')
        web_lifecycle.validate(scope_body, 'actor', 'test/scope')
        for dimension, body in zip(('reviewed', 'integrated'), bodies[2:4]):
            self.assertEqual(scope, body['scope'])
            self.assertEqual('performed', body['provenance'])
            self.assertIsInstance(body['evidence'], list)
            web_lifecycle.validate(dict(body, task='TASK', operation='fact', dimension=dimension), 'actor', 'test/'+dimension)
        for path in ('/merge-slot', '/lifecycle/scope', '/lifecycle/facts/reviewed', '/lifecycle/facts/integrated', '/merge-slot/release'):
            self.assertIn(path, self.value['coordinator'])

    def test_noproxy_is_opt_in_scoped_and_header_identical(self):
        v = self.value
        header = 'header = "Authorization: Bearer SECRET"\n'
        self.assertEqual(header, v['bodyOff'])
        self.assertIn('the noproxy option for the server you chose', v['proxyGuide'])
        self.assertNotIn('It holds three lines:', v['proxyGuide'])
        self.assertIn('It holds three lines:', v['bridge'])
        for text in (v['body'], v['lines']):
            self.assertEqual(1, text.count('noproxy = "example.org"'))
            self.assertTrue(text.endswith(header))
        self.assertIn('noproxy = "::1"', v['ipv6']['posix'])
        self.assertTrue(all(v['invalid']))
        self.assertIn('noproxy', v['on']['powershell'])
        self.assertNotIn('noproxy = "example.org"', v['off']['powershell'])

    @unittest.skipUnless(os.name == 'nt', 'PowerShell rewrite executes on Windows CI')
    def test_powershell_rewrite_deduplicates_and_preserves_header(self):
        shell = shutil.which('powershell') or shutil.which('pwsh')
        self.assertIsNotNone(shell, 'Windows qualification requires PowerShell')
        with tempfile.TemporaryDirectory() as root:
            file = Path(root)/'a'
            header = b'header = "Authorization: Bearer SECRET"\r\n'
            file.write_bytes(b'noproxy = "old"\r\n--noproxy "other"\r\n'+header)
            env = dict(os.environ, USERPROFILE=root)
            for variant in ('on', 'on', 'ipv6', 'off', 'off'):
                done = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', self.value[variant]['powershell']],
                                      env=env, capture_output=True, timeout=30)
                self.assertEqual(0, done.returncode, done.stderr)
                content = file.read_bytes()
                self.assertTrue(content.endswith(header))
                self.assertEqual(1 if variant != 'off' else 0, content.count(b'noproxy ='))
                self.assertNotIn(b'old', content)
                self.assertNotIn(b'other', content)
                self.assertEqual(b'', done.stdout)

    @unittest.skipIf(os.name == 'nt', 'POSIX rewrite executes in a POSIX shell')
    def test_posix_rewrite_deduplicates_changes_host_disables_and_preserves_header(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root)/'a'
            header = b'header = "Authorization: Bearer SECRET"\n'
            file.write_bytes(b'noproxy = "old"\n  noproxy = "other"\n--noproxy "another"\nnoproxy: "last"\n'+header)
            env = dict(os.environ, HOME=root)
            for variant in ('on', 'on', 'ipv6', 'off', 'off'):
                done = subprocess.run(['sh', '-c', self.value[variant]['posix']], env=env, capture_output=True, timeout=20)
                self.assertEqual(0, done.returncode, done.stderr)
                content = file.read_bytes()
                self.assertTrue(content.endswith(header))
                self.assertEqual(1 if variant != 'off' else 0, content.count(b'noproxy ='))
                self.assertNotIn(b'old', content)
                self.assertNotIn(b'other', content)
                self.assertEqual(b'', done.stdout)
            self.assertEqual(0o600, file.stat().st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
