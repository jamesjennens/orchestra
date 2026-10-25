"""The reference nudge supervisor (kittrial-5bb.415): tools/nudge/supervisor.py, with a fake terminal.

It types only the kit's own fixed text, only into the lane's harness at its prompt, reads nothing but its
own four routes, refuses a config others can read, and never prints its token.
"""
import ast
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tools' / 'nudge'))

import nudge_messages
import supervisor

TOKEN = 'tok-secret-7f3a9c1e5b'
POSIX_ONLY = {'fcntl', 'pwd', 'grp', 'termios', 'tty', 'pty', 'resource', 'curses', 'readline', 'posix', 'syslog'}
LANE = 'usr_0123456789abcdef'


class FakeTerminal(supervisor.Terminal):
    def __init__(self, foreground='copilot', screen='some output\n> ', present=True):
        self.running, self.shown, self.present = foreground, screen, present
        self.typed, self.restarted = [], []

    def exists(self):
        return self.present

    def foreground(self):
        return self.running

    def screen(self):
        return self.shown

    def type_line(self, text):
        self.typed.append(text)

    def restart(self, command):
        self.restarted.append(command)
        self.present, self.running, self.shown = True, 'copilot', 'ready\n> '


class FakeApi:
    def __init__(self, items, claimed=True):
        self.items, self.claimable, self.calls, self.answers = items, claimed, [], []

    def heartbeat(self, lanes):
        self.calls.append(('heartbeat', sorted(lanes)))

    def pending(self):
        self.calls.append(('pending',))
        return self.items

    def claim(self, nudge_id):
        self.calls.append(('claim', nudge_id))
        return {'claimed': self.claimable, 'state': 'claimed' if self.claimable else 'cancelled'}

    def outcome(self, nudge_id, outcome, detail=None):
        self.answers.append((nudge_id, outcome, detail))


def item(message='resume', message_set=nudge_messages.MESSAGE_SET, lane=LANE, nudge_id='nudge-' + '0' * 24, **more):
    return dict({'id': nudge_id, 'project': 'alpha', 'lane': lane, 'message': message, 'message_set': message_set}, **more)


def config(**lane):
    setting = dict({'target': '%1', 'harness': 'copilot', 'prompt': r'^> $', 'start_command': ['copilot']}, **lane)
    return {'server': 'https://x.invalid', 'token': TOKEN, 'lanes': {LANE: setting}}


class Run:
    def __init__(self, items, terminal=None, cfg=None, claimed=True, clock=None):
        self.terminal = terminal or FakeTerminal()
        self.api, self.lines = FakeApi(items, claimed), []
        now = [1000.0] if clock is None else clock
        self.supervisor = supervisor.Supervisor(self.api, cfg or config(), {LANE: self.terminal},
                                                clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                                                log=self.lines.append)
        self.now = now

    def poll(self):
        return self.supervisor.poll()


class TypingTests(unittest.TestCase):
    def test_it_types_the_fixed_text_when_the_harness_is_at_its_prompt(self):
        run = Run([item('resume')])
        self.assertEqual(1, run.poll())
        self.assertEqual([nudge_messages.TEXTS['resume']], run.terminal.typed)
        self.assertEqual([('nudge-' + '0' * 24, 'delivered', None)], run.api.answers)

    def test_it_never_types_into_anything_but_the_harness_at_its_prompt(self):
        for label, terminal in (('a shell', FakeTerminal(foreground='bash')),
                                ('an editor', FakeTerminal(foreground='vim')),
                                ('the harness running a tool', FakeTerminal(screen='running tests...\n')),
                                ('the harness with an empty screen', FakeTerminal(screen='')),
                                ('no process', FakeTerminal(foreground=None)),
                                ('the harness by path', FakeTerminal(foreground='/usr/bin/copilot-wrapper'))):
            with self.subTest(label=label):
                run = Run([item('resume')], terminal)
                run.poll()
                self.assertEqual([], terminal.typed)
                self.assertEqual([('nudge-' + '0' * 24, 'failed', 'not-at-prompt')], run.api.answers)

    def test_a_lane_with_no_terminal_is_no_session(self):
        run = Run([item('resume')], FakeTerminal(present=False))
        run.poll()
        self.assertEqual([], run.terminal.typed)
        self.assertEqual('no-session', run.api.answers[0][2])

    def test_nothing_the_server_sends_is_typed(self):
        for label, request in (('an unknown key', item('shutdown', text='rm -rf ~')),
                               ('an unknown set', item('resume', 'nudge-messages-v9', text='rm -rf ~')),
                               ('no set', item('resume', None))):
            with self.subTest(label=label):
                run = Run([request])
                run.poll()
                self.assertEqual([], run.terminal.typed)
                self.assertEqual('unsupported', run.api.answers[0][1])
        run = Run([item('resume', text='rm -rf ~')])
        run.poll()
        self.assertEqual([nudge_messages.TEXTS['resume']], run.terminal.typed)

    def test_restart_starts_the_lanes_own_command_then_types(self):
        run = Run([item('restart')], FakeTerminal(foreground='bash', screen='$ '))
        run.poll()
        self.assertEqual([['copilot']], run.terminal.restarted)
        self.assertEqual([nudge_messages.TEXTS['restart']], run.terminal.typed)
        self.assertEqual('restarted', run.api.answers[0][1])

    def test_restart_with_no_start_command_is_start_failed_and_types_nothing(self):
        cfg = config()
        del cfg['lanes'][LANE]['start_command']
        run = Run([item('restart')], cfg=cfg)
        run.poll()
        self.assertEqual(([], [('nudge-' + '0' * 24, 'failed', 'start-failed')]), (run.terminal.typed, run.api.answers))

    def test_a_restart_that_never_reaches_the_prompt_is_start_failed(self):
        class Stuck(FakeTerminal):
            def restart(self, command):
                self.restarted.append(command)
        run = Run([item('restart')], Stuck(foreground='bash', screen='$ '))
        run.poll()
        self.assertEqual(([], 'start-failed'), (run.terminal.typed, run.api.answers[0][2]))

    def test_a_lane_typed_to_a_moment_ago_is_coalesced(self):
        run = Run([item('resume', nudge_id='nudge-' + '1' * 24)])
        run.poll()
        run.api.items = [item('resume', nudge_id='nudge-' + '2' * 24)]
        run.now[0] += 60
        run.poll()
        self.assertEqual(1, len(run.terminal.typed))
        self.assertEqual(['delivered', 'coalesced'], [answer[1] for answer in run.api.answers])
        run.api.items = [item('resume', nudge_id='nudge-' + '3' * 24)]
        run.now[0] += supervisor.COALESCE_SECONDS
        run.poll()
        self.assertEqual(2, len(run.terminal.typed))

    def test_a_request_it_could_not_claim_is_left_alone(self):
        run = Run([item('resume')], claimed=False)
        self.assertEqual(0, run.poll())
        self.assertEqual(([], []), (run.terminal.typed, run.api.answers))

    def test_a_lane_it_was_not_given_is_never_claimed(self):
        run = Run([item('resume', lane='usr_other')])
        run.poll()
        self.assertEqual([('heartbeat', [LANE]), ('pending',)], run.api.calls)

    def test_only_the_carrier_routes_are_ever_called(self):
        paths = []

        class Opener:
            def __call__(self, request, timeout=None):
                paths.append((request.get_method(), request.full_url, request.get_header('Authorization')))
                raise urllib.error.URLError('closed')
        api = supervisor.Api('https://x.invalid/', TOKEN, Opener())
        for call in (lambda: api.heartbeat([LANE]), api.pending, lambda: api.claim('nudge-' + '0' * 24),
                     lambda: api.outcome('nudge-' + '0' * 24, 'failed', 'no-session')):
            with self.assertRaises(supervisor.Refused) as refused:
                call()
            self.assertNotIn(TOKEN, str(refused.exception))
        self.assertEqual({('POST', 'https://x.invalid/v1/me/carrier'), ('GET', 'https://x.invalid/v1/me/nudges'),
                          ('POST', 'https://x.invalid/v1/me/nudges/nudge-' + '0' * 24 + '/claim'),
                          ('POST', 'https://x.invalid/v1/me/nudges/nudge-' + '0' * 24 + '/outcome')},
                         {(method, url) for method, url, _ in paths})
        self.assertEqual({'Bearer ' + TOKEN}, {header for _, _, header in paths})
        self.assertFalse([url for _, url, _ in paths if TOKEN in url])

    def test_the_token_is_never_in_a_log_line(self):
        run = Run([item('resume')])
        run.supervisor.say('failed with ' + TOKEN)
        self.assertEqual(['failed with ***'], run.lines)


@unittest.skipUnless(os.name == 'posix', 'permissions are checked on POSIX only')
class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / 'nudge.json'
        self.path.write_text(json.dumps(config()), encoding='utf-8')
        os.chmod(self.path, 0o600)

    def start(self, path=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = supervisor.main(['--config', str(path or self.path), '--once'])
        return code, out.getvalue() + err.getvalue()

    def test_a_private_regular_file_loads(self):
        self.assertEqual(TOKEN, supervisor.load_config(str(self.path))['token'])

    def test_a_group_or_world_readable_file_is_refused_without_the_token(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path, mode)
                code, shown = self.start()
                self.assertEqual(2, code)
                self.assertIn('refused to start', shown)
                self.assertNotIn(TOKEN, shown)

    def test_a_symbolic_link_is_refused_even_to_a_private_file(self):
        link = self.dir / 'link.json'
        link.symlink_to(self.path)
        code, shown = self.start(link)
        self.assertEqual(2, code)
        self.assertIn('symbolic link', shown)
        self.assertNotIn(TOKEN, shown)

    def test_a_config_that_is_not_valid_is_refused_without_its_content(self):
        for body in ('{', json.dumps({'server': 'x', 'token': TOKEN}), json.dumps({'server': 'x', 'token': TOKEN, 'lanes': {'a': {}}})):
            with self.subTest(body=body[:20]):
                self.path.write_text(body, encoding='utf-8')
                code, shown = self.start()
                self.assertEqual(2, code)
                self.assertNotIn(TOKEN, shown)


class ImportTests(unittest.TestCase):
    def test_no_module_level_import_is_posix_only(self):
        for name in ('tools/nudge/supervisor.py', 'nudge_messages.py', 'nudges.py', 'tests/test_nudge_supervisor.py',
                     'tests/test_lane_nudge_carrier.py'):
            tree = ast.parse((KIT / name).read_text(encoding='utf-8'))
            for node in tree.body:
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) else []
                with self.subTest(file=name):
                    self.assertFalse({module.split('.')[0] for module in modules} & POSIX_ONLY, (name, modules))


if __name__ == '__main__':
    unittest.main()
