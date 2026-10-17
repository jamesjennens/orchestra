"""Waking a lane: the example wakers, the contract they keep, and the texts that send a lane to them.

kittrial-5bb.306. `tools/wake/` holds example poll-and-wake scripts: the kit supplies
the polling (`client.py wait`) and a hook, and the lane supplies the one wake command
that starts a new turn of its harness. These tests drive the shell waker against a fake
client (no live endpoint) and pin the parts that go wrong without them: the state file
and fingerprint, one wake per new fingerprint, one instance at a time, the configurable
command running exactly once, the scheduler wiring, and the sentences in the docs and
the worker prompts that tell a new lane to build and test its waker.

What runs here: the POSIX sh waker under sh, bash and dash, and the launchd plist
through plistlib. What is reasoned only: wake.ps1 (no PowerShell on this host - the
parse test skips with that reason) and launchd/systemd/cron/Task Scheduler themselves.
"""
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
WAKE = KIT / 'tools' / 'wake'
sys.path.insert(0, str(KIT))
import onboarding
import worker

SHELL = WAKE / 'wake.sh'
POWERSHELL = WAKE / 'wake.ps1'
README = WAKE / 'README.md'

#: What a secret of this service looks like. The example waker must carry none: the
#: client config is its only credential input.
SECRET = re.compile(r'(?:orc|agt|cred|sess)_[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{12,}|password\s*[:=]', re.I)
#: This host, this task, or a real home directory: none may be baked into a generic kit.
HOST_SPECIFIC = re.compile(r'/home/[A-Za-z0-9_.-]+|/srv/|dsuser|goomba|koopa|orchestra-drop|kittrial-5bb', re.I)
#: Client actions a waker must never make: it only watches, it never does the work.
WORK_ACTIONS = ('claim', 'checkpoint', 'update', 'comments', 'review', 'ready', 'work', 'brief', 'guidance',
                'session', 'proposal', 'ref', 'capability', 'lifecycle', 'admin')

#: A stand-in for client.py. It prints the wait answer the real client would print and
#: exits with the same codes: 0 work, 1 the timeout, 2 refused, 3 unreachable. It records
#: every invocation, so a test can see the client is only ever asked to wait.
FAKE_CLIENT = r'''
import json
import os
import sys
import time

args = sys.argv[1:]
if 'wait' not in args:
    sys.stdout.write('fake client: the waker may only run wait\n')
    raise SystemExit(7)
index = args.index('wait')
options = {}
position = index + 1
while position < len(args):
    name = args[position]
    if name.startswith('--') and position + 1 < len(args):
        options[name] = args[position + 1]
        position += 2
    else:
        position += 1
calls = os.environ.get('WAKE_FAKE_CALLS')
if calls:
    with open(calls, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(args) + '\n')
with open(os.environ['WAKE_FAKE_SCENARIO'], encoding='utf-8') as handle:
    scenario = json.load(handle)
if scenario.get('sleep'):
    time.sleep(scenario['sleep'])
if scenario.get('stderr'):
    sys.stderr.write(scenario['stderr'])
if scenario.get('forced_exit') is not None:
    if scenario.get('fingerprint') is not None:
        print(json.dumps({'schema': 'wait-v1', 'fingerprint': scenario['fingerprint'],
                          'items': scenario.get('items') or []}))
    raise SystemExit(scenario['forced_exit'])
fingerprint = scenario['fingerprint']
items = scenario.get('items') or []
since = options.get('--since')
if since is None:
    print(json.dumps({'schema': 'wait-v1', 'fingerprint': fingerprint, 'items': items}))
    raise SystemExit(0 if items else 1)
if since != fingerprint:
    print(json.dumps({'schema': 'wait-v1', 'fingerprint': fingerprint, 'items': items}))
    raise SystemExit(0)
print(json.dumps({'schema': 'wait-v1', 'fingerprint': fingerprint, 'items': [], 'timed_out': True}))
raise SystemExit(1)
'''


def flat(path):
    return ' '.join(Path(path).read_text(encoding='utf-8').split())


def section(text, heading):
    """The part of a Markdown document under one level-two heading, without the next one."""
    part = text.split('\n' + heading + '\n', 1)
    if len(part) != 2:
        return ''
    return part[1].split('\n## ', 1)[0]


class WakeCase(unittest.TestCase):
    """A private directory, a fake client and a wake log, for driving wake.sh."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.state = self.dir / 'wake.state'
        self.lock = self.dir / 'wake.lock'
        self.log = self.dir / 'wake.log'
        self.calls = self.dir / 'calls.jsonl'
        self.scenario = self.dir / 'scenario.json'
        self.wakes = self.dir / 'wakes.txt'
        self.client = self.dir / 'fake_client.py'
        self.client.write_text(FAKE_CLIENT, encoding='utf-8')
        self.config = self.dir / 'client.json'
        self.config.write_text('{"transport": "local"}\n', encoding='utf-8')
        self.env = dict(os.environ)
        self.env['WAKE_FAKE_SCENARIO'] = str(self.scenario)
        self.env['WAKE_FAKE_CALLS'] = str(self.calls)
        self.env.pop('WAKE_CLIENT', None)
        self.env.pop('WAKE_CONFIG', None)
        self.env.pop('WAKE_COMMAND', None)

    def fingerprint(self, number):
        return 'wait1:%064x' % number

    def scenario_for(self, number, items=None, **extra):
        data = {'fingerprint': self.fingerprint(number),
                'items': [{'task': 'task-%d' % number}] if items is None else items}
        data.update(extra)
        self.scenario.write_text(json.dumps(data), encoding='utf-8')
        return data['fingerprint']

    def script_args(self, *extra, wake=None):
        command = wake if wake is not None else 'echo one >> ' + shlex.quote(str(self.wakes))
        args = ['sh', str(SHELL),
                '--python', sys.executable,
                '--client', str(self.client),
                '--config', str(self.config),
                '--project', 'sample-project',
                '--actor', 'sample/actor',
                '--wake', command,
                '--state', str(self.state),
                '--lock', str(self.lock),
                '--log', str(self.log),
                '--timeout', '5']
        args.extend(extra)
        return args

    def run_wake(self, *extra, wake=None, env=None):
        command = self.script_args(*extra, wake=wake)
        environment = dict(self.env)
        if env:
            environment.update(env)
        return subprocess.run(command, capture_output=True, text=True, timeout=60, env=environment)

    def wake_lines(self):
        return self.wakes.read_text(encoding='utf-8').splitlines() if self.wakes.exists() else []

    def log_lines(self):
        return self.log.read_text(encoding='utf-8').splitlines() if self.log.exists() else []

    def state_text(self):
        return self.state.read_text(encoding='utf-8').strip() if self.state.exists() else None

    def calls_made(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding='utf-8').splitlines()]


class ParseTests(unittest.TestCase):
    """The scripts are programs, not text: they parse, and the plist is a real plist."""

    def test_the_posix_script_has_a_posix_shebang_and_parses_under_sh_bash_and_dash(self):
        first = SHELL.read_text(encoding='utf-8').splitlines()[0]
        self.assertEqual(first, '#!/bin/sh')
        parsed = []
        for shell in ('sh', 'bash', 'dash'):
            executable = shutil.which(shell)
            if not executable:
                print('NOTE: ParseTests.test_the_posix_script_has_a_posix_shebang_and_parses_under_sh_bash_and_dash '
                      'did not run %s -n: %s is not installed on this host.' % (shell, shell), file=sys.stderr)
                continue
            with self.subTest(shell=shell):
                done = subprocess.run([executable, '-n', str(SHELL)], capture_output=True, text=True,
                                      encoding='utf-8', timeout=60)
                self.assertEqual(done.returncode, 0, '%s -n: %s' % (shell, done.stderr))
                parsed.append(shell)
        self.assertIn('sh', parsed, 'no POSIX shell was available to parse wake.sh')

    def test_the_powershell_script_parses_where_powershell_is_installed(self):
        executable = shutil.which('pwsh') or shutil.which('powershell')
        if not executable:
            self.skipTest('PowerShell is not installed on this host, so tools/wake/wake.ps1 was not parsed')
        script = ("$errors = $null; "
                  "[System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$errors) | Out-Null; "
                  "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
                  % str(POWERSHELL).replace("'", "''"))
        done = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-Command', script],
                              capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_launchd_plist_is_a_plist_with_a_login_run_and_an_interval(self):
        data = plistlib.loads((WAKE / 'macos' / 'com.orchestra.wake.plist').read_bytes())
        self.assertEqual(data['Label'], 'com.orchestra.wake')
        self.assertIs(data['RunAtLoad'], True)
        self.assertEqual(data['StartInterval'], 300)
        self.assertIn('wake.sh', data['ProgramArguments'][0])
        self.assertEqual(data['ProgramArguments'][1], '--env-file')
        self.assertTrue(data['ProgramArguments'][2].endswith('wake.env'))


class FingerprintTests(WakeCase):
    """The state file and the fingerprint: one wake per new fingerprint, never twice."""

    def test_a_new_fingerprint_wakes_exactly_once_and_the_same_fingerprint_does_not(self):
        first = self.scenario_for(1)
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), ['one'])
        self.assertEqual(self.state_text(), first)
        self.assertEqual(len(self.log_lines()), 1)
        self.assertIn('fingerprint=' + first, self.log_lines()[0])
        self.assertIn('items=1', self.log_lines()[0])
        self.assertIn('result=ok', self.log_lines()[0])
        self.assertEqual(len(self.calls_made()), 1)
        self.assertIn('wait', self.calls_made()[0])
        self.assertNotIn('--since', self.calls_made()[0])
        self.assertIn('--timeout', self.calls_made()[0], 'one run is bounded by the wait timeout')
        self.assertEqual(self.calls_made()[0][self.calls_made()[0].index('--timeout') + 1], '5')

        # The same fingerprint, as client.py wait sees it on a restart: it blocks, times out,
        # and the waker has nothing new to wake for.
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), ['one'])
        self.assertEqual(len(self.log_lines()), 1)
        self.assertEqual(self.state_text(), first)
        self.assertIn('--since', self.calls_made()[-1])
        self.assertEqual(self.calls_made()[-1][self.calls_made()[-1].index('--since') + 1], first)

        second = self.scenario_for(2)
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), ['one', 'one'])
        self.assertEqual(self.state_text(), second)
        self.assertEqual(len(self.log_lines()), 2)
        self.assertIn(second, self.log_lines()[1])

        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), ['one', 'one'])
        self.assertEqual(len(self.log_lines()), 2)

    def test_no_wake_when_the_wait_times_out_and_the_state_is_left_alone(self):
        first = self.scenario_for(1, items=[])
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), [])
        self.assertEqual(self.log_lines(), [])
        self.assertFalse(self.state.exists(), 'a timed-out first wait must not invent a fingerprint')
        self.assertEqual(len(self.calls_made()), 1)

        self.state.write_text(first + '\n', encoding='utf-8')
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), [])
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.state_text(), first)

    def test_a_changed_fingerprint_with_no_rows_is_recorded_without_waking_the_lane(self):
        first = self.scenario_for(1)
        self.assertEqual(self.run_wake().returncode, 0)
        self.assertEqual(self.wake_lines(), ['one'])

        second = self.scenario_for(2, items=[])
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.state_text(), second, 'the waker must move its state past a change it saw')
        self.assertEqual(self.wake_lines(), ['one'], 'it must not wake the lane to find nothing')
        self.assertEqual(len(self.log_lines()), 1)

        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), ['one'])
        self.assertEqual(len(self.log_lines()), 1)

    def test_an_unusable_state_file_stops_the_run_instead_of_guessing(self):
        self.scenario_for(1)
        self.state.write_text('not-a-fingerprint\n', encoding='utf-8')
        done = self.run_wake()
        self.assertEqual(done.returncode, 1)
        self.assertIn('does not hold a wait fingerprint', done.stderr)
        self.assertEqual(self.wake_lines(), [])
        self.assertEqual(self.calls_made(), [], 'a state it cannot read is refused before any poll')


class LockTests(WakeCase):
    """One instance at a time: a live holder means this run exits, a dead one is taken over."""

    def hold(self, pid):
        self.lock.mkdir()
        (self.lock / 'pid').write_text('%s\n' % pid, encoding='utf-8')

    def test_a_live_holder_makes_this_run_exit_without_polling(self):
        self.scenario_for(1)
        self.hold(os.getpid())
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.wake_lines(), [])
        self.assertTrue(self.lock.exists(), 'a run must not remove the lock another live run holds')

    def test_a_lock_left_by_a_dead_run_is_taken_over(self):
        self.scenario_for(1)
        self.hold(99999999)
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.wake_lines(), ['one'])
        self.assertFalse(self.lock.exists(), 'the run that took the lock over must release it')

    def test_a_lock_with_no_pid_yet_is_left_alone(self):
        self.scenario_for(1)
        self.lock.mkdir()
        done = self.run_wake()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.wake_lines(), [])
        self.assertTrue(self.lock.exists(), 'another run may be between its mkdir and its pid write')

    def test_a_lock_that_gains_a_foreign_file_is_not_recursively_removed(self):
        # The lock is released with rm -f of its pid file and rmdir of the directory.
        # A directory that gained anything else must survive: rmdir refuses it. This is
        # the direct guard against rm -rf on a configured path.
        self.scenario_for(1)
        intruder = self.lock / 'notes.txt'
        script = ('echo one >> %s; printf x > %s'
                  % (shlex.quote(str(self.wakes)), shlex.quote(str(intruder))))
        done = self.run_wake(wake=script)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(intruder.exists(), 'release must not remove a lock directory that holds more than its pid file')
        self.assertFalse((self.lock / 'pid').exists(), 'the run must still take its own pid file away')
        self.assertEqual(self.wake_lines(), ['one'])

    def test_two_runs_started_together_wake_once(self):
        self.scenario_for(1, sleep=1)
        args = self.script_args()
        first = subprocess.Popen(args, env=dict(self.env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(args, env=dict(self.env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, first_error = first.communicate(timeout=60)
        _, second_error = second.communicate(timeout=60)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(second.returncode, 0, second_error)
        self.assertEqual(self.wake_lines(), ['one'])
        self.assertEqual(len(self.log_lines()), 1)
        self.assertEqual(self.state_text(), self.fingerprint(1))


SETTING_NAMES = (('--state', 'WAKE_STATE'), ('--lock', 'WAKE_LOCK'), ('--log', 'WAKE_LOG'))


class UnsafePathTests(WakeCase):
    """A configured path is refused before anything is created or removed.

    The kit's rule is never to remove a path that came from configuration
    recursively, least of all in a script people run on their own machines. wake.sh
    therefore checks WAKE_STATE, WAKE_LOCK and WAKE_LOG first, and fails closed.
    """

    def refused(self, *arguments):
        self.scenario_for(1)
        done = self.run_wake(*arguments)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(self.calls_made(), [], 'a refused path must be refused before the poll')
        self.assertEqual(self.wake_lines(), [])
        self.assertFalse(self.state.exists(), 'a refused path must not create the state')
        self.assertFalse(self.lock.exists(), 'a refused path must not create the lock')
        self.assertFalse(self.log.exists(), 'a refused path must not create the log')
        return done

    def test_an_empty_state_lock_or_log_is_refused(self):
        for setting, name in SETTING_NAMES:
            with self.subTest(setting=setting):
                done = self.refused(setting, '')
                self.assertIn(name, done.stderr)
                self.assertIn('is empty', done.stderr)

    def test_an_empty_environment_value_is_refused_too(self):
        # "Unset" takes the default; "set but empty" is a typo and must fail closed.
        # The script is run from a copy in the test directory so that, if this ever
        # regresses, the default paths it would fall back to are the test's own.
        script = self.dir / 'wake.sh'
        shutil.copy(str(SHELL), str(script))
        for setting, name in SETTING_NAMES:
            with self.subTest(setting=setting):
                self.scenario_for(1)
                command = self.script_args()
                command[1] = str(script)
                index = command.index(setting)
                del command[index:index + 2]
                environment = dict(self.env)
                environment[name] = ''
                done = subprocess.run(command, capture_output=True, text=True, timeout=60, env=environment)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn(name, done.stderr)
                self.assertIn('is empty', done.stderr)
                self.assertEqual(self.calls_made(), [], 'the refusal must come before the poll')
                self.assertFalse(self.lock.exists(), 'a refused path must not create the lock')

    def test_the_filesystem_root_is_refused_for_state_lock_and_log(self):
        for setting, name in SETTING_NAMES:
            with self.subTest(setting=setting):
                done = self.refused(setting, '/')
                self.assertIn(name, done.stderr)
                self.assertIn('filesystem root', done.stderr)

    def test_the_home_directory_is_refused_for_state_lock_and_log(self):
        home = os.environ.get('HOME') or str(Path.home())
        if not home or not home.rstrip('/'):
            self.skipTest('HOME is empty or the filesystem root on this host, so there is no home directory to compare')
        for setting, name in SETTING_NAMES:
            for form in (home, home.rstrip('/') + '/'):
                with self.subTest(setting=setting, path=form):
                    done = self.refused(setting, form)
                    self.assertIn(name, done.stderr)
                    self.assertIn('home directory', done.stderr)
        self.assertTrue(Path(home).is_dir(), 'a refused home directory must not be touched')

    def test_an_existing_directory_is_refused_where_a_file_is_expected(self):
        foreign = self.dir / 'foreign'
        foreign.mkdir()
        (foreign / 'notes.txt').write_text('keep me\n', encoding='utf-8')
        for setting, name in (('--state', 'WAKE_STATE'), ('--log', 'WAKE_LOG')):
            with self.subTest(setting=setting):
                done = self.refused(setting, str(foreign))
                self.assertIn(name, done.stderr)
                self.assertIn('it must name a file', done.stderr)
                self.assertTrue((foreign / 'notes.txt').exists(),
                                'a refused directory must be left exactly as it was')

    def test_a_lock_directory_that_is_not_this_wakers_own_is_refused(self):
        foreign = self.dir / 'foreign'
        foreign.mkdir()
        (foreign / 'notes.txt').write_text('keep me\n', encoding='utf-8')
        done = self.refused('--lock', str(foreign))
        self.assertIn('WAKE_LOCK', done.stderr)
        self.assertIn("not this waker's lock", done.stderr)
        self.assertTrue((foreign / 'notes.txt').exists(),
                        'a foreign lock directory must not be used or removed')

    def test_a_lock_that_is_a_file_is_refused(self):
        file_lock = self.dir / 'lock.txt'
        file_lock.write_text('not a lock\n', encoding='utf-8')
        done = self.refused('--lock', str(file_lock))
        self.assertIn('WAKE_LOCK', done.stderr)
        self.assertIn('existing file', done.stderr)
        self.assertTrue(file_lock.exists(), 'a refused lock file must not be removed')

    def test_a_lock_that_is_a_symbolic_link_is_refused(self):
        target = self.dir / 'target'
        target.mkdir()
        link = self.dir / 'link'
        os.symlink(str(target), str(link))
        done = self.refused('--lock', str(link))
        self.assertIn('WAKE_LOCK', done.stderr)
        self.assertIn('symbolic link', done.stderr)
        self.assertTrue(target.is_dir() and link.is_symlink(),
                        'a refused symbolic link and its target must be left alone')


class RecursiveRemovalTests(unittest.TestCase):
    """The kit never removes a configured path recursively; grep for it as a test."""

    # Any rm invocation with an -r or -R flag (alone, combined, or spelled --recursive).
    RECURSIVE_RM = re.compile(r'\brm\s+(?:--?[A-Za-z-]+\s+)*--?[A-Za-z-]*[rR][A-Za-z-]*')

    def test_no_recursive_removal_remains_anywhere_in_the_wake_tools(self):
        offenders = []
        for path in sorted(WAKE.rglob('*')):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if self.RECURSIVE_RM.search(line):
                    offenders.append('%s:%d: %s' % (path.relative_to(KIT), number, line.strip()))
        self.assertEqual(offenders, [], 'recursive rm on a configured path:\n' + '\n'.join(offenders))


class WakeCommandTests(WakeCase):
    """The wake command is the lane's setting, is required, and runs exactly once per wake."""

    def test_two_different_commands_are_both_used_and_neither_is_hard_coded(self):
        self.scenario_for(1)
        other = self.dir / 'other-wakes.txt'
        done = self.run_wake(wake='echo second >> ' + shlex.quote(str(other)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(other.read_text(encoding='utf-8').splitlines(), ['second'])
        self.assertEqual(self.wake_lines(), [], 'the default command is only the test helper, not the script')

        self.scenario_for(2)
        done = self.run_wake(wake='echo second >> ' + shlex.quote(str(other)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(other.read_text(encoding='utf-8').splitlines(), ['second', 'second'])

    def test_the_wake_command_receives_the_fingerprint_and_the_row_count(self):
        first = self.scenario_for(1)
        script = ('printf "%s %s\\n" "$WAKE_FINGERPRINT" "$WAKE_ITEMS" >> '
                  + shlex.quote(str(self.dir / 'hooked.txt')))
        done = self.run_wake(wake=script)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.dir / 'hooked.txt').read_text(encoding='utf-8').splitlines(),
                         ['%s 1' % first])

    def test_a_missing_or_placeholder_wake_command_is_refused_before_any_poll(self):
        self.scenario_for(1)
        done = self.run_wake(wake='')
        self.assertEqual(done.returncode, 2)
        self.assertIn('--wake', done.stderr)
        done = self.run_wake(wake='REPLACE_WAKE_COMMAND')
        self.assertEqual(done.returncode, 2)
        self.assertIn('placeholder', done.stderr)
        self.assertEqual(self.calls_made(), [])

    def test_a_failing_wake_command_is_logged_once_and_not_retried_for_the_same_state(self):
        first = self.scenario_for(1)
        done = self.run_wake(wake='exit 1')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.state_text(), first)
        self.assertEqual(len(self.log_lines()), 1)
        self.assertIn('result=failed', self.log_lines()[0])
        self.assertIn('wake command failed', done.stderr)

        done = self.run_wake(wake='exit 1')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.log_lines()), 1, 'the same fingerprint must never run the command twice')

    def test_the_client_is_only_ever_asked_to_wait(self):
        self.scenario_for(1)
        self.assertEqual(self.run_wake().returncode, 0)
        self.scenario_for(2)
        self.assertEqual(self.run_wake().returncode, 0)
        calls = self.calls_made()
        self.assertEqual(len(calls), 2)
        for call in calls:
            with self.subTest(call=call):
                self.assertIn('wait', call)
                for action in WORK_ACTIONS:
                    self.assertNotIn(action, call)


class ClientAnswerTests(WakeCase):
    """How a refused or unreachable client ends the run, and the configurable env file."""

    def test_a_refused_wait_and_an_unreachable_wait_leave_the_state_and_log_alone(self):
        for code, text in ((2, 'ValueError: refused\n'), (3, 'the service cannot be reached\n')):
            with self.subTest(code=code):
                self.scenario_for(1, forced_exit=code, stderr=text)
                done = self.run_wake()
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertIn(text.strip(), done.stderr)
                self.assertEqual(self.wake_lines(), [])
                self.assertEqual(self.log_lines(), [])
                self.assertFalse(self.state.exists())

    def test_a_client_answer_that_is_not_a_wait_read_is_refused(self):
        self.scenario.write_text(json.dumps({'fingerprint': 'not-a-fingerprint', 'items': [{'task': 't'}]}),
                                 encoding='utf-8')
        done = self.run_wake()
        self.assertEqual(done.returncode, 3)
        self.assertIn('fingerprint', done.stderr)
        self.assertEqual(self.wake_lines(), [])
        self.assertEqual(self.log_lines(), [])

    def test_the_env_file_supplies_every_setting_and_a_flag_wins_over_it(self):
        self.scenario_for(1)
        env_wakes = self.dir / 'env-wakes.txt'
        flag_wakes = self.dir / 'flag-wakes.txt'
        env_file = self.dir / 'wake.env'
        env_file.write_text('\n'.join([
            'WAKE_PYTHON=' + shlex.quote(sys.executable),
            'WAKE_CLIENT=' + shlex.quote(str(self.client)),
            'WAKE_CONFIG=' + shlex.quote(str(self.config)),
            'WAKE_PROJECT=' + shlex.quote('sample-project'),
            'WAKE_ACTOR=' + shlex.quote('sample/actor'),
            'WAKE_COMMAND=' + shlex.quote('echo env >> ' + str(env_wakes)),
            'WAKE_TIMEOUT=5',
            'WAKE_STATE=' + shlex.quote(str(self.state)),
            'WAKE_LOCK=' + shlex.quote(str(self.lock)),
            'WAKE_LOG=' + shlex.quote(str(self.log)),
        ]) + '\n', encoding='utf-8')
        done = subprocess.run(['sh', str(SHELL), '--env-file', str(env_file)],
                              capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(env_wakes.read_text(encoding='utf-8').splitlines(), ['env'])
        self.assertEqual(len(self.log_lines()), 1)

        self.scenario_for(2)
        done = subprocess.run(['sh', str(SHELL), '--env-file', str(env_file),
                               '--wake', 'echo flag >> ' + str(flag_wakes)],
                              capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(flag_wakes.read_text(encoding='utf-8').splitlines(), ['flag'])
        self.assertEqual(env_wakes.read_text(encoding='utf-8').splitlines(), ['env'],
                         'the flag must win over the env file, not run both commands')

    def test_an_unknown_setting_or_bad_number_is_a_usage_error(self):
        self.scenario_for(1)
        for extra, said in ((('--nonesuch', 'x'), 'unknown argument'),
                            (('--timeout', 'soon'), '--timeout'),
                            (('--interval', 'soon'), '--interval')):
            with self.subTest(extra=extra):
                done = self.run_wake(*extra)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn(said, done.stderr)
        self.assertEqual(self.calls_made(), [])


class SchedulerTests(unittest.TestCase):
    """The scheduler wiring is present, generic, and tells the same contract as the script."""

    def test_linux_units_run_the_waker_from_the_env_file(self):
        service = (WAKE / 'linux' / 'orchestra-wake.service').read_text(encoding='utf-8')
        timer = (WAKE / 'linux' / 'orchestra-wake.timer').read_text(encoding='utf-8')
        self.assertIn('Type=oneshot', service)
        self.assertIn('ExecStart=%h/.orchestra/wake/wake.sh --env-file %h/.orchestra/wake/wake.env', service)
        self.assertIn('TimeoutStartSec=', service)
        self.assertIn('OnBootSec=1min', timer)
        self.assertIn('OnUnitActiveSec=5min', timer)
        self.assertIn('Unit=orchestra-wake.service', timer)
        self.assertIn('WantedBy=timers.target', timer)

    def test_the_cron_line_is_a_cron_line(self):
        lines = [line for line in (WAKE / 'linux' / 'crontab.example').read_text(encoding='utf-8').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(len(lines), 1, lines)
        fields = lines[0].split()
        self.assertGreaterEqual(len(fields), 6)
        self.assertEqual(fields[:5], ['*/5', '*', '*', '*', '*'])
        command = ' '.join(fields[5:])
        self.assertIn('wake.sh', command)
        self.assertIn('--env-file', command)
        self.assertIn('--timeout', command)

    def test_the_readme_documents_a_logon_run_and_an_every_n_minutes_run_on_windows(self):
        text = flat(README)
        for said in ('Task Scheduler', 'at logon', 'every N minutes', 'New-ScheduledTaskTrigger',
                     'RepetitionInterval', 'Run only when user is logged on',
                     'systemd --user', 'crontab', 'launchd'):
            with self.subTest(said=said):
                self.assertIn(said, text)

    def test_the_wake_command_examples_cover_a_prompt_cli_a_task_and_the_vs_code_bridge(self):
        text = flat(README)
        self.assertIn('agent-cli --prompt', text)
        self.assertIn('scheduled task', text.lower())
        self.assertIn('VS Code bridge', text)

    def test_nothing_here_is_this_host_or_this_tasks_history_or_a_secret(self):
        for path in sorted(WAKE.rglob('*')):
            if not path.is_file():
                continue
            with self.subTest(file=str(path.relative_to(WAKE))):
                text = path.read_text(encoding='utf-8')
                self.assertIsNone(HOST_SPECIFIC.search(text), 'host-specific text in %s' % path)
                self.assertIsNone(SECRET.search(text), 'a secret-looking string in %s' % path)

    def test_the_powershell_script_carries_the_same_contract_as_the_sh_script(self):
        """Reasoned only: PowerShell is not installed on this host; the parse test says so."""
        text = POWERSHELL.read_text(encoding='utf-8')
        for said in ('--config', '--project', '--actor', 'wait', '--since', '--timeout',
                     'ConvertFrom-Json', 'CreateNew', 'WAKE_FINGERPRINT', 'WAKE_ITEMS',
                     'cmd.exe /d /c $Wake', 'WAKE_COMMAND', 'WriteAllText', 'Add-Content',
                     'StreamWriter', 'Get-Process'):
            with self.subTest(said=said):
                self.assertIn(said, text)
        self.assertNotRegex(text, r"\$Wake\s*=\s*'[^']", 'the wake command is never a default in the script')


class TextTests(unittest.TestCase):
    """The docs and the prompts send a new lane to tools/wake, and state the no-idle rule."""

    def test_finding_work_has_a_waking_section_with_the_contract(self):
        text = (KIT / 'docs' / 'FINDING_WORK.md').read_text(encoding='utf-8')
        self.assertIn('\n## Waking your agent\n', text)
        waking = section(text, '## Waking your agent')
        self.assertTrue(waking.strip())
        flat_waking = ' '.join(waking.split())
        for said in ('client.py wait --since FINGERPRINT', 'one wake command', 'wakes at most once per new fingerprint',
                     'holds a lock so one instance polls at a time', 'one log line per wake',
                     'reads no secret except the client config', 'never runs the work',
                     'tools/wake/', 'A CLI that takes a prompt', 'scheduled task', 'VS Code bridge',
                     'test it once', 'Record in a checkpoint how this lane is woken',
                     'A lane must not end its turn to wait without a waker'):
            with self.subTest(said=said):
                self.assertIn(said, flat_waking)
        # The served page renders headings and plain paragraphs only: no list and no table.
        self.assertIsNone(re.search(r'^\s*[-*]\s', waking, re.M))
        self.assertNotIn('\n|', waking)

    def test_the_worker_prompt_and_the_onboarding_text_say_build_test_and_record(self):
        prompt = flat(KIT / 'templates' / 'WORKER_PROMPT.md')
        start = flat(KIT / 'docs' / 'WORKER_START.md')
        setup = ' '.join(worker.START_GUIDANCE.split())
        for name, text in (('worker prompt', prompt), ('onboarding text', start), ('start setup text', setup)):
            with self.subTest(text=name):
                for said in ("tools/wake/", 'test it once', 'show that it woke you',
                             'Record in a checkpoint how you are woken',
                             'A lane must not end its turn to wait without a waker'):
                    self.assertIn(said, text)
        self.assertIn('client.py wait --since FINGERPRINT', prompt)
        self.assertIn('ONE wake command', prompt)

    def test_the_onboarding_text_still_fits_the_bound_onboard_reads_it_with(self):
        size = (KIT / 'docs' / 'WORKER_START.md').stat().st_size
        self.assertLessEqual(size, onboarding.START_LIMIT,
                             'docs/WORKER_START.md is %d bytes; the onboarding text must stay under %d'
                             % (size, onboarding.START_LIMIT))

    def test_the_worker_prompt_still_ends_where_its_pinned_test_expects(self):
        # test_finding_work pins the first prompt's opening and closing sentence: a
        # waker paragraph is added in the middle, never after the last sentence.
        prompt = onboarding.prompt_of((KIT / 'templates' / 'WORKER_PROMPT.md').read_text(encoding='utf-8'))
        self.assertTrue(prompt.startswith('Work only in the dedicated working directory'))
        self.assertTrue(prompt.rstrip().endswith('files.'))


if __name__ == '__main__':
    unittest.main()
