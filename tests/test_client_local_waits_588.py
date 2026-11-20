"""``client.py wait --recheck-in / --until-file / --until-exit`` (kittrial-5bb.588).

The three options are local: the client looks at its own clock, file system and process
table between its usual polls and never asks the server for more. Every wait here runs on a
fake clock (it only moves when something sleeps) and a fake request: nothing sleeps for real,
no process is signalled and no file outside a temporary folder is touched.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import client
import waiting

WALL = 1_800_000_000.0   # an arbitrary fake wall-clock base, never compared with a real date


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


class FakeKill:
    def __init__(self, script):
        self.calls = []
        self.script = list(script)

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        outcome = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if outcome is not None:
            raise outcome


class FakeKernel:
    def __init__(self, handle, waits, last_error=87):
        self.handle = handle
        self.waits = list(waits)
        self.last_error = last_error
        self.calls = []

    def GetLastError(self):
        self.calls.append('GetLastError')
        return self.last_error

    def OpenProcess(self, access, inherit, pid):
        self.calls.append('OpenProcess')
        self.access = access
        return self.handle

    def WaitForSingleObject(self, handle, ms):
        self.calls.append('WaitForSingleObject')
        self.wait_args = (handle, ms)
        return self.waits.pop(0)

    def CloseHandle(self, handle):
        self.calls.append('CloseHandle')

    def __getattr__(self, name):   # TerminateProcess and anything else must never be reached
        raise AssertionError('unexpected kernel32 call: %s' % name)


class FakeCtypes:
    def __init__(self, kernel):
        self.windll = mock.Mock(kernel32=kernel)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, 'config.json')
        self.time = FakeTime()
        self.fingerprint = waiting.worker_view([])['fingerprint']
        self.requests = []
        self.view = waiting.worker_view([])
        self.changes_at = None   # request number from which the server answers with news

    def request(self, config, project, actor, args, action='bd', path=None):
        self.requests.append((self.time.now, list(args), action))
        view = self.view
        if self.changes_at is not None and len(self.requests) >= self.changes_at:
            view = dict(view, fingerprint=waiting.FINGERPRINT + 'f' * 64)   # news arrived
        return {'returncode': 0, 'stdout': json.dumps(view), 'stderr': ''}

    def wait(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        arguments = list(args)
        if '--since' not in arguments:
            arguments += ['--since', self.fingerprint]
        with mock.patch.object(client, 'request', self.request), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = client._wait_command(arguments, {}, 'p', 'a', None,
                                        config_path=self.config_path,
                                        clock=self.time.clock, sleep=self.time.sleep,
                                        wall=WALL, **kwargs)
        text = out.getvalue()
        return code, (json.loads(text) if text else None), err.getvalue()


class ParsingTests(Harness):
    def test_each_option_parses_and_wakes(self):
        code, view, _ = self.wait('--interval', '60', '--recheck-in', '90s')
        self.assertEqual((code, view['local_wake']['kind'], view['local_wake']['what']),
                         (0, 'time-reached', '90s'))
        code, view, _ = self.wait('--interval', '60', '--until-file', '/x/y', exists=lambda p: True)
        self.assertEqual((code, view['local_wake']['kind']), (0, 'file-seen'))
        code, view, _ = self.wait('--interval', '60', '--until-exit', '4242', alive=lambda p: False)
        self.assertEqual((code, view['local_wake']['kind'], view['local_wake']['what']),
                         (0, 'process-exited', '4242'))

    def test_durations_accepted(self):
        for text, seconds in (('30s', 30), ('30m', 1800), ('2h', 7200), ('1d', 86400),
                              ('14d', 14 * 86400)):
            self.assertEqual(client._local_duration(text), seconds)

    def test_bad_durations_are_refused_with_exit_two(self):
        for bad in ('0s', '15d', '14d1s', 'abc', '-5s', '5', '', '1.5h', '1w', '337h'):
            code, view, err = self.wait('--recheck-in', bad)
            self.assertEqual(code, 2, bad)
            self.assertIsNone(view)
            self.assertIn('--recheck-in', err)
            self.assertEqual(err.count('\n'), 1)
            self.assertEqual(self.requests, [])

    def test_a_too_long_path_is_refused_and_a_1024_one_is_not(self):
        code, _, err = self.wait('--until-file', '/' + 'a' * 1024)
        self.assertEqual(code, 2)
        self.assertIn('--until-file', err)
        code, view, _ = self.wait('--until-file', '/' + 'a' * 1023, exists=lambda p: True)
        self.assertEqual(code, 0)

    def test_bad_pids_are_refused(self):
        for bad in ('0', '-1', 'abc', '1.5', ''):
            code, _, err = self.wait('--until-exit', bad)
            self.assertEqual(code, 2, bad)
            self.assertIn('--until-exit', err)

    def test_help_lists_the_options(self):
        text = client._wait_parser(add_help=True).format_help()
        for option in ('--recheck-in', '--until-file', '--until-exit'):
            self.assertIn(option, text)

    def test_the_path_is_printed_escaped(self):
        code, view, _ = self.wait('--until-file', '/tmp/a\x1b[31mb\nc‮', exists=lambda p: True)
        what = view['local_wake']['what']
        self.assertEqual(what, '/tmp/a\\x1b[31mb\\nc\\u202e')
        self.assertTrue(what.isprintable())


class LocalWakeTests(Harness):
    def test_file_seen_after_a_few_slices_keeps_the_callers_fingerprint(self):
        appears = {'at': 3.0}
        exists_calls = []

        def exists(path):
            exists_calls.append(path)
            return self.time.now >= appears['at']
        code, view, _ = self.wait('--interval', '60', '--timeout', '300',
                                  '--until-file', '/some/flag', exists=exists)
        self.assertEqual(code, 0)
        self.assertEqual(view['local_wake'], {
            'kind': 'file-seen', 'what': '/some/flag', 'at': '2027-01-15T08:00:03Z'})
        self.assertEqual(view['fingerprint'], self.fingerprint)
        self.assertIs(view['timed_out'], False)
        self.assertEqual(set(exists_calls), {'/some/flag'})
        self.assertTrue(all(step <= 1.0 for step in self.time.sleeps))
        self.assertEqual(len(self.requests), 1)       # no request beyond the first read

    def test_at_is_derived_from_the_fake_wall_clock(self):
        code, view, _ = self.wait('--interval', '60', '--recheck-in', '30s')
        import datetime
        expected = datetime.datetime.fromtimestamp(WALL + 30, datetime.timezone.utc)
        self.assertEqual(view['local_wake']['at'], expected.strftime('%Y-%m-%dT%H:%M:%SZ'))

    def test_recheck_in_beats_a_later_timeout_and_loses_to_an_earlier_one(self):
        code, view, _ = self.wait('--interval', '60', '--timeout', '300', '--recheck-in', '2m')
        self.assertEqual((code, view['local_wake']['kind']), (0, 'time-reached'))
        self.assertEqual(self.time.now, 120)
        self.time.now = 0.0
        code, view, _ = self.wait('--interval', '60', '--timeout', '60', '--recheck-in', '2m')
        self.assertEqual(code, 1)
        self.assertIs(view['timed_out'], True)
        self.assertNotIn('local_wake', view)

    def test_first_local_condition_wins_when_combined(self):
        code, view, _ = self.wait('--interval', '60', '--recheck-in', '1h',
                                  '--until-file', '/f', '--until-exit', '7',
                                  exists=lambda p: False,
                                  alive=lambda pid: self.time.now < 5)
        self.assertEqual(view['local_wake']['kind'], 'process-exited')
        self.assertEqual(self.time.now, 5)

    def test_a_server_event_before_the_local_condition_is_the_normal_answer(self):
        self.changes_at = 2
        code, view, _ = self.wait('--interval', '60', '--until-file', '/never',
                                  exists=lambda p: False)
        self.assertEqual(code, 0)
        self.assertNotIn('local_wake', view)
        self.assertNotEqual(view['fingerprint'], self.fingerprint)
        self.assertEqual(len(self.requests), 2)

    def test_the_local_options_never_reach_the_server_request(self):
        self.wait('--interval', '60', '--recheck-in', '1s', '--until-file', '/f', '--until-exit', '9',
                  exists=lambda p: False, alive=lambda p: True)
        joined = json.dumps(self.requests)
        for word in ('recheck', 'until', '/f'):
            self.assertNotIn(word, joined)

    def test_the_server_is_asked_no_more_often_with_a_pending_local_option(self):
        code_plain, view_plain, _ = self.wait('--interval', '60', '--timeout', '300')
        plain = [t for t, _, _ in self.requests]
        self.assertEqual(code_plain, 1)
        self.assertGreaterEqual(len(plain), 2)
        self.requests.clear()
        self.time.now = 0.0
        self.time.sleeps.clear()
        file_checks = []
        code, view, _ = self.wait('--interval', '60', '--timeout', '300',
                                  '--until-file', '/never/appears',
                                  exists=lambda p: file_checks.append(self.time.now) or False)
        local = [t for t, _, _ in self.requests]
        self.assertEqual(code, 1)
        self.assertIs(view['timed_out'], True)
        self.assertEqual(local, plain)                  # same count AND same schedule
        self.assertGreaterEqual(len(file_checks), 300)  # it did look, once a second or so
        self.assertTrue(all(step <= 1.0 for step in self.time.sleeps))


class ProcessAliveTests(unittest.TestCase):
    def test_posix_calls_kill_with_signal_zero_and_nothing_else(self):
        kill = FakeKill([None, None, ProcessLookupError()])
        results = [client._process_alive(123, platform='linux', _kill=kill) for _ in range(3)]
        self.assertEqual(results, [True, True, False])
        self.assertEqual(kill.calls, [(123, 0)] * 3)    # every call is signal 0, to that pid

    def test_posix_permission_error_means_alive(self):
        kill = FakeKill([PermissionError()])
        self.assertTrue(client._process_alive(1, platform='darwin', _kill=kill))
        self.assertEqual(kill.calls, [(1, 0)])

    def test_posix_touches_no_other_process_function(self):
        kill = FakeKill([None])
        with mock.patch.object(os, 'killpg', side_effect=AssertionError('killpg')), \
                mock.patch.object(os, 'kill', side_effect=AssertionError('os.kill')), \
                mock.patch.object(os, '_exit', side_effect=AssertionError('exit')):
            self.assertTrue(client._process_alive(5, platform='linux', _kill=kill))
        self.assertEqual(kill.calls, [(5, 0)])

    def test_windows_running_then_exited_then_null_handle(self):
        running = FakeKernel(handle=77, waits=[0x102])
        exited = FakeKernel(handle=77, waits=[0])
        gone = FakeKernel(handle=0, waits=[], last_error=87)       # ERROR_INVALID_PARAMETER
        denied = FakeKernel(handle=0, waits=[], last_error=5)      # ERROR_ACCESS_DENIED
        with mock.patch.object(os, 'kill', side_effect=AssertionError('os.kill on Windows')):
            self.assertTrue(client._process_alive(9, platform='win32', _ctypes=FakeCtypes(running)))
            self.assertFalse(client._process_alive(9, platform='win32', _ctypes=FakeCtypes(exited)))
            self.assertFalse(client._process_alive(9, platform='win32', _ctypes=FakeCtypes(gone)))
            # A process this account may not open still exists: never a false "exited".
            self.assertTrue(client._process_alive(9, platform='win32', _ctypes=FakeCtypes(denied)))
        self.assertEqual(running.calls, ['OpenProcess', 'WaitForSingleObject', 'CloseHandle'])
        self.assertEqual(exited.calls, ['OpenProcess', 'WaitForSingleObject', 'CloseHandle'])
        self.assertEqual(gone.calls, ['OpenProcess', 'GetLastError'])   # nothing to wait on or close
        self.assertEqual(denied.calls, ['OpenProcess', 'GetLastError'])
        self.assertEqual(running.access, 0x00100000 | 0x1000)   # SYNCHRONIZE | QUERY_LIMITED
        self.assertEqual(running.wait_args, (77, 0))             # a zero-time look

    def test_windows_branch_never_calls_kill_even_when_a_kill_is_injected(self):
        kill = FakeKill([None])
        kernel = FakeKernel(handle=3, waits=[0x102])
        client._process_alive(3, platform='win32', _kill=kill, _ctypes=FakeCtypes(kernel))
        self.assertEqual(kill.calls, [])

    def test_ctypes_is_imported_only_in_the_windows_branch(self):
        text = (KIT / 'client.py').read_text()
        for line in text.splitlines():
            if line.strip().startswith(('import ctypes', 'from ctypes')):
                self.assertTrue(line.startswith(' '), 'module-level ctypes import: %r' % line)


if __name__ == '__main__':
    unittest.main()
