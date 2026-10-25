#!/usr/bin/env python3
"""Reference lane nudge supervisor (kittrial-5bb.415; docs/NUDGE_CARRIER.md).

A program to COPY and run as the lane's own user, as tools/wake/ is, not a service the kit runs. It holds
a ``nudges`` credential, polls the server for nudge requests addressed to this account's lanes, and types
the kit's fixed text (``nudge_messages.py``) into the lane's terminal, only when that terminal's foreground
process is the lane's configured harness at its own prompt. It never reads a task, a comment or the board,
and it never types anything the server sent: the server sends a key and a message-set version, the text
comes from this program's own copy. It answers each request once with a closed outcome.

The terminal is behind the ``Terminal`` interface; ``TmuxTerminal`` is the one adapter supplied.
Nothing here imports a POSIX-only module at module level.
"""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import nudge_messages  # noqa: E402

POLL_FLOOR = 30
#: A lane that was typed to this recently is not typed to again (the outcome is ``coalesced``).
COALESCE_SECONDS = 5 * 60
RESTART_WAIT = 60


class Refused(Exception):
    """The supervisor will not start, or will not type: the sentence says why and never holds the token."""


class Terminal:
    """What the supervisor needs from a terminal. An adapter implements these four; a test fakes them."""

    def exists(self):
        raise NotImplementedError

    def foreground(self):
        """The name of the process in the foreground of the terminal, or ``None``."""
        raise NotImplementedError

    def screen(self):
        """The visible text of the terminal."""
        raise NotImplementedError

    def type_line(self, text):
        """Type ``text`` as the terminal's next input, then Enter."""
        raise NotImplementedError

    def restart(self, command):
        """Stop what runs in the terminal and start ``command`` (a list of arguments) there."""
        raise NotImplementedError


class TmuxTerminal(Terminal):
    def __init__(self, target, tmux='tmux'):
        self.target, self.tmux = target, tmux

    def _run(self, *args):
        done = subprocess.run([self.tmux] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=15)
        return done.returncode, done.stdout

    def exists(self):
        try:
            return self._run('display-message', '-p', '-t', self.target, '#{pane_id}')[0] == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def foreground(self):
        code, out = self._run('display-message', '-p', '-t', self.target, '#{pane_current_command}')
        return out.strip() or None if code == 0 else None

    def screen(self):
        code, out = self._run('capture-pane', '-p', '-t', self.target)
        return out if code == 0 else ''

    def type_line(self, text):
        self._run('send-keys', '-t', self.target, '-l', text)
        self._run('send-keys', '-t', self.target, 'Enter')

    def restart(self, command):
        import shlex
        self._run('respawn-pane', '-k', '-t', self.target, ' '.join(shlex.quote(part) for part in command))


def check_config_file(path):
    """Refuse a config that others could read or that is not the file it names (it holds the token)."""
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused('the config file cannot be read')
    if stat.S_ISLNK(info.st_mode):
        raise Refused('the config file is a symbolic link; give the file itself')
    if not stat.S_ISREG(info.st_mode):
        raise Refused('the config file is not a regular file')
    if os.name != 'posix':
        raise Refused('the permissions of the config file cannot be checked on this system')
    if info.st_mode & 0o077:
        raise Refused('the config file is readable by its group or others; run chmod 600 on it')
    if hasattr(os, 'getuid') and info.st_uid != os.getuid():
        raise Refused('the config file belongs to another user')


def load_config(path):
    check_config_file(path)
    with open(path, encoding='utf-8') as handle:
        config = json.load(handle)
    token = config.get('token')
    if not isinstance(config.get('server'), str) or not isinstance(token, str) or not token \
            or not isinstance(config.get('lanes'), dict) or not config['lanes']:
        raise Refused('the config needs server, token and lanes')
    for lane, setting in config['lanes'].items():
        if not isinstance(setting, dict) or not isinstance(setting.get('target'), str) \
                or not isinstance(setting.get('harness'), str) or not isinstance(setting.get('prompt'), str):
            raise Refused('lane %s needs target, harness and prompt' % lane)
        re.compile(setting['prompt'])
    return config


class Api:
    """The four routes of the carrier and nothing else; the token goes only in the Authorization header."""

    def __init__(self, server, token, opener=None):
        self.server, self.token = server.rstrip('/'), token
        self.opener = opener or urllib.request.urlopen

    def call(self, method, path, body=None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(self.server + path, data=data, method=method,
                                         headers={'Authorization': 'Bearer ' + self.token,
                                                  'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=30) as answer:
                return json.loads(answer.read().decode('utf-8') or '{}')
        except urllib.error.HTTPError as error:
            raise Refused('the server answered %s for %s %s' % (error.code, method, path))
        except (OSError, ValueError):
            raise Refused('the server could not be reached for %s %s' % (method, path))

    def heartbeat(self, lanes):
        return self.call('POST', '/v1/me/carrier', {'kind': 'supervisor', 'lanes': sorted(lanes)})

    def pending(self):
        return self.call('GET', '/v1/me/nudges').get('items') or []

    def claim(self, nudge_id):
        return self.call('POST', '/v1/me/nudges/%s/claim' % nudge_id, {})

    def outcome(self, nudge_id, outcome, detail=None):
        body = {'outcome': outcome}
        if detail:
            body['detail'] = detail
        return self.call('POST', '/v1/me/nudges/%s/outcome' % nudge_id, body)


def at_prompt(terminal, setting):
    """True only when the foreground process is the lane's harness and its last screen line is its prompt."""
    foreground = terminal.foreground()
    if not foreground or os.path.basename(foreground) != setting['harness']:
        return False
    lines = [line for line in terminal.screen().splitlines() if line.strip()]
    return bool(lines) and re.search(setting['prompt'], lines[-1]) is not None


class Supervisor:
    def __init__(self, api, config, terminals, clock=time.time, sleep=time.sleep, log=None):
        self.api, self.config, self.terminals = api, config, terminals
        self.clock, self.sleep = clock, sleep
        self.typed = {}
        self.log = log or (lambda line: print(line, flush=True))

    def say(self, line):
        # The token is never part of a line, whatever the line was built from.
        self.log(line.replace(self.config['token'], '***'))

    def poll(self):
        """One pass: heartbeat, then each request for a configured lane. Returns how many were answered."""
        lanes = self.config['lanes']
        self.api.heartbeat(lanes)
        answered = 0
        for item in self.api.pending():
            lane = item.get('lane')
            if lane not in lanes:
                continue
            claimed = self.api.claim(item['id'])
            if not claimed.get('claimed'):
                self.say('%s: not claimed (%s)' % (item['id'], claimed.get('state')))
                continue
            outcome, detail = self.carry(item, lanes[lane], lane)
            self.api.outcome(item['id'], outcome, detail)
            self.say('%s: %s%s' % (item['id'], outcome, ' ' + detail if detail else ''))
            answered += 1
        return answered

    def carry(self, item, setting, lane):
        """``(outcome, detail)`` for one claimed request. The only call that types is the one at the end."""
        text = nudge_messages.text_for(item.get('message'), item.get('message_set'))
        if text is None:
            return 'unsupported', None
        terminal = self.terminals[lane]
        now = self.clock()
        if now - self.typed.get(lane, -COALESCE_SECONDS) < COALESCE_SECONDS:
            return 'coalesced', None
        restarted = False
        if item['message'] == 'restart':
            command = setting.get('start_command')
            if not command:
                return 'failed', 'start-failed'
            terminal.restart(command)
            deadline = self.clock() + RESTART_WAIT
            while not at_prompt(terminal, setting):
                if self.clock() >= deadline:
                    return 'failed', 'start-failed'
                self.sleep(1)
            restarted = True
        elif not terminal.exists():
            return 'failed', 'no-session'
        if not at_prompt(terminal, setting):
            return 'failed', 'not-at-prompt'
        terminal.type_line(text)
        self.typed[lane] = now
        return ('restarted' if restarted else 'delivered'), None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reference lane nudge supervisor (a program to copy).')
    parser.add_argument('--config', required=True, help='a JSON file, mode 600, holding the server, the token and the lanes')
    parser.add_argument('--once', action='store_true', help='poll once and exit')
    parser.add_argument('--interval', type=int, default=POLL_FLOOR, help='seconds between polls (at least %d)' % POLL_FLOOR)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (Refused, ValueError, OSError, re.error) as error:
        print('nudge supervisor: refused to start: %s' % (error if isinstance(error, Refused) else 'the config is not valid'),
              file=sys.stderr)
        return 2
    terminals = {lane: TmuxTerminal(setting['target']) for lane, setting in config['lanes'].items()}
    supervisor = Supervisor(Api(config['server'], config['token']), config, terminals)
    interval = max(POLL_FLOOR, args.interval)
    while True:
        try:
            supervisor.poll()
        except Refused as error:
            supervisor.say('poll failed: %s' % error)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
